import csv
import logging
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import cast
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import DatabaseError, connection, transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from . import activity_log, analysis_jobs, backup_requests, login_throttle, undo
from .admin_scope import ScopedProfileAccessForm, admin_scope
from .backup_status import request_backup_status
from .forecast import payday_forecast
from .forms import (
    ALERT_SETTING_DEFAULTS,
    PAGE_CHOICES,
    PAGE_LABEL_DEFAULTS,
    PAGE_TITLE_DEFAULTS,
    AlertSettingsForm,
    BillOccurrenceAmountForm,
    BillOccurrenceSkipForm,
    BudgetForm,
    BudgetPasswordResetForm,
    BudgetProfileForm,
    BudgetUserCreationForm,
    ConfirmDeletionForm,
    ExpenseForm,
    ExpenseSplitEditFormSet,
    ExpenseSplitFormSet,
    GeneratePayPeriodsForm,
    HistoryTransactionEditForm,
    IncomeForm,
    OutlookSettingsForm,
    PageNameForm,
    PayPeriodForm,
    ProfileAuthenticationForm,
    QuickActionForm,
    RecurringBillForm,
    RefundForm,
    SavingsGoalForm,
    SavingsMoveForm,
    SavingsSourceAllocationFormSet,
    TransferForm,
    add_form_token,
)
from .models import (
    Account,
    ApplicationSetting,
    AuditEvent,
    BalanceAdjustment,
    BillOccurrence,
    Category,
    Debt,
    Expense,
    ExpenseSplit,
    Household,
    HouseholdMembership,
    Income,
    IncomeAllocation,
    PayPeriod,
    PayPeriodBudget,
    PaySchedule,
    RecurringBill,
    Refund,
    ReportAnalysisRun,
    SavingsGoal,
    SavingsGoalEntry,
    StatementLine,
    Transfer,
    UserPreference,
    paid_total,
)
from .outlook import default_outlook_range, outlook_data, pie_slices
from .pay_periods import (
    create_period_budget,
    reassign_bill_occurrence_periods,
    sync_pay_period_boundaries,
)
from .reconciliation import account_ledger, row_sort_key
from .services import (
    account_balance,
    archive_savings_goal,
    audit,
    bill_forecast,
    change_bill_frequency,
    choose_occurrence,
    claim_form_token,
    complete_savings_goal,
    confirm_unchanged_bills,
    create_expense,
    create_income,
    create_refund,
    create_savings_goal,
    create_transfer,
    delete_savings_goal,
    end_of_month_ahead,
    ensure_bill_occurrences,
    ensure_open,
    ensure_savings_goals,
    expected_deposit_date,
    expense_category_allowed,
    finish_savings_removal,
    income_source_defaults,
    live_bill_occurrences,
    net_spending,
    normalize_goal_priorities,
    pay_bill_occurrence,
    period_for_due_date,
    reallocate_savings,
    recent_paid_amounts,
    reconcile_savings,
    remove_bill,
    restore_savings_goal,
    rewrite_savings_entries,
    savings_account,
    savings_drift,
    savings_ledger_balance,
    snapshot_savings_entries,
    update_bill_status,
    validate_expense_against_refunds,
    validate_refund_rules,
)
from .setup_checklist import setup_checklist
from .sign_ins import record_failed_sign_in, record_sign_in
from .statements import link_new_entry, release_entry_matches, release_statement_match


def household(request=None):
    if request and request.user.is_authenticated:
        selected = request.session.get("active_household_id")
        memberships = Household.objects.filter(memberships__user=request.user)
        home = memberships.filter(pk=selected).first() if selected else None
        if home:
            return home
        home = memberships.order_by("pk").first()
        if home:
            request.session["active_household_id"] = home.pk
            return home
        raise PermissionDenied("This user is not associated with a budget profile.")
    return Household.objects.first()


def common(request=None):
    home = household(request)
    return {
        "household": home,
        "page_labels": page_labels(home),
        "page_titles": page_titles(home),
        "accounts": [
            (a, account_balance(a))
            for a in Account.objects.filter(household=home, archived_at=None)
        ]
        if home
        else [],
    }


def setting_value(home, key, default):
    if not home:
        return default
    setting = ApplicationSetting.objects.filter(household=home, key=key).first()
    return setting.value if setting else default


def dashboard_overview_title(home):
    if not home:
        return "Budget Overview"
    owner = home.name.strip()
    for suffix in (" budget", " household"):
        if owner.casefold().endswith(suffix):
            owner = owner[: -len(suffix)].strip()
    if owner.casefold() == "my":
        return "My Budget Overview"
    if owner.casefold().endswith(("'s", "’s")):
        owner = owner[:-2].rstrip()
    owner = owner or "Profile"
    return f"{owner}’s Budget Overview"


def page_labels(home):
    saved = setting_value(home, "page_labels", {})
    return (
        {**PAGE_LABEL_DEFAULTS, **saved} if isinstance(saved, dict) else PAGE_LABEL_DEFAULTS.copy()
    )


def page_name_rows(home, selected=None):
    """Each renamable page with its original and current name, for Admin → Page names."""
    labels = page_labels(home)
    return [
        {
            "key": key,
            "original": original,
            "current": labels.get(key, PAGE_LABEL_DEFAULTS[key]),
            "selected": key == selected,
        }
        for key, original in PAGE_CHOICES
    ]


def page_titles(home):
    saved = setting_value(home, "page_labels", {})
    titles = (
        {**PAGE_TITLE_DEFAULTS, **saved} if isinstance(saved, dict) else PAGE_TITLE_DEFAULTS.copy()
    )
    titles["dashboard"] = dashboard_overview_title(home)
    return titles


def custom_quick_actions(home):
    saved = setting_value(home, "quick_actions", [])
    if not isinstance(saved, list):
        return []
    allowed = dict(QuickActionForm.DESTINATIONS)
    actions = []
    for index, item in enumerate(saved):
        if not isinstance(item, dict) or item.get("destination") not in allowed:
            continue
        actions.append(
            {
                "index": index,
                "label": str(item.get("label", ""))[:40],
                "url": reverse(item["destination"]),
                "destination_label": allowed[item["destination"]],
            }
        )
    return actions


def alert_settings(home):
    saved = setting_value(home, "alert_settings", {})
    values = (
        {**ALERT_SETTING_DEFAULTS, **saved}
        if isinstance(saved, dict)
        else {**ALERT_SETTING_DEFAULTS}
    )
    try:
        threshold = Decimal(str(values["spending_low_balance_threshold"]))
    except (InvalidOperation, TypeError, ValueError):
        threshold = Decimal(ALERT_SETTING_DEFAULTS["spending_low_balance_threshold"])
    return {
        "spending_low_balance_enabled": bool(values["spending_low_balance_enabled"]),
        "spending_low_balance_threshold": max(threshold, Decimal("0.00")),
        "bills_shortage_enabled": bool(values["bills_shortage_enabled"]),
        "bill_confirmation_enabled": bool(values["bill_confirmation_enabled"]),
    }


def form_return_url(request, fallback="/"):
    candidate = request.POST.get("return_url") or request.META.get("HTTP_REFERER")
    if candidate and url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return fallback


