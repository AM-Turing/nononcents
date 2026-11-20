from decimal import Decimal

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, RangeOperators
from django.db import models
from django.db.models import Deferrable, F, Func, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

MONEY = {"max_digits": 14, "decimal_places": 2, "default": Decimal("0.00")}


def pay_period_range():
    """The days a pay period covers, both ends included (Postgres ``daterange``)."""
    return Func(
        F("start_date"),
        F("end_date"),
        Value("[]"),
        function="daterange",
        output_field=DateRangeField(),
    )


def paid_total():
    """Annotation for ``BillOccurrence.amount_paid``: what its payments add up to (never null)."""
    return Coalesce(
        Sum("expenses__amount"),
        Value(Decimal("0.00")),
        output_field=models.DecimalField(max_digits=14, decimal_places=2),
    )


class Household(models.Model):
    name = models.CharField(max_length=120, default="My Household")

    def __str__(self):
        return self.name


class HouseholdMembership(models.Model):
    household = models.ForeignKey(Household, related_name="memberships", on_delete=models.CASCADE)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="household_memberships", on_delete=models.CASCADE
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["household", "user"], name="unique_household_membership"
            )
        ]

    def __str__(self):
        return f"{self.user} — {self.household}"


class Account(models.Model):
    class Type(models.TextChoices):
        SPENDING = "spending", "Spending"
        BILLS = "bills", "Bills"
        SAVINGS = "savings", "Savings"
        OTHER = "other", "Other"

    household = models.ForeignKey(Household, on_delete=models.CASCADE)
    name = models.CharField(max_length=80)
    type = models.CharField(max_length=16, choices=Type, default=Type.OTHER)
    opening_balance = models.DecimalField(**MONEY)
    display_order = models.PositiveSmallIntegerField(default=0)
    archived_at = models.DateTimeField(null=True, blank=True)
    statement_suffix = models.CharField(
        max_length=4,
        blank=True,
        help_text="Last 4 digits of the bank account number, for statement imports.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["household", "name"], name="unique_account_name"),
            models.UniqueConstraint(
                fields=["household", "statement_suffix"],
                condition=~Q(statement_suffix=""),
                name="unique_account_statement_suffix",
            ),
        ]
        ordering = ["display_order", "name"]

    def __str__(self):
        return self.name


class Category(models.Model):
    class Type(models.TextChoices):
        SPENDING = "spending", "Spending"
        BILL = "bill", "Bill"
        INCOME = "income", "Income"
        SAVINGS = "savings", "Savings"
        ADJUSTMENT = "adjustment", "Adjustment"

    household = models.ForeignKey(Household, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    type = models.CharField(max_length=16, choices=Type)
    display_order = models.PositiveSmallIntegerField(default=0)
    is_essential = models.BooleanField(
        default=True,
        help_text="Essential categories are excluded from discretionary spending cut scenarios.",
    )
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["household", "name", "type"], name="unique_category_name_type"
            )
        ]
        ordering = ["name"]

    def __str__(self):
        return self.name


class PaySchedule(models.Model):
    class Frequency(models.TextChoices):
        WEEKLY = "weekly", "Weekly"
        BIWEEKLY = "biweekly", "Bi-weekly"
        SEMIMONTHLY = "semimonthly", "Semi-monthly"
        MONTHLY = "monthly", "Monthly"

    household = models.OneToOneField(Household, on_delete=models.CASCADE)
    frequency = models.CharField(max_length=16, choices=Frequency, default=Frequency.SEMIMONTHLY)
    nominal_days = models.JSONField(default=list)


class PayPeriod(models.Model):
    household = models.ForeignKey(Household, on_delete=models.CASCADE)
    nominal_payday = models.DateField()
    expected_deposit_date = models.DateField()
    actual_deposit_date = models.DateField(null=True, blank=True)
    start_date = models.DateField()
    end_date = models.DateField()
    is_locked = models.BooleanField(default=False)
    is_exceptional = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(end_date__gte=models.F("start_date")), name="valid_pay_period_range"
            ),
            models.UniqueConstraint(fields=["household", "start_date"], name="unique_period_start"),
            # Postgres only (the migration skips SQLite): no two periods of one household
            # may share a day. The app also checks this in forms and services. Deferred, so
            # that moving two neighbouring periods (saved one after the other) is checked
            # once, when the transaction commits.
            ExclusionConstraint(
                name="no_overlapping_pay_periods",
                deferrable=Deferrable.DEFERRED,
                expressions=[
                    (pay_period_range(), RangeOperators.OVERLAPS),
                    ("household", RangeOperators.EQUAL),
                ],
            ),
        ]
        ordering = ["-start_date"]

    def __str__(self):
        return f"{self.start_date} – {self.end_date}"


