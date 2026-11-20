"""The administrator's activity log: filters, plain-language rows and grouping.

Each row says when, who and what. "What" describes the record itself (an expense's amount,
merchant, date and account) rather than an internal id, and changes read "merchant: snackbar →
Snackbar". One action that changed many records at once (for example combining merchant names)
is one row: "10 expenses".
"""

import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from .models import ApplicationSetting, AuditEvent

logger = logging.getLogger("budget.activity_log")
PRUNED_KEY = "activity_log_last_pruned"

FILTERS = {
    "all": "All",
    "sign_ins": "Sign-ins",
    "failed": "Failed sign-ins",
    "transactions": "Transactions",
    "other": "Other changes",
}
TRANSACTION_TYPES = (
    "budget.Expense",
    "budget.Income",
    "budget.Refund",
    "budget.Transfer",
    "budget.BalanceAdjustment",
)
LABELS = {
    "budget.Expense": ("Expense", "expenses"),
    "budget.Income": ("Income", "income entries"),
    "budget.Refund": ("Refund", "refunds"),
    "budget.Transfer": ("Transfer", "transfers"),
    "budget.BalanceAdjustment": ("Balance correction", "balance corrections"),
    "budget.RecurringBill": ("Bill", "bills"),
    "budget.BillOccurrence": ("Bill due", "bill due dates"),
    "budget.PayPeriod": ("Pay period", "pay periods"),
    "budget.SavingsGoal": ("Savings goal", "savings goals"),
    "budget.Household": ("Budget profile", "budget profiles"),
    "budget.ApplicationSetting": ("Setting", "settings"),
    "auth.User": ("User", "users"),
    "backup.Request": ("Backup request", "backup requests"),
}
ACTION_WORDS = {
    AuditEvent.Action.CREATE: "Added",
    AuditEvent.Action.UPDATE: "Changed",
    AuditEvent.Action.DELETE: "Deleted",
    AuditEvent.Action.LOGIN: "Signed in",
    AuditEvent.Action.LOGIN_FAILED: "Sign-in failed",
}
MONEY_FIELDS = {"amount", "balance", "expected_amount", "confirmed_amount", "default_amount"}
GROUP_WINDOW = timedelta(seconds=2)


def filter_events(queryset, key):
    if key == "sign_ins":
        return queryset.filter(action=AuditEvent.Action.LOGIN)
    if key == "failed":
        return queryset.filter(action=AuditEvent.Action.LOGIN_FAILED)
    if key == "transactions":
        return queryset.filter(record_type__in=TRANSACTION_TYPES)
    if key == "other":
        return queryset.exclude(record_type__in=TRANSACTION_TYPES).exclude(
            action__in=[AuditEvent.Action.LOGIN, AuditEvent.Action.LOGIN_FAILED]
        )
    return queryset


def activity_rows(household, key="all", limit=100):
    """Up to ``limit`` rows (after grouping) for the household, newest first."""
    queryset = filter_events(
        AuditEvent.objects.select_related("actor").filter(household=household), key
    )
    events = list(queryset[: limit * 5])
    objects = _load_objects(events)
    rows = [_row(group, objects) for group in _group(events)]
    return rows[:limit]


def _same_action(first, event):
    """Did ``event`` come from the same single action as ``first``?

    Transactions are grouped only when the same change was made to each (for example
    combining merchant names); other records (bill dates, pay periods) also group when an
    automatic clean-up changed the same fields on many of them in the same moment.
    """
    if (
        event.action in (AuditEvent.Action.LOGIN, AuditEvent.Action.LOGIN_FAILED)
        or first.action != event.action
        or first.actor_id != event.actor_id
        or first.record_type != event.record_type
        or first.occurred_at - event.occurred_at > GROUP_WINDOW
    ):
        return False
    if first.changes == event.changes:
        return (
            event.action == AuditEvent.Action.UPDATE or event.record_type not in TRANSACTION_TYPES
        )
    return (
        event.record_type not in TRANSACTION_TYPES
        and isinstance(first.changes, dict)
        and isinstance(event.changes, dict)
        and set(first.changes) == set(event.changes)
    )