@login_required
def dashboard(request):
    ctx = common(request)
    home = household(request)
    today = timezone.localdate()
    period = (
        PayPeriod.objects.filter(household=home, start_date__lte=today, end_date__gte=today).first()
        if home
        else None
    )
    spending = Account.objects.filter(household=home, type=Account.Type.SPENDING).first()
    bills_account = Account.objects.filter(household=home, type=Account.Type.BILLS).first()
    spending_balance = account_balance(spending) if spending else Decimal("0")
    bills_balance = account_balance(bills_account) if bills_account else Decimal("0")
    forecast = payday_forecast(home, today) if period else {"available": False, "reason": ""}
    bills = (
        live_bill_occurrences(home)
        .filter(due_date__range=(period.start_date, period.end_date))
        .select_related("bill")
        if period
        else BillOccurrence.objects.none()
    )
    bill_forecasts = [(item, bill_forecast(item)) for item in bills.order_by("due_date")]
    spent = net_spending(home, period.start_date, period.end_date) if period else Decimal("0.00")
    budget = PayPeriodBudget.objects.filter(pay_period=period).first() if period else None
    next_period = forecast.get("next_period")
    current_bills_total = sum((fc["amount"] for _, fc in bill_forecasts), Decimal("0.00"))
    bills_remaining = sum((fc["remaining"] for _, fc in bill_forecasts), Decimal("0.00"))
    spending_budget_target = budget.spending_target if budget else Decimal("0.00")
    spending_remaining = spending_budget_target - spent
    spending_used_percent = (
        (spent / spending_budget_target * Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if spending_budget_target > 0
        else Decimal("0.00")
    )
    bills_paid = current_bills_total - bills_remaining
    bills_paid_percent = (
        (bills_paid / current_bills_total * Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if current_bills_total > 0
        else Decimal("0.00")
    )
    spending_chart = spending_chart_data(home, period)
    # Bills paid from the Bills account this period (the Bills card reconciles with them).
    bills_account_rows = [
        fc for item, fc in bill_forecasts if item.effective_account.type == Account.Type.BILLS
    ]
    bills_account_total = sum((fc["amount"] for fc in bills_account_rows), Decimal("0.00"))
    bills_account_unpaid = sum((fc["remaining"] for fc in bills_account_rows), Decimal("0.00"))
    held_for_next = forecast.get("held_for_next", Decimal("0.00"))
    surplus = forecast.get("bills_surplus")
    bills_surplus_amount = surplus["amount"] if surplus else Decimal("0.00")
    bills_shortage = max(-bills_surplus_amount, Decimal("0.00"))
    alerts = alert_settings(home)
    alert_key = (
        f"bills-shortage-{period.pk}-{bills_balance}-{bills_surplus_amount}" if period else ""
    )
    ctx.update(
        {
            "period": period,
            "budget": budget,
            "forecast": forecast,
            "spent": spent,
            "spending_budget_target": spending_budget_target,
            "spending_remaining": spending_remaining,
            "spending_over": max(-spending_remaining, Decimal("0.00")),
            "spending_used_percent": spending_used_percent,
            "current_bills_total": current_bills_total,
            "bills_paid": bills_paid,
            "bills_paid_percent": bills_paid_percent,
            "next_period": next_period,
            "bill_occurrences": [item for item, _ in bill_forecasts][:8],
            "unconfirmed": bills.filter(status=BillOccurrence.Status.UNCONFIRMED).count(),
            "bills_remaining": bills_remaining,
            "bills_balance": bills_balance,
            "bills_shortage": bills_shortage,
            "bills_shortage_alert": alerts["bills_shortage_enabled"]
            and bills_shortage > 0
            and not request.session.get(alert_key),
            "bills_alert_key": alert_key,
            "bills_account": bills_account,
            "spending_balance": spending_balance,
            "spending_account": spending,
            "bills_account_total": bills_account_total,
            "bills_account_paid": bills_account_total - bills_account_unpaid,
            "bills_account_left": sum(1 for fc in bills_account_rows if fc["remaining"] > 0),
            "held_for_next": held_for_next,
            "spending_chart": spending_chart,
            "low_balance_alert_enabled": alerts["spending_low_balance_enabled"],
            "low_balance_threshold": alerts["spending_low_balance_threshold"],
            "bill_confirmation_alert_enabled": alerts["bill_confirmation_enabled"],
            "custom_quick_actions": custom_quick_actions(home),
            "setup_checklist": setup_checklist(home),
        }
    )
    return render(request, "budget/dashboard.html", ctx)


def spending_chart_data(home, period):
    if not home or not period:
        return {"items": [], "gradient": "var(--muted) 0deg 360deg", "total": Decimal("0")}
    date_range = (period.start_date, period.end_date)
    refund_rows = (
        Refund.objects.filter(
            household=home, category__type=Category.Type.SPENDING, date__range=date_range
        )
        .values("category__name")
        .annotate(total=Sum("amount"))
    )
    totals = expense_category_totals(
        household=home,
        start=period.start_date,
        end=period.end_date,
        category_type=Category.Type.SPENDING,
    )
    for row in refund_rows:
        totals[row["category__name"]] = (
            totals.get(row["category__name"], Decimal("0")) - row["total"]
        )
    values = sorted(
        ((name, value) for name, value in totals.items() if value > 0),
        key=lambda item: item[1],
        reverse=True,
    )
    total = sum((value for _, value in values), Decimal("0"))
    palette = ["#4477aa", "#ee6677", "#228833", "#ccbb44", "#66ccee", "#aa3377", "#bbbbbb"]
    cursor = Decimal("0")
    items, segments = [], []
    for index, (name, value) in enumerate(values):
        end = cursor + (value / total * Decimal("360"))
        color = palette[index % len(palette)]
        segments.append(f"{color} {cursor:.2f}deg {end:.2f}deg")
        percent = value / total * 100
        items.append(
            {
                "name": name,
                "amount": value,
                "percent": percent,
                "share": percent.quantize(Decimal("1"), rounding=ROUND_HALF_UP),
                "color": color,
            }
        )
        cursor = end
    return {
        "items": items,
        "gradient": ", ".join(segments),
        "total": total,
        "slices": pie_slices(items, total),
    }


def expense_category_totals(*, account=None, household=None, start, end, category_type):
    expense_filters = {"date__range": (start, end)}
    if account is not None:
        expense_filters["account"] = account
    if household is not None:
        expense_filters["household"] = household
    unsplit = (
        Expense.objects.filter(
            **expense_filters,
            splits__isnull=True,
            category__type=category_type,
        )
        .values("category__name")
        .annotate(total=Sum("amount"))
    )
    split_filters = {f"expense__{key}": value for key, value in expense_filters.items()}
    split = (
        ExpenseSplit.objects.filter(**split_filters, category__type=category_type)
        .values("category__name")
        .annotate(total=Sum("amount"))
    )
    totals = {row["category__name"]: row["total"] for row in unsplit}
    for row in split:
        totals[row["category__name"]] = (
            totals.get(row["category__name"], Decimal("0.00")) + row["total"]
        )
    return totals


def expense_total_by_type(category_type, **filters):
    return sum(
        expense_category_totals(category_type=category_type, **filters).values(),
        Decimal("0.00"),
    )


@login_required
def acknowledge_bills_shortage(request):
    if request.method == "POST":
        alert_key = request.POST.get("alert_key", "")
        if alert_key.startswith("bills-shortage-"):
            request.session[alert_key] = True
    return redirect("dashboard")


def savings_source_values(formset):
    return [
        (item["goal"], item["amount"], item.get("fulfills_goal", False))
        for item in formset.cleaned_data
        if item and item.get("goal") and item.get("amount")
    ]


MERCHANT_CAP = 300
REMEMBER_DATE_DAYS = 7
ADD_FORMS = {
    "expense": "expense_add",
    "refund": "refund_add",
    "income": "income_add",
    "transfer": "transfer_add",
}


def merchant_memory(home):
    """Datalist names (the most used, listed A to Z) and a casefolded merchant -> latest
    category/account map."""
    counts: dict[str, dict[str, int]] = {}
    for name, used in (
        Expense.objects.filter(household=home)
        .exclude(merchant="")
        .values_list("merchant")
        .annotate(used=Count("pk"))
    ):
        if not name.strip():
            continue
        spellings = counts.setdefault(name.strip().casefold(), {})
        spellings[name.strip()] = spellings.get(name.strip(), 0) + used
    ranked = sorted(
        counts.items(), key=lambda item: (-sum(item[1].values()), min(item[1]), item[0])
    )[:MERCHANT_CAP]
    names = sorted(
        (max(spellings, key=lambda n: (spellings[n], n)) for _, spellings in ranked),
        key=lambda name: (name.casefold(), name),
    )
    wanted = {key for key, _ in ranked}
    latest: dict[str, dict[str, int]] = {}
    recent = (
        Expense.objects.filter(household=home)
        .order_by("-date", "-pk")
        .values_list("merchant", "category_id", "account_id")
    )
    for name, category_id, account_id in recent.iterator():
        key = name.strip().casefold()
        if key in wanted and key not in latest:
            latest[key] = {"category": category_id, "account": account_id}
            if len(latest) == len(wanted):
                break
    return names, latest


def remembered_entry_initial(request, home):
    """Form initial from the last saved expense/refund: recent date and a usable account."""
    initial: dict[str, object] = {}
    today = timezone.localdate()
    saved = parse_date(str(request.session.get("last_entry_date", "")))
    if saved and 0 <= (today - saved).days <= REMEMBER_DATE_DAYS:
        initial["date"] = saved
    account_id = request.session.get("last_entry_account")
    if isinstance(account_id, int):
        account = Account.objects.filter(pk=account_id, household=home, archived_at=None).first()
        if account:
            initial["account"] = account
    return initial


STATEMENT_LINE_SESSION_KEY = "statement_line_pending"


def statement_line_initial(request, home, kind):
    """Initial values for 'Add as new entry' from a statement line (GET ?statement_line=)."""
    raw = request.GET.get("statement_line", "")
    if request.method != "GET":
        return {}
    request.session.pop(STATEMENT_LINE_SESSION_KEY, None)
    if not raw.isdigit():
        return {}
    line = StatementLine.objects.filter(
        pk=int(raw), statement__household=home, status=StatementLine.Status.PENDING
    ).first()
    if line is None or (kind == "income") != (line.amount > 0):
        return {}
    request.session[STATEMENT_LINE_SESSION_KEY] = {"line": line.pk, "kind": kind}
    initial = {"date": line.date, "amount": abs(line.amount)}
    if kind == "income":
        initial[f"allocation_{line.account_id}"] = abs(line.amount)
    else:
        initial["account"] = line.account
    return initial


def link_statement_line(request, home, kind, account, key, amount):
    """After saving an entry started from a statement line, link the two; returns a redirect."""
    pending = request.session.pop(STATEMENT_LINE_SESSION_KEY, None)
    if not pending or pending.get("kind") != kind:
        return None
    linked = link_new_entry(
        household=home,
        actor=request.user,
        line_id=pending["line"],
        account=account,
        key=key,
        amount=amount,
    )
    line = StatementLine.objects.filter(pk=pending["line"]).first()
    if linked:
        messages.success(request, "Linked to the statement line and marked cleared.")
    else:
        messages.info(
            request, "Saved, but not linked: the amount or account differs from the line."
        )
    return redirect("statement_review", pk=line.statement_id) if line else None


def _save_model_form(request, form_class, title, kind):
    home = household(request)
    ensure_savings_goals(home)
    fulfillment_goal = None
    if request.method == "GET" and kind == "expense" and request.GET.get("savings_goal"):
        fulfillment_goal = SavingsGoal.objects.filter(
            pk=request.GET["savings_goal"],
            household=home,
            status=SavingsGoal.Status.COMPLETED,
            is_system=False,
        ).first()
        if fulfillment_goal and fulfillment_goal.current_amount <= 0:
            fulfillment_goal = None
    form_initial = remembered_entry_initial(request, home) if request.method == "GET" else {}
    remembered_date = form_initial.get("date")
    form_initial.update(statement_line_initial(request, home, kind))
    if fulfillment_goal:
        form_initial.update(
            {
                "account": savings_account(home),
                "amount": fulfillment_goal.current_amount,
                "notes": f"Fulfills Savings goal: {fulfillment_goal.name}",
            }
        )
    form = add_form_token(
        form_class(request.POST or None, household=home, initial=form_initial or None)
    )
    form.fields["merchant"].widget.attrs["list"] = "merchant-options"
    if (
        remembered_date
        and form_initial.get("date") == remembered_date
        and remembered_date != timezone.localdate()
    ):
        form.fields["date"].help_text = (
            f"Same date as your last entry ({remembered_date:%b %-d, %Y}). "
            "Change it if this entry is from a different day."
        )
    merchant_names, merchant_map = merchant_memory(home)
    add_another = request.method == "POST" and "add_another" in request.POST
    destination = ADD_FORMS[kind] if add_another else "dashboard"
    split_formset = (
        ExpenseSplitFormSet(
            request.POST or None,
            prefix="splits",
            form_kwargs={"household": home},
        )
        if kind == "expense"
        else None
    )
    savings_formset = (
        SavingsSourceAllocationFormSet(
            request.POST or None,
            prefix="savings",
            form_kwargs={"household": home},
            initial=(
                [
                    {
                        "goal": fulfillment_goal,
                        "amount": fulfillment_goal.current_amount,
                        "fulfills_goal": True,
                    }
                ]
                if fulfillment_goal
                else None
            ),
        )
        if kind == "expense"
        else None
    )
    forms_valid = form.is_valid() if request.method == "POST" else False
    splits_valid = (
        split_formset.is_valid()
        if forms_valid and split_formset is not None and form.cleaned_data.get("split_transaction")
        else True
    )
    savings_valid = (
        savings_formset.is_valid()
        if forms_valid
        and savings_formset is not None
        and form.cleaned_data["account"].type == Account.Type.SAVINGS
        else True
    )
    if request.method == "POST" and forms_valid and splits_valid and savings_valid:
        try:
            with transaction.atomic():
                if not claim_form_token(home, form.cleaned_data["form_token"]):
                    messages.info(request, DUPLICATE_SUBMISSION_MESSAGE)
                    return redirect(destination)
                obj = _create_from_form(request, home, kind, form, split_formset, savings_formset)
            request.session["last_entry_date"] = form.cleaned_data["date"].isoformat()
            request.session["last_entry_account"] = form.cleaned_data["account"].pk
            balance = account_balance(obj.account)
            sign = "-" if balance < 0 else ""
            messages.success(
                request,
                f"{kind.capitalize()} of ${obj.amount:,.2f} saved to {obj.account.name}. "
                f"Resulting balance: {sign}${abs(balance):,.2f}.",
            )
            linked = link_statement_line(
                request, home, kind, obj.account, f"{kind}:{obj.pk}", obj.amount
            )
            return linked or redirect(destination)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "budget/form.html",
        {
            **common(request),
            "form": form,
            "title": title,
            "add_type": kind,
            "return_url": form_return_url(request),
            "expense_split_formset": split_formset,
            "savings_formset": savings_formset,
            "add_another": True,
            "merchant_names": merchant_names,
            "merchant_map": merchant_map,
            "merchant_bound": request.method == "POST",
        },
    )


DUPLICATE_SUBMISSION_MESSAGE = "That entry was already saved."


def _create_from_form(request, home, kind, form, split_formset, savings_formset):
    if kind == "expense":
        assert split_formset is not None
        split_values = (
            [(item["category"], item["amount"]) for item in split_formset.cleaned_data if item]
            if form.cleaned_data["split_transaction"]
            else None
        )
        obj = create_expense(
            household=home,
            actor=request.user,
            entry_date=form.cleaned_data["date"],
            account=form.cleaned_data["account"],
            category=form.cleaned_data["category"],
            amount=form.cleaned_data["amount"],
            merchant=form.cleaned_data["merchant"],
            notes=form.cleaned_data["notes"],
            bill=form.cleaned_data.get("bill"),
            occurrence=form.cleaned_data.get("bill_occurrence"),
            splits=split_values,
            savings_allocations=savings_source_values(savings_formset)
            if form.cleaned_data["account"].type == Account.Type.SAVINGS
            else None,
            essential_override=form.cleaned_data.get("is_essential_override"),
        )
    else:
        obj = create_refund(
            household=home,
            actor=request.user,
            entry_date=form.cleaned_data["date"],
            account=form.cleaned_data["account"],
            category=form.cleaned_data["category"],
            amount=form.cleaned_data["amount"],
            merchant=form.cleaned_data["merchant"],
            notes=form.cleaned_data["notes"],
            original_expense=form.cleaned_data.get("original_expense"),
            savings_goal=form.cleaned_data.get("savings_destination_goal"),
        )
    return obj


@login_required
def transaction_add(request):
    return render(request, "budget/transaction_add.html", common(request))


@login_required
def expense_add(request):
    return _save_model_form(request, ExpenseForm, "Add expense", "expense")


@login_required
def expense_saved(request):
    # The old "add another?" page is gone; keep the URL working for bookmarks.
    return redirect("expense_add")


@login_required
def refund_add(request):
    return _save_model_form(request, RefundForm, "Add refund", "refund")


@login_required
def transfer_add(request):
    home = household(request)
    ensure_savings_goals(home)
    destination = None
    if request.GET.get("to") == "bills":
        destination = Account.objects.filter(household=home, type=Account.Type.BILLS).first()
    destination_view = ADD_FORMS["transfer"] if "add_another" in request.POST else "dashboard"
    form = add_form_token(
        TransferForm(request.POST or None, initial={"destination": destination}, household=home)
    )
    savings_formset = SavingsSourceAllocationFormSet(
        request.POST or None,
        prefix="savings",
        form_kwargs={"household": home},
    )
    form_valid = form.is_valid() if request.method == "POST" else False
    savings_valid = (
        savings_formset.is_valid()
        if form_valid and form.cleaned_data["source"].type == Account.Type.SAVINGS
        else True
    )
    if request.method == "POST" and form_valid and savings_valid:
        try:
            with transaction.atomic():
                if not claim_form_token(home, form.cleaned_data["form_token"]):
                    messages.info(request, DUPLICATE_SUBMISSION_MESSAGE)
                    return redirect(destination_view)
                create_transfer(
                    household=home,
                    actor=request.user,
                    entry_date=form.cleaned_data["date"],
                    source=form.cleaned_data["source"],
                    destination=form.cleaned_data["destination"],
                    amount=form.cleaned_data["amount"],
                    notes=form.cleaned_data["notes"],
                    savings_source_allocations=savings_source_values(savings_formset)
                    if form.cleaned_data["source"].type == Account.Type.SAVINGS
                    else None,
                    savings_destination_goal=form.cleaned_data.get("savings_destination_goal"),
                )
            messages.success(
                request, "Transfer saved; income and spending totals were not changed."
            )
            return redirect(destination_view)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "budget/form.html",
        {
            **common(request),
            "form": form,
            "title": "Add transfer",
            "add_type": "transfer",
            "add_another": True,
            "return_url": form_return_url(request),
            "savings_formset": savings_formset,
        },
    )


@login_required
def income_add(request):
    home = household(request)
    ensure_savings_goals(home)
    accounts = list(Account.objects.filter(household=home, archived_at=None))
    form = add_form_token(
        IncomeForm(
            request.POST or None,
            accounts=accounts,
            household=home,
            initial=statement_line_initial(request, home, "income") or None,
        )
    )
    source_defaults = income_source_defaults(home)
    destination_view = ADD_FORMS["income"] if "add_another" in request.POST else "dashboard"
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                if not claim_form_token(home, form.cleaned_data["form_token"]):
                    messages.info(request, DUPLICATE_SUBMISSION_MESSAGE)
                    return redirect(destination_view)
                income = create_income(
                    household=home,
                    actor=request.user,
                    income_date=form.cleaned_data["date"],
                    source=form.cleaned_data["source"],
                    amount=form.cleaned_data["amount"],
                    notes=form.cleaned_data["notes"],
                    irregular=form.cleaned_data["is_irregular"],
                    allocations=[
                        (a, form.cleaned_data.get(f"allocation_{a.pk}") or 0) for a in accounts
                    ],
                    savings_goal=form.cleaned_data.get("savings_destination_goal"),
                    is_paycheck=form.cleaned_data["is_paycheck"],
                )
                anchored = (
                    sync_pay_period_boundaries(home, request.user)
                    if form.cleaned_data["is_paycheck"]
                    else None
                )
            messages.success(request, "Income and allocations saved.")
            if anchored:
                pay_period_sync_messages(request, anchored)
            pending = request.session.get(STATEMENT_LINE_SESSION_KEY) or {}
            line = (
                StatementLine.objects.filter(pk=pending["line"]).first()
                if isinstance(pending.get("line"), int)
                else None
            )
            allocation = (
                income.allocations.filter(account_id=line.account_id).first() if line else None
            )
            linked = (
                link_statement_line(
                    request,
                    home,
                    "income",
                    allocation.account,
                    f"allocation:{allocation.pk}",
                    allocation.amount,
                )
                if allocation
                else None
            )
            request.session.pop(STATEMENT_LINE_SESSION_KEY, None)
            return linked or redirect(destination_view)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "budget/form.html",
        {
            **common(request),
            "form": form,
            "title": "Add income",
            "add_type": "income",
            "add_another": True,
            "allocation_help": True,
            "income_sources": [source for source, _ in source_defaults],
            "income_defaults": {source.casefold(): entry for source, entry in source_defaults},
            "income_bound": request.method == "POST",
            "return_url": form_return_url(request),
        },
    )


def pay_period_sync_messages(request, result):
    for change in result.changes:
        if change.after["start_date"] != change.before["start_date"]:
            messages.info(
                request,
                f"Pay period now starts {change.after['start_date']:%b %-d, %Y}, "
                "the day the paycheck landed.",
            )
    for note in result.kept:
        messages.warning(request, note)


HISTORY_PAGE_SIZE = 50
HISTORY_TYPES = [
    ("expense", "Expense"),
    ("income", "Income"),
    ("refund", "Refund"),
    ("transfer", "Transfer"),
    ("adjustment", "Balance correction"),
]


def history_cleared_label(kind, obj, item=None):
    if item is not None:
        return "Cleared" if item.cleared_at else "Not cleared"
    if kind in {"expense", "refund", "adjustment"}:
        flags = [obj.cleared_at is not None]
    elif kind == "income":
        flags = [a.cleared_at is not None for a in obj.allocations.all()]
    else:
        flags = [obj.source_cleared_at is not None, obj.destination_cleared_at is not None]
    if flags and all(flags):
        return "Cleared"
    return "Partly cleared" if any(flags) else "Not cleared"