class DatedMoney(models.Model):
    household = models.ForeignKey(Household, on_delete=models.PROTECT)
    date = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Income(DatedMoney):
    source = models.CharField(max_length=160)
    is_irregular = models.BooleanField(default=False)
    is_paycheck = models.BooleanField(
        default=False,
        help_text="The regular paycheck. Its date starts the pay period it funds.",
    )

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="income_positive")]

    def __str__(self):
        return self.source


class IncomeAllocation(models.Model):
    income = models.ForeignKey(Income, related_name="allocations", on_delete=models.CASCADE)
    account = models.ForeignKey(Account, on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    cleared_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="allocation_positive"),
            models.UniqueConstraint(
                fields=["income", "account"], name="one_allocation_per_account"
            ),
        ]


class RecurringBill(models.Model):
    household = models.ForeignKey(Household, on_delete=models.CASCADE)
    name = models.CharField(max_length=160)
    default_amount = models.DecimalField(max_digits=14, decimal_places=2)
    due_day = models.PositiveSmallIntegerField()
    category = models.ForeignKey(Category, on_delete=models.PROTECT)
    account = models.ForeignKey(Account, on_delete=models.PROTECT)
    active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    effective_start = models.DateField()
    effective_end = models.DateField(null=True, blank=True)

    class Frequency(models.IntegerChoices):
        MONTHLY = 1, "Every month"
        EVERY_2_MONTHS = 2, "Every 2 months"
        EVERY_3_MONTHS = 3, "Every 3 months"
        YEARLY = 12, "Once a year"

    every_months = models.PositiveSmallIntegerField(
        "How often", choices=Frequency, default=Frequency.MONTHLY
    )
    schedule_start = models.DateField(
        null=True,
        blank=True,
        help_text="First month of the current schedule; empty means the bill's start date.",
    )
    debt = models.ForeignKey(
        "Debt",
        related_name="bills",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text="The debt this bill pays down, if any.",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(default_amount__gt=0), name="bill_default_positive"),
            models.CheckConstraint(
                condition=Q(due_day__gte=1, due_day__lte=31), name="valid_due_day"
            ),
            models.UniqueConstraint(fields=["household", "name"], name="unique_bill_name"),
        ]

    def __str__(self):
        return self.name


class BillOccurrence(models.Model):
    class Status(models.TextChoices):
        UNCONFIRMED = "unconfirmed", "Unconfirmed"
        CONFIRMED = "confirmed", "Confirmed"
        PARTIAL = "partial", "Partially paid"
        PAID = "paid", "Paid"
        SKIPPED = "skipped", "Skipped"

    bill = models.ForeignKey(RecurringBill, related_name="occurrences", on_delete=models.PROTECT)
    category = models.ForeignKey(
        Category,
        related_name="bill_occurrences",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    account = models.ForeignKey(
        Account,
        related_name="bill_occurrences",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    due_date = models.DateField()
    expected_amount = models.DecimalField(max_digits=14, decimal_places=2)
    confirmed_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status, default=Status.UNCONFIRMED)
    pay_period = models.ForeignKey(PayPeriod, null=True, blank=True, on_delete=models.PROTECT)
    notes = models.TextField(blank=True)
    skipped_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(expected_amount__gt=0), name="occurrence_expected_positive"
            ),
            models.UniqueConstraint(
                models.F("bill"),
                models.functions.ExtractYear("due_date"),
                models.functions.ExtractMonth("due_date"),
                name="unique_bill_occurrence_month",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.bill.name} — due {self.due_date:%b %-d, %Y} — "
            f"${self.remaining_amount:,.2f} remaining ({self.get_status_display()})"
        )

    @property
    def target_amount(self):
        return self.confirmed_amount if self.confirmed_amount is not None else self.expected_amount

    @property
    def amount_paid(self):
        # A read-only page can annotate ``paid_total`` on its queryset to avoid one query
        # per read; code that writes never does, so it always sees the live total.
        annotated = self.__dict__.get("paid_total")
        if annotated is not None:
            return annotated
        return self.expenses.aggregate(total=models.Sum("amount"))["total"] or Decimal("0.00")

    @property
    def remaining_amount(self):
        if self.status == self.Status.SKIPPED:
            return Decimal("0.00")
        return max(self.target_amount - self.amount_paid, Decimal("0.00"))

    @property
    def effective_category(self):
        return self.category or self.bill.category

    @property
    def effective_account(self):
        return self.account or self.bill.account


