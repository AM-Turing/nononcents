"""Undo for one-tap deletes.

A delete stores a small snapshot in the user's session; Undo re-creates the row through the
same validated services as normal entry (``create_expense`` and friends), so every rule
(locked periods, bill links, allocation totals) is checked again. The restored row gets a new
id; the audit trail records the delete and a ``restore`` that names the deleted id.

Rows that touch a Savings account or a savings goal are not undoable: re-creating them would
have to rebuild goal entries, so they keep the confirmation page instead.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Account, BillOccurrence, Category, Expense, Refund, Transfer
from .pay_periods import sync_pay_period_boundaries
from .services import (
    audit,
    create_expense,
    create_income,
    create_refund,
    create_transfer,
    savings_entries_for,
    update_bill_status,
)

SESSION_KEY = "undo_delete"
PAID_SESSION_KEY = "undo_paid"
UNDO_WINDOW = timedelta(minutes=10)


def _iso(value):
    return value.isoformat() if value else None


def _when(value):
    return datetime.fromisoformat(value) if value else None


def is_undoable(kind: str, obj) -> bool:
    if savings_entries_for(obj).exists():
        return False
    if kind == "income":
        accounts = [a.account for a in obj.allocations.select_related("account")]
    elif kind == "transfer":
        accounts = [obj.source, obj.destination]
    else:
        accounts = [obj.account]
    return all(account.type != Account.Type.SAVINGS for account in accounts)


def snapshot(kind: str, obj, description: str) -> dict:
    """JSON-safe description of ``obj`` sufficient to re-create it."""
    data: dict = {
        "kind": kind,
        "deleted_id": obj.pk,
        "household_id": obj.household_id,
        "description": description,
        "date": obj.date.isoformat(),
        "amount": str(obj.amount),
        "notes": obj.notes,
        "deleted_at": timezone.now().isoformat(),
    }
    if kind == "expense":
        occurrence = obj.bill_occurrence
        data.update(
            account_id=obj.account_id,
            category_id=obj.category_id,
            merchant=obj.merchant,
            occurrence_id=obj.bill_occurrence_id,
            bill_id=occurrence.bill_id if occurrence else None,
            splits=[[s.category_id, str(s.amount)] for s in obj.splits.all()],
            essential_override=obj.is_essential_override,
            cleared_at=_iso(obj.cleared_at),
            refund_ids=list(obj.refunds.values_list("pk", flat=True)),
        )
    elif kind == "refund":
        data.update(
            account_id=obj.account_id,
            category_id=obj.category_id,
            merchant=obj.merchant,
            original_expense_id=obj.original_expense_id,
            cleared_at=_iso(obj.cleared_at),
        )
    elif kind == "income":
        data.update(
            source=obj.source,
            irregular=obj.is_irregular,
            paycheck=obj.is_paycheck,
            allocations=[
                [a.account_id, str(a.amount), _iso(a.cleared_at)] for a in obj.allocations.all()
            ],
        )
    elif kind == "transfer":
        data.update(
            source_id=obj.source_id,
            destination_id=obj.destination_id,
            source_cleared_at=_iso(obj.source_cleared_at),
            destination_cleared_at=_iso(obj.destination_cleared_at),
        )
    return data


def pending(session, household, key: str = SESSION_KEY) -> dict | None:
    """The undo snapshot still inside its window for this household, else None."""
    data = session.get(key)
    if not data or data.get("household_id") != household.pk:
        return None
    if timezone.now() - datetime.fromisoformat(data["deleted_at"]) > UNDO_WINDOW:
        return None
    return data


def claim(session, household, key: str = SESSION_KEY) -> dict | None:
    """Take the pending snapshot so it can be restored once, even across workers.

    The session row is locked and re-read: if another request already took the snapshot
    (this request's copy of the session is stale) there is nothing left to restore. Call
    inside the transaction that restores, so a failed restore releases the claim.
    """
    data = pending(session, household, key)
    session.pop(key, None)
    if data is None or not session.session_key:
        return data
    row = Session.objects.select_for_update().filter(session_key=session.session_key).first()
    if row is None:
        return data
    stored = row.get_decoded()
    if stored.get(key) is None:
        return None
    stored.pop(key)
    row.session_data = SessionStore().encode(stored)
    row.save(update_fields=["session_data"])
    return data


def paid_snapshot(expense, occurrence, prior_confirmed) -> dict:
    """What Undo needs to take back the Bills "Paid" button: the payment and the month's state."""
    return {
        "household_id": expense.household_id,
        "expense_id": expense.pk,
        "occurrence_id": occurrence.pk,
        "month": f"{occurrence.due_date:%Y-%m}",
        "prior_confirmed_amount": str(prior_confirmed) if prior_confirmed is not None else None,
        "name": occurrence.bill.name,
        "amount": str(expense.amount),
        "account": expense.account.name,
        "date": expense.date.isoformat(),
        "deleted_at": timezone.now().isoformat(),
    }


def _account(household, pk):
    account = Account.objects.filter(household=household, pk=pk).first()
    if account is None:
        raise ValidationError("An account used by the deleted entry no longer exists.")
    return account


def _category(household, pk):
    category = Category.objects.filter(household=household, pk=pk).first()
    if category is None:
        raise ValidationError("A category used by the deleted entry no longer exists.")
    return category


def restore(household, actor, data: dict):
    """Re-create the deleted row from ``data``; raises ValidationError if it no longer fits."""
    kind = data["kind"]
    entry_date = date.fromisoformat(data["date"])
    amount = Decimal(data["amount"])
    with transaction.atomic():
        if kind == "expense":
            occurrence = (
                BillOccurrence.objects.filter(
                    pk=data["occurrence_id"], bill__household=household
                ).first()
                if data["occurrence_id"]
                else None
            )
            if data["occurrence_id"] and occurrence is None:
                raise ValidationError("The bill month this expense paid no longer exists.")
            obj = create_expense(
                household=household,
                actor=actor,
                entry_date=entry_date,
                account=_account(household, data["account_id"]),
                category=_category(household, data["category_id"]),
                amount=amount,
                merchant=data["merchant"],
                notes=data["notes"],
                bill=occurrence.bill if occurrence else None,
                occurrence=occurrence,
                splits=[(_category(household, c), Decimal(v)) for c, v in data["splits"]] or None,
                essential_override=data["essential_override"],
            )
            if data["cleared_at"]:
                Expense.objects.filter(pk=obj.pk).update(cleared_at=_when(data["cleared_at"]))
            Refund.objects.filter(
                household=household, pk__in=data["refund_ids"], original_expense=None
            ).update(original_expense=obj)
            if occurrence:
                update_bill_status(occurrence)
        elif kind == "refund":
            original = (
                Expense.objects.filter(household=household, pk=data["original_expense_id"]).first()
                if data["original_expense_id"]
                else None
            )
            obj = create_refund(
                household=household,
                actor=actor,
                entry_date=entry_date,
                account=_account(household, data["account_id"]),
                category=_category(household, data["category_id"]),
                amount=amount,
                merchant=data["merchant"],
                notes=data["notes"],
                original_expense=original,
            )
            if data["cleared_at"]:
                Refund.objects.filter(pk=obj.pk).update(cleared_at=_when(data["cleared_at"]))
        elif kind == "income":
            allocations = [(_account(household, a), Decimal(v)) for a, v, _ in data["allocations"]]
            obj = create_income(
                household=household,
                actor=actor,
                income_date=entry_date,
                source=data["source"],
                amount=amount,
                allocations=allocations,
                notes=data["notes"],
                irregular=data["irregular"],
                is_paycheck=data["paycheck"],
            )
            for account_id, _, cleared in data["allocations"]:
                if cleared:
                    obj.allocations.filter(account_id=account_id).update(cleared_at=_when(cleared))
            if data["paycheck"]:
                sync_pay_period_boundaries(household, actor)
        elif kind == "transfer":
            obj = create_transfer(
                household=household,
                actor=actor,
                entry_date=entry_date,
                source=_account(household, data["source_id"]),
                destination=_account(household, data["destination_id"]),
                amount=amount,
                notes=data["notes"],
            )
            Transfer.objects.filter(pk=obj.pk).update(
                source_cleared_at=_when(data["source_cleared_at"]),
                destination_cleared_at=_when(data["destination_cleared_at"]),
            )
        else:
            raise ValidationError("Unknown entry type.")
        audit(household, actor, obj, "restore", {"restored_from_deleted_id": data["deleted_id"]})
    return obj