@login_required
def transactions(request):
    home = household(request)
    start = parse_date(request.GET.get("start", ""))
    end = parse_date(request.GET.get("end", ""))
    filters = {"household": home}
    if start:
        filters["date__gte"] = start
    if end:
        filters["date__lte"] = end
    keyword = request.GET.get("q", "").strip()
    type_filter = request.GET.get("type", "")
    if type_filter not in {key for key, _ in HISTORY_TYPES}:
        type_filter = ""
    filter_accounts = list(Account.objects.filter(household=home))
    account_param = request.GET.get("account", "")
    selected_account = next((a for a in filter_accounts if str(a.pk) == account_param), None)
    amount = searchable_amount(keyword)
    amount_filter = Q(amount=amount) if amount is not None else Q()
    expense_or_refund_search = (
        Q(merchant__icontains=keyword)
        | Q(notes__icontains=keyword)
        | Q(category__name__icontains=keyword)
        | Q(account__name__icontains=keyword)
        | amount_filter
    )
    expense_search = expense_or_refund_search | Q(splits__category__name__icontains=keyword)
    if amount is not None:
        expense_search |= Q(splits__amount=amount)
    income_search = (
        Q(source__icontains=keyword)
        | Q(notes__icontains=keyword)
        | Q(allocations__account__name__icontains=keyword)
        | amount_filter
    )
    transfer_search = (
        Q(notes__icontains=keyword)
        | Q(source__name__icontains=keyword)
        | Q(destination__name__icontains=keyword)
        | amount_filter
    )
    adjustment_search = (
        Q(reason__icontains=keyword)
        | Q(notes__icontains=keyword)
        | Q(account__name__icontains=keyword)
        | amount_filter
    )
    account_filter = {"account": selected_account} if selected_account else {}
    income_scope = Income.objects.filter(**filters)
    transfer_scope = Transfer.objects.filter(**filters)
    if selected_account:
        income_scope = income_scope.filter(allocations__account=selected_account)
        transfer_scope = transfer_scope.filter(
            Q(source=selected_account) | Q(destination=selected_account)
        )
    ledger = (
        {(i.base, i.pk): i for i in account_ledger(selected_account)} if selected_account else {}
    )
    sources = [
        (
            Expense.objects.filter(**filters, **account_filter).filter(expense_search).distinct(),
            "expense",
            "Expense",
            -1,
            lambda q: q.select_related("account", "category").prefetch_related("splits__category"),
        ),
        (
            Refund.objects.filter(**filters, **account_filter).filter(expense_or_refund_search),
            "refund",
            "Refund",
            1,
            lambda q: q.select_related("account", "category"),
        ),
        (
            income_scope.filter(income_search).distinct(),
            "income",
            "Income",
            1,
            lambda q: q.prefetch_related("allocations__account"),
        ),
        (
            transfer_scope.filter(transfer_search),
            "transfer",
            "Transfer",
            0,
            lambda q: q.select_related("source", "destination"),
        ),
        (
            BalanceAdjustment.objects.filter(**filters, **account_filter).filter(adjustment_search),
            "adjustment",
            "Balance correction",
            0,
            lambda q: q.select_related("account"),
        ),
    ]
    # Sort, count and page on light (sort key, kind, id) tuples; load full rows for one page only.
    entries = []
    for qs, kind, _label, _sign, _load in sources:
        if type_filter and type_filter != kind:
            continue
        for pk, entry_date, created_at in qs.order_by().values_list("pk", "date", "created_at"):
            entries.append((row_sort_key(kind, entry_date, created_at, pk), kind, pk))
    entries.sort(reverse=True)
    page = Paginator(entries, HISTORY_PAGE_SIZE).get_page(request.GET.get("page"))
    rows = []
    for qs, kind, label, sign, load in sources:
        ids = [pk for _, k, pk in page.object_list if k == kind]
        if not ids:
            continue
        for x in load(qs.model.objects.filter(pk__in=ids)):
            item = ledger.get((kind, x.pk))
            row_sign, row_amount = sign, x.amount
            if kind == "adjustment":
                row_sign = 1 if x.is_increase else -1
            if item is not None:
                row_sign = 1 if item.amount > 0 else -1
                row_amount = abs(item.amount)
            is_adjustment = kind == "adjustment"
            rows.append(
                {
                    "sort": row_sort_key(kind, x.date, x.created_at, x.pk),
                    "date": x.date,
                    "type": label,
                    "description": x.reason if is_adjustment else transaction_description(x, kind),
                    "account": x.account.name if is_adjustment else transaction_account(x, kind),
                    "category": "" if is_adjustment else transaction_category(x, kind),
                    "notes": x.notes,
                    "amount": row_amount,
                    "sign": row_sign,
                    "balance_after": item.balance_after if item is not None else None,
                    "cleared": history_cleared_label(kind, x, item),
                    "edit_url": None
                    if is_adjustment
                    else reverse("transaction_edit", args=[kind, x.pk]),
                    "delete_url": reverse("adjustment_delete", args=[x.pk])
                    if is_adjustment
                    else reverse("transaction_delete", args=[kind, x.pk]),
                    "quick_delete": not is_adjustment,
                }
            )
    rows.sort(key=lambda x: x["sort"], reverse=True)
    params = request.GET.copy()
    params.pop("page", None)
    return render(
        request,
        "budget/transactions.html",
        {
            **common(request),
            "rows": rows,
            "page_obj": page,
            "keyword": keyword,
            "filter_accounts": filter_accounts,
            "selected_account": selected_account,
            "type_filter": type_filter,
            "history_types": HISTORY_TYPES,
            "query_string": params.urlencode(),
            "has_filters": bool(keyword or start or end or type_filter or selected_account),
        },
    )


def transaction_description(obj, kind):
    if kind in {"expense", "refund"}:
        return obj.merchant or obj.notes or kind.title()
    if kind == "income":
        return obj.source
    return obj.notes or f"{obj.source.name} → {obj.destination.name}"


def transaction_account(obj, kind):
    if kind in {"expense", "refund"}:
        return obj.account.name
    if kind == "transfer":
        return f"{obj.source.name} → {obj.destination.name}"
    allocations = sorted(obj.allocations.all(), key=lambda item: item.account.name.casefold())
    return "; ".join(f"{item.account.name}: ${item.amount:,.2f}" for item in allocations)


def transaction_category(obj, kind):
    if kind == "expense":
        splits = list(obj.splits.all())
        if splits:
            details = "; ".join(f"{item.category.name}: ${item.amount:,.2f}" for item in splits)
            return f"Split — {details}"
        return obj.category.name
    if kind == "refund":
        return obj.category.name
    return ""


def transaction_configuration(kind):
    configurations = {
        "expense": (Expense, "Expense", "merchant"),
        "refund": (Refund, "Refund", "merchant"),
        "income": (Income, "Income", "source"),
        "transfer": (Transfer, "Transfer", "notes"),
    }
    if kind not in configurations:
        raise PermissionDenied("Unknown transaction type.")
    return configurations[kind]


@login_required
def transaction_edit(request, kind, pk):
    home = household(request)
    model, label, description_field = transaction_configuration(kind)
    obj = get_object_or_404(model, pk=pk, household=home)
    allocations = (
        list(
            obj.allocations.select_related("account").order_by(
                "account__display_order", "account__name"
            )
        )
        if kind == "income"
        else []
    )
    expense_splits = (
        list(obj.splits.select_related("category").order_by("pk")) if kind == "expense" else []
    )
    savings_entry_query = (
        SavingsGoalEntry.objects.filter(income_allocation__income=obj)
        if kind == "income"
        else obj.savings_goal_entries.all()
    )
    savings_entries = list(savings_entry_query.select_related("goal").order_by("pk"))
    savings_source_entries = [entry for entry in savings_entries if entry.amount < 0]
    savings_destination_entry = next((entry for entry in savings_entries if entry.amount > 0), None)
    source_is_savings = (kind == "expense" and obj.account.type == Account.Type.SAVINGS) or (
        kind == "transfer" and obj.source.type == Account.Type.SAVINGS
    )
    destination_is_savings = (
        (kind == "refund" and obj.account.type == Account.Type.SAVINGS)
        or (kind == "transfer" and obj.destination.type == Account.Type.SAVINGS)
        or (
            kind == "income"
            and any(item.account.type == Account.Type.SAVINGS for item in allocations)
        )
    )
    savings_source_formset = (
        SavingsSourceAllocationFormSet(
            request.POST or None,
            prefix="savings",
            initial=[
                {
                    "goal": entry.goal,
                    "amount": -entry.amount,
                    "fulfills_goal": entry.fulfills_goal,
                }
                for entry in savings_source_entries
            ],
            form_kwargs={
                "household": home,
                "current_goal_ids": [entry.goal_id for entry in savings_source_entries],
            },
        )
        if source_is_savings
        else None
    )
    split_initial = (
        [{"category": item.category, "amount": item.amount} for item in expense_splits]
        if expense_splits
        else [{}, {}]
    )
    split_formset = (
        ExpenseSplitEditFormSet(
            request.POST or None,
            prefix="splits",
            initial=split_initial,
            form_kwargs={
                "household": home,
                "current_category_ids": [item.category_id for item in expense_splits],
            },
        )
        if kind == "expense"
        else None
    )
    category = obj.category if kind in {"expense", "refund"} else None
    current_bill = obj.bill_occurrence.bill if kind == "expense" and obj.bill_occurrence else None
    form = HistoryTransactionEditForm(
        request.POST or None,
        transaction_type=label,
        allocations=allocations,
        expense_splits=expense_splits,
        household=home,
        category=category,
        account=obj.account if kind in {"expense", "refund"} else None,
        bill=current_bill,
        savings_destination_goal=(
            savings_destination_entry.goal if savings_destination_entry else None
        ),
        destination_is_savings=destination_is_savings,
        original_date=obj.date,
        initial={
            "date": obj.date,
            "description": getattr(obj, description_field),
            "amount": obj.amount,
            "notes": obj.notes,
            "is_essential_override": (
                "true"
                if kind == "expense" and obj.is_essential_override is True
                else "false"
                if kind == "expense" and obj.is_essential_override is False
                else ""
            ),
        },
    )
    form_valid = form.is_valid() if request.method == "POST" else False
    splits_valid = (
        split_formset.is_valid()
        if form_valid and split_formset is not None and form.cleaned_data["split_transaction"]
        else True
    )
    savings_valid = (
        savings_source_formset.is_valid()
        if form_valid and savings_source_formset is not None
        else True
    )
    if request.method == "POST" and form_valid and splits_valid and savings_valid:
        try:
            with transaction.atomic():
                obj = get_object_or_404(
                    model.objects.select_for_update(), pk=obj.pk, household=home
                )
                ensure_open(home, obj.date)
                ensure_open(home, form.cleaned_data["date"])
                if kind == "expense":
                    validate_expense_against_refunds(
                        cast(Expense, obj),
                        new_amount=form.cleaned_data["amount"],
                        new_date=form.cleaned_data["date"],
                    )
                if kind == "refund":
                    refund = cast(Refund, obj)
                    validate_refund_rules(
                        household=home,
                        entry_date=form.cleaned_data["date"],
                        category=form.cleaned_data["category"],
                        amount=form.cleaned_data["amount"],
                        original_expense=refund.original_expense,
                        refund=refund,
                    )
                old_occurrence = obj.bill_occurrence if kind == "expense" else None
                old_savings = snapshot_savings_entries(obj, detach_income=True)
                new_inflow = None
                new_inflow_allocation = None
                old_amount = obj.amount
                was_cleared_before = (
                    obj.source_cleared_at is not None or obj.destination_cleared_at is not None
                    if kind == "transfer"
                    else getattr(obj, "cleared_at", None) is not None
                )
                old_allocation_state = {
                    item.account_id: (item.amount, item.cleared_at) for item in allocations
                }
                obj.date = form.cleaned_data["date"]
                obj.amount = form.cleaned_data["amount"]
                amount_changed = obj.amount != old_amount
                if amount_changed and kind in {"expense", "refund"}:
                    obj.cleared_at = None
                if amount_changed and kind == "transfer":
                    obj.source_cleared_at = None
                    obj.destination_cleared_at = None
                if amount_changed and kind != "income":
                    release_entry_matches(home, request.user, kind, obj)
                setattr(obj, description_field, form.cleaned_data["description"])
                if "notes" in form.fields:
                    obj.notes = form.cleaned_data["notes"]
                if "category" in form.fields and form.cleaned_data["category"] is not None:
                    obj.category = form.cleaned_data["category"]
                selected_bill = form.cleaned_data["bill"] if kind == "expense" else None
                if kind == "expense":
                    if selected_bill is None:
                        obj.bill_occurrence = None
                    elif not (
                        old_occurrence
                        and old_occurrence.bill_id == selected_bill.pk
                        and old_occurrence.status != BillOccurrence.Status.SKIPPED
                    ):
                        obj.bill_occurrence = choose_occurrence(
                            selected_bill, obj.date, exclude_expense=obj
                        )
                    obj.is_essential_override = form.cleaned_data.get("is_essential_override")
                obj.save()
                if kind == "income":
                    income = cast(Income, obj)
                    # Rows are updated in place: a statement line matched to an allocation
                    # refers to it by id, so replacing the rows would orphan the match.
                    released = []
                    for item in allocations:
                        new_value = form.cleaned_data[f"allocation_{item.account_id}"]
                        if new_value == item.amount:
                            continue
                        amount_changed = True
                        released.append((item.account, f"allocation:{item.pk}"))
                        if new_value > 0:
                            item.amount = new_value
                            item.cleared_at = None
                            item.save(update_fields=["amount", "cleared_at"])
                        else:
                            item.delete()
                    for account, key in released:
                        release_statement_match(home, request.user, account, key)
                    savings_allocation = income.allocations.filter(
                        account__type=Account.Type.SAVINGS
                    ).first()
                    if savings_allocation:
                        new_inflow = (
                            form.cleaned_data.get("savings_destination_goal"),
                            savings_allocation.amount,
                        )
                        new_inflow_allocation = savings_allocation
                split_values = None
                if kind == "expense" and form.cleaned_data["split_transaction"]:
                    assert split_formset is not None
                    split_values = [
                        (item["category"], item["amount"])
                        for item in split_formset.cleaned_data
                        if item
                    ]
                    if len(split_values) < 2:
                        raise ValidationError(
                            "Add at least two category amounts for a split transaction."
                        )
                    if sum((amount for _, amount in split_values), Decimal("0.00")) != obj.amount:
                        raise ValidationError(
                            "Split amounts must add up exactly to the expense total."
                        )
                    split_categories = [split_category for split_category, _ in split_values]
                    if len({item.pk for item in split_categories}) != len(split_categories):
                        raise ValidationError("Use each split category only once.")
                    expense = cast(Expense, obj)
                    if selected_bill and any(
                        item.type != Category.Type.BILL for item in split_categories
                    ):
                        raise ValidationError("Choose Bills categories for this bill payment.")
                    if not selected_bill and any(
                        not expense_category_allowed(expense.account, item)
                        for item in split_categories
                    ):
                        raise ValidationError(
                            "Every split category must match the transaction account."
                        )
                    expense.splits.all().delete()
                    ExpenseSplit.objects.bulk_create(
                        [
                            ExpenseSplit(
                                expense=expense,
                                category=split_category,
                                amount=split_amount,
                            )
                            for split_category, split_amount in split_values
                        ]
                    )
                    expense.category = split_categories[0]
                    expense.save(update_fields=["category"])
                elif kind == "expense":
                    cast(Expense, obj).splits.all().delete()
                new_outflow = None
                if source_is_savings:
                    assert savings_source_formset is not None
                    new_outflow = savings_source_values(savings_source_formset)
                if destination_is_savings and kind in {"refund", "transfer"}:
                    new_inflow = (form.cleaned_data.get("savings_destination_goal"), obj.amount)
                if kind == "income":
                    inflow_kind = SavingsGoalEntry.Kind.INCOME
                elif kind == "refund":
                    inflow_kind = SavingsGoalEntry.Kind.REFUND
                else:
                    inflow_kind = SavingsGoalEntry.Kind.TRANSFER_IN
                rewrite_savings_entries(
                    household=home,
                    actor=request.user,
                    old_entries=old_savings,
                    entry_date=obj.date,
                    amount=obj.amount,
                    kind=(
                        SavingsGoalEntry.Kind.EXPENSE
                        if kind == "expense"
                        else SavingsGoalEntry.Kind.TRANSFER_OUT
                    ),
                    outflow=new_outflow,
                    inflow=new_inflow,
                    inflow_kind=inflow_kind,
                    income_allocation=new_inflow_allocation,
                    expense=obj if kind == "expense" else None,
                    refund=obj if kind == "refund" else None,
                    transfer=obj if kind == "transfer" else None,
                )
                if old_occurrence:
                    update_bill_status(old_occurrence)
                if (
                    kind == "expense"
                    and obj.bill_occurrence
                    and (not old_occurrence or obj.bill_occurrence_id != old_occurrence.pk)
                ):
                    update_bill_status(obj.bill_occurrence)
                audit_changes = {
                    "date": obj.date.isoformat(),
                    "description": form.cleaned_data["description"],
                    "amount": str(obj.amount),
                }
                if category is not None:
                    audit_changes["category"] = obj.category.name
                if kind == "expense":
                    audit_changes["bill"] = selected_bill.name if selected_bill else ""
                if split_values is not None:
                    audit_changes["splits"] = [
                        {"category": split_category.name, "amount": str(split_amount)}
                        for split_category, split_amount in split_values
                    ]
                audit(
                    home,
                    request.user,
                    obj,
                    "update",
                    audit_changes,
                )
                anchored = (
                    sync_pay_period_boundaries(home, request.user)
                    if kind == "income" and cast(Income, obj).is_paycheck
                    else None
                )
            if anchored:
                pay_period_sync_messages(request, anchored)
            was_cleared = (
                any(cleared for _, cleared in old_allocation_state.values())
                if kind == "income"
                else was_cleared_before
            )
            reset_note = (
                " It was marked uncleared because the amount changed."
                if amount_changed and was_cleared
                else ""
            )
            messages.success(request, f"{label} updated.{reset_note}")
            return redirect("transactions")
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "budget/form.html",
        {
            **common(request),
            "form": form,
            "title": f"Edit {label.lower()}",
            "allocation_help": kind == "income",
            "return_url": form_return_url(request, reverse("transactions")),
            "expense_split_formset": split_formset,
            "savings_formset": savings_source_formset,
            "merchant_names": (merchant_memory(home)[0] if kind in {"expense", "refund"} else None),
        },
    )


