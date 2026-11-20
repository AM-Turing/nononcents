"""Spending-habit views: small purchases, merchant frequency, category trends, cleanup.

Spending follows the one definition in ``services.net_spending``: expenses in Spending
categories on any account (a split expense counts each split in its own category), minus
refunds in Spending categories. Bills, transfers and balance corrections never count.

* A **small purchase** is an expense whose whole purchase total is at or below the view's
  threshold (a split purchase is judged by its total, then counted in each category).
  Refunds reduce totals but are never counted as purchases.
* Merchants are grouped by their name ignoring case and repeated spaces; the most-used
  spelling is shown.
* A **habit** is a merchant with at least ``min_repeat`` small purchases in the range;
  its monthly cost is the range total ÷ (days in range up to today ÷ 30.44).
* "Essential" uses the expense's own override when set, else the category's setting.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from .models import (
    Category,
    CategoryBudget,
    Expense,
    ExpenseSplit,
    HabitView,
    PayPeriod,
    Refund,
)
from .services import audit, ensure_open, money

DAYS_PER_MONTH = Decimal("30.44")
ZERO = Decimal("0.00")


def merchant_key(name: str) -> str:
    return " ".join((name or "").split()).casefold()


@dataclass(frozen=True)
class Column:
    label: str
    start: date
    end: date


@dataclass
class Item:
    date: date
    merchant: str
    category: Category
    amount: Decimal  # negative for refunds
    purchase_total: Decimal
    essential: bool
    purchase_id: int | None = None  # the expense; None for refunds
    is_refund: bool = False


@dataclass
class MerchantRow:
    name: str
    total: Decimal = ZERO
    last: date | None = None
    spellings: Counter = field(default_factory=Counter)
    purchases: set = field(default_factory=set)

    @property
    def count(self) -> int:
        return len(self.purchases)

    @property
    def average(self) -> Decimal:
        return money(self.total / self.count) if self.count else ZERO


@dataclass
class Habit:
    merchant: MerchantRow
    per_month: Decimal
    per_year: Decimal


@dataclass
class TrendRow:
    category: Category
    amounts: list[Decimal]

    @property
    def total(self) -> Decimal:
        return sum(self.amounts, ZERO)

    @property
    def average(self) -> Decimal:
        return money(self.total / len(self.amounts)) if self.amounts else ZERO


@dataclass
class HabitReport:
    view: HabitView
    columns: list[Column]
    start: date
    end: date
    total: Decimal
    small_count: int
    small_total: Decimal
    small_merchants: list[MerchantRow]
    merchants: list[MerchantRow]
    habits: list[Habit]
    trend: list[TrendRow]
    column_totals: list[Decimal]

    @property
    def small_share(self) -> Decimal:
        if self.total <= 0:
            return ZERO
        return (self.small_total * 100 / self.total).quantize(Decimal("1"), ROUND_HALF_UP)


def default_view(household) -> HabitView:
    """An unsaved view with the defaults, used until the owner saves one."""
    return HabitView(household=household, name="Last 6 pay periods")


def columns_for(view: HabitView, today: date) -> list[Column]:
    if view.basis == HabitView.Basis.MONTH:
        first = today.replace(day=1)
        cols = []
        for back in range(view.periods - 1, -1, -1):
            index = first.year * 12 + first.month - 1 - back
            start = date(index // 12, index % 12 + 1, 1)
            following = date((index + 1) // 12, (index + 1) % 12 + 1, 1)
            cols.append(Column(f"{start:%b %Y}", start, following - timedelta(days=1)))
        return cols
    periods = list(
        PayPeriod.objects.filter(household=view.household, start_date__lte=today).order_by(
            "-start_date"
        )[: view.periods]
    )
    return [
        Column(f"{p.start_date:%b %-d}–{p.end_date:%b %-d}", p.start_date, p.end_date)
        for p in reversed(periods)
    ]


def _items(view: HabitView, start: date, end: date) -> list[Item]:
    home = view.household
    chosen = {c.pk for c in view.categories.all()} if view.pk else set()

    def keep(category: Category, essential: bool) -> bool:
        if chosen and category.pk not in chosen:
            return False
        return view.include_essential or not essential

    def essential_of(expense: Expense, category: Category) -> bool:
        override = expense.is_essential_override
        return category.is_essential if override is None else override

    items = []
    for expense in Expense.objects.filter(
        household=home,
        date__range=(start, end),
        splits__isnull=True,
        category__type=Category.Type.SPENDING,
    ).select_related("category"):
        essential = essential_of(expense, expense.category)
        if keep(expense.category, essential):
            items.append(
                Item(
                    expense.date,
                    expense.merchant,
                    expense.category,
                    expense.amount,
                    expense.amount,
                    essential,
                    expense.pk,
                )
            )
    for split in ExpenseSplit.objects.filter(
        expense__household=home,
        expense__date__range=(start, end),
        category__type=Category.Type.SPENDING,
    ).select_related("expense", "category"):
        essential = essential_of(split.expense, split.category)
        if keep(split.category, essential):
            items.append(
                Item(
                    split.expense.date,
                    split.expense.merchant,
                    split.category,
                    split.amount,
                    split.expense.amount,
                    essential,
                    split.expense_id,
                )
            )
    for refund in Refund.objects.filter(
        household=home, date__range=(start, end), category__type=Category.Type.SPENDING
    ).select_related("category"):
        if keep(refund.category, refund.category.is_essential):
            items.append(
                Item(
                    refund.date,
                    refund.merchant,
                    refund.category,
                    -refund.amount,
                    -refund.amount,
                    refund.category.is_essential,
                    is_refund=True,
                )
            )
    return items


def _merchant_rows(items: list[Item]) -> list[MerchantRow]:
    rows: dict[str, MerchantRow] = {}
    for item in items:
        key = merchant_key(item.merchant)
        if not key:
            continue
        row = rows.setdefault(key, MerchantRow(name=item.merchant.strip()))
        row.total += item.amount
        if not item.is_refund and item.purchase_id not in row.purchases:
            row.purchases.add(item.purchase_id)
            row.spellings[" ".join(item.merchant.split())] += 1
            row.last = max(row.last or item.date, item.date)
    for row in rows.values():
        if row.spellings:
            row.name = row.spellings.most_common(1)[0][0]
    return [row for row in rows.values() if row.count]


def build_report(view: HabitView, today: date) -> HabitReport:
    columns = columns_for(view, today)
    if not columns:
        return HabitReport(view, [], today, today, ZERO, 0, ZERO, [], [], [], [], [])
    start, end = columns[0].start, columns[-1].end
    items = _items(view, start, end)
    total = money(sum((i.amount for i in items), ZERO))
    # Count each small purchase once even when it is split across categories.
    small = [i for i in items if not i.is_refund and i.purchase_total <= view.small_threshold]
    small_total = money(sum((i.amount for i in small), ZERO))
    purchases = {i.purchase_id for i in small}
    small_merchants = sorted(_merchant_rows(small), key=lambda r: (-r.total, r.name))

    merchants = sorted(_merchant_rows(items), key=lambda r: (-r.count, -r.total, r.name))
    # Days so far: the current period's future days have no spending yet.
    span = Decimal((min(end, today) - start).days + 1)
    habits = [
        Habit(
            row,
            money(row.total * DAYS_PER_MONTH / span),
            money(row.total * DAYS_PER_MONTH / span * 12),
        )
        for row in small_merchants
        if row.count >= view.min_repeat
    ]

    by_category: dict[int, list[Decimal]] = defaultdict(lambda: [ZERO] * len(columns))
    categories: dict[int, Category] = {}
    for item in items:
        for index, column in enumerate(columns):
            if column.start <= item.date <= column.end:
                by_category[item.category.pk][index] += item.amount
                categories[item.category.pk] = item.category
                break
    trend = sorted(
        (
            TrendRow(categories[pk], [money(v) for v in values])
            for pk, values in by_category.items()
        ),
        key=lambda row: (-row.total, row.category.name),
    )
    column_totals = [
        money(sum((row.amounts[i] for row in trend), ZERO)) for i in range(len(columns))
    ]
    return HabitReport(
        view,
        columns,
        start,
        end,
        total,
        len(purchases),
        small_total,
        small_merchants,
        merchants,
        habits,
        trend,
        column_totals,
    )


# --- Cleanup queue ---------------------------------------------------------------------


@dataclass
class SpellingGroup:
    key: str
    spellings: list[tuple[str, int]]  # most used first

    @property
    def suggested(self) -> str:
        return self.spellings[0][0]


def cleanup_queue(household, limit: int = 50):
    """Expenses (any category) with no merchant, and merchants typed more than one way."""
    blank = list(
        Expense.objects.filter(household=household, merchant__regex=r"^\s*$")
        .select_related("category", "account")
        .order_by("-date", "-pk")[:limit]
    )
    counts: dict[str, Counter] = defaultdict(Counter)
    for name in (
        Expense.objects.filter(household=household)
        .exclude(merchant="")
        .values_list("merchant", flat=True)
    ):
        counts[merchant_key(name)][name] += 1
    groups = [
        SpellingGroup(key, spellings.most_common())
        for key, spellings in counts.items()
        if key and len(spellings) > 1
    ]
    groups.sort(key=lambda g: -sum(n for _, n in g.spellings))
    return blank, groups[:limit]


def unify_merchant(household, actor, key: str, name: str) -> tuple[int, int]:
    """Use one spelling for every expense and refund whose merchant matches ``key``.

    Only the merchant text changes. Rows in locked pay periods are left alone.
    Returns (changed, skipped because locked).
    """
    name = " ".join(name.split())
    if not name or merchant_key(name) != key:
        raise ValidationError("Choose one of the spellings already used for this merchant.")
    changed = skipped = 0
    with transaction.atomic():
        for model in (Expense, Refund):
            for row in model.objects.select_for_update().filter(household=household):
                if merchant_key(row.merchant) != key or row.merchant == name:
                    continue
                try:
                    ensure_open(household, row.date)
                except ValidationError:
                    skipped += 1
                    continue
                before = row.merchant
                row.merchant = name
                row.save(update_fields=["merchant", "updated_at"])
                audit(household, actor, row, "update", {"merchant": [before, name]})
                changed += 1
    return changed, skipped


# --- Category budgets ------------------------------------------------------------------


def budget_for(category: Category, on: date) -> CategoryBudget | None:
    return (
        CategoryBudget.objects.filter(category=category, effective_from__lte=on)
        .filter(_open_on(on))
        .order_by("-effective_from")
        .first()
    )


def _open_on(on: date) -> Q:
    return Q(effective_to__isnull=True) | Q(effective_to__gte=on)


def set_category_budget(household, actor, category: Category, amount, effective_from: date):
    """Set the per-pay-period budget from ``effective_from``; earlier periods keep theirs.

    ``amount`` 0 removes the budget from that date (the row records the change).
    """
    if category.household_id != household.pk or category.type != Category.Type.SPENDING:
        raise ValidationError("Choose a spending category from this budget profile.")
    amount = money(amount)
    if amount < 0:
        raise ValidationError("A budget cannot be negative.")
    with transaction.atomic():
        rows = CategoryBudget.objects.select_for_update().filter(category=category)
        if rows.filter(effective_from__gt=effective_from).exists():
            raise ValidationError("A later budget for this category already exists.")
        current = (
            rows.filter(effective_from__lte=effective_from)
            .filter(_open_on(effective_from))
            .order_by("-effective_from")
            .first()
        )
        if current and current.effective_from == effective_from:
            before = current.amount
            current.amount = amount
            current.save(update_fields=["amount"])
            audit(household, actor, current, "update", {"amount": [str(before), str(amount)]})
            return current
        if current:
            current.effective_to = effective_from - timedelta(days=1)
            current.save(update_fields=["effective_to"])
        row = CategoryBudget.objects.create(
            household=household,
            category=category,
            amount=amount,
            effective_from=effective_from,
            created_by=actor,
        )
        audit(
            household,
            actor,
            row,
            "create",
            {"amount": str(amount), "effective_from": effective_from.isoformat()},
        )
        return row


@dataclass
class BudgetLine:
    category: Category
    budget: Decimal
    spent: Decimal

    @property
    def remaining(self) -> Decimal:
        return money(self.budget - self.spent)

    @property
    def percent(self) -> int:
        if self.budget <= 0:
            return 0
        return int((self.spent * 100 / self.budget).quantize(Decimal("1"), ROUND_HALF_UP))


def category_budget_status(household, period: PayPeriod) -> list[BudgetLine]:
    """Budget vs net spending per category for one pay period (remaining may be negative)."""
    view = HabitView(household=household, name="budget", include_essential=True)
    items = _items(view, period.start_date, period.end_date)
    spent: dict[int, Decimal] = defaultdict(lambda: ZERO)
    for item in items:
        spent[item.category.pk] += item.amount
    lines = []
    for category in Category.objects.filter(
        household=household, type=Category.Type.SPENDING, archived_at=None
    ):
        budget = budget_for(category, period.start_date)
        if budget is None or budget.amount <= 0:
            continue
        lines.append(BudgetLine(category, budget.amount, money(spent[category.pk])))
    lines.sort(key=lambda line: (line.remaining, line.category.name))
    return lines
