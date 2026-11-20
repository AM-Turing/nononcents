"""Debts page, debt editing, balance entry and category budget setting.

Calculations live in ``budget.debts`` and ``budget.habits``.
"""

from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import debts, habits
from .debt_forms import CategoryBudgetForm, DebtBalanceForm, DebtForm, ExtraForm
from .models import Debt, DebtBalance, RecurringBill
from .services import audit
from .views import common, household

ZERO = Decimal("0.00")


def _debt(request, pk) -> Debt:
    return get_object_or_404(Debt, pk=pk, household=household(request))


def _debt_card(debt: Debt, today) -> dict:
    payment, basis = debts.monthly_payment(debt)
    estimate = debts.estimated_balance(debt, today)
    projection = None
    if estimate.estimate is not None:
        projection = debts.project(estimate.estimate, debt.apr, payment, today)
    return {
        "debt": debt,
        "payment": payment,
        "basis": basis,
        "bills": [b for b in debt.bills.all() if b.active and b.effective_end is None],
        "estimate": estimate,
        "projection": projection,
        "history": list(debt.balances.all()[:24]),
        "balance_form": DebtBalanceForm(),
    }


@login_required
def debt_list(request):
    home = household(request)
    today = timezone.localdate()
    extra_form = ExtraForm(request.GET or None)
    extra = ZERO
    if request.GET.get("extra", "") != "":
        if extra_form.is_valid():
            extra = extra_form.cleaned_data["extra"]
        else:
            messages.error(request, "Extra per month must be a number of 0 or more; ignored.")
    all_debts = Debt.objects.filter(household=home).prefetch_related("bills", "balances")
    cards = [_debt_card(d, today) for d in all_debts if d.archived_at is None]
    archived = [d for d in all_debts if d.archived_at is not None]
    items = [
        (c["debt"], c["estimate"].estimate, c["payment"])
        for c in cards
        if c["estimate"].estimate is not None and c["payment"] > 0
    ]
    comparison: dict[str, object] | None = None
    if items:
        baseline = debts.plan(items, rollover=False)
        with_extra = debts.plan(items, extra=extra)
        comparison = {
            "baseline": baseline,
            "with_extra": with_extra,
            "baseline_date": baseline.payoff_date(today),
            "extra_date": with_extra.payoff_date(today),
        }
        if baseline.months is not None and with_extra.months is not None:
            comparison["months_sooner"] = baseline.months - with_extra.months
            comparison["interest_saved"] = baseline.total_interest - with_extra.total_interest
    total_owed = sum((c["estimate"].estimate or ZERO for c in cards), ZERO)
    total_payments = sum((c["payment"] for c in cards), ZERO)
    return render(
        request,
        "budget/debts.html",
        {
            **common(request),
            "cards": cards,
            "archived": archived,
            "total_owed": total_owed,
            "total_payments": total_payments,
            "comparison": comparison,
            "extra": extra,
            "extra_form": extra_form,
            "today": today,
        },
    )


def _save_links(home, actor, debt: Debt, chosen) -> None:
    chosen_ids = {b.pk for b in chosen}
    changed = RecurringBill.objects.select_for_update().filter(household=home)
    for bill in changed.filter(debt=debt).exclude(pk__in=chosen_ids):
        bill.debt = None
        bill.save(update_fields=["debt"])
        audit(home, actor, bill, "update", {"debt": [debt.name, None]})
    for bill in changed.filter(pk__in=chosen_ids).exclude(debt=debt):
        before = bill.debt.name if bill.debt else None
        bill.debt = debt
        bill.save(update_fields=["debt"])
        audit(home, actor, bill, "update", {"debt": [before, debt.name]})


def _debt_form_view(request, debt: Debt | None):
    home = household(request)
    form = DebtForm(request.POST or None, instance=debt, household=home)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            saved = form.save(commit=False)
            saved.household = home
            created = saved.pk is None
            before = (
                {}
                if created
                else {
                    k: str(v)
                    for k, v in Debt.objects.values().get(pk=saved.pk).items()
                    if k in form.changed_data
                }
            )
            saved.save()
            _save_links(home, request.user, saved, form.cleaned_data["bills"])
            changes = {k: [before.get(k), str(getattr(saved, k))] for k in form.changed_data}
            changes.pop("bills", None)
            audit(home, request.user, saved, "create" if created else "update", changes)
        messages.success(request, f"Saved {saved.name}.")
        return redirect("debts")
    return render(
        request,
        "budget/debt_form.html",
        {
            **common(request),
            "form": form,
            "debt": debt,
            "title": f"Edit {debt.name}" if debt else "Add a debt",
        },
    )


@login_required
def debt_add(request):
    return _debt_form_view(request, None)


@login_required
def debt_edit(request, pk):
    return _debt_form_view(request, _debt(request, pk))


@login_required
@require_POST
def debt_archive(request, pk):
    home = household(request)
    restore = request.POST.get("restore") == "1"
    with transaction.atomic():
        debt = get_object_or_404(Debt.objects.select_for_update(), pk=pk, household=home)
        debt.archived_at = None if restore else timezone.now()
        debt.save(update_fields=["archived_at"])
        audit(home, request.user, debt, "update", {"archived": [restore, not restore]})
    messages.success(request, f"{'Restored' if restore else 'Archived'} {debt.name}.")
    return redirect("debts")


@login_required
def debt_balance_add(request, pk):
    debt = _debt(request, pk)
    form = DebtBalanceForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        with transaction.atomic():
            row = (
                DebtBalance.objects.select_for_update()
                .filter(debt=debt, as_of=data["as_of"])
                .first()
            )
            if row:
                before = [str(row.balance), row.notes]
                row.balance, row.notes = data["balance"], data["notes"]
                row.save(update_fields=["balance", "notes"])
                audit(
                    household(request),
                    request.user,
                    row,
                    "update",
                    {"balance": [before[0], str(row.balance)], "notes": [before[1], row.notes]},
                )
            else:
                row = DebtBalance.objects.create(
                    debt=debt,
                    as_of=data["as_of"],
                    balance=data["balance"],
                    notes=data["notes"],
                    created_by=request.user,
                )
                audit(
                    household(request),
                    request.user,
                    row,
                    "create",
                    {"as_of": str(row.as_of), "balance": str(row.balance)},
                )
        messages.success(request, f"Balance saved for {debt.name}.")
        return redirect("debts")
    return render(
        request,
        "budget/debt_form.html",
        {
            **common(request),
            "form": form,
            "debt": debt,
            "title": f"Enter a balance for {debt.name}",
            "balance_mode": True,
        },
    )


@login_required
@require_POST
def category_budget_set(request):
    home = household(request)
    form = CategoryBudgetForm(request.POST, household=home)
    if form.is_valid():
        data = form.cleaned_data
        try:
            habits.set_category_budget(
                home, request.user, data["category"], data["amount"], data["effective_from"]
            )
            messages.success(request, f"Budget saved for {data['category'].name}.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
    else:
        problems = [f"{f.label}: {' '.join(e)}" for f, e in _errors(form)]
        messages.error(request, "Budget not saved. " + " ".join(problems))
    return redirect(reverse("budgets"))


def _errors(form):
    return [(form.fields[name], errs) for name, errs in form.errors.items() if name in form.fields]
