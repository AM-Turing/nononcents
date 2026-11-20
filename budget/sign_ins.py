"""Record sign-ins and failed sign-ins in the activity log.

A failed sign-in keeps the username only when it belongs to an existing user (people type
passwords into that box); otherwise it records "unknown user". Blocked attempts (the username
or device is locked out) are not recorded, so the log cannot grow without limit.
"""

from django.contrib.auth import get_user_model

from . import login_throttle
from .models import AuditEvent, Household


def client_ip(request) -> str:
    """The visitor's address (X-Real-IP only from a trusted proxy); see login_throttle."""
    return login_throttle.client_ip(request) or ""


def record_sign_in(request, user, profile):
    AuditEvent.objects.create(
        household=profile,
        actor=user,
        record_type="auth.User",
        record_id=str(user.pk),
        action=AuditEvent.Action.LOGIN,
        changes={"profile": profile.name if profile else "", "ip": client_ip(request)},
    )


def record_failed_sign_in(request, form):
    codes = {error.code for error in form.non_field_errors().as_data()}
    if "locked" in codes:
        return
    typed = (form.data.get("username") or "").strip()[:150]
    profile_id = form.data.get("profile") or ""
    profile = Household.objects.filter(pk=profile_id).first() if profile_id.isdigit() else None
    user = form.get_user()
    known = user or (
        get_user_model().objects.filter(username__iexact=typed).first() if typed else None
    )
    username = known.username if known else ""
    if not typed:
        reason = "No username entered"
    elif user is None:
        reason = "Wrong username or password"
    else:
        reason = "No access to the selected profile"
    changes = {
        "username": username,
        "profile": profile.name if profile else "",
        "reason": reason,
        "ip": client_ip(request),
    }
    if typed and not known:
        changes["unknown_user"] = True
    AuditEvent.objects.create(
        household=profile,
        actor=user,
        record_type="auth.User",
        record_id=str(user.pk) if user else "",
        action=AuditEvent.Action.LOGIN_FAILED,
        changes=changes,
    )