class Expense(DatedMoney):
    account = models.ForeignKey(Account, related_name="expenses", on_delete=models.PROTECT)
    category = models.ForeignKey(Category, related_name="expenses", on_delete=models.PROTECT)
    merchant = models.CharField(max_length=160, blank=True)
    bill_occurrence = models.ForeignKey(
        BillOccurrence, related_name="expenses", null=True, blank=True, on_delete=models.PROTECT
    )
    is_essential_override = models.BooleanField(
        null=True,
        blank=True,
        help_text="Leave blank to use the category's essential classification.",
    )
    cleared_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="expense_positive")]


class ExpenseSplit(models.Model):
    expense = models.ForeignKey(Expense, related_name="splits", on_delete=models.CASCADE)
    category = models.ForeignKey(Category, related_name="expense_splits", on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="expense_split_positive"),
            models.UniqueConstraint(
                fields=["expense", "category"], name="one_expense_split_per_category"
            ),
        ]
        ordering = ["pk"]


class Refund(DatedMoney):
    account = models.ForeignKey(Account, related_name="refunds", on_delete=models.PROTECT)
    category = models.ForeignKey(Category, related_name="refunds", on_delete=models.PROTECT)
    original_expense = models.ForeignKey(
        Expense, related_name="refunds", null=True, blank=True, on_delete=models.PROTECT
    )
    merchant = models.CharField(max_length=160, blank=True)
    cleared_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="refund_positive")]


class Transfer(DatedMoney):
    source = models.ForeignKey(Account, related_name="outgoing_transfers", on_delete=models.PROTECT)
    destination = models.ForeignKey(
        Account, related_name="incoming_transfers", on_delete=models.PROTECT
    )
    source_cleared_at = models.DateTimeField(null=True, blank=True)
    destination_cleared_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="transfer_positive"),
            models.CheckConstraint(
                condition=~Q(source=models.F("destination")), name="different_transfer_accounts"
            ),
        ]


class BalanceAdjustment(DatedMoney):
    account = models.ForeignKey(Account, related_name="adjustments", on_delete=models.PROTECT)
    is_increase = models.BooleanField()
    reason = models.CharField(max_length=255)
    cleared_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="adjustment_positive")
        ]


class Reconciliation(models.Model):
    """History of bank reconciliations; never a source of balances."""

    household = models.ForeignKey(Household, on_delete=models.PROTECT)
    account = models.ForeignKey(Account, related_name="reconciliations", on_delete=models.PROTECT)
    statement_date = models.DateField()
    statement_balance = models.DecimalField(max_digits=14, decimal_places=2)
    ledger_balance = models.DecimalField(max_digits=14, decimal_places=2)
    adjustment = models.OneToOneField(
        BalanceAdjustment,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reconciliation",
    )
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-statement_date", "-created_at", "-pk"]


class StatementImport(models.Model):
    """One account's section of a bank statement, staged for the monthly completeness check.

    Statement lines never become ledger rows by themselves: the owner confirms a match
    ("Same purchase"), adds a new entry, or dismisses the line. The PDF itself is not kept.
    """

    household = models.ForeignKey(Household, on_delete=models.CASCADE)
    account = models.ForeignKey(Account, related_name="statements", on_delete=models.PROTECT)
    period_start = models.DateField()
    period_end = models.DateField()
    closing_balance = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    # False when the rows could not be added up against any balance on the statement.
    verified = models.BooleanField(default=True)
    file_sha256 = models.CharField(max_length=64)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_end", "account__display_order", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "file_sha256"], name="unique_statement_file_per_account"
            ),
            models.CheckConstraint(
                condition=Q(period_end__gte=models.F("period_start")),
                name="statement_period_order",
            ),
        ]

    def __str__(self):
        return f"{self.account} statement {self.period_start:%b %-d} – {self.period_end:%b %-d, %Y}"