def _delete_transaction(request, home, kind, model, obj, description):
    """Delete ``obj`` with its side effects; caller provides the transaction."""
    obj = get_object_or_404(model.objects.select_for_update(), pk=obj.pk, household=home)
    ensure_open(home, obj.date)
    linked_refund_count = obj.refunds.count() if kind == "expense" else 0
    undo_data = undo.snapshot(kind, obj, description) if undo.is_undoable(kind, obj) else None
    occurrence = obj.bill_occurrence if kind == "expense" else None
    if linked_refund_count:
        obj.refunds.update(original_expense=None)
    audit(
        home,
        request.user,
        obj,
        "delete",
        {
            "date": obj.date.isoformat(),
            "description": description,
            "amount": str(obj.amount),
            "detached_refunds": linked_refund_count,
        },
    )
    removed_savings = snapshot_savings_entries(obj)
    was_paycheck = kind == "income" and obj.is_paycheck
    release_entry_matches(home, request.user, kind, obj)
    obj.delete()
    finish_savings_removal(home, request.user, removed_savings)
    if occurrence:
        update_bill_status(occurrence)
    if was_paycheck:
        sync_pay_period_boundaries(home, request.user)
    return undo_data


def _history_return(request):
    target = request.POST.get("next", "")
    if target.startswith("/transactions/") and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return reverse("transactions")


@login_required
def transaction_delete(request, kind, pk):
    home = household(request)
    model, label, _ = transaction_configuration(kind)
    obj = get_object_or_404(model, pk=pk, household=home)
    description = transaction_description(obj, kind)
    if request.method == "POST" and request.POST.get("quick"):
        if not undo.is_undoable(kind, obj):
            return redirect("transaction_delete", kind=kind, pk=obj.pk)
        try:
            with transaction.atomic():
                undo_data = _delete_transaction(request, home, kind, model, obj, description)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect(_history_return(request))
        if undo_data:
            request.session[undo.SESSION_KEY] = undo_data
        return redirect(_history_return(request))
    confirmation_value = f"{label}: {description}"
    form = ConfirmDeletionForm(request.POST or None, expected_value=confirmation_value)
    linked_refund_count = obj.refunds.count() if kind == "expense" else 0
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                _delete_transaction(request, home, kind, model, obj, description)
            messages.success(request, f"{label} deleted. Related totals were updated.")
            return redirect("transactions")
        except ValidationError as exc:
            form.add_error(None, exc)
    dependency_note = (
        f" {linked_refund_count} linked refund(s) will be preserved and detached."
        if linked_refund_count
        else ""
    )
    return render(
        request,
        "budget/confirm_delete.html",
        {
            **common(request),
            "form": form,
            "title": f"Delete {label.lower()}",
            "warning": (
                f"Delete the {label.lower()} “{description}” for ${obj.amount:,.2f}? "
                "Account balances, budgets, reports, and linked bill status will be "
                f"recalculated.{dependency_note} This cannot be undone."
            ),
            "submit_label": f"Delete {label.lower()}",
            "return_url": reverse("transactions"),
        },
    )


@login_required
@require_POST
def transaction_undo(request):
    home = household(request)
    try:
        with transaction.atomic():
            data = undo.claim(request.session, home)
            if data is not None:
                undo.restore(home, request.user, data)
    except ValidationError as exc:
        messages.error(request, "Could not restore the entry: " + " ".join(exc.messages))
        return redirect(_history_return(request))
    if data is None:
        messages.error(request, "There is nothing to undo, or the undo time has passed.")
    else:
        messages.success(request, f"Restored “{data['description']}”.")
    return redirect(_history_return(request))


def searchable_amount(value):
    if not value:
        return None
    normalized = value.replace("$", "").replace(",", "").strip()
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


@login_required
def bills(request):
    home = household(request)
    selected = month_from(request.GET.get("month"))
    next_month = (selected + timedelta(days=32)).replace(day=1)
    previous_month = (selected - timedelta(days=1)).replace(day=1)
    month_end = next_month - timedelta(days=1)

    ensure_bill_occurrences(home, through=month_end, start=selected)

    # Read-only page: one annotated query supplies every Paid figure the page reads.
    qs = month_occurrences(home, selected).annotate(paid_total=paid_total())
    recent_paid = recent_paid_amounts(qs)
    for item in qs:
        item.forecast = None
        if item.status == BillOccurrence.Status.UNCONFIRMED:
            forecast = bill_forecast(item, recent_paid[item.pk])
            if forecast["amount"] != item.expected_amount:
                item.forecast = forecast
    return render(
        request,
        "budget/bills.html",
        {
            **common(request),
            "occurrences": qs,
            "today": timezone.localdate(),
            "selected_month": selected,
            "previous_month": previous_month,
            "next_month": next_month,
            "month_total": sum(
                (item.target_amount for item in qs if item.status != BillOccurrence.Status.SKIPPED),
                Decimal("0"),
            ),
            "month_remaining": sum((item.remaining_amount for item in qs), Decimal("0")),
            "has_unconfirmed": any(item.status == BillOccurrence.Status.UNCONFIRMED for item in qs),
        },
    )


def month_occurrences(home, selected):
    next_month = (selected + timedelta(days=32)).replace(day=1)
    month_end = next_month - timedelta(days=1)
    return (
        BillOccurrence.objects.select_related(
            "bill", "bill__category", "bill__account", "category", "account"
        )
        .filter(
            bill__household=home,
            bill__effective_start__lte=month_end,
            due_date__year=selected.year,
            due_date__month=selected.month,
        )
        .filter(Q(bill__effective_end=None) | Q(bill__effective_end__gte=selected))
        .order_by("due_date", "bill__name")
    )


def _bills_redirect(month_value, anchor=""):
    selected = month_from(month_value)
    return redirect(f"/bills/?month={selected:%Y-%m}{anchor}")


@login_required
@require_POST
def bill_mark_paid(request, pk):
    occurrence = get_object_or_404(
        BillOccurrence.objects.select_related("bill"),
        pk=pk,
        bill__household=household(request),
    )
    try:
        with transaction.atomic():
            prior_confirmed = (
                BillOccurrence.objects.select_for_update()
                .values_list("confirmed_amount", flat=True)
                .get(pk=occurrence.pk)
            )
            expense = pay_bill_occurrence(occurrence, request.user, timezone.localdate())
        request.session[undo.PAID_SESSION_KEY] = undo.paid_snapshot(
            expense, occurrence, prior_confirmed
        )
    except ValidationError as exc:
        messages.error(request, exc.message)
    return _bills_redirect(request.POST.get("month"), f"#bill-{occurrence.pk}")


@login_required
@require_POST
def bill_paid_undo(request):
    """Take back the last "Paid" press: delete its payment and restore the month exactly."""
    home = household(request)
    try:
        with transaction.atomic():
            data = undo.claim(request.session, home, undo.PAID_SESSION_KEY)
            if data is not None:
                expense = Expense.objects.filter(
                    household=home,
                    pk=data["expense_id"],
                    bill_occurrence_id=data["occurrence_id"],
                    amount=Decimal(data["amount"]),
                ).first()
                if expense is None:
                    data = None
                else:
                    _delete_transaction(request, home, "expense", Expense, expense, data["name"])
                    prior = data["prior_confirmed_amount"]
                    occurrence = BillOccurrence.objects.select_for_update().get(
                        pk=data["occurrence_id"]
                    )
                    occurrence.confirmed_amount = Decimal(prior) if prior is not None else None
                    occurrence.save(update_fields=["confirmed_amount"])
                    update_bill_status(occurrence)
                    audit(
                        home,
                        request.user,
                        occurrence,
                        "update",
                        {"undo_mark_paid": True, "confirmed_amount": prior},
                    )
    except ValidationError as exc:
        messages.error(request, "Could not undo the payment: " + " ".join(exc.messages))
        return _bills_redirect(request.POST.get("month"))
    if data is None:
        messages.error(request, "There is nothing to undo, or the undo time has passed.")
    else:
        messages.success(request, f"{data['name']} is unpaid again.")
    return _bills_redirect(
        request.POST.get("month"), f"#bill-{data['occurrence_id']}" if data else ""
    )


@login_required
@require_POST
def bills_confirm_all(request):
    home = household(request)
    selected = month_from(request.POST.get("month"))
    occurrences = month_occurrences(home, selected).filter(status=BillOccurrence.Status.UNCONFIRMED)
    confirmed, locked = confirm_unchanged_bills(list(occurrences), request.user)
    messages.success(
        request,
        f"Confirmed {confirmed} bills" + (f" ({locked} skipped: locked)" if locked else ""),
    )
    return _bills_redirect(request.POST.get("month"))


def month_from(value):
    try:
        return (
            datetime.strptime(value, "%Y-%m").date().replace(day=1)
            if value
            else timezone.localdate().replace(day=1)
        )
    except ValueError:
        return timezone.localdate().replace(day=1)


@login_required
def bill_confirm(request, pk):
    obj = get_object_or_404(BillOccurrence, pk=pk, bill__household=household(request))
    if request.method == "POST":
        try:
            with transaction.atomic():
                ensure_open(obj.bill.household, obj.due_date)
                obj.confirmed_amount = obj.expected_amount
                obj.status = BillOccurrence.Status.CONFIRMED
                obj.skipped_reason = ""
                obj.save(update_fields=["confirmed_amount", "status", "skipped_reason"])
                update_bill_status(obj)
                audit(
                    obj.bill.household,
                    request.user,
                    obj,
                    "update",
                    {"confirmed_amount": str(obj.confirmed_amount), "status": obj.status},
                )
            messages.success(request, f"{obj.bill.name} confirmed.")
        except ValidationError as exc:
            messages.error(request, exc.message)
    selected = request.POST.get("month", "")
    destination = f"/bills/?month={selected}" if selected else reverse("bills")
    return redirect(f"{destination}#bill-{obj.pk}")


@login_required
def bill_skip(request, pk):
    occurrence = get_object_or_404(
        BillOccurrence.objects.select_related("bill"),
        pk=pk,
        bill__household=household(request),
    )
    form = BillOccurrenceSkipForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                ensure_open(occurrence.bill.household, occurrence.due_date)
                if occurrence.amount_paid > 0:
                    raise ValidationError("A bill with linked payments cannot be skipped.")
                occurrence.status = BillOccurrence.Status.SKIPPED
                occurrence.skipped_reason = form.cleaned_data["reason"]
                occurrence.save(update_fields=["status", "skipped_reason"])
                audit(
                    occurrence.bill.household,
                    request.user,
                    occurrence,
                    "update",
                    {"status": occurrence.status, "skipped_reason": occurrence.skipped_reason},
                )
            messages.success(request, f"{occurrence.bill.name} skipped for this month.")
            return redirect(f"/bills/?month={occurrence.due_date:%Y-%m}")
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "budget/form.html",
        {
            **common(request),
            "form": form,
            "title": f"Skip {occurrence.bill.name} — {occurrence.due_date:%B %Y}",
            "return_url": form_return_url(request, f"/bills/?month={occurrence.due_date:%Y-%m}"),
        },
    )


@login_required
def bill_delete(request, pk):
    occurrence = get_object_or_404(
        BillOccurrence.objects.select_related("bill"),
        pk=pk,
        bill__household=household(request),
    )
    selected_month = occurrence.due_date.replace(day=1)
    form = ConfirmDeletionForm(request.POST or None, expected_value=occurrence.bill.name)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                ensure_open(occurrence.bill.household, occurrence.due_date)
                kept = remove_bill(
                    occurrence.bill, request.user, selected_month - timedelta(days=1)
                )
            kept_note = (
                f"; {kept} later month{'s' if kept != 1 else ''} with payments kept."
                if kept
                else "."
            )
            messages.success(
                request,
                f"{occurrence.bill.name} removed. Existing payment history was preserved"
                + kept_note,
            )
            return redirect(f"/bills/?month={selected_month:%Y-%m}")
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "budget/confirm_delete.html",
        {
            **common(request),
            "form": form,
            "title": f"Remove {occurrence.bill.name}",
            "warning": (
                "This stops the recurring bill from this month forward. "
                "Existing bill and payment history will be preserved."
            ),
            "submit_label": "Remove bill",
            "return_url": f"/bills/?month={selected_month:%Y-%m}",
        },
    )