def _group(events):
    groups: list[list[AuditEvent]] = []
    for event in events:
        if groups and _same_action(groups[-1][0], event):
            groups[-1].append(event)
        else:
            groups.append([event])
    return groups


def _load_objects(events):
    wanted: dict[str, set[int]] = {}
    for event in events:
        if event.record_type.startswith("budget.") and event.record_id.isdigit():
            wanted.setdefault(event.record_type, set()).add(int(event.record_id))
    related = {
        "budget.Expense": ("account", "category"),
        "budget.Refund": ("account", "category"),
        "budget.Transfer": ("source", "destination"),
        "budget.BalanceAdjustment": ("account",),
        "budget.BillOccurrence": ("bill",),
    }
    objects = {}
    for label, ids in wanted.items():
        try:
            model = apps.get_model(label)
        except LookupError:
            continue
        queryset = model.objects.filter(pk__in=ids).select_related(*related.get(label, ()))
        for obj in queryset:
            objects[(label, str(obj.pk))] = obj
    return objects


def _row(group, objects):
    event = group[0]
    actor = event.actor
    who = (actor.get_full_name().strip() or actor.username) if actor else "System (automatic)"
    if event.action == AuditEvent.Action.LOGIN_FAILED and not actor:
        who = "—"
    if len(group) > 1:
        plural = LABELS.get(event.record_type, (event.record_type, "records"))[1]
        what = f"{len(group)} {plural}"
    else:
        what = describe(event, objects.get((event.record_type, event.record_id)))
    return {
        "when": event.occurred_at,
        "who": who,
        "action": ACTION_WORDS.get(event.action, event.get_action_display()),
        "failed": event.action == AuditEvent.Action.LOGIN_FAILED,
        "what": what,
        "details": _shared_details(group),
        "count": len(group),
    }


def _money(value):
    try:
        return f"${Decimal(str(value)):,.2f}"
    except (InvalidOperation, ValueError, TypeError):
        return str(value)


def _date(value):
    return f"{value:%b} {value.day}, {value.year}" if hasattr(value, "year") else str(value)


def describe(event, obj):
    """One line saying what the record is."""
    changes = event.changes if isinstance(event.changes, dict) else {}
    if event.record_type == "backup.Request":
        return "Backup requested from Admin"
    if event.action == AuditEvent.Action.LOGIN:
        return f"Signed in to {changes.get('profile') or 'a budget profile'}"
    if event.action == AuditEvent.Action.LOGIN_FAILED:
        profile = changes.get("profile")
        if changes.get("unknown_user"):
            return "Unknown user" + (f" for {profile}" if profile else "")
        tried = changes.get("username") or "(blank)"
        return f"Username “{tried}”" + (f" for {profile}" if profile else "")
    label = LABELS.get(event.record_type, (event.record_type.split(".")[-1], ""))[0]
    if obj is not None:
        return f"{label} {_describe_object(event.record_type, obj)}".strip()
    # The record no longer exists: use what was saved with the event.
    parts = []
    if "amount" in changes and not isinstance(changes["amount"], list):
        parts.append(_money(changes["amount"]))
    for key in ("description", "merchant", "name", "source", "bill"):
        if changes.get(key) and not isinstance(changes[key], list):
            parts.append(str(changes[key]))
            break
    for key in ("date", "due_date", "start_date"):
        if changes.get(key) and not isinstance(changes[key], list):
            saved = parse_date(str(changes[key]))
            parts.append(_date(saved) if saved else str(changes[key]))
            break
    text = " · ".join(parts) or f"#{event.record_id}"
    suffix = "" if event.action == AuditEvent.Action.DELETE else " (since deleted)"
    return f"{label} {text}{suffix}"