class StatementLine(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Needs review"
        MATCHED = "matched", "Same as an entry"
        DISMISSED = "dismissed", "Dismissed"

    statement = models.ForeignKey(StatementImport, related_name="lines", on_delete=models.CASCADE)
    account = models.ForeignKey(Account, related_name="statement_lines", on_delete=models.PROTECT)
    date = models.DateField()
    amount = models.DecimalField(
        max_digits=14, decimal_places=2, help_text="Signed: positive is money in."
    )
    description = models.CharField(
        max_length=255, blank=True, help_text="The bank's wording, kept for reference only."
    )
    fingerprint = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)
    matched_key = models.CharField(
        max_length=40,
        blank=True,
        help_text="Ledger item key (role:row id) this line was confirmed against.",
    )
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["date", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "fingerprint"], name="unique_statement_line_per_account"
            ),
            models.CheckConstraint(condition=~Q(amount=0), name="statement_line_nonzero"),
        ]


class SavingsGoal(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        COMPLETED = "completed", "Completed"
        ARCHIVED = "archived", "Archived"

    household = models.ForeignKey(Household, related_name="savings_goals", on_delete=models.CASCADE)
    name = models.CharField(max_length=120)
    target_amount = models.DecimalField(max_digits=14, decimal_places=2)
    priority = models.PositiveSmallIntegerField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status, default=Status.ACTIVE)
    is_system = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["priority", "created_at", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(target_amount__gte=0), name="goal_target_nonnegative"
            ),
            models.UniqueConstraint(
                fields=["household", "name"],
                condition=~Q(status="archived"),
                name="unique_current_savings_goal_name",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def current_amount(self):
        return self.entries.aggregate(total=models.Sum("amount"))["total"] or Decimal("0.00")

    @property
    def progress_percent(self):
        if self.target_amount <= 0:
            return Decimal("100.0")
        return min(
            (self.current_amount / self.target_amount * Decimal("100")).quantize(Decimal("0.1")),
            Decimal("100.0"),
        )

    @property
    def remaining_amount(self):
        return max(self.target_amount - self.current_amount, Decimal("0.00"))


class SavingsGoalEntry(models.Model):
    class Kind(models.TextChoices):
        INITIAL = "initial", "Initial allocation"
        INCOME = "income", "Income"
        REFUND = "refund", "Refund"
        TRANSFER_IN = "transfer_in", "Transfer in"
        TRANSFER_OUT = "transfer_out", "Transfer out"
        EXPENSE = "expense", "Expense"
        REALLOCATION_IN = "reallocation_in", "Reallocation in"
        REALLOCATION_OUT = "reallocation_out", "Reallocation out"
        RECONCILIATION = "reconciliation", "Reconciliation"

    goal = models.ForeignKey(SavingsGoal, related_name="entries", on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    date = models.DateField()
    kind = models.CharField(max_length=24, choices=Kind)
    note = models.CharField(max_length=255, blank=True)
    fulfills_goal = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    income_allocation = models.ForeignKey(
        IncomeAllocation,
        related_name="savings_goal_entries",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    expense = models.ForeignKey(
        Expense,
        related_name="savings_goal_entries",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    refund = models.ForeignKey(
        Refund,
        related_name="savings_goal_entries",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    transfer = models.ForeignKey(
        Transfer,
        related_name="savings_goal_entries",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    adjustment = models.ForeignKey(
        BalanceAdjustment,
        related_name="savings_goal_entries",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )

    class Meta:
        ordering = ["date", "created_at", "pk"]
        constraints = [
            models.CheckConstraint(condition=~Q(amount=0), name="savings_entry_nonzero"),
            models.CheckConstraint(
                condition=(
                    Q(
                        income_allocation__isnull=False,
                        expense__isnull=True,
                        refund__isnull=True,
                        transfer__isnull=True,
                        adjustment__isnull=True,
                    )
                    | Q(
                        income_allocation__isnull=True,
                        expense__isnull=False,
                        refund__isnull=True,
                        transfer__isnull=True,
                        adjustment__isnull=True,
                    )
                    | Q(
                        income_allocation__isnull=True,
                        expense__isnull=True,
                        refund__isnull=False,
                        transfer__isnull=True,
                        adjustment__isnull=True,
                    )
                    | Q(
                        income_allocation__isnull=True,
                        expense__isnull=True,
                        refund__isnull=True,
                        transfer__isnull=False,
                        adjustment__isnull=True,
                    )
                    | Q(
                        income_allocation__isnull=True,
                        expense__isnull=True,
                        refund__isnull=True,
                        transfer__isnull=True,
                        adjustment__isnull=False,
                    )
                    | Q(
                        income_allocation__isnull=True,
                        expense__isnull=True,
                        refund__isnull=True,
                        transfer__isnull=True,
                        adjustment__isnull=True,
                    )
                ),
                name="savings_entry_one_source",
            ),
        ]


class PayPeriodBudget(models.Model):
    class Mode(models.TextChoices):
        FIXED = "fixed", "Fixed pay-period target"
        PRORATED = "prorated", "Prorated weekly target"

    pay_period = models.OneToOneField(PayPeriod, related_name="budget", on_delete=models.CASCADE)
    weekly_target = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("700.00"))
    spending_target = models.DecimalField(**MONEY)
    expected_paycheck = models.DecimalField(**MONEY)
    bills_target = models.DecimalField(**MONEY)
    bills_cushion = models.DecimalField(**MONEY)
    temporary_adjustment = models.DecimalField(**MONEY)
    mode = models.CharField(max_length=16, choices=Mode, default=Mode.FIXED)
    carryover_enabled = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(weekly_target__gte=0), name="period_budget_weekly_nonnegative"
            ),
            models.CheckConstraint(
                condition=Q(spending_target__gte=0), name="period_budget_spending_nonnegative"
            ),
            models.CheckConstraint(
                condition=Q(expected_paycheck__gte=0), name="period_budget_paycheck_nonnegative"
            ),
            models.CheckConstraint(
                condition=Q(bills_target__gte=0), name="period_budget_bills_nonnegative"
            ),
            models.CheckConstraint(
                condition=Q(bills_cushion__gte=0), name="period_budget_cushion_nonnegative"
            ),
        ]


class CategoryBudget(models.Model):
    """Spending allowed in one category per pay period, effective from a date.

    Changing an amount closes the current row and starts a new one, so earlier periods
    keep the budget they had. A period uses the row in effect on its start date.
    """

    household = models.ForeignKey(Household, on_delete=models.CASCADE)
    category = models.ForeignKey(Category, related_name="budgets", on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["category__name", "-effective_from"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gte=0), name="category_budget_nonnegative"),
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True)
                | Q(effective_to__gte=models.F("effective_from")),
                name="category_budget_dates",
            ),
            models.UniqueConstraint(
                fields=["category", "effective_from"], name="one_category_budget_per_start"
            ),
        ]