@login_required
def bill_add(request):
    home = household(request)
    initial = {
        "effective_start": timezone.localdate().replace(day=1),
        "account": Account.objects.filter(household=home, type=Account.Type.BILLS).first(),
        "active": True,
    }
    form = RecurringBillForm(request.POST or None, initial=initial, household=home)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            bill = form.save(commit=False)
            bill.household = home
            bill.save()
            # Generate from the bill's first month (or this month, if it started earlier)
            # through at least three months ahead, so a future-dated bill is never empty.
            first_month = max(
                bill.effective_start.replace(day=1), timezone.localdate().replace(day=1)
            )
            ensure_bill_occurrences(
                home,
                start=first_month,
                through=max(
                    end_of_month_ahead(timezone.localdate(), 3), end_of_month_ahead(first_month, 0)
                ),
            )
            audit(home, request.user, bill, "create", {"default_amount": str(bill.default_amount)})
        messages.success(request, f"{bill.name} added. Its monthly obligation is ready to review.")
        return redirect(f"/bills/?month={bill.effective_start:%Y-%m}")
    return render(
        request,
        "budget/form.html",
        {
            **common(request),
            "form": form,
            "title": "Add bill",
            "return_url": form_return_url(request, "/bills/"),
        },
    )


@login_required
def bill_edit_amount(request, pk):
    occurrence = get_object_or_404(
        BillOccurrence.objects.select_related("bill"),
        pk=pk,
        bill__household=household(request),
    )
    if request.method == "GET" and occurrence.confirmed_amount is None:
        occurrence.confirmed_amount = occurrence.expected_amount
    original_due_date = occurrence.due_date
    form = BillOccurrenceAmountForm(request.POST or None, instance=occurrence)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                ensure_open(occurrence.bill.household, original_due_date)
                occurrence = form.save(commit=False)
                occurrence.category = form.cleaned_data["category"]
                occurrence.account = form.cleaned_data["account"]
                ensure_open(occurrence.bill.household, occurrence.due_date)
                occurrence.pay_period = period_for_due_date(
                    occurrence.bill.household, occurrence.due_date
                )
                occurrence.status = BillOccurrence.Status.CONFIRMED
                occurrence.skipped_reason = ""
                occurrence.save()
                update_bill_status(occurrence)
                audit(
                    occurrence.bill.household,
                    request.user,
                    occurrence,
                    "update",
                    {
                        "due_date": occurrence.due_date.isoformat(),
                        "confirmed_amount": str(occurrence.confirmed_amount),
                    },
                )
                if occurrence.bill.name != form.cleaned_data["bill_name"]:
                    occurrence.bill.name = form.cleaned_data["bill_name"]
                    occurrence.bill.save(update_fields=["name"])
                    audit(
                        occurrence.bill.household,
                        request.user,
                        occurrence.bill,
                        "update",
                        {"name": occurrence.bill.name},
                    )
                bill_fields = []
                bill_changes = {}
                if occurrence.bill.category_id != occurrence.category_id:
                    occurrence.bill.category = occurrence.category
                    bill_fields.append("category")
                    bill_changes["category"] = occurrence.category.name
                if occurrence.bill.account_id != occurrence.account_id:
                    occurrence.bill.account = occurrence.account
                    bill_fields.append("account")
                    bill_changes["account"] = occurrence.account.name
                if bill_fields:
                    later = occurrence.bill.occurrences.filter(due_date__gt=occurrence.due_date)
                    for later_due in later.values_list("due_date", flat=True):
                        ensure_open(occurrence.bill.household, later_due)
                    occurrence.bill.save(update_fields=bill_fields)
                    later.update(category=occurrence.category, account=occurrence.account)
                    audit(
                        occurrence.bill.household,
                        request.user,
                        occurrence.bill,
                        "update",
                        bill_changes,
                    )
                if form.cleaned_data["update_future_default"]:
                    occurrence.bill.default_amount = occurrence.confirmed_amount
                    occurrence.bill.save(update_fields=["default_amount"])
                    audit(
                        occurrence.bill.household,
                        request.user,
                        occurrence.bill,
                        "update",
                        {"default_amount": str(occurrence.confirmed_amount)},
                    )
                if form.cleaned_data["update_future_due_day"]:
                    occurrence.bill.due_day = occurrence.due_date.day
                    occurrence.bill.save(update_fields=["due_day"])
                    audit(
                        occurrence.bill.household,
                        request.user,
                        occurrence.bill,
                        "update",
                        {"due_day": occurrence.due_date.day},
                    )
                frequency_changed = form.cleaned_data["bill_every_months"] not in (
                    None,
                    occurrence.bill.every_months,
                )
                if frequency_changed:
                    removed, kept = change_bill_frequency(
                        occurrence.bill,
                        request.user,
                        form.cleaned_data["bill_every_months"],
                        form.cleaned_data["bill_first_month"] or occurrence.due_date,
                    )
                    occurrence.bill.refresh_from_db()
                bill_level_change = (
                    bool(bill_fields)
                    or form.cleaned_data["update_future_default"]
                    or form.cleaned_data["update_future_due_day"]
                )
                if bill_level_change:
                    ensure_bill_occurrences(
                        occurrence.bill.household,
                        through=end_of_month_ahead(timezone.localdate(), 3),
                    )
            future_changes = []
            if form.cleaned_data["update_future_due_day"]:
                future_changes.append("due date")
            if form.cleaned_data["update_future_default"]:
                future_changes.append("amount")
            scope = (
                f" Future {' and '.join(future_changes)} defaults were updated."
                if future_changes
                else ""
            )
            if frequency_changed:
                scope += (
                    f" Now {occurrence.bill.get_every_months_display().lower()}"
                    f" from {occurrence.bill.schedule_start:%B %Y}; {removed} future month"
                    f"{'s' if removed != 1 else ''} removed"
                    + (f", {kept} kept (paid, skipped or locked)" if kept else "")
                    + "."
                )
            messages.success(request, f"{occurrence.bill.name} updated for this month.{scope}")
            return redirect(f"/bills/?month={occurrence.due_date:%Y-%m}")
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "budget/form.html",
        {
            **common(request),
            "form": form,
            "title": f"Edit {occurrence.bill.name} — {occurrence.due_date:%B %Y}",
            "return_url": form_return_url(request, f"/bills/?month={occurrence.due_date:%Y-%m}"),
        },
    )


@login_required
def budgets(request):
    home = household(request)
    period_id = request.GET.get("period")
    period = (
        PayPeriod.objects.filter(household=home, pk=period_id).first()
        if period_id
        else PayPeriod.objects.filter(
            household=home, start_date__lte=timezone.localdate(), end_date__gte=timezone.localdate()
        ).first()
    )
    budget = PayPeriodBudget.objects.get_or_create(pay_period=period)[0] if period else None
    form = BudgetForm(request.POST or None, instance=budget)
    if request.method == "POST" and period and form.is_valid():
        try:
            with transaction.atomic():
                ensure_open(home, period.start_date)
                form.save()
            messages.success(request, "Budget saved.")
            return redirect(f"{reverse('budgets')}?period={period.pk}")
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "budget/budgets.html",
        {
            **common(request),
            "period": period,
            "form": form,
            "periods": PayPeriod.objects.filter(household=home)[:24],
            **_category_budget_context(home),
        },
    )


def _category_budget_context(home) -> dict:
    from . import habits
    from .debt_forms import CategoryBudgetForm

    today = timezone.localdate()
    current = PayPeriod.objects.filter(
        household=home, start_date__lte=today, end_date__gte=today
    ).first()
    form = CategoryBudgetForm(household=home, default_date=current.start_date if current else today)
    rows = [
        (category, habits.budget_for(category, today))
        for category in form.fields["category"].queryset
    ]
    return {"category_budget_form": form, "category_budget_rows": rows}


@login_required
def pay_periods(request):
    home = household(request)
    generate_form = GeneratePayPeriodsForm(request.POST or None, household=home)
    if request.method == "POST" and generate_form.is_valid():
        try:
            with transaction.atomic():
                schedule, _ = PaySchedule.objects.select_for_update().get_or_create(
                    household=home,
                    defaults={
                        "frequency": PaySchedule.Frequency.SEMIMONTHLY,
                        "nominal_days": [1, 15],
                    },
                )
                schedule.frequency = generate_form.cleaned_data["frequency"]
                if schedule.frequency == PaySchedule.Frequency.SEMIMONTHLY:
                    schedule.nominal_days = [1, 15]
                elif schedule.frequency == PaySchedule.Frequency.MONTHLY:
                    latest = PayPeriod.objects.filter(household=home).order_by("-end_date").first()
                    if not latest:
                        raise ValidationError(
                            "Add the current pay period before generating future ones."
                        )
                    schedule.nominal_days = [latest.nominal_payday.day]
                schedule.save(update_fields=["frequency", "nominal_days"])
                audit(
                    home,
                    request.user,
                    schedule,
                    "update",
                    {"fields_updated": ["frequency", "nominal_days"]},
                )
                created = generate_future_pay_periods(
                    home, generate_form.cleaned_data["generation_through"]
                )
                for period in created:
                    audit(home, request.user, period, "create", pay_period_audit_values(period))
                reassign_bill_occurrence_periods(home)
            messages.success(request, f"{len(created)} pay periods generated.")
            return redirect("pay_periods")
        except ValidationError as exc:
            generate_form.add_error(None, exc)
    return render(
        request,
        "budget/pay_periods.html",
        {
            **common(request),
            "periods": PayPeriod.objects.filter(household=home),
            "generate_form": generate_form,
            "pay_schedule": PaySchedule.objects.filter(household=home).first(),
        },
    )


def next_nominal_payday(after, frequency, nominal_days):
    if frequency == PaySchedule.Frequency.WEEKLY:
        return after + timedelta(days=7)
    if frequency == PaySchedule.Frequency.BIWEEKLY:
        return after + timedelta(days=14)
    if frequency == PaySchedule.Frequency.MONTHLY:
        anchor_day = nominal_days[0]
        year, month = after.year, after.month
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
        return date(year, month, min(anchor_day, monthrange(year, month)[1]))
    year, month = after.year, after.month
    for _ in range(36):
        candidates = sorted(
            {date(year, month, min(day, monthrange(year, month)[1])) for day in nominal_days}
        )
        future = next((candidate for candidate in candidates if candidate > after), None)
        if future:
            return future
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    raise ValidationError("Could not determine the next payday from this profile's schedule.")


def generate_future_pay_periods(home, through):
    """Create contiguous future periods, each starting on its expected deposit date.

    The first new period starts the day after the latest period ends. Each period ends
    the day before the next payday's expected deposit; a recorded paycheck later moves
    the boundary to the real deposit date (``budget.pay_periods``).
    """
    schedule = PaySchedule.objects.select_for_update().filter(household=home).first()
    nominal_days = sorted(
        {int(day) for day in (schedule.nominal_days if schedule else []) if 1 <= int(day) <= 31}
    )
    if not nominal_days:
        raise ValidationError("Configure at least one nominal payday for this profile.")
    assert schedule is not None
    latest = (
        PayPeriod.objects.select_for_update().filter(household=home).order_by("-end_date").first()
    )
    if not latest:
        raise ValidationError("Add the current pay period before generating future ones.")

    created = []
    frequency = schedule.frequency
    start = latest.end_date + timedelta(days=1)
    # Every schedule continues from the latest nominal payday. The day after the latest
    # period is its expected deposit date, a day or more before the nominal payday, so
    # using it as the next nominal payday would drift earlier with each batch.
    nominal = next_nominal_payday(latest.nominal_payday, frequency, nominal_days)
    stub_next = None
    if frequency != PaySchedule.Frequency.SEMIMONTHLY and expected_deposit_date(nominal) > start:
        # The latest period does not end where the cadence says (a gap): the first new
        # period is a stub that starts the day after it and ends at the next payday.
        stub_next, nominal = nominal, start
    while start <= through:
        next_nominal = stub_next or next_nominal_payday(nominal, frequency, nominal_days)
        stub_next = None
        # A stale nominal payday (behind the latest period) is skipped rather than
        # producing an empty or overlapping period.
        while expected_deposit_date(next_nominal) <= start:
            nominal, next_nominal = (
                next_nominal,
                next_nominal_payday(next_nominal, frequency, nominal_days),
            )
        end = expected_deposit_date(next_nominal) - timedelta(days=1)
        if PayPeriod.objects.filter(
            household=home, start_date__lte=end, end_date__gte=start
        ).exists():
            raise ValidationError("Generation would overlap an existing pay period.")
        period = PayPeriod.objects.create(
            household=home,
            nominal_payday=nominal,
            expected_deposit_date=expected_deposit_date(nominal),
            start_date=start,
            end_date=end,
        )
        create_period_budget(period)
        created.append(period)
        nominal, start = next_nominal, end + timedelta(days=1)
    return created


@login_required
def pay_period_add(request):
    home = household(request)
    form = PayPeriodForm(request.POST or None, household=home)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                period = form.save(commit=False)
                period.household = home
                period.save()
                create_period_budget(period)
                reassign_bill_occurrence_periods(home)
                audit(home, request.user, period, "create", pay_period_audit_values(period))
            messages.success(request, "Pay period added.")
            return redirect("pay_periods")
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "budget/form.html",
        {
            **common(request),
            "form": form,
            "title": "Add pay period",
            "return_url": reverse("pay_periods"),
        },
    )


@login_required
def pay_period_edit(request, pk):
    home = household(request)
    period = get_object_or_404(PayPeriod, household=home, pk=pk)
    if period.is_locked:
        messages.error(request, "Reopen this pay period before editing it.")
        return redirect("pay_periods")
    form = PayPeriodForm(request.POST or None, instance=period, household=home)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                for neighbour in form.neighbour_adjustments:
                    neighbour.save(update_fields=["start_date", "end_date"])
                    audit(
                        home, request.user, neighbour, "update", pay_period_audit_values(neighbour)
                    )
                period = form.save()
                reassign_bill_occurrence_periods(home)
                audit(home, request.user, period, "update", pay_period_audit_values(period))
            messages.success(request, "Pay period updated.")
            return redirect("pay_periods")
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "budget/form.html",
        {
            **common(request),
            "form": form,
            "title": "Edit pay period",
            "return_url": reverse("pay_periods"),
        },
    )


def pay_period_audit_values(period):
    return {
        "nominal_payday": period.nominal_payday.isoformat(),
        "expected_deposit_date": period.expected_deposit_date.isoformat(),
        "actual_deposit_date": (
            period.actual_deposit_date.isoformat() if period.actual_deposit_date else None
        ),
        "start_date": period.start_date.isoformat(),
        "end_date": period.end_date.isoformat(),
        "is_exceptional": period.is_exceptional,
    }


