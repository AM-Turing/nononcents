"""Add, edit, archive and remove accounts and categories."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import models, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .manage_forms import AccountForm, CategoryForm
from .models import Account, Category, HabitView, Reconciliation
from .reconciliation import uncleared_summary
from .services import account_balance, audit
from .views import common, household

REQUIRED_TYPES: dict[str, str] = {Account.Type.SPENDING: "Spending", Account.Type.BILLS: "Bills"}
CATEGORY_KINDS = (Category.Type.SPENDING, Category.Type.BILL)


def _reverse_rows(obj) -> list[models.QuerySet]:
    """Every table row that points at this object (not many-to-many)."""
    found = []
    for rel in obj._meta.related_objects:
        if rel.many_to_many:
            continue
        found.append(rel.related_model._default_manager.filter(**{rel.field.name: obj}))
    return found


def account_in_use(account: Account) -> bool:
    return any(qs.exists() for qs in _reverse_rows(account))


def category_in_use(category: Category) -> bool:
    return any(qs.exists() for qs in _reverse_rows(category))


def category_entry_count(category: Category) -> int:
    return (
        category.expenses.count()
        + category.expense_splits.count()
        + category.refunds.count()
        + category.recurringbill_set.count()
    )


def _is_last_required(home, account: Account) -> bool:
    """True when this is the only active account of a type the app needs."""
    if account.type not in REQUIRED_TYPES or account.archived_at is not None:
        return False
    return not (
        Account.objects.filter(household=home, type=account.type, archived_at=None)
        .exclude(pk=account.pk)
        .exists()
    )


# ---- Accounts -------------------------------------------------------------------------


@login_required
def accounts(request):
    home = household(request)
    rows = []
    archived = []
    for account in Account.objects.filter(household=home):
        if account.archived_at is not None:
            archived.append(account)
            continue
        count, total = uncleared_summary(account)
        rows.append(
            {
                "account": account,
                "balance": account_balance(account),
                "last": Reconciliation.objects.filter(account=account)
                .select_related("adjustment")
                .first(),
                "uncleared_count": count,
                "uncleared_total": total,
            }
        )
    return render(
        request,
        "budget/accounts.html",
        {**common(request), "rows": rows, "archived": archived},
    )


def _render_account_form(request, form, account, locked):
    return render(
        request,
        "budget/account_form.html",
        {
            **common(request),
            "form": form,
            "account": account,
            "locked": locked,
            "title": f"Edit {account.name}" if account else "Add account",
        },
    )


def _account_form_view(request, account: Account | None):
    home = household(request)
    locked = account is not None and account_in_use(account)
    form = AccountForm(request.POST or None, instance=account, household=home, locked=locked)
    if request.method != "POST" or not form.is_valid():
        return _render_account_form(request, form, account, locked)
    with transaction.atomic():
        list(Account.objects.select_for_update().filter(household=home))
        created = account is None
        before: dict[str, str] = {}
        if account is not None:
            # Re-check under the lock: activity may have appeared meanwhile.
            account = Account.objects.get(pk=account.pk, household=home)
            locked = account_in_use(account)
            before = {f: str(v) for f, v in Account.objects.values().get(pk=account.pk).items()}
            form = AccountForm(request.POST, instance=account, household=home, locked=locked)
            if not form.is_valid():
                return _render_account_form(request, form, account, locked)
            if "type" in form.changed_data and _is_last_required(
                home, Account.objects.get(pk=account.pk)
            ):
                form.add_error(
                    "type", f"Keep at least one active {REQUIRED_TYPES[before['type']]} account."
                )
                return _render_account_form(request, form, account, locked)
        saved = form.save(commit=False)
        saved.household = home
        saved.save()
        if created:
            changes: dict = {
                f: str(getattr(saved, f)) for f in ("name", "type", "opening_balance")
            } | {"display_order": saved.display_order}
        else:
            changes = {f: [before[f], str(getattr(saved, f))] for f in form.changed_data}
        # The log says the ending changed, not the digits (as statement imports do).
        if "statement_suffix" in changes:
            changes["statement_suffix"] = "changed" if saved.statement_suffix else "cleared"
        audit(home, request.user, saved, "create" if created else "update", changes)
    messages.success(request, f"Saved {saved.name}.")
    return redirect("accounts")


@login_required
def account_add(request):
    return _account_form_view(request, None)


@login_required
def account_edit(request, pk):
    account = get_object_or_404(Account, pk=pk, household=household(request), archived_at=None)
    return _account_form_view(request, account)


@login_required
@require_POST
def account_remove(request, pk):
    home = household(request)
    with transaction.atomic():
        list(Account.objects.select_for_update().filter(household=home))
        account = get_object_or_404(Account, pk=pk, household=home, archived_at=None)
        if _is_last_required(home, account):
            messages.error(
                request,
                f"Can't remove {account.name}: the app needs at least one active "
                f"{REQUIRED_TYPES[account.type]} account.",
            )
            return redirect("accounts")
        name = account.name
        if account_in_use(account):
            account.archived_at = timezone.now()
            account.save(update_fields=["archived_at"])
            audit(home, request.user, account, "update", {"archived": [False, True]})
            messages.success(request, f"{name} has history, so it was archived. History is kept.")
        else:
            audit(home, request.user, account, "delete", {"name": name, "type": account.type})
            account.delete()
            messages.success(request, f"Removed {name}.")
    return redirect("accounts")


@login_required
@require_POST
def account_restore(request, pk):
    home = household(request)
    with transaction.atomic():
        account = get_object_or_404(
            Account.objects.select_for_update(), pk=pk, household=home, archived_at__isnull=False
        )
        account.archived_at = None
        account.save(update_fields=["archived_at"])
        audit(home, request.user, account, "update", {"archived": [True, False]})
    messages.success(request, f"Restored {account.name}.")
    return redirect("accounts")


# ---- Categories -----------------------------------------------------------------------


@login_required
def categories(request):
    home = household(request)
    active = Category.objects.filter(household=home, archived_at=None)

    def rows(qs):
        return [{"category": c, "count": category_entry_count(c)} for c in qs]

    context = {
        **common(request),
        "spending": rows(active.filter(type=Category.Type.SPENDING)),
        "bills": rows(active.filter(type=Category.Type.BILL)),
        "other": rows(active.exclude(type__in=CATEGORY_KINDS)),
        "archived": Category.objects.filter(household=home, archived_at__isnull=False),
    }
    return render(request, "budget/categories.html", context)


def _category_form_view(request, category: Category | None):
    home = household(request)
    if category is not None and category.type not in CATEGORY_KINDS:
        messages.error(request, "This kind of category can't be edited here.")
        return redirect("categories")
    locked = category is not None and category_in_use(category)
    form = CategoryForm(request.POST or None, household=home, instance=category, locked_kind=locked)
    if category is None and request.method != "POST":
        kind = request.GET.get("kind")
        kind = kind if kind in CATEGORY_KINDS else Category.Type.SPENDING
        form.initial = {"kind": kind, "is_essential": kind == Category.Type.BILL}
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        with transaction.atomic():
            if category is None:
                saved = Category.objects.create(
                    household=home,
                    name=data["name"],
                    type=data["kind"],
                    is_essential=data["is_essential"],
                )
                audit(
                    home,
                    request.user,
                    saved,
                    "create",
                    {"name": saved.name, "type": saved.type, "is_essential": saved.is_essential},
                )
            else:
                saved = Category.objects.select_for_update().get(pk=category.pk, household=home)
                new_type = saved.type if category_in_use(saved) else data["kind"]
                changes = {}
                for field, new in (
                    ("name", data["name"]),
                    ("type", new_type),
                    ("is_essential", data["is_essential"]),
                ):
                    old = getattr(saved, field)
                    if old != new:
                        changes[field] = [str(old), str(new)]
                        setattr(saved, field, new)
                if changes:
                    saved.save()
                    audit(home, request.user, saved, "update", changes)
        messages.success(request, f"Saved {saved.name}.")
        return redirect("categories")
    return render(
        request,
        "budget/category_form.html",
        {
            **common(request),
            "form": form,
            "category": category,
            "locked": locked,
            "title": f"Edit {category.name}" if category else "Add category",
        },
    )


@login_required
def category_add(request):
    return _category_form_view(request, None)


@login_required
def category_edit(request, pk):
    category = get_object_or_404(Category, pk=pk, household=household(request), archived_at=None)
    return _category_form_view(request, category)


@login_required
@require_POST
def category_remove(request, pk):
    home = household(request)
    with transaction.atomic():
        category = get_object_or_404(
            Category.objects.select_for_update(), pk=pk, household=home, archived_at=None
        )
        name = category.name
        if category_in_use(category):
            category.archived_at = timezone.now()
            category.save(update_fields=["archived_at"])
            audit(home, request.user, category, "update", {"archived": [False, True]})
            messages.success(
                request,
                f"{name} has history, so it was archived. History is kept and "
                "it is no longer offered on new entries.",
            )
        else:
            for view in HabitView.objects.filter(household=home, categories=category):
                view.categories.remove(category)
            audit(home, request.user, category, "delete", {"name": name, "type": category.type})
            category.delete()
            messages.success(request, f"Removed {name}.")
    return redirect("categories")


@login_required
@require_POST
def category_restore(request, pk):
    home = household(request)
    with transaction.atomic():
        category = get_object_or_404(
            Category.objects.select_for_update(),
            pk=pk,
            household=home,
            archived_at__isnull=False,
        )
        category.archived_at = None
        category.save(update_fields=["archived_at"])
        audit(home, request.user, category, "update", {"archived": [True, False]})
    messages.success(request, f"Restored {category.name}.")
    return redirect("categories")