class HabitView(models.Model):
    """A saved way of looking at spending habits on the Habits page."""

    class Basis(models.TextChoices):
        PAY_PERIOD = "pay_period", "Pay periods"
        MONTH = "month", "Calendar months"

    household = models.ForeignKey(Household, on_delete=models.CASCADE)
    name = models.CharField(max_length=80)
    small_threshold = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("20.00"),
        help_text="A purchase this size or smaller counts as a small purchase.",
    )
    basis = models.CharField(max_length=16, choices=Basis, default=Basis.PAY_PERIOD)
    periods = models.PositiveSmallIntegerField(
        default=6, help_text="Pay periods or months to look back, including this one."
    )
    min_repeat = models.PositiveSmallIntegerField(
        default=3, help_text="Small purchases at one merchant before it counts as a habit."
    )
    categories = models.ManyToManyField(
        Category, blank=True, help_text="Leave empty to include every spending category."
    )
    include_essential = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["household", "name"], name="unique_habit_view_name"),
            models.CheckConstraint(condition=Q(small_threshold__gt=0), name="habit_threshold"),
            models.CheckConstraint(
                condition=Q(periods__gte=1, periods__lte=24), name="habit_periods_range"
            ),
            models.CheckConstraint(condition=Q(min_repeat__gte=2), name="habit_min_repeat"),
        ]

    def __str__(self):
        return self.name


