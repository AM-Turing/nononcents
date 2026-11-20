from django.http import HttpRequest

from .backup_status import request_backup_status


def backup_alert(request: HttpRequest) -> dict[str, object]:
    """Expose a non-healthy backup status to staff users. Never touches the database."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or not user.is_staff:
        return {}
    status = request_backup_status(request)
    if status.level == "ok":
        return {}
    return {"backup_alert": status}


def undo_delete(request: HttpRequest) -> dict[str, object]:
    """The last one-tap delete and the last "Paid" button press, while they can be undone.

    Session only, no queries.
    """
    from datetime import datetime

    from django.utils import timezone

    from .undo import PAID_SESSION_KEY, SESSION_KEY, UNDO_WINDOW

    session = getattr(request, "session", None)
    if session is None:
        return {}
    found: dict[str, object] = {}
    for key, name in ((SESSION_KEY, "undo_delete"), (PAID_SESSION_KEY, "undo_paid")):
        data = session.get(key)
        if not data or data.get("household_id") != session.get("active_household_id"):
            continue
        if timezone.now() - datetime.fromisoformat(data["deleted_at"]) > UNDO_WINDOW:
            session.pop(key, None)
            continue
        found[name] = data
    return found


def page_names(request: HttpRequest) -> dict[str, object]:
    """Custom page names (Admin → Page names) for the menus and headings on every page."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    from django.core.exceptions import PermissionDenied

    from .views import household, page_labels, page_titles

    try:
        home = household(request)
    except PermissionDenied:
        return {}
    return {"page_labels": page_labels(home), "page_titles": page_titles(home)}


def appearance(request: HttpRequest) -> dict[str, object]:
    """The signed-in person's saved Display Mode, so it follows them to every browser,
    phone and address (the page falls back to the browser's own memory when signed out)."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    from .models import UserPreference

    saved = UserPreference.objects.filter(user=user).values_list("appearance", flat=True).first()
    return {"saved_appearance": saved} if saved else {}
