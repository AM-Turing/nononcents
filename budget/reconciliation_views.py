from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .forms import ConfirmDeletionForm
from .models import Account, BalanceAdjustment
from .reconciliation import (
    ReconcileForm,
    StatementForm,
    reconcile_account,
    uncleared_items,
)
from .services import account_balance, audit, ensure_open, money
from .views import common, household


@login_required
def account_reconcile(request, pk):
    home = household(request)
    account = get_object_or_404(Account, pk=pk, household=home, archived_at=None)
    action = request.POST.get("action") if request.method == "POST" else ""
    context = {**common(request), "account": account, "compare": None}
    if request.method != "POST":
        context["form"] = StatementForm(initial={"statement_date": timezone.localdate()})
        return render(request, "budget/reconcile.html", context)

    form = ReconcileForm(request.POST) if action == "reconcile" else StatementForm(request.POST)
    context["form"] = form
    checked = set(request.POST.getlist("clear"))
    if form.is_valid():
        statement_date = form.cleaned_data["statement_date"]
        statement_balance = money(form.cleaned_data["statement_balance"])
        if action == "reconcile":
            try:
                result = reconcile_account(
                    household=home,
                    actor=request.user,
                    account_id=account.pk,
                    statement_date=statement_date,
                    statement_balance=statement_balance,
                    selected_keys=request.POST.getlist("clear"),
                    post_correction=form.cleaned_data["post_correction"],
                    reason=form.cleaned_data["reason"],
                )
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                if result.adjustment:
                    outcome = f"Posted a balance correction of ${result.adjustment.amount:,.2f}. "
                else:
                    outcome = "The app matches the bank. "
                messages.success(
                    request,
                    f"{account.name} reconciled through {statement_date:%b %-d, %Y}. {outcome}"
                    f"Balance is now ${statement_balance:,.2f}.",
                )
                return redirect("accounts")
        ledger_balance = account_balance(account, through=statement_date)
        context["compare"] = {
            "statement_date": statement_date,
            "statement_balance": statement_balance,
            "ledger_balance": ledger_balance,
            "difference": money(statement_balance - ledger_balance),
            "items": [
                {"item": i, "checked": (i.key in checked) if action == "reconcile" else True}
                for i in uncleared_items(account, statement_date)
            ],
        }
    return render(request, "budget/reconcile.html", context)


@login_required
def adjustment_delete(request, pk):
    home = household(request)
    obj = get_object_or_404(BalanceAdjustment, pk=pk, household=home)
    description = f"Balance correction: ${obj.amount:,.2f}"
    form = ConfirmDeletionForm(request.POST or None, expected_value=description)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                obj = BalanceAdjustment.objects.select_for_update().get(pk=obj.pk)
                ensure_open(home, obj.date)
                audit(
                    home,
                    request.user,
                    obj,
                    "delete",
                    {
                        "date": obj.date.isoformat(),
                        "account": obj.account.name,
                        "amount": str(obj.amount),
                        "is_increase": obj.is_increase,
                    },
                )
                obj.delete()
            messages.success(request, "Balance correction deleted. The account balance changed.")
            return redirect("transactions")
        except ValidationError as exc:
            form.add_error(None, exc)
    sign = "+" if obj.is_increase else "−"
    return render(
        request,
        "budget/confirm_delete.html",
        {
            **common(request),
            "form": form,
            "title": "Delete balance correction",
            "warning": (
                f"Delete the balance correction “{obj.reason}” ({sign}${obj.amount:,.2f}) on "
                f"{obj.account.name}? The account balance will change. This cannot be undone."
            ),
            "submit_label": "Delete balance correction",
            "return_url": reverse("transactions"),
        },
    )