class ReportAnalysisRun(models.Model):
    """One Deep pattern analysis, run in the background so the page never times out.

    Only the checked result (amounts verified against the report) is stored; the report
    data sent to the local model is not.
    """

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    household = models.ForeignKey(Household, on_delete=models.CASCADE)
    start = models.DateField()
    end = models.DateField()
    status = models.CharField(max_length=16, choices=Status, default=Status.RUNNING)
    result = models.JSONField(null=True, blank=True)
    error = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class Debt(models.Model):
    """A debt the household is paying off. Balances are entered from lender statements."""

    class Kind(models.TextChoices):
        LOAN = "loan", "Loan"
        CREDIT_CARD = "credit_card", "Credit card"
        MORTGAGE = "mortgage", "Mortgage"
        OTHER = "other", "Other"

    household = models.ForeignKey(Household, on_delete=models.CASCADE)
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=16, choices=Kind, default=Kind.LOAN)
    apr = models.DecimalField(
        "APR (%)",
        max_digits=6,
        decimal_places=3,
        help_text="Annual percentage rate from the lender, e.g. 7.49.",
    )
    minimum_payment = models.DecimalField(max_digits=14, decimal_places=2)
    notes = models.TextField(blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["household", "name"], name="unique_debt_name"),
            models.CheckConstraint(condition=Q(apr__gte=0, apr__lte=100), name="debt_apr_range"),
            models.CheckConstraint(
                condition=Q(minimum_payment__gte=0), name="debt_minimum_nonnegative"
            ),
        ]

    def __str__(self):
        return self.name


class DebtBalance(models.Model):
    """A balance read from a lender statement; history, never derived or edited in place."""

    debt = models.ForeignKey(Debt, related_name="balances", on_delete=models.PROTECT)
    as_of = models.DateField()
    balance = models.DecimalField(max_digits=14, decimal_places=2)
    notes = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-as_of", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["debt", "as_of"], name="one_debt_balance_per_day"),
            models.CheckConstraint(condition=Q(balance__gte=0), name="debt_balance_nonnegative"),
        ]


class UserPreference(models.Model):
    class Appearance(models.TextChoices):
        SYSTEM = "system", "System"
        LIGHT = "light", "Light"
        DARK = "dark", "Dark"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    appearance = models.CharField(max_length=8, choices=Appearance, default=Appearance.SYSTEM)


class ApplicationSetting(models.Model):
    household = models.ForeignKey(Household, on_delete=models.CASCADE)
    key = models.CharField(max_length=80)
    value = models.JSONField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["household", "key"], name="unique_setting_key")
        ]


class AuditEvent(models.Model):
    class Action(models.TextChoices):
        CREATE = "create", "Create"
        UPDATE = "update", "Update"
        DELETE = "delete", "Delete"
        LOGIN = "login", "Signed in"
        LOGIN_FAILED = "login_failed", "Sign-in failed"

    household = models.ForeignKey(Household, null=True, on_delete=models.SET_NULL)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    occurred_at = models.DateTimeField(auto_now_add=True)
    record_type = models.CharField(max_length=100)
    record_id = models.CharField(max_length=64)
    action = models.CharField(max_length=20, choices=Action)
    changes = models.JSONField(default=dict)

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [models.Index(fields=["household", "occurred_at"], name="audit_household_time")]


class FormSubmission(models.Model):
    """Idempotency token for create forms so a double-submitted form saves only once."""

    token = models.UUIDField(unique=True)
    household = models.ForeignKey(Household, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)


class LoginFailure(models.Model):
    """One failed sign-in, used for throttling. Never stores a password."""

    username_key = models.CharField(max_length=150, db_index=True)
    ip = models.GenericIPAddressField(null=True)
    at = models.DateTimeField(db_index=True, default=timezone.now)
