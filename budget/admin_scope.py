"""Scope of the application admin pages: which profiles and users a staff user may manage."""

from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q

from .forms import ProfileAccessForm
from .models import Household


def admin_scope(user):
    """Return (profiles_qs, users_qs) the given staff user may administer.

    Superusers manage everything. Other staff manage only profiles they belong to, and
    users whose memberships all lie inside those profiles (never superusers or other staff;
    the staff user themself stays listed so they can reset their own password).
    """
    user_model = get_user_model()
    if user.is_superuser:
        return Household.objects.order_by("name"), user_model.objects.order_by("username")
    own_profile_ids = list(
        Household.objects.filter(memberships__user=user).values_list("pk", flat=True)
    )
    profiles = Household.objects.filter(pk__in=own_profile_ids).order_by("name")
    outside = Household.objects.exclude(pk__in=own_profile_ids)
    users = (
        user_model.objects.filter(household_memberships__household__in=own_profile_ids)
        .exclude(household_memberships__household__in=outside)
        .filter(is_superuser=False)
        .filter(Q(is_staff=False) | Q(pk=user.pk))
        .distinct()
        .order_by("username")
    )
    return profiles, users


class ScopedProfileAccessForm(ProfileAccessForm):
    """Profile access form limited to in-scope users.

    Current members outside the actor's scope are not shown and are always retained.
    """

    def __init__(self, *args, scope_users, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["users"].queryset = scope_users
        visible = set(scope_users.values_list("pk", flat=True))
        self.hidden_members = list(
            get_user_model()
            .objects.filter(household_memberships__household=self.profile)
            .exclude(pk__in=visible)
            .order_by("username")
        )
        self.fields["users"].initial = [
            pk for pk in self.profile.memberships.values_list("user_id", flat=True) if pk in visible
        ]

    def clean_users(self):
        users = list(self.cleaned_data["users"]) + self.hidden_members
        selected_ids = {user.pk for user in users}
        if self.profile == self.active_profile and self.actor.pk not in selected_ids:
            raise forms.ValidationError(
                "You cannot remove your own access from the active profile."
            )
        if not any(user.is_staff or user.is_superuser for user in users):
            raise forms.ValidationError(
                "At least one administrator must retain access to this profile."
            )
        return users