@login_required
def pay_period_lock(request, pk):
    home = household(request)
    if request.method != "POST":
        return redirect("pay_periods")
    with transaction.atomic():
        period = get_object_or_404(PayPeriod.objects.select_for_update(), household=home, pk=pk)
        if period.is_locked and not request.user.is_staff:
            raise PermissionDenied("Only an administrator can reopen a locked pay period.")
        period.is_locked = not period.is_locked
        period.save(update_fields=["is_locked"])
        audit(home, request.user, period, "update", {"is_locked": period.is_locked})
    messages.success(request, "Pay period locked." if period.is_locked else "Pay period reopened.")
    return redirect("pay_periods")


def category_totals_for_period(home, start, end):
    totals = {}
    for category_type in (Category.Type.SPENDING, Category.Type.BILL):
        for name, amount in expense_category_totals(
            category_type=category_type,
            household=home,
            start=start,
            end=end,
        ).items():
            totals[(name, category_type)] = amount
    refunds = (
        Refund.objects.filter(household=home, date__range=(start, end))
        .values("category__name", "category__type")
        .annotate(total=Sum("amount"))
    )
    for refund in refunds:
        key = (refund["category__name"], refund["category__type"])
        totals[key] = totals.get(key, Decimal("0.00")) - refund["total"]
    return totals


def category_report_rows(home, start, end):
    period_length = (end - start).days + 1
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=max(period_length - 1, 0))
    current = category_totals_for_period(home, start, end)
    previous = category_totals_for_period(home, previous_start, previous_end)
    keys = set(current) | set(previous)
    rows = []
    for name, category_type in keys:
        amount = current.get((name, category_type), Decimal("0.00"))
        previous_amount = previous.get((name, category_type), Decimal("0.00"))
        if amount == 0 and previous_amount == 0:
            continue
        change = amount - previous_amount
        rows.append(
            {
                "name": name,
                "type": Category.Type(category_type).label,
                "amount": amount,
                "previous_amount": previous_amount,
                "change": change,
                "change_abs": abs(change),
                "change_percent": (
                    (change / previous_amount * Decimal("100")).quantize(
                        Decimal("0.1"), rounding=ROUND_HALF_UP
                    )
                    if previous_amount != 0
                    else None
                ),
            }
        )
    rows.sort(key=lambda item: item["amount"], reverse=True)
    chart_total = sum((item["amount"] for item in rows if item["amount"] > 0), Decimal("0.00"))
    palette = ["#4477aa", "#ee6677", "#228833", "#ccbb44", "#66ccee", "#aa3377"]
    cursor = Decimal("0.00")
    segments = []
    for index, row in enumerate(rows):
        row["color"] = palette[index % len(palette)]
        row["share"] = (
            (row["amount"] / chart_total * Decimal("100")).quantize(
                Decimal("0.1"), rounding=ROUND_HALF_UP
            )
            if row["amount"] > 0 and chart_total > 0
            else Decimal("0.0")
        )
        if row["amount"] <= 0 or chart_total <= 0:
            continue
        end_percent = cursor + (row["amount"] / chart_total * Decimal("100"))
        segments.append(f"{row['color']} {cursor:.2f}% {end_percent:.2f}%")
        cursor = end_percent
    return {
        "rows": rows,
        "chart_total": chart_total,
        "chart_gradient": ", ".join(segments) or "var(--muted) 0% 100%",
        "previous_start": previous_start,
        "previous_end": previous_end,
    }


def _aggregate_amount(queryset):
    return queryset.aggregate(value=Sum("amount"))["value"] or Decimal("0.00")


def account_flow_for_period(account, start, end):
    date_range = (start, end)
    income = _aggregate_amount(
        IncomeAllocation.objects.filter(account=account, income__date__range=date_range)
    )
    refunds = _aggregate_amount(Refund.objects.filter(account=account, date__range=date_range))
    transfers_in = _aggregate_amount(
        Transfer.objects.filter(destination=account, date__range=date_range)
    )
    transfers_out = _aggregate_amount(
        Transfer.objects.filter(source=account, date__range=date_range)
    )
    expenses = _aggregate_amount(Expense.objects.filter(account=account, date__range=date_range))
    adjustments_in = _aggregate_amount(
        BalanceAdjustment.objects.filter(account=account, is_increase=True, date__range=date_range)
    )
    adjustments_out = _aggregate_amount(
        BalanceAdjustment.objects.filter(account=account, is_increase=False, date__range=date_range)
    )
    inflows = income + refunds + transfers_in + adjustments_in
    outflows = expenses + transfers_out + adjustments_out
    return {
        "income": income,
        "refunds": refunds,
        "transfers_in": transfers_in,
        "adjustments_in": adjustments_in,
        "expenses": expenses,
        "transfers_out": transfers_out,
        "adjustments_out": adjustments_out,
        "inflows": inflows,
        "outflows": outflows,
        "net": inflows - outflows,
    }


def account_report_rows(home, start, end, previous_start, previous_end):
    rows = []
    for account in Account.objects.filter(household=home).order_by("display_order", "name"):
        current = account_flow_for_period(account, start, end)
        previous = account_flow_for_period(account, previous_start, previous_end)
        rows.append(
            {
                "name": account.name,
                "type": account.get_type_display(),
                **current,
                "previous_inflows": previous["inflows"],
                "previous_outflows": previous["outflows"],
                "previous_net": previous["net"],
                "net_change": current["net"] - previous["net"],
                "net_abs": abs(current["net"]),
                "net_change_abs": abs(current["net"] - previous["net"]),
            }
        )
    return rows


def report_analysis_payload(data, outlook=None):
    historical_periods = data.get("historical_periods", [])
    payload = {
        # One cutoff for every figure below; the selected range may extend past it.
        "as_of": str(outlook["as_of"] if outlook else data["end"]),
        "selected_period": {
            "start": str(data["start"]),
            "end": str(outlook["end"] if outlook else data["end"]),
        },
        "comparison_period": {
            "start": str(data["previous_start"]),
            "end": str(data["previous_end"]),
        },
        "totals": {
            "selected": {
                key: f"{data[key]:.2f}"
                for key in ("spending", "refunds", "bills", "income", "transfers")
            },
            "comparison": {
                key: f"{data['comparison_totals'][key]:.2f}"
                for key in ("spending", "refunds", "bills", "income", "transfers")
            },
        },
        "categories": [
            {
                "name": row["name"],
                "type": row["type"],
                "current_net_spending": f"{row['amount']:.2f}",
                "previous_net_spending": f"{row['previous_amount']:.2f}",
                "change": f"{row['change']:.2f}",
                "share_percent": f"{row['share']:.1f}",
            }
            for row in data["category_rows"]
        ],
        "accounts": [
            {
                key: f"{row[key]:.2f}" if isinstance(row[key], Decimal) else row[key]
                for key in (
                    "name",
                    "type",
                    "income",
                    "refunds",
                    "transfers_in",
                    "adjustments_in",
                    "expenses",
                    "transfers_out",
                    "adjustments_out",
                    "inflows",
                    "outflows",
                    "net",
                    "previous_inflows",
                    "previous_outflows",
                    "previous_net",
                    "net_change",
                )
            }
            for row in data["account_rows"]
        ],
        "historical_periods": historical_periods,
    }
    if outlook:
        actual_end = outlook["actual_end"]
        period_status = (
            "future"
            if actual_end < data["start"]
            else "in_progress"
            if actual_end < outlook["end"]
            else "complete"
        )
        active_history = [
            row
            for row in historical_periods
            if any(
                Decimal(row[key]) != 0
                for key in ("income", "spending", "bills", "adjusted_savings_change")
            )
        ]

        def historical_average(key):
            if not active_history:
                return Decimal("0.00")
            return (
                sum((Decimal(row[key]) for row in active_history), Decimal("0.00"))
                / Decimal(len(active_history))
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        def income_rows(name):
            return [
                {"date": str(row["date"]), "amount": f"{row['amount']:.2f}"}
                for row in outlook[name]
            ]

        def schedule_rows(name):
            return [
                {
                    "deposit_date": str(row["deposit_date"]),
                    "date_source": row["date_source"],
                    "amount": f"{row['amount']:.2f}",
                    "timing": row["timing"],
                }
                for row in outlook[name]
            ]

        selected_schedule = outlook["selected_pay_schedule"]
        comparison_schedule = outlook["comparison_pay_schedule"]
        direct_income_comparison = (
            period_status == "complete"
            and bool(selected_schedule)
            and len(selected_schedule) == len(comparison_schedule)
        )
        payload["analysis_context"] = {
            "as_of_date": str(actual_end),
            "selected_period_status": period_status,
            "selected_period_days": outlook["period_days"],
            "observed_days": outlook["elapsed_days"],
            "remaining_days": outlook["remaining_days"],
            "selected_actuals_are_partial": period_status != "complete",
            "direct_selected_vs_adjacent_income_comparison_is_valid": (direct_income_comparison),
            "historical_windows_with_activity": len(active_history),
            "interpretation": (
                "Use projected full-period values for an in-progress range. Missing future "
                "deposits are scheduled income, not lost income. Use actual values for a "
                "completed historical range."
            ),
        }
        payload["trend_evidence"] = {
            "income": {
                "actual_through_as_of": f"{outlook['actual_income']:.2f}",
                "scheduled_remaining": f"{outlook['scheduled_income_remaining']:.2f}",
                "projected_full_period": f"{outlook['projected_income']:.2f}",
                "adjacent_completed_period": (f"{data['comparison_totals']['income']:.2f}"),
                "completed_history_average": f"{historical_average('income'):.2f}",
                "selected_deposits": income_rows("selected_income_by_date"),
                "adjacent_period_deposits": income_rows("comparison_income_by_date"),
                "recent_deposit_pattern": income_rows("recent_income_by_date"),
                "selected_schedule": schedule_rows("selected_pay_schedule"),
                "adjacent_period_schedule": schedule_rows("comparison_pay_schedule"),
            },
            "non_bill_spending": {
                "actual_through_as_of": f"{outlook['actual_spending']:.2f}",
                "projected_remaining": f"{outlook['projected_spending_remaining']:.2f}",
                "projected_full_period": (
                    f"{outlook['actual_spending'] + outlook['projected_spending_remaining']:.2f}"
                ),
                "budget_target": f"{outlook['budget_target']:.2f}",
                "adjacent_completed_period": (f"{data['comparison_totals']['spending']:.2f}"),
                "completed_history_average": f"{historical_average('spending'):.2f}",
            },
            "bills": {
                "actual_through_as_of": f"{outlook['actual_bills']:.2f}",
                "projected_remaining": f"{outlook['projected_bills_remaining']:.2f}",
                "projected_full_period": (
                    f"{outlook['actual_bills'] + outlook['projected_bills_remaining']:.2f}"
                ),
                "adjacent_completed_period": f"{data['comparison_totals']['bills']:.2f}",
                "completed_history_average": f"{historical_average('bills'):.2f}",
            },
            "savings": {
                "actual_change_through_as_of": f"{outlook['savings_change']:.2f}",
                "goal_fulfillment_spending": f"{outlook['fulfilled_savings']:.2f}",
                "adjusted_actual_change": (
                    f"{outlook['savings_change'] + outlook['fulfilled_savings']:.2f}"
                ),
                "completed_history_average_adjusted_change": (
                    f"{historical_average('adjusted_savings_change'):.2f}"
                ),
            },
        }
        top_goal = outlook.get("top_goal")
        payload["outlook"] = {
            "forecast": {
                key: f"{outlook[key]:.2f}"
                for key in (
                    "actual_income",
                    "projected_income",
                    "actual_spending",
                    "actual_bills",
                    "prior_bills",
                    "projected_spending_remaining",
                    "projected_bills_remaining",
                    "projected_end_balance",
                    "opening_balance",
                    "forecast_surplus",
                )
            },
            "savings": {
                key: f"{outlook[key]:.2f}"
                for key in (
                    "savings_start",
                    "savings_end",
                    "savings_change",
                    "fulfilled_savings",
                )
            },
            "savings_health": outlook["savings_health"],
            "first_priority": (
                {
                    "name": top_goal["name"],
                    "current": f"{top_goal['current']:.2f}",
                    "target": f"{top_goal['target']:.2f}",
                    "progress_percent": f"{top_goal['progress']:.1f}",
                    "net_contributions": f"{top_goal['net_contributions']:.2f}",
                    "completion_projection": top_goal["completion"],
                }
                if top_goal
                else None
            ),
            "nonessential_monthly_pace": f"{outlook['monthly_discretionary']:.2f}",
            "cut_scenarios": [
                {
                    "name": row["name"],
                    "percent": f"{row['rate']:.1f}",
                    "potential_cut": f"{row['potential']:.2f}",
                    "realistically_available_for_savings": f"{row['available']:.2f}",
                }
                for row in outlook["cut_scenarios"]
            ],
            "nonessential_categories": [
                {
                    "name": row["name"],
                    "monthly_pace": f"{row['monthly_amount']:.2f}",
                    "options": [
                        {
                            "name": option["name"],
                            "percent": f"{option['rate']:.1f}",
                            "cut": f"{option['amount']:.2f}",
                        }
                        for option in row["scenarios"]
                    ],
                }
                for row in outlook["category_cut_rows"]
            ],
            "top_merchants_by_category": [
                {
                    "category": row["name"],
                    "merchants": [
                        {"name": merchant["name"], "amount": f"{merchant['amount']:.2f}"}
                        for merchant in row["merchants"]
                    ],
                }
                for row in outlook["category_rows"]
                if row["merchants"]
            ],
        }
    return payload


def report_period_totals(home, start, end):
    spending = net_spending(home, start, end, Category.Type.SPENDING)
    bills = net_spending(home, start, end, Category.Type.BILL)
    spending_refunds = Refund.objects.filter(
        household=home, category__type=Category.Type.SPENDING, date__range=(start, end)
    ).aggregate(v=Sum("amount"))["v"] or Decimal("0")
    bill_refunds = Refund.objects.filter(
        household=home, category__type=Category.Type.BILL, date__range=(start, end)
    ).aggregate(v=Sum("amount"))["v"] or Decimal("0")
    income = Income.objects.filter(household=home, date__range=(start, end)).aggregate(
        v=Sum("amount")
    )["v"] or Decimal("0")
    transfers = Transfer.objects.filter(household=home, date__range=(start, end)).aggregate(
        v=Sum("amount")
    )["v"] or Decimal("0")
    return {
        "spending": spending,
        "refunds": spending_refunds + bill_refunds,
        "bills": bills,
        "income": income,
        "transfers": transfers,
    }


def report_data(home, start, end):
    category_data = category_report_rows(home, start, end)
    totals = report_period_totals(home, start, end)
    comparison_totals = report_period_totals(
        home, category_data["previous_start"], category_data["previous_end"]
    )
    account_rows = account_report_rows(
        home,
        start,
        end,
        category_data["previous_start"],
        category_data["previous_end"],
    )
    period_days = (end - start).days + 1
    historical_periods = []
    history_savings = savings_account(home)
    history_end = start - timedelta(days=1)
    for _ in range(6):
        history_start = history_end - timedelta(days=period_days - 1)
        history_totals = report_period_totals(home, history_start, history_end)
        history_categories = category_report_rows(home, history_start, history_end)["rows"]
        history_savings_change = Decimal("0.00")
        history_fulfilled_savings = Decimal("0.00")
        if history_savings:
            history_savings_change = account_balance(
                history_savings, history_end
            ) - account_balance(history_savings, history_start - timedelta(days=1))
            history_fulfilled_savings = -sum(
                (
                    entry.amount
                    for entry in SavingsGoalEntry.objects.filter(
                        goal__household=home,
                        date__range=(history_start, history_end),
                        fulfills_goal=True,
                        amount__lt=0,
                    )
                ),
                Decimal("0.00"),
            )
        historical_periods.append(
            {
                "start": str(history_start),
                "end": str(history_end),
                **{key: f"{value:.2f}" for key, value in history_totals.items()},
                "savings_change": f"{history_savings_change:.2f}",
                "goal_fulfillment_spending": f"{history_fulfilled_savings:.2f}",
                "adjusted_savings_change": (
                    f"{history_savings_change + history_fulfilled_savings:.2f}"
                ),
                "top_categories": [
                    {
                        "name": row["name"],
                        "type": row["type"],
                        "net_spending": f"{row['amount']:.2f}",
                    }
                    for row in history_categories[:6]
                    if row["amount"] != 0
                ],
            }
        )
        history_end = history_start - timedelta(days=1)
    return {
        "start": start,
        "end": end,
        **totals,
        "comparison_totals": comparison_totals,
        "category_rows": category_data["rows"],
        "category_chart_total": category_data["chart_total"],
        "category_chart_gradient": category_data["chart_gradient"],
        "previous_start": category_data["previous_start"],
        "previous_end": category_data["previous_end"],
        "account_rows": account_rows,
        "historical_periods": historical_periods,
        "account_inflows": sum((row["inflows"] for row in account_rows), Decimal("0.00")),
        "account_outflows": sum((row["outflows"] for row in account_rows), Decimal("0.00")),
        "generated": timezone.localtime(),
    }


@login_required
@require_POST
def reports_analysis_start(request):
    """Start Deep pattern analysis. POST only, so a link, refresh or prefetch cannot start one."""
    home = household(request)
    default_start, default_end = default_outlook_range(home, timezone.localdate())
    start = date_from(request.POST.get("start"), default_start)
    end = date_from(request.POST.get("end"), default_end)
    if end < start:
        start, end = end, start
    run = analysis_jobs.start_run(home, request.user, start, end)
    if (run.start, run.end) != (start, end):
        messages.info(
            request,
            "An analysis for another date range is still running. Showing that one; "
            "start a new one when it finishes.",
        )
    return redirect(
        f"{reverse('reports')}?start={run.start:%Y-%m-%d}&end={run.end:%Y-%m-%d}"
        f"&analysis={run.pk}#deep-analysis-title"
    )


@login_required
@require_GET
def reports_analysis_status(request, pk):
    """Status only (no analysis text or figures), polled while a run is going."""
    run = get_object_or_404(ReportAnalysisRun, pk=pk, household=household(request))
    state = analysis_jobs.run_state(run)
    response = JsonResponse({"status": state, "running": state == ReportAnalysisRun.Status.RUNNING})
    response["Cache-Control"] = "no-store"
    return response


@login_required
def reports(request):
    home = household(request)
    ensure_savings_goals(home)
    today = timezone.localdate()
    default_start, default_end = default_outlook_range(home, today)
    start = date_from(request.GET.get("start") or request.POST.get("start"), default_start)
    end = date_from(request.GET.get("end") or request.POST.get("end"), default_end)
    if end < start:
        start, end = end, start
    data = outlook_data(home, start, end, today)
    as_of = data["as_of"]
    legacy_report = report_data(home, start, end)
    analysis = None
    analysis_error = ""
    analysis_running = False
    run_pk = None
    run_id = request.GET.get("analysis", "")
    if run_id.isdigit():
        run = get_object_or_404(ReportAnalysisRun, pk=int(run_id), household=home)
        state = analysis_jobs.run_state(run)
        if state == ReportAnalysisRun.Status.DONE:
            analysis = run.result
        elif state == ReportAnalysisRun.Status.FAILED:
            analysis_error = run.error or "The analysis took too long. Try again later."
        else:
            analysis_running = True
            run_pk = run.pk
    settings_form = OutlookSettingsForm(
        request.POST or None,
        household=home,
        rates=data["cut_rates"],
    )
    if request.method == "POST" and settings_form.is_valid():
        with transaction.atomic():
            selected = set(
                settings_form.cleaned_data["nonessential_categories"].values_list("pk", flat=True)
            )
            categories = Category.objects.filter(
                household=home, type=Category.Type.SPENDING, archived_at=None
            )
            categories.update(is_essential=True)
            categories.filter(pk__in=selected).update(is_essential=False)
            ApplicationSetting.objects.update_or_create(
                household=home,
                key="outlook_cut_rates",
                defaults={
                    "value": {
                        "limited": str(settings_form.cleaned_data["limited_rate"]),
                        "moderate": str(settings_form.cleaned_data["moderate_rate"]),
                        "significant": str(settings_form.cleaned_data["significant_rate"]),
                    }
                },
            )
        messages.success(request, "Outlook assumptions updated.")
        return redirect(f"{reverse('reports')}?start={start}&end={end}")
    if request.GET.get("format") == "csv":
        response = HttpResponse(
            content_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="budget-outlook-{start}-{end}.csv"'
            },
        )
        writer = csv.writer(response)
        writer.writerow(["Budget outlook", start, end, "As of", as_of])
        writer.writerow(["Section", "Amount"])
        for label, key in (
            ("Actual spending through as-of", "actual_spending"),
            ("Actual bills through as-of", "actual_bills"),
            ("Actual income through as-of", "actual_income"),
            ("Projected income", "projected_income"),
            ("Projected total expenses", "projected_total"),
            ("Projected end balance", "projected_end_balance"),
            ("Projected balance change", "forecast_surplus"),
            ("Recurring bills scheduled in range", "recurring_total"),
            ("Subscriptions scheduled in range", "subscription_total"),
            ("Savings at start", "savings_start"),
            ("Savings at as-of", "savings_end"),
        ):
            writer.writerow([label, data[key]])
        writer.writerow([])
        writer.writerow(["Category", "Type", "Net amount", "Share"])
        for row in data["category_rows"]:
            writer.writerow(
                [csv_text(row["name"]), csv_text(row["type"]), row["amount"], row["share"]]
            )
        writer.writerow([])
        writer.writerow(["Date", "Net spending and bills"])
        for row in data["daily_rows"]:
            writer.writerow([row["date"], row["amount"]])
        return response
    return render(
        request,
        "budget/reports.html",
        {
            **common(request),
            **data,
            "settings_form": settings_form,
            "legacy_report": legacy_report,
            "analysis": analysis,
            "analysis_error": analysis_error,
            "analysis_running": analysis_running,
            "run_pk": run_pk,
        },
    )


