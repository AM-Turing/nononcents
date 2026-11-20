from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from budget.admin_scope import admin_scope
from budget.models import Household, HouseholdMembership

User = get_user_model()
PASSWORD = "pw-test-1"


class StaffScopeTests(TestCase):
    def setUp(self):
        self.mine = Household.objects.create(name="Mine")
        self.other = Household.objects.create(name="Other")
        self.staff = User.objects.create_user("staff", password=PASSWORD, is_staff=True)
        self.root = User.objects.create_user(
            "root", password=PASSWORD, is_superuser=True, is_staff=True
        )
        self.peer = User.objects.create_user("peer", password=PASSWORD, is_staff=True)
        self.inside = User.objects.create_user("inside", password=PASSWORD)
        self.shared = User.objects.create_user("shared", password=PASSWORD)
        self.outsider = User.objects.create_user("outsider", password=PASSWORD)
        for home, user in (
            (self.mine, self.staff),
            (self.mine, self.peer),
            (self.mine, self.inside),
            (self.mine, self.shared),
            (self.other, self.shared),
            (self.other, self.outsider),
            (self.mine, self.root),
            (self.other, self.root),
        ):
            HouseholdMembership.objects.create(household=home, user=user)

    def test_superuser_sees_everything(self):
        profiles, users = admin_scope(self.root)
        self.assertEqual(set(profiles), {self.mine, self.other})
        self.assertEqual(users.count(), User.objects.count())
        self.client.force_login(self.root)
        response = self.client.get(reverse("application_admin"))
        self.assertEqual(len(response.context["profiles"]), 2)

    def test_profile_staff_scope(self):
        profiles, users = admin_scope(self.staff)
        self.assertEqual(list(profiles), [self.mine])
        # peer (staff), root (superuser) and shared (outside membership) are excluded.
        self.assertEqual({u.username for u in users}, {"staff", "inside"})

    def test_admin_page_lists_only_scope(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("application_admin"))
        self.assertEqual([p.pk for p in response.context["profiles"]], [self.mine.pk])
        self.assertEqual({u.username for u in response.context["users"]}, {"staff", "inside"})
        self.assertEqual(response.context["profiles"][0].member_count, 2)
        self.assertContains(response, "Backups")

    def test_out_of_scope_actions_404(self):
        self.client.force_login(self.staff)
        for name, target in (
            ("application_user_password", self.outsider),
            ("application_user_password", self.shared),
            ("application_user_password", self.peer),
            ("application_user_password", self.root),
            ("application_user_delete", self.outsider),
            ("application_user_delete", self.shared),
            ("application_user_delete", self.peer),
            ("application_profile_delete", self.other),
            ("application_profile_access", self.other),
        ):
            url = reverse(name, args=[target.pk])
            self.assertEqual(self.client.get(url).status_code, 404, (name, target))
            self.assertEqual(self.client.post(url, {}).status_code, 404, (name, target))
        self.assertTrue(User.objects.filter(pk=self.outsider.pk).exists())
        self.assertTrue(Household.objects.filter(pk=self.other.pk).exists())

    def test_in_scope_actions_work(self):
        self.client.force_login(self.staff)
        url = reverse("application_user_password", args=[self.inside.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        url = reverse("application_profile_access", args=[self.mine.pk])
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_access_form_hides_and_retains_out_of_scope_members(self):
        self.client.force_login(self.staff)
        url = reverse("application_profile_access", args=[self.mine.pk])
        response = self.client.get(url)
        self.assertNotContains(response, "outsider")
        self.assertNotContains(response, "peer")
        response = self.client.post(url, {"users": [self.staff.pk]})
        self.assertRedirects(response, reverse("application_admin"))
        members = set(self.mine.memberships.values_list("user__username", flat=True))
        self.assertEqual(members, {"staff", "peer", "shared", "root"})

    def test_cannot_add_out_of_scope_user_to_profile(self):
        self.client.force_login(self.staff)
        url = reverse("application_profile_access", args=[self.mine.pk])
        response = self.client.post(url, {"users": [self.staff.pk, self.outsider.pk]})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.mine.memberships.filter(user=self.outsider).exists())

    def test_cannot_grant_staff_flag(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("application_admin"))
        self.assertNotContains(response, "user-is_staff")
        data = {
            "intent": "add_user",
            "user-username": "newbie",
            "user-password1": "a-Long-pass-2468",
            "user-password2": "a-Long-pass-2468",
        }
        response = self.client.post(reverse("application_admin"), {**data, "user-is_staff": "on"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="newbie").exists())
        self.client.post(reverse("application_admin"), data)
        newbie = User.objects.get(username="newbie")
        self.assertFalse(newbie.is_staff)
        self.assertTrue(newbie.household_memberships.filter(household=self.mine).exists())

    def test_new_profile_adds_creator_as_member(self):
        self.client.force_login(self.staff)
        self.client.post(
            reverse("application_admin"),
            {"intent": "add_profile", "profile-name": "Fresh", "profile-timezone": "UTC"},
        )
        fresh = Household.objects.get(name="Fresh")
        self.assertTrue(fresh.memberships.filter(user=self.staff).exists())
        profiles, _ = admin_scope(self.staff)
        self.assertIn(fresh, profiles)

    def test_user_shared_with_outside_profile_is_out_of_scope(self):
        _, users = admin_scope(self.staff)
        self.assertNotIn(self.shared, users)
        self.client.force_login(self.staff)
        response = self.client.get(reverse("application_user_password", args=[self.shared.pk]))
        self.assertEqual(response.status_code, 404)
