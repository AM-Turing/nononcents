"""Habits page: small purchases, merchant frequency, trends, budgets and cleanup.

Calculations live in ``budget.habits``; this module only selects the view and renders.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import habits
from .habit_forms import AdjustForm, HabitViewForm
from .habits import ZERO
from .models import HabitView, PayPeriod
from .services import audit, money
from .views import common, household


@login_required
def habits_page(request):
    home = household(request)
    today = timezone.localdate()
    saved = list(HabitView.objects.filter(household=home))
    selected = None
    if request.GET.get("view"):
        try:
            pk = int(request.GET["view"])
        except ValueError:
            pk = 0
        selected = get_object_or_404(HabitView, pk=pk, household=home)
    elif saved:
        selected = saved[0]
    view = selected or habits.default_view(home)
    adjusted = False
    adjust = AdjustForm(request.GET) if "adjust" in request.GET else None
    if adjust is not None and adjust.is_valid():
        # An unsaved copy: never persisted, so it covers every spending category.
        data = adjust.cleaned_data
        view = HabitView(
            household=home,
            name=view.name,
            small_threshold=data["small_threshold"],
            basis=data["basis"],
            periods=data["periods"],
            min_repeat=view.min_repeat,
            include_essential=view.include_essential,
        )
        adjusted = True
    elif adjust is not None:
        messages.error(request, "Check the adjust settings and try again.")
        adjust = None
    if not adjusted:
        adjust = AdjustForm(
            initial={
                "small_threshold": view.small_threshold,
                "basis": view.basis,
                "periods": view.periods,
            }
        )
    report = habits.build_report(view, today)
    period = PayPeriod.objects.filter(
        household=home, start_date__lte=today, end_date__gte=today
    ).first()
    budget_lines = habits.category_budget_status(home, period) if period else []
    blank, groups = habits.cleanup_queue(home)
    return render(
        request,
        "budget/habits.html",
        {
            **common(request),
            "saved_views": saved,
            "selected": selected,
            "view": view,
            "adjusted": adjusted,
            "adjust": adjust,
            "report": report,
            "top_small": report.small_merchants[:15],
            "more_small": report.small_merchants[15:],
            "top_merchants": report.merchants[:15],
            "more_merchants": report.merchants[15:],
            "overall_average": money(sum(report.column_totals, ZERO) / len(report.column_totals))
            if report.column_totals
            else ZERO,
            "period": period,
            "budget_lines": budget_lines,
            "blank": blank,
            "groups": groups,
        },
    )


def _form_page(request, form, title, view=None):
    return render(
        request,
        "budget/habit_view_form.html",
        {**common(request), "form": form, "title": title, "habit_view": view},
    )


@login_required
def habit_view_add(request):
    home = household(request)
    form = HabitViewForm(request.POST or None, household=home, user=request.user)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            view = form.save()
            audit(home, request.user, view, "create", {"name": view.name})
        messages.success(request, "View saved.")
        return redirect(f"{reverse('habits')}?view={view.pk}")
    return _form_page(request, form, "Save as new view")


@login_required
def habit_view_edit(request, pk):
    home = household(request)
    view = get_object_or_404(HabitView, pk=pk, household=home)
    form = HabitViewForm(request.POST or None, instance=view, household=home, user=request.user)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            view = form.save()
            audit(home, request.user, view, "update", {"name": view.name})
        messages.success(request, "View updated.")
        return redirect(f"{reverse('habits')}?view={view.pk}")
    return _form_page(request, form, "Edit view", view)


@login_required
@require_POST
def habit_view_delete(request, pk):
    home = household(request)
    with transaction.atomic():
        view = get_object_or_404(HabitView, pk=pk, household=home)
        name = view.name
        audit(home, request.user, view, "delete", {"name": name})
        view.delete()
    messages.success(request, f"Deleted view “{name}”.")
    return redirect("habits")


@login_required
@require_POST
def habit_unify(request):
    home = household(request)
    key = request.POST.get("key", "")
    try:
        changed, skipped = habits.unify_merchant(
            home, request.user, key, request.POST.get("name", "")
        )
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    else:
        messages.success(
            request, f"Renamed {changed} entries ({skipped} in locked periods left as they were)."
        )
    return redirect(reverse("habits") + "#cleanup")