def csv_text(value):
    """Stop spreadsheets reading a text cell as a formula (CSV injection).

    Only text cells go through this; amounts stay plain numbers, so a negative amount keeps
    its minus sign.
    """
    text = str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


def date_from(value, default):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date() if value else default
    except ValueError:
        return default


@login_required
def savings(request):
    home = household(request)
    ensure_savings_goals(home)
    drift = savings_drift(home)
    account = savings_account(home)
    goals = list(
        SavingsGoal.objects.filter(household=home, is_system=False).prefetch_related("entries")
    )
    active = [goal for goal in goals if goal.status == SavingsGoal.Status.ACTIVE]
    active.sort(key=lambda goal: (goal.priority or 9999, goal.created_at, goal.pk))
    completed = [goal for goal in goals if goal.status == SavingsGoal.Status.COMPLETED]
    completed.sort(key=lambda goal: (goal.completed_at or goal.created_at), reverse=True)
    archived = [goal for goal in goals if goal.status == SavingsGoal.Status.ARCHIVED]
    archived.sort(key=lambda goal: (goal.archived_at or goal.created_at), reverse=True)
    unallocated = SavingsGoal.objects.filter(household=home, is_system=True).first()
    total_allocated = sum((goal.current_amount for goal in goals), Decimal("0.00")) + (
        unallocated.current_amount if unallocated else Decimal("0.00")
    )
    return render(
        request,
        "budget/savings.html",
        {
            **common(request),
            "savings_account": account,
            "savings_balance": savings_ledger_balance(home),
            "total_allocated": total_allocated,
            "active_goals": active,
            "completed_goals": completed,
            "archived_goals": archived,
            "unallocated_goal": unallocated,
            "savings_drift": drift,
            "savings_drift_abs": abs(drift),
        },
    )


@login_required
@require_POST
def savings_reconcile(request):
    home = household(request)
    try:
        difference = reconcile_savings(home, request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        if difference:
            messages.success(request, f"${abs(difference):,.2f} assigned to Unallocated Savings.")
        else:
            messages.info(request, "Savings goals already match the Savings balance.")
    return redirect("savings")


@login_required
def savings_goal_add(request):
    home = household(request)
    ensure_savings_goals(home)
    count = SavingsGoal.objects.filter(
        household=home, status=SavingsGoal.Status.ACTIVE, is_system=False
    ).count()
    form = SavingsGoalForm(
        request.POST or None,
        household=home,
        initial={"priority": count + 1},
    )
    if request.method == "POST" and form.is_valid():
        try:
            create_savings_goal(
                household=home,
                actor=request.user,
                name=form.cleaned_data["name"],
                target_amount=form.cleaned_data["target_amount"],
                priority=form.cleaned_data["priority"],
                initial_amount=form.cleaned_data.get("initial_amount") or 0,
                source_goal=form.cleaned_data.get("source_goal"),
            )
            messages.success(request, "Savings goal added.")
            return redirect("savings")
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "budget/form.html",
        {
            **common(request),
            "form": form,
            "title": "Add savings goal",
            "return_url": reverse("savings"),
        },
    )


@login_required
def savings_goal_edit(request, pk):
    home = household(request)
    goal = get_object_or_404(SavingsGoal, pk=pk, household=home, is_system=False)
    form = SavingsGoalForm(
        request.POST or None,
        instance=goal,
        household=home,
        editing=True,
    )
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                goal = SavingsGoal.objects.select_for_update().get(pk=goal.pk)
                old = {"name": goal.name, "target_amount": str(goal.target_amount)}
                goal.name = form.cleaned_data["name"].strip()
                goal.target_amount = form.cleaned_data["target_amount"]
                requested_priority = form.cleaned_data.get("priority")
                if (
                    goal.status == SavingsGoal.Status.COMPLETED
                    and goal.current_amount < goal.target_amount
                ):
                    goal.status = SavingsGoal.Status.ACTIVE
                    goal.completed_at = None
                    goal.priority = requested_priority or 9999
                elif goal.status == SavingsGoal.Status.ACTIVE:
                    goal.priority = requested_priority or 9999
                goal.save()
                normalize_goal_priorities(home)
                audit(
                    home,
                    request.user,
                    goal,
                    "update",
                    {**old, "new_target_amount": str(goal.target_amount)},
                )
            messages.success(request, "Savings goal updated.")
            return redirect("savings")
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "budget/form.html",
        {
            **common(request),
            "form": form,
            "title": f"Edit {goal.name}",
            "return_url": reverse("savings"),
        },
    )


@login_required
def savings_move(request):
    home = household(request)
    ensure_savings_goals(home)
    form = add_form_token(SavingsMoveForm(request.POST or None, household=home))
    source_formset = SavingsSourceAllocationFormSet(
        request.POST or None,
        prefix="savings",
        form_kwargs={"household": home},
    )
    form_valid = form.is_valid() if request.method == "POST" else False
    source_valid = source_formset.is_valid() if form_valid else False
    if request.method == "POST" and form_valid and source_valid:
        try:
            values = savings_source_values(source_formset)
            with transaction.atomic():
                if not claim_form_token(home, form.cleaned_data["form_token"]):
                    messages.info(request, DUPLICATE_SUBMISSION_MESSAGE)
                    return redirect("savings")
                if form.cleaned_data.get("destination_goal"):
                    reallocate_savings(
                        household=home,
                        actor=request.user,
                        entry_date=form.cleaned_data["date"],
                        allocations=values,
                        destination_goal=form.cleaned_data["destination_goal"],
                        amount=form.cleaned_data["amount"],
                        note=form.cleaned_data["notes"],
                    )
                    messages.success(
                        request, "Savings allocations updated; the account balance did not change."
                    )
                else:
                    create_transfer(
                        household=home,
                        actor=request.user,
                        entry_date=form.cleaned_data["date"],
                        source=savings_account(home),
                        destination=form.cleaned_data["destination_account"],
                        amount=form.cleaned_data["amount"],
                        notes=form.cleaned_data["notes"],
                        savings_source_allocations=values,
                    )
                    messages.success(
                        request, "Money moved from Savings and goal totals were updated."
                    )
            return redirect("savings")
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "budget/form.html",
        {
            **common(request),
            "form": form,
            "title": "Move savings",
            "return_url": reverse("savings"),
            "savings_formset": source_formset,
        },
    )