def _describe_object(record_type, obj):
    if record_type == "budget.Expense":
        name = obj.merchant.strip() or obj.category.name
        return f"{_money(obj.amount)} · {name} · {_date(obj.date)} · {obj.account.name}"
    if record_type == "budget.Refund":
        name = obj.merchant.strip() or obj.category.name
        return f"{_money(obj.amount)} · {name} · {_date(obj.date)} · {obj.account.name}"
    if record_type == "budget.Income":
        return f"{_money(obj.amount)} · {obj.source} · {_date(obj.date)}"
    if record_type == "budget.Transfer":
        route = f"{obj.source.name} → {obj.destination.name}"
        return f"{_money(obj.amount)} · {route} · {_date(obj.date)}"
    if record_type == "budget.BalanceAdjustment":
        # A word joiner keeps the sign on the same line as the amount.
        sign = "+\u2060" if obj.is_increase else "−\u2060"
        return f"{sign}{_money(obj.amount)} · {obj.account.name} · {_date(obj.date)}"
    if record_type == "budget.BillOccurrence":
        return f"{obj.bill.name} · {_date(obj.due_date)}"
    if record_type == "budget.PayPeriod":
        return f"{_date(obj.start_date)} – {_date(obj.end_date)}"
    for attribute in ("name", "key", "username"):
        if getattr(obj, attribute, None):
            return str(getattr(obj, attribute))
    return f"#{obj.pk}"


def _shared_details(group):
    """Details of one event, or the lines every event in a group has in common."""
    lines = details(group[0])
    if len(group) > 1:
        common = set(lines)
        for event in group[1:]:
            common &= set(details(event))
        lines = [line for line in lines if line in common]
    return lines


def details(event):
    """The recorded changes as short readable lines."""
    changes = event.changes if isinstance(event.changes, dict) else {}
    if event.action in (AuditEvent.Action.LOGIN, AuditEvent.Action.LOGIN_FAILED):
        lines = [changes["reason"]] if changes.get("reason") else []
        if changes.get("ip"):
            lines.append(f"From {changes['ip']}")
        return lines
    lines = []
    for key, value in changes.items():
        name = key.replace("_", " ")
        if isinstance(value, list) and len(value) == 2:
            old, new = (_money(v) if key in MONEY_FIELDS else _plain(v) for v in value)
            lines.append(f"{name}: {old} → {new}")
        elif event.action == AuditEvent.Action.DELETE and key in ("detached_refunds",):
            if value:
                lines.append(f"{name}: {_plain(value)}")
        else:
            lines.append(f"{name}: {_money(value) if key in MONEY_FIELDS else _plain(value)}")
    return lines


def _plain(value):
    if value is None or value == "":
        return "—"
    if isinstance(value, str) and len(value) == 10:
        saved = parse_date(value)
        if saved:
            return _date(saved)
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return str(value)


def retention_text():
    """The Admin caption sentence about how long entries are kept."""
    days = settings.ACTIVITY_LOG_RETENTION_DAYS
    if days <= 0:
        return "Entries are kept until they are removed."
    return f"Entries are kept for {days} days; older ones are removed automatically."


def prune_activity_log(dry_run=False):
    """Delete activity-log entries older than the retention period; return how many.

    Only AuditEvent rows are touched. 0 days means keep everything.
    """
    days = settings.ACTIVITY_LOG_RETENTION_DAYS
    if days <= 0:
        return 0
    old = AuditEvent.objects.filter(occurred_at__lt=timezone.now() - timedelta(days=days))
    if dry_run:
        return old.count()
    with transaction.atomic():
        return old.delete()[1].get("budget.AuditEvent", 0)


def _prune_once_a_day(household_id):
    today = timezone.localdate().isoformat()
    with transaction.atomic():
        ApplicationSetting.objects.get_or_create(
            household_id=household_id, key=PRUNED_KEY, defaults={"value": ""}
        )
        # Whoever locks the row first prunes; other workers skip it instead of waiting.
        marker = (
            ApplicationSetting.objects.select_for_update(skip_locked=True)
            .filter(household_id=household_id, key=PRUNED_KEY)
            .exclude(value=today)
            .first()
        )
        if marker is None:
            return
        prune_activity_log()
        marker.value = today
        marker.save(update_fields=["value"])


def prune_daily_after_commit(household):
    """Prune at most once a day, after the caller's transaction has committed.

    A failure is logged (no payloads) and never reaches the caller.
    """
    if household is None or settings.ACTIVITY_LOG_RETENTION_DAYS <= 0:
        return

    def run():
        try:
            _prune_once_a_day(household.pk)
        except Exception:
            logger.exception("Activity log pruning failed")

    transaction.on_commit(run)