@login_required
def savings_goal_action(request, pk, action):
    if request.method != "POST":
        return redirect("savings")
    home = household(request)
    goal = get_object_or_404(SavingsGoal, pk=pk, household=home, is_system=False)
    try:
        if action == "complete":
            complete_savings_goal(goal, request.user)
            messages.success(request, f"{goal.name} marked complete.")
        elif action == "archive":
            archive_savings_goal(goal, request.user)
            messages.success(
                request,
                f"{goal.name} archived for long-term Savings; its allocation remains intact.",
            )
        elif action == "restore":
            restore_savings_goal(goal, request.user)
            messages.success(request, f"{goal.name} restored for access to its funds.")
        elif action == "delete":
            if not request.POST.get("confirmed"):
                return render(
                    request,
                    "budget/confirm_delete.html",
                    {
                        **common(request),
                        "form": forms.Form(),
                        "confirmed_flag": True,
                        "title": f"Delete {goal.name}",
                        "warning": (
                            f"Delete the savings goal “{goal.name}”? "
                            "It has no entries. This cannot be undone."
                        ),
                        "submit_label": "Delete goal",
                        "return_url": reverse("savings"),
                    },
                )
            name = goal.name
            delete_savings_goal(goal, request.user)
            messages.success(request, f"{name} deleted.")
        elif action in {"up", "down"} and goal.status == SavingsGoal.Status.ACTIVE:
            goals = list(
                SavingsGoal.objects.filter(
                    household=home, status=SavingsGoal.Status.ACTIVE, is_system=False
                ).order_by("priority", "created_at")
            )
            index = goals.index(goal)
            other_index = index - 1 if action == "up" else index + 1
            if 0 <= other_index < len(goals):
                goals[index], goals[other_index] = goals[other_index], goals[index]
                with transaction.atomic():
                    for position, item in enumerate(goals, 1):
                        SavingsGoal.objects.filter(pk=item.pk).update(priority=position)
                messages.success(request, "Goal priorities updated.")
        else:
            raise ValidationError("That savings goal action is not available.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("savings")


@login_required
def appearance(request):
    if (
        request.method == "POST"
        and request.POST.get("appearance") in UserPreference.Appearance.values
    ):
        UserPreference.objects.update_or_create(
            user=request.user, defaults={"appearance": request.POST["appearance"]}
        )
        messages.success(request, "Appearance updated.")
    next_url = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        next_url = "dashboard"
    return redirect(next_url)


def staff_user(user):
    return user.is_authenticated and user.is_staff


@user_passes_test(staff_user)
def application_user_password(request, pk):
    home = household(request)
    _, scope_users = admin_scope(request.user)
    account_user = get_object_or_404(
        scope_users.filter(household_memberships__household=home), pk=pk
    )
    form = BudgetPasswordResetForm(account_user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            form.save()
            audit(home, request.user, account_user, "update", {"password_reset": True})
        if account_user == request.user:
            update_session_auth_hash(request, account_user)
        messages.success(request, f"Password reset for {account_user.username}.")
        return redirect("application_admin")
    return render(
        request,
        "budget/form.html",
        {
            **common(request),
            "form": form,
            "title": f"Reset password for {account_user.username}",
            "return_url": reverse("application_admin"),
        },
    )


@user_passes_test(staff_user)
def application_user_delete(request, pk):
    home = household(request)
    _, scope_users = admin_scope(request.user)
    account_user = get_object_or_404(
        scope_users.filter(household_memberships__household=home), pk=pk
    )
    if account_user == request.user:
        messages.error(request, "You cannot delete your own account while signed in.")
        return redirect("application_admin")
    form = ConfirmDeletionForm(
        request.POST or None,
        expected_value=account_user.username,
    )
    if request.method == "POST" and form.is_valid():
        username = account_user.username
        with transaction.atomic():
            other_memberships = account_user.household_memberships.exclude(household=home)
            if other_memberships.exists():
                HouseholdMembership.objects.filter(household=home, user=account_user).delete()
                audit(
                    home,
                    request.user,
                    account_user,
                    "delete",
                    {"profile_access_removed": True},
                )
                message = f"{username} no longer has access to {home.name}."
            else:
                audit(
                    home,
                    request.user,
                    account_user,
                    "delete",
                    {"user_deleted": True},
                )
                account_user.delete()
                message = f"User {username} deleted."
        messages.success(request, message)
        return redirect("application_admin")
    return render(
        request,
        "budget/confirm_delete.html",
        {
            **common(request),
            "form": form,
            "title": f"Delete user {account_user.username}?",
            "warning": (
                "This removes their access. If this is their only profile, the login is deleted."
            ),
            "return_url": reverse("application_admin"),
        },
    )


def profile_has_financial_history(profile):
    return any(
        queryset.filter(household=profile).exists()
        for queryset in (
            Income.objects,
            Expense.objects,
            Refund.objects,
            Transfer.objects,
            BalanceAdjustment.objects,
            RecurringBill.objects,
            PayPeriod.objects,
            Debt.objects,
        )
    )


@user_passes_test(staff_user)
def application_profile_delete(request, pk):
    active_home = household(request)
    scope_profiles, _ = admin_scope(request.user)
    profile = get_object_or_404(scope_profiles, pk=pk)
    if profile == active_home:
        messages.error(request, "The active profile cannot be deleted. Switch profiles first.")
        return redirect("application_admin")
    form = ConfirmDeletionForm(request.POST or None, expected_value=profile.name)
    if request.method == "POST" and form.is_valid():
        if profile_has_financial_history(profile):
            form.add_error(
                None,
                (
                    "This profile contains bills, pay periods, or transaction history "
                    "and cannot be deleted."
                ),
            )
        else:
            profile_name = profile.name
            profile_id = str(profile.pk)
            with transaction.atomic():
                profile.delete()
                AuditEvent.objects.create(
                    household=active_home,
                    actor=request.user,
                    record_type="budget.Household",
                    record_id=profile_id,
                    action=AuditEvent.Action.DELETE,
                    changes={"profile_deleted": profile_name},
                )
            messages.success(request, f"Profile {profile_name} deleted.")
            return redirect("application_admin")
    return render(
        request,
        "budget/confirm_delete.html",
        {
            **common(request),
            "form": form,
            "title": f"Delete profile {profile.name}?",
            "warning": (
                "Only profiles without bills, pay periods, or transaction history can be deleted."
            ),
            "return_url": reverse("application_admin"),
        },
    )


@user_passes_test(staff_user)
def application_profile_access(request, pk):
    active_home = household(request)
    scope_profiles, scope_users = admin_scope(request.user)
    profile = get_object_or_404(scope_profiles, pk=pk)
    form = ScopedProfileAccessForm(
        request.POST or None,
        profile=profile,
        active_profile=active_home,
        actor=request.user,
        scope_users=scope_users,
    )
    if request.method == "POST" and form.is_valid():
        selected_users = list(form.cleaned_data["users"])
        selected_ids = {user.pk for user in selected_users}
        existing_memberships = list(
            HouseholdMembership.objects.select_related("user").filter(household=profile)
        )
        existing_ids = {membership.user_id for membership in existing_memberships}
        added_users = [user for user in selected_users if user.pk not in existing_ids]
        removed_memberships = [
            membership
            for membership in existing_memberships
            if membership.user_id not in selected_ids
        ]
        with transaction.atomic():
            HouseholdMembership.objects.bulk_create(
                [HouseholdMembership(household=profile, user=user) for user in added_users]
            )
            if removed_memberships:
                HouseholdMembership.objects.filter(
                    pk__in=[membership.pk for membership in removed_memberships]
                ).delete()
            audit(
                profile,
                request.user,
                profile,
                "update",
                {
                    "access_granted": [user.username for user in added_users],
                    "access_revoked": [
                        membership.user.username for membership in removed_memberships
                    ],
                },
            )
        messages.success(request, f"Access to {profile.name} updated.")
        return redirect("application_admin")
    return render(
        request,
        "budget/profile_access.html",
        {
            **common(request),
            "form": form,
            "profile": profile,
            "return_url": reverse("application_admin"),
        },
    )


@user_passes_test(staff_user)
def application_admin(request):
    home = household(request)
    current_alerts = alert_settings(home)
    labels_form = PageNameForm(prefix="page")
    alerts_form = AlertSettingsForm(initial=current_alerts, prefix="alerts")
    action_form = QuickActionForm(prefix="action")
    user_form = BudgetUserCreationForm(prefix="user")
    if not request.user.is_superuser:
        del user_form.fields["is_staff"]
    scope_profiles, scope_users = admin_scope(request.user)
    profile_form = BudgetProfileForm(prefix="profile")

    if request.method == "POST":
        intent = request.POST.get("intent")
        if intent == "backup_now":
            if backup_requests.request_backup(request.user.username):
                AuditEvent.objects.create(
                    household=home,
                    actor=request.user,
                    record_type="backup.Request",
                    record_id="",
                    action=AuditEvent.Action.CREATE,
                    changes={},
                )
                messages.success(request, "Backup requested. It starts within a minute.")
            elif backup_requests.pending():
                messages.info(request, "A backup request is already waiting.")
            else:
                messages.error(request, "Back up now is not set up on this server yet.")
            return redirect("application_admin")
        if intent == "save_page_name":
            labels_form = PageNameForm(request.POST, prefix="page")
            if labels_form.is_valid():
                page = labels_form.cleaned_data["page"]
                name = labels_form.cleaned_data["name"]
                default = PAGE_LABEL_DEFAULTS[page]
                with transaction.atomic():
                    saved = setting_value(home, "page_labels", {})
                    saved = dict(saved) if isinstance(saved, dict) else {}
                    old = saved.get(page, default)
                    if name and name != default:
                        saved[page] = name
                    else:
                        saved.pop(page, None)
                    setting, _ = ApplicationSetting.objects.update_or_create(
                        household=home, key="page_labels", defaults={"value": saved}
                    )
                    audit(home, request.user, setting, "update", {page: [old, name or default]})
                messages.success(request, f"{default} page is now called {name or default}.")
                return redirect(f"{reverse('application_admin')}?page={page}")
        elif intent == "save_alerts":
            alerts_form = AlertSettingsForm(request.POST, prefix="alerts")
            if alerts_form.is_valid():
                with transaction.atomic():
                    setting, _ = ApplicationSetting.objects.update_or_create(
                        household=home,
                        key="alert_settings",
                        defaults={"value": alerts_form.cleaned_setting_value()},
                    )
                    audit(
                        home,
                        request.user,
                        setting,
                        "update",
                        {
                            "fields_updated": [
                                "spending_low_balance_enabled",
                                "spending_low_balance_threshold",
                                "bills_shortage_enabled",
                                "bill_confirmation_enabled",
                            ]
                        },
                    )
                messages.success(request, "Profile alerts updated.")
                return redirect("application_admin")
        elif intent == "add_action":
            action_form = QuickActionForm(request.POST, prefix="action")
            if action_form.is_valid():
                actions = setting_value(home, "quick_actions", [])
                actions = list(actions) if isinstance(actions, list) else []
                actions.append(action_form.cleaned_data)
                setting, _ = ApplicationSetting.objects.update_or_create(
                    household=home, key="quick_actions", defaults={"value": actions}
                )
                audit(
                    home,
                    request.user,
                    setting,
                    "update",
                    {"quick_action_added": action_form.cleaned_data["label"]},
                )
                messages.success(request, "Quick action added to the dashboard.")
                return redirect("application_admin")
        elif intent == "delete_action":
            actions = setting_value(home, "quick_actions", [])
            try:
                index = int(request.POST.get("index", ""))
                removed = actions[index]
            except (TypeError, ValueError, IndexError):
                messages.error(request, "That quick action no longer exists.")
            else:
                actions = list(actions)
                actions.pop(index)
                setting, _ = ApplicationSetting.objects.update_or_create(
                    household=home, key="quick_actions", defaults={"value": actions}
                )
                audit(
                    home,
                    request.user,
                    setting,
                    "update",
                    {"quick_action_removed": removed.get("label", "")},
                )
                messages.success(request, "Quick action removed.")
                return redirect("application_admin")
        elif intent == "add_user":
            user_form = BudgetUserCreationForm(request.POST, prefix="user")
            if not request.user.is_superuser:
                del user_form.fields["is_staff"]
            if not request.user.is_superuser and request.POST.get("user-is_staff"):
                user_form.add_error(None, "You cannot grant administrator access.")
            elif user_form.is_valid():
                new_user = user_form.save(commit=False)
                new_user.email = user_form.cleaned_data["email"]
                new_user.is_staff = bool(user_form.cleaned_data.get("is_staff"))
                with transaction.atomic():
                    new_user.save()
                    HouseholdMembership.objects.create(household=home, user=new_user)
                    audit(
                        home,
                        request.user,
                        new_user,
                        "create",
                        {"username": new_user.username, "is_staff": new_user.is_staff},
                    )
                messages.success(request, f"User {new_user.username} created.")
                return redirect("application_admin")
        elif intent == "add_profile":
            profile_form = BudgetProfileForm(request.POST, prefix="profile")
            if profile_form.is_valid():
                with transaction.atomic():
                    profile = profile_form.save()
                    HouseholdMembership.objects.create(household=profile, user=request.user)
                    initialize_profile(profile)
                    audit(
                        profile,
                        request.user,
                        profile,
                        "create",
                        {"name": profile.name},
                    )
                messages.success(
                    request,
                    f"Profile {profile.name} created. Sign out to select it at login.",
                )
                return redirect("application_admin")

    log_filter = request.GET.get("log", "all")
    if log_filter not in activity_log.FILTERS:
        log_filter = "all"
    profile_rows = list(scope_profiles)
    for row in profile_rows:
        members = (
            row.memberships.all()
            if request.user.is_superuser
            else row.memberships.filter(user__in=scope_users)
        )
        row.member_count = members.count()

    return render(
        request,
        "budget/application_admin.html",
        {
            **common(request),
            "backup_status": request_backup_status(request),
            "labels_form": labels_form,
            "page_name_rows": page_name_rows(home, request.GET.get("page")),
            "backup_pending": backup_requests.pending(),
            "backup_requests_available": backup_requests.available(),
            "alerts_form": alerts_form,
            "action_form": action_form,
            "user_form": user_form,
            "profile_form": profile_form,
            "quick_actions": custom_quick_actions(home),
            "users": scope_users.filter(household_memberships__household=home),
            "profiles": profile_rows,
            "activity": activity_log.activity_rows(home, log_filter),
            "activity_filters": activity_log.FILTERS,
            "activity_retention": activity_log.retention_text(),
            "activity_filter": log_filter,
        },
    )


@login_required
def first_profile(request):
    """One-time setup: the first administrator creates the first budget profile."""
    if not request.user.is_superuser or Household.objects.exists():
        return redirect("dashboard")
    form = BudgetProfileForm(
        request.POST or None, prefix="profile", initial={"name": "My Household"}
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            profile = form.save()
            HouseholdMembership.objects.create(household=profile, user=request.user)
            initialize_profile(profile)
            audit(profile, request.user, profile, "create", {"name": profile.name})
        request.session["active_household_id"] = profile.pk
        messages.success(request, f"Profile {profile.name} created. Welcome.")
        return redirect("dashboard")
    return render(request, "budget/first_profile.html", {"form": form, "first_run": True})


def initialize_profile(profile):
    for order, (name, account_type) in enumerate(
        (
            ("Spending", Account.Type.SPENDING),
            ("Bills", Account.Type.BILLS),
            ("Savings", Account.Type.SAVINGS),
        )
    ):
        Account.objects.get_or_create(
            household=profile,
            name=name,
            defaults={"type": account_type, "display_order": order},
        )
    for name, category_type in (
        ("General", Category.Type.SPENDING),
        ("ATM Withdrawal", Category.Type.SPENDING),
        ("Other Bills", Category.Type.BILL),
        ("Income", Category.Type.INCOME),
    ):
        Category.objects.get_or_create(
            household=profile,
            name=name,
            type=category_type,
            defaults={"display_order": 0},
        )
    PaySchedule.objects.get_or_create(household=profile, defaults={"nominal_days": [1, 15]})


class ProfileLoginView(auth_views.LoginView):
    template_name = "registration/login.html"
    authentication_form = ProfileAuthenticationForm

    def form_valid(self, form):
        profile = form.cleaned_data.get("profile")
        login_throttle.clear_failures(self.request, form.cleaned_data["username"])
        if profile is not None:
            self.request.session["active_household_id"] = profile.pk
        record_sign_in(self.request, form.get_user(), profile)
        return super().form_valid(form)

    def form_invalid(self, form):
        if self.request.method == "POST":
            record_failed_sign_in(self.request, form)
        return super().form_invalid(form)


def admin_login_redirect(request):
    """Send Django admin sign-ins through the app's throttled, logged sign-in page."""
    target = request.GET.get("next", "")
    if not url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        target = reverse("admin:index")
    if request.user.is_authenticated:
        if request.user.is_staff:
            return redirect(target)
        raise PermissionDenied
    return redirect(f"{reverse('login')}?{urlencode({'next': target})}")


def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError as exc:
        logging.getLogger("budget.health").error(
            "Health check database failure: %s", type(exc).__name__
        )
        response = HttpResponse("database unavailable", content_type="text/plain", status=503)
    else:
        response = HttpResponse("ok", content_type="text/plain")
    response["Cache-Control"] = "no-store"
    return response
