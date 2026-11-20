import uuid
from datetime import timedelta
from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, SetPasswordForm, UserCreationForm
from django.db.models import Q
from django.utils import timezone

from . import login_throttle
from .models import (
    Account,
    BillOccurrence,
    Category,
    Expense,
    Household,
    HouseholdMembership,
    PayPeriod,
    PayPeriodBudget,
    PaySchedule,
    RecurringBill,
    Refund,
    SavingsGoal,
    Transfer,
)
from .money import MoneyField, MoneyInput
from .services import default_income_split, live_bill_occurrences


def household_filter(household):
    return {"household": household} if household is not None else {}


class PayPeriodForm(forms.ModelForm):
    class Meta:
        model = PayPeriod
        fields = (
            "nominal_payday",
            "expected_deposit_date",
            "start_date",
            "end_date",
            "is_exceptional",
        )
        widgets = {
            "nominal_payday": forms.DateInput(attrs={"type": "date"}),
            "expected_deposit_date": forms.DateInput(attrs={"type": "date"}),
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
        }
        help_texts = {
            "nominal_payday": "The scheduled payday, even when the deposit arrives early.",
            "expected_deposit_date": "The date the deposit is expected to reach the account.",
            "start_date": (
                "Recording the paycheck sets this to the day it lands. Changing it also moves "
                "the previous period's end."
            ),
            "end_date": "Changing this also moves the next period's start.",
            "is_exceptional": "Mark periods that differ from the normal pay schedule.",
        }

    def __init__(self, *args, household, **kwargs):
        super().__init__(*args, **kwargs)
        self.household = household

    def clean(self):
        from .pay_periods import validate_contiguous_edit

        data = super().clean() or {}
        self.neighbour_adjustments = []
        start, end = data.get("start_date"), data.get("end_date")
        if start and end and end < start:
            self.add_error("end_date", "The end date cannot be before the start date.")
        elif start and end:
            period = self.instance
            period.household = self.household
            try:
                self.neighbour_adjustments = validate_contiguous_edit(period, start, end)
            except forms.ValidationError as exc:
                self.add_error(None, exc)
        return data


class GeneratePayPeriodsForm(forms.Form):
    frequency = forms.ChoiceField(
        choices=PaySchedule.Frequency.choices,
        help_text=(
            "Weekly and bi-weekly schedules continue from the latest nominal payday. "
            "Monthly uses that payday's day of month; semi-monthly uses the 1st and 15th."
        ),
    )
    generation_through = forms.DateField(
        label="Generate through",
        help_text="Creates the individual pay period that contains this date.",
        widget=forms.DateInput(attrs={"type": "date"}),
    )

    def __init__(self, *args, household, **kwargs):
        super().__init__(*args, **kwargs)
        self.household = household
        schedule = PaySchedule.objects.filter(household=household).first()
        if not self.is_bound:
            self.fields["frequency"].initial = (
                schedule.frequency if schedule else PaySchedule.Frequency.SEMIMONTHLY
            )

    def clean_generation_through(self):
        through = self.cleaned_data["generation_through"]
        latest = PayPeriod.objects.filter(household=self.household).order_by("-end_date").first()
        if not latest:
            raise forms.ValidationError("Add the current pay period before generating future ones.")
        if through <= latest.end_date:
            raise forms.ValidationError(
                f"Choose a date after the latest period ends on {latest.end_date:%B %-d, %Y}."
            )
        return through


ALERT_SETTING_DEFAULTS = {
    "spending_low_balance_enabled": True,
    "spending_low_balance_threshold": "200.00",
    "bills_shortage_enabled": True,
    "bill_confirmation_enabled": True,
}


class AlertSettingsForm(forms.Form):
    spending_low_balance_enabled = forms.BooleanField(
        required=False, label="Show the Spending low-balance alert"
    )
    spending_low_balance_threshold = MoneyField(
        label="Spending low-balance threshold",
        min_value=0,
    )
    bills_shortage_enabled = forms.BooleanField(
        required=False, label="Show the current-period Bills funding shortage alert"
    )
    bill_confirmation_enabled = forms.BooleanField(
        required=False, label="Show the bill-confirmation alert"
    )

    def cleaned_setting_value(self):
        return {
            "spending_low_balance_enabled": self.cleaned_data["spending_low_balance_enabled"],
            "spending_low_balance_threshold": str(
                self.cleaned_data["spending_low_balance_threshold"]
            ),
            "bills_shortage_enabled": self.cleaned_data["bills_shortage_enabled"],
            "bill_confirmation_enabled": self.cleaned_data["bill_confirmation_enabled"],
        }


def add_form_token(form):
    """Add the hidden idempotency token used by create forms (kept when re-rendered)."""
    message = "This form is no longer valid. Reload the page and enter the entry again."
    form.fields["form_token"] = forms.UUIDField(
        widget=forms.HiddenInput,
        initial=uuid.uuid4,
        error_messages={"required": message, "invalid": message},
    )
    return form


_REQUIRED_MESSAGES = {
    "amount": "Enter an amount.",
    "date": "Enter a date.",
    "merchant": "Enter a merchant.",
    "source": "Enter where the income came from.",
    "name": "Enter a name.",
}
_CHOOSE_MESSAGES = {
    "account": "Choose an account",
    "category": "Choose a category",
    "source": "Choose the account the money comes from",
    "destination": "Choose the account the money goes to",
}
_DEFAULT_REQUIRED = forms.Field.default_error_messages["required"]


def name_the_field(form):
    """Say which field is missing, and give empty select choices meaningful text."""
    for name, field in form.fields.items():
        is_choice = isinstance(field, forms.ChoiceField) or isinstance(
            field, forms.ModelChoiceField
        )
        if field.error_messages.get("required") == _DEFAULT_REQUIRED:
            label = str(field.label or name.replace("_", " ").capitalize())
            if is_choice and name in _CHOOSE_MESSAGES:
                message = f"{_CHOOSE_MESSAGES[name]}."
            elif not is_choice and name in _REQUIRED_MESSAGES:
                message = _REQUIRED_MESSAGES[name]
            else:
                message = f"{label} is required."
            field.error_messages["required"] = message
        empty = _CHOOSE_MESSAGES.get(name, "None")
        if isinstance(field, forms.ModelChoiceField):
            if field.empty_label is not None:
                field.empty_label = empty
        elif isinstance(field, forms.ChoiceField):
            field.choices = [
                (value, empty if value == "" and text == "---------" else text)
                for value, text in list(field.widget.choices)
            ]


class NamedFieldMessages:
    """Mixin: run ``name_the_field`` once the form's fields exist."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        name_the_field(self)


UNUSUAL_PAST_DAYS = 60
UNUSUAL_FUTURE_DAYS = 31
REJECT_DAYS = 365


class UnusualDateGuard:
    """Reject far-off dates and require confirmation for unusual ones.

    Host forms set ``self.household`` and may set ``self.original_date`` (an unchanged date
    on edit never needs confirmation).
    """

    household = None
    original_date = None

    def _confirm_field(self):
        return forms.BooleanField(
            required=False,
            label="This date is correct",
            help_text="Tick this to save a date that looks unusual.",
        )

    def clean_date(self):
        value = self.cleaned_data["date"]
        if self.original_date is not None and value == self.original_date:
            return value
        if abs((value - timezone.localdate()).days) > REJECT_DAYS:
            raise forms.ValidationError(
                "This date is more than a year from today. Check the year and try again."
            )
        return value

    def clean(self):
        data = super().clean() or {}  # type: ignore[misc]
        value = data.get("date")
        if value is not None and not self.has_error("date"):
            self._check_unusual_date(value)
        return data

    def _check_unusual_date(self, value):
        if self.original_date is not None and value == self.original_date:
            return
        today = timezone.localdate()
        reasons = []
        if (today - value).days > UNUSUAL_PAST_DAYS:
            reasons.append(f"more than {UNUSUAL_PAST_DAYS} days ago")
        if (value - today).days > UNUSUAL_FUTURE_DAYS:
            reasons.append(f"more than {UNUSUAL_FUTURE_DAYS} days in the future")
        if (
            self.household is not None
            and not PayPeriod.objects.filter(
                household=self.household, start_date__lte=value, end_date__gte=value
            ).exists()
        ):
            reasons.append("not in any pay period")
        if not reasons:
            return
        self.fields["confirm_unusual_date"] = self._confirm_field()
        if self.data.get(self.add_prefix("confirm_unusual_date")) in {"on", "true", "True", "1"}:
            return
        self.add_error(
            "date",
            f"This date is {' and '.join(reasons)} - check the year. "
            "Tick 'This date is correct' to save anyway.",
        )


# The browser decides how a date field looks (calendar, day/month order), so no format is named.
DATE_HELP = "Choose a date from the calendar, or type it the way your browser shows it."


class MoneyDateMixin(NamedFieldMessages, UnusualDateGuard, forms.ModelForm):
    def __init__(self, *args, household=None, **kwargs):
        self.household = household
        super().__init__(*args, **kwargs)

    amount = MoneyField(
        min_value=Decimal("0.01"),
        widget=MoneyInput(attrs={"data-autofocus": True}),
    )
    date = forms.DateField(
        initial=timezone.localdate,
        help_text=DATE_HELP,
        widget=forms.DateInput(
            attrs={
                "type": "date",
                "title": "Date",
            }
        ),
    )


REFUND_CATEGORY_TYPES = (Category.Type.SPENDING, Category.Type.BILL)
ORIGINAL_EXPENSE_DAYS = 120


class ExpenseChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        description = obj.merchant.strip() or obj.notes.strip() or obj.category.name
        return f"{obj.date:%Y-%m-%d} — {description} — ${obj.amount:,.2f}"


def payable_occurrences(household):
    """Bill months a payment may be applied to: live, not skipped, not long since paid."""
    if household is None:
        return BillOccurrence.objects.none()
    return (
        live_bill_occurrences(household)
        .filter(
            ~Q(status=BillOccurrence.Status.PAID)
            | Q(due_date__gte=timezone.localdate() - timedelta(days=90))
        )
        .select_related("bill")
        .order_by("bill__name", "due_date")
    )


class ExpenseForm(MoneyDateMixin):
    is_essential_override = forms.TypedChoiceField(
        label="Spending classification",
        required=False,
        choices=(
            ("", "Use category default"),
            ("true", "Essential"),
            ("false", "Non-essential"),
        ),
        coerce=lambda value: value == "true",
        empty_value=None,
    )
    category = forms.ModelChoiceField(queryset=Category.objects.none(), required=False)
    split_transaction = forms.BooleanField(
        required=False,
        label="Split this transaction across categories",
        help_text="Add at least two category amounts that equal the total expense exactly.",
    )
    bill = forms.ModelChoiceField(
        label="Apply payment to bill (optional)",
        queryset=RecurringBill.objects.none(),
        required=False,
        help_text=(
            "Choose the bill being paid. The matching monthly bill is selected "
            "automatically from the transaction date."
        ),
    )

    bill_occurrence = forms.ModelChoiceField(
        label="Bill month (optional)",
        queryset=BillOccurrence.objects.none(),
        required=False,
        help_text=(
            "Leave blank to apply the payment to the unpaid bill month nearest the "
            "transaction date."
        ),
    )

    class Meta:
        model = Expense
        fields = [
            "amount",
            "date",
            "account",
            "category",
            "merchant",
            "is_essential_override",
            "notes",
        ]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, household=household, **kwargs)
        self.order_fields(
            [
                "amount",
                "date",
                "account",
                "category",
                "merchant",
                "notes",
                "split_transaction",
                "is_essential_override",
                "bill",
                "bill_occurrence",
            ]
        )
        payable = payable_occurrences(household)
        self.fields["bill"].queryset = (
            RecurringBill.objects.filter(
                Q(active=True) | Q(pk__in=payable.values("bill_id")), **household_filter(household)
            )
            .distinct()
            .order_by("name")
        )
        self.fields["bill_occurrence"].queryset = payable
        self.fields["account"].queryset = Account.objects.filter(
            archived_at=None, **household_filter(household)
        )
        self.fields["category"].queryset = Category.objects.filter(
            archived_at=None, **household_filter(household)
        )

    MORE_OPTION_FIELDS = ("split_transaction", "is_essential_override", "bill", "bill_occurrence")

    @property
    def main_fields(self):
        return [f for f in self.visible_fields() if f.name not in self.MORE_OPTION_FIELDS]

    @property
    def more_fields(self):
        return [self[name] for name in self.MORE_OPTION_FIELDS]

    @property
    def more_options_open(self):
        """True when any field tucked into "More options" has a value or an error."""
        for field in self.more_fields:
            if field.errors:
                return True
            value = field.value()
            if field.name == "split_transaction":
                if value and value not in ("False", "false", "0"):
                    return True
            elif value not in (None, "", []):
                return True
        return False

    def clean(self):
        data = super().clean() or {}
        account, category, bill = (
            data.get("account"),
            data.get("category"),
            data.get("bill"),
        )
        if account and category:
            if bill:
                if account.type not in {Account.Type.BILLS, Account.Type.SPENDING}:
                    self.add_error("account", "Pay a bill from the Bills or Spending account.")
                if category.type != Category.Type.BILL:
                    self.add_error("category", "Choose a Bills category for the selected bill.")
            elif not category_allowed_for_account(account, category):
                self.add_error("category", "Category does not match the selected account.")
        occurrence = data.get("bill_occurrence")
        if occurrence and not bill:
            self.add_error("bill_occurrence", "Choose a bill before choosing a bill month.")
        elif occurrence and bill and occurrence.bill_id != bill.pk:
            self.add_error("bill_occurrence", "Choose a bill month that belongs to this bill.")
        if not data.get("split_transaction") and not category:
            self.add_error("category", "Choose a category.")
        return data


def category_allowed_for_account(account, category):
    if account.type == Account.Type.BILLS:
        return category.type == Category.Type.BILL
    if account.type == Account.Type.SPENDING:
        return category.type == Category.Type.SPENDING or (
            category.type == Category.Type.BILL and category.name.casefold() == "utilities"
        )
    return category.type == Category.Type.SPENDING


class ExpenseSplitForm(NamedFieldMessages, forms.Form):
    category = forms.ModelChoiceField(label="What it was for", queryset=Category.objects.none())
    amount = MoneyField(
        label="Cost of this part",
        min_value=Decimal("0.01"),
    )

    def __init__(self, *args, household=None, current_category_ids=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = Category.objects.filter(
            Q(archived_at=None) | Q(pk__in=current_category_ids),
            **household_filter(household),
        )


ExpenseSplitFormSet = forms.formset_factory(
    ExpenseSplitForm, extra=2, max_num=10, validate_max=True
)
ExpenseSplitEditFormSet = forms.formset_factory(
    ExpenseSplitForm, extra=0, max_num=10, validate_max=True
)


class RefundForm(MoneyDateMixin):
    savings_destination_goal = forms.ModelChoiceField(
        label="Savings goal",
        queryset=SavingsGoal.objects.none(),
        required=False,
        help_text="Required when the refund is deposited into Savings.",
    )
    original_expense = ExpenseChoiceField(
        label="Original expense (optional)", queryset=Expense.objects.none(), required=False
    )

    class Meta:
        model = Refund
        fields = [
            "amount",
            "date",
            "account",
            "category",
            "merchant",
            "original_expense",
            "savings_destination_goal",
            "notes",
        ]

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, household=household, **kwargs)
        self.fields["account"].queryset = Account.objects.filter(
            archived_at=None, **household_filter(household)
        )
        self.fields["category"].queryset = Category.objects.filter(
            archived_at=None, type__in=REFUND_CATEGORY_TYPES, **household_filter(household)
        )
        recent = Q(date__gte=timezone.localdate() - timedelta(days=ORIGINAL_EXPENSE_DAYS))
        if self.instance.pk and self.instance.original_expense_id:
            recent |= Q(pk=self.instance.original_expense_id)
        self.fields["original_expense"].queryset = (
            Expense.objects.filter(recent, **household_filter(household))
            .select_related("account", "category")
            .order_by("-date", "-pk")
        )
        self.fields["savings_destination_goal"].queryset = SavingsGoal.objects.filter(
            status=SavingsGoal.Status.ACTIVE,
            is_system=False,
            **household_filter(household),
        ).order_by("priority", "name")

    def clean(self):
        data = super().clean() or {}
        if (
            data.get("account")
            and data["account"].type == Account.Type.SAVINGS
            and not data.get("savings_destination_goal")
        ):
            self.add_error("savings_destination_goal", "Choose the goal receiving this refund.")
        return data


class TransferForm(MoneyDateMixin):
    savings_destination_goal = forms.ModelChoiceField(
        label="Savings destination goal",
        queryset=SavingsGoal.objects.none(),
        required=False,
        help_text="Required when money moves into Savings.",
    )

    class Meta:
        model = Transfer
        fields = [
            "amount",
            "date",
            "source",
            "destination",
            "savings_destination_goal",
            "notes",
        ]

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, household=household, **kwargs)
        accounts = Account.objects.filter(archived_at=None, **household_filter(household))
        self.fields["source"].queryset = accounts
        self.fields["destination"].queryset = accounts
        self.fields["savings_destination_goal"].queryset = SavingsGoal.objects.filter(
            status=SavingsGoal.Status.ACTIVE,
            is_system=False,
            **household_filter(household),
        ).order_by("priority", "name")

    def clean(self):
        data = super().clean() or {}
        if (
            data.get("destination")
            and data["destination"].type == Account.Type.SAVINGS
            and not data.get("savings_destination_goal")
        ):
            self.add_error("savings_destination_goal", "Choose the goal receiving this money.")
        return data


class IncomeForm(NamedFieldMessages, UnusualDateGuard, forms.Form):
    date = forms.DateField(
        initial=timezone.localdate, widget=forms.DateInput(attrs={"type": "date"})
    )
    source = forms.CharField(
        max_length=160, widget=forms.TextInput(attrs={"list": "income-sources"})
    )
    amount = MoneyField(
        min_value=Decimal("0.01"),
    )
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    is_irregular = forms.BooleanField(required=False, label="This is irregular income")
    is_paycheck = forms.BooleanField(
        required=False,
        label="This is the regular paycheck",
        help_text="The pay period it funds starts on this date.",
    )
    savings_destination_goal = forms.ModelChoiceField(
        label="Savings destination goal",
        queryset=SavingsGoal.objects.none(),
        required=False,
        help_text="Required when any income is allocated to Savings.",
    )

    def __init__(self, *args, accounts=(), household=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.household = household
        self.accounts = list(accounts)
        for account in self.accounts:
            self.fields[f"allocation_{account.pk}"] = MoneyField(
                label=f"{account.name} allocation",
                required=False,
                initial=0,
                min_value=0,
            )
        self.fields["savings_destination_goal"].queryset = SavingsGoal.objects.filter(
            status=SavingsGoal.Status.ACTIVE,
            is_system=False,
            **household_filter(household),
        ).order_by("priority", "name")

    def _apply_default_split(self, data):
        """No-JS fallback: fill blank allocations from the source's previous split."""
        fields = [f"allocation_{account.pk}" for account in self.accounts]
        if any(data.get(name) for name in fields):
            return
        if not data.get("source") or not data.get("amount") or self.errors.get("amount"):
            return
        split = default_income_split(self.household, data["source"], data["amount"])
        if not split or any(account not in self.accounts for account in split):
            return
        for account, value in split.items():
            data[f"allocation_{account.pk}"] = value

    def clean(self):
        data = super().clean() or {}
        self._apply_default_split(data)
        savings_total = sum(
            (
                data.get(f"allocation_{account.pk}") or 0
                for account in self.accounts
                if account.type == Account.Type.SAVINGS
            ),
            0,
        )
        if savings_total > 0 and not data.get("savings_destination_goal"):
            self.add_error("savings_destination_goal", "Choose the goal receiving this income.")
        if data.get("is_paycheck") and data.get("is_irregular"):
            self.add_error("is_paycheck", "A regular paycheck cannot also be irregular income.")
        return data


class SavingsGoalChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.name} — ${obj.current_amount:,.2f} available"


class SavingsSourceAllocationForm(NamedFieldMessages, forms.Form):
    goal = SavingsGoalChoiceField(queryset=SavingsGoal.objects.none())
    amount = MoneyField(
        label="Dollars from this goal",
        min_value=Decimal("0.01"),
    )
    fulfills_goal = forms.BooleanField(
        required=False,
        label="This spending fulfills the goal",
    )

    def __init__(self, *args, household=None, current_goal_ids=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["goal"].queryset = (
            SavingsGoal.objects.filter(
                Q(entries__amount__gt=0) | Q(pk__in=current_goal_ids),
                Q(status__in=[SavingsGoal.Status.ACTIVE, SavingsGoal.Status.COMPLETED])
                | Q(pk__in=current_goal_ids),
                household=household,
            )
            .distinct()
            .order_by("status", "priority", "name")
        )


SavingsSourceAllocationFormSet = forms.formset_factory(
    SavingsSourceAllocationForm, extra=3, max_num=20, validate_max=True
)
SavingsSourceAllocationEditFormSet = forms.formset_factory(
    SavingsSourceAllocationForm, extra=0, max_num=20, validate_max=True
)


class SavingsGoalForm(NamedFieldMessages, forms.ModelForm):
    initial_amount = MoneyField(
        label="Initial allocated amount",
        required=False,
        initial=Decimal("0.00"),
        min_value=0,
    )
    source_goal = SavingsGoalChoiceField(
        label="Move the initial amount from",
        queryset=SavingsGoal.objects.none(),
        required=False,
    )

    class Meta:
        model = SavingsGoal
        fields = ["name", "target_amount", "priority"]
        field_classes = {"target_amount": MoneyField}
        widgets = {
            "priority": forms.NumberInput(attrs={"inputmode": "numeric", "min": "1"}),
        }

    def __init__(self, *args, household=None, editing=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.household = household
        self.fields["source_goal"].queryset = (
            SavingsGoal.objects.filter(household=household, entries__amount__gt=0)
            .distinct()
            .order_by("status", "priority", "name")
        )
        if editing:
            self.fields.pop("initial_amount")
            self.fields.pop("source_goal")

    def clean(self):
        data = super().clean() or {}
        initial = data.get("initial_amount") or Decimal("0.00")
        if initial > 0 and not data.get("source_goal"):
            self.add_error("source_goal", "Choose where the initial allocation comes from.")
        return data


class SavingsMoveForm(NamedFieldMessages, forms.Form):
    date = forms.DateField(
        initial=timezone.localdate, widget=forms.DateInput(attrs={"type": "date"})
    )
    amount = MoneyField(
        min_value=Decimal("0.01"),
    )
    destination_goal = forms.ModelChoiceField(
        label="Move to another goal",
        queryset=SavingsGoal.objects.none(),
        required=False,
    )
    destination_account = forms.ModelChoiceField(
        label="Or move to another account",
        queryset=Account.objects.none(),
        required=False,
    )
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["destination_goal"].queryset = SavingsGoal.objects.filter(
            household=household,
            status=SavingsGoal.Status.ACTIVE,
            is_system=False,
        ).order_by("priority", "name")
        self.fields["destination_account"].queryset = Account.objects.filter(
            household=household, archived_at=None
        ).exclude(type=Account.Type.SAVINGS)

    def clean(self):
        data = super().clean() or {}
        if bool(data.get("destination_goal")) == bool(data.get("destination_account")):
            raise forms.ValidationError("Choose either one destination goal or one account.")
        return data


class OutlookSettingsForm(forms.Form):
    nonessential_categories = forms.ModelMultipleChoiceField(
        label="Non-essential spending categories",
        queryset=Category.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Transactions can override this category-level classification.",
    )
    limited_rate = forms.DecimalField(
        label="Limited cut percentage", min_value=0, max_value=100, decimal_places=1
    )
    moderate_rate = forms.DecimalField(
        label="Moderate cut percentage", min_value=0, max_value=100, decimal_places=1
    )
    significant_rate = forms.DecimalField(
        label="Significant cut percentage", min_value=0, max_value=100, decimal_places=1
    )

    def __init__(self, *args, household=None, rates=None, **kwargs):
        super().__init__(*args, **kwargs)
        categories = Category.objects.filter(
            household=household, type=Category.Type.SPENDING, archived_at=None
        ).order_by("name")
        self.fields["nonessential_categories"].queryset = categories
        if not self.is_bound:
            self.initial.update(
                {
                    "nonessential_categories": categories.filter(is_essential=False),
                    "limited_rate": (rates or {}).get("limited", Decimal("10")),
                    "moderate_rate": (rates or {}).get("moderate", Decimal("25")),
                    "significant_rate": (rates or {}).get("significant", Decimal("50")),
                }
            )

    def clean(self):
        data = super().clean() or {}
        limited = data.get("limited_rate")
        moderate = data.get("moderate_rate")
        significant = data.get("significant_rate")
        if (
            limited is not None
            and moderate is not None
            and significant is not None
            and not limited <= moderate <= significant
        ):
            raise forms.ValidationError(
                "Limited, moderate, and significant percentages must increase in that order."
            )
        return data


class HistoryTransactionEditForm(NamedFieldMessages, UnusualDateGuard, forms.Form):
    transaction_type = forms.CharField(label="Type", disabled=True)
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    description = forms.CharField(max_length=160, required=False)
    amount = MoneyField(
        min_value=Decimal("0.01"),
    )
    split_transaction = forms.BooleanField(
        required=False,
        label="Split this transaction across categories",
        help_text="Add at least two category amounts that equal the total expense exactly.",
    )
    category = forms.ModelChoiceField(queryset=Category.objects.none(), required=False)
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    is_essential_override = forms.TypedChoiceField(
        label="Spending classification",
        required=False,
        choices=(
            ("", "Use category default"),
            ("true", "Essential"),
            ("false", "Non-essential"),
        ),
        coerce=lambda value: value == "true",
        empty_value=None,
    )
    savings_destination_goal = forms.ModelChoiceField(
        label="Savings destination goal",
        queryset=SavingsGoal.objects.none(),
        required=False,
    )
    bill = forms.ModelChoiceField(
        label="Apply payment to bill (optional)",
        queryset=RecurringBill.objects.none(),
        required=False,
        help_text=(
            "Choose the bill being paid. The matching monthly bill is selected "
            "automatically from the transaction date."
        ),
    )

    def __init__(
        self,
        *args,
        transaction_type,
        allocations=(),
        expense_splits=(),
        household=None,
        category=None,
        account=None,
        bill=None,
        savings_destination_goal=None,
        destination_is_savings=False,
        original_date=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.household = household
        self.original_date = original_date
        self.transaction_type = transaction_type
        self.account = account
        self.fields["transaction_type"].initial = transaction_type
        self.allocations = list(allocations)
        self.expense_splits = list(expense_splits)
        self.fields["split_transaction"].initial = bool(self.expense_splits)
        if transaction_type == "Transfer":
            self.fields["description"].label = "Notes"
            self.fields.pop("notes")
        if transaction_type != "Expense":
            self.fields.pop("split_transaction")
            self.fields.pop("bill")
            self.fields.pop("is_essential_override")
        else:
            self.fields["bill"].initial = bill
            self.fields["bill"].queryset = RecurringBill.objects.filter(
                Q(active=True) | Q(pk=bill.pk if bill else None),
                **household_filter(household),
            ).order_by("name")
        if category is None:
            self.fields.pop("category")
        else:
            self.fields["category"].required = transaction_type != "Expense"
            self.fields["category"].initial = category
            category_filters = (
                {"type__in": REFUND_CATEGORY_TYPES} if transaction_type == "Refund" else {}
            )
            self.fields["category"].queryset = Category.objects.filter(
                Q(archived_at=None) | Q(pk=category.pk),
                **category_filters,
                **household_filter(household),
            ).order_by("name")
        if destination_is_savings:
            current_pk = savings_destination_goal.pk if savings_destination_goal else None
            self.fields["savings_destination_goal"].initial = savings_destination_goal
            self.fields["savings_destination_goal"].queryset = SavingsGoal.objects.filter(
                Q(status=SavingsGoal.Status.ACTIVE) | Q(pk=current_pk),
                household=household,
                is_system=False,
            ).order_by("priority", "name")
        else:
            self.fields.pop("savings_destination_goal")
        for allocation in self.allocations:
            self.fields[f"allocation_{allocation.account_id}"] = MoneyField(
                label=f"{allocation.account.name} allocation",
                min_value=0,
                initial=allocation.amount,
            )

    def clean_description(self):
        description = self.cleaned_data["description"].strip()
        if self.transaction_type == "Income" and not description:
            raise forms.ValidationError("Enter an income description.")
        return description

    def clean(self):
        data = super().clean() or {}
        category = data.get("category")
        if self.transaction_type == "Expense":
            bill = data.get("bill")
            if bill and (
                not self.account
                or self.account.type
                not in {
                    Account.Type.BILLS,
                    Account.Type.SPENDING,
                }
            ):
                self.add_error("bill", "Pay a bill from the Bills or Spending account.")
            if data.get("split_transaction"):
                return data
            if not category:
                self.add_error("category", "Choose a category.")
            elif bill and category.type != Category.Type.BILL:
                self.add_error("category", "Choose a Bills category for this bill payment.")
            elif (
                not bill
                and self.account
                and not category_allowed_for_account(self.account, category)
            ):
                self.add_error("category", "Category does not match the transaction account.")
        if self.allocations and data.get("amount") is not None:
            allocated = sum(
                (data.get(f"allocation_{item.account_id}") or 0 for item in self.allocations),
                0,
            )
            if allocated != data["amount"]:
                self.add_error(None, "Account allocations must add up exactly to the income total.")
        if "savings_destination_goal" in self.fields and not data.get("savings_destination_goal"):
            self.add_error("savings_destination_goal", "Choose the goal receiving this money.")
        return data


class BudgetForm(NamedFieldMessages, forms.ModelForm):
    class Meta:
        model = PayPeriodBudget
        fields = ["spending_target", "expected_paycheck"]
        labels = {
            "spending_target": "Spending target for this pay period",
            "expected_paycheck": "Expected paycheck for this pay period",
        }
        help_texts = {
            "spending_target": "Net Spending-category purchases you plan for this period.",
            "expected_paycheck": (
                "Used for the next-payday forecast until the paycheck is recorded. "
                "New periods copy the previous period's values."
            ),
        }
        field_classes = {"spending_target": MoneyField, "expected_paycheck": MoneyField}

    def clean(self):
        data = super().clean() or {}
        for name in ("spending_target", "expected_paycheck"):
            if data.get(name) is not None and data[name] < 0:
                self.add_error(name, "Enter zero or more.")
        return data


class RecurringBillForm(NamedFieldMessages, forms.ModelForm):
    class Meta:
        model = RecurringBill
        fields = [
            "name",
            "default_amount",
            "due_day",
            "every_months",
            "category",
            "account",
            "effective_start",
            "effective_end",
            "notes",
            "active",
        ]
        labels = {
            "default_amount": "Usual monthly amount",
            "due_day": "Day of month due",
            "account": "Payment account",
            "effective_start": "First month",
            "effective_end": "Last month (optional)",
        }
        field_classes = {"default_amount": MoneyField}
        widgets = {
            "due_day": forms.NumberInput(attrs={"inputmode": "numeric", "min": 1, "max": 31}),
            "effective_start": forms.DateInput(attrs={"type": "date"}),
            "effective_end": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["every_months"].required = False
        self.fields["category"].queryset = Category.objects.filter(
            type=Category.Type.BILL, archived_at=None, **household_filter(household)
        )
        self.fields["account"].queryset = Account.objects.filter(
            type__in=(Account.Type.BILLS, Account.Type.SPENDING),
            archived_at=None,
            **household_filter(household),
        )

    def clean(self):
        data = super().clean() or {}
        if not data.get("every_months"):
            data["every_months"] = RecurringBill.Frequency.MONTHLY
        category = data.get("category")
        account = data.get("account")
        if category and category.type != Category.Type.BILL:
            self.add_error("category", "Choose a Bill category.")
        if account and account.type not in {Account.Type.BILLS, Account.Type.SPENDING}:
            self.add_error("account", "Choose a Bills or Spending account.")
        if data.get("effective_end") and data.get("effective_start"):
            if data["effective_end"] < data["effective_start"]:
                self.add_error("effective_end", "Last month cannot precede the first month.")
        return data


class BillOccurrenceAmountForm(NamedFieldMessages, forms.ModelForm):
    bill_name = forms.CharField(
        label="Bill name",
        max_length=160,
        help_text="Changing the name updates this recurring bill in every month.",
    )
    bill_every_months = forms.TypedChoiceField(
        label="How often",
        choices=RecurringBill.Frequency.choices,
        coerce=int,
        required=False,
        empty_value=None,
        help_text="Changing this removes future unpaid months that no longer fit.",
    )
    bill_first_month = forms.DateField(
        label="First due month",
        required=False,
        widget=forms.DateInput(attrs={"type": "month"}, format="%Y-%m"),
        input_formats=["%Y-%m", "%Y-%m-%d"],
        help_text="The schedule counts from this month (only used when How often changes).",
    )
    update_future_due_day = forms.BooleanField(
        required=False,
        label="Use this due day for future months too",
        help_text="Leave unchecked to change only the selected month.",
    )
    update_future_default = forms.BooleanField(
        required=False,
        label="Use this amount for future months too",
        help_text="Leave unchecked to change only the selected month.",
    )
    category = forms.ModelChoiceField(
        label="Category",
        queryset=Category.objects.none(),
        required=False,
        help_text="This month and future months use this category; prior months stay unchanged.",
    )
    account = forms.ModelChoiceField(
        label="Payment account",
        queryset=Account.objects.none(),
        required=False,
        help_text="This month and future months use this account; prior months stay unchanged.",
    )

    class Meta:
        model = BillOccurrence
        fields = ["category", "account", "due_date", "confirmed_amount", "notes"]
        labels = {
            "due_date": "Due date for this month",
            "confirmed_amount": "Amount for this month",
        }
        field_classes = {"confirmed_amount": MoneyField}
        widgets = {
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "confirmed_amount": MoneyInput(attrs={"data-autofocus": True}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order_fields(
            [
                "bill_name",
                "category",
                "account",
                "due_date",
                "confirmed_amount",
                "notes",
                "update_future_due_day",
                "update_future_default",
                "bill_every_months",
                "bill_first_month",
            ]
        )
        self.original_due_date = self.instance.due_date
        self.fields["bill_name"].initial = self.instance.bill.name
        self.fields["bill_every_months"].initial = self.instance.bill.every_months
        self.fields["bill_first_month"].initial = self.instance.due_date.replace(day=1)
        home = self.instance.bill.household
        self.fields["category"].queryset = Category.objects.filter(
            household=home, type=Category.Type.BILL, archived_at=None
        )
        self.fields["account"].queryset = Account.objects.filter(
            household=home,
            type__in=(Account.Type.BILLS, Account.Type.SPENDING),
            archived_at=None,
        )
        self.fields["category"].initial = self.instance.effective_category
        self.fields["account"].initial = self.instance.effective_account

    def clean_bill_name(self):
        name = self.cleaned_data["bill_name"].strip()
        if (
            RecurringBill.objects.filter(household=self.instance.bill.household, name=name)
            .exclude(pk=self.instance.bill_id)
            .exists()
        ):
            raise forms.ValidationError("A bill with this name already exists.")
        return name

    def clean_due_date(self):
        due_date = self.cleaned_data["due_date"]
        if (due_date.year, due_date.month) != (
            self.original_due_date.year,
            self.original_due_date.month,
        ):
            raise forms.ValidationError("Choose a date in the same month as this bill.")
        return due_date

    def clean_confirmed_amount(self):
        amount = self.cleaned_data.get("confirmed_amount")
        if amount is None or amount < 0:
            raise forms.ValidationError("Enter an amount of zero or more.")
        return amount

    def clean(self):
        data = super().clean() or {}
        data["category"] = data.get("category") or self.instance.effective_category
        data["account"] = data.get("account") or self.instance.effective_account
        if data.get("update_future_default") and data.get("confirmed_amount") == 0:
            self.add_error(
                "update_future_default",
                "A zero amount can apply only to this month. Leave this option unchecked.",
            )
        return data


class BillOccurrenceSkipForm(forms.Form):
    reason = forms.CharField(
        label="Reason for skipping",
        max_length=255,
        widget=forms.TextInput(
            attrs={"placeholder": "Why is this bill not due?", "aria-label": "Reason for skipping"}
        ),
    )

    def clean_reason(self):
        reason = self.cleaned_data["reason"].strip()
        if not reason:
            raise forms.ValidationError("Enter a reason for skipping this bill.")
        return reason


PAGE_LABEL_DEFAULTS = {
    "dashboard": "Home",
    "transactions": "History",
    "bills": "Bills",
    "budgets": "Budgets",
    "savings": "Savings",
    "reports": "Outlook",
    "habits": "Habits",
    "debts": "Debts",
    "accounts": "Accounts",
    "categories": "Categories",
    "application_admin": "Admin",
    "docs": "Help",
}

# A page's heading is its menu name unless an owner renames it.
PAGE_TITLE_DEFAULTS = dict(PAGE_LABEL_DEFAULTS)

# The pages that can be renamed, as people see them in the menus (Admin → Page names).
PAGE_CHOICES = (
    ("dashboard", "Home"),
    ("transactions", "History"),
    ("bills", "Bills"),
    ("budgets", "Budgets"),
    ("savings", "Savings"),
    ("reports", "Outlook"),
    ("habits", "Habits"),
    ("debts", "Debts"),
    ("accounts", "Accounts"),
    ("categories", "Categories"),
    ("application_admin", "Admin"),
    ("docs", "Help"),
)


class PageNameForm(forms.Form):
    """Rename one page: its menu label and its heading. A blank name restores the default."""

    page = forms.ChoiceField(label="Page", choices=PAGE_CHOICES)
    name = forms.CharField(
        label="New name",
        max_length=40,
        required=False,
        help_text="Shown in the menus and as the page heading. Leave blank for the original name.",
    )

    def clean_name(self):
        return " ".join(self.cleaned_data["name"].split())


class QuickActionForm(forms.Form):
    DESTINATIONS = (
        ("expense_add", "Add expense"),
        ("income_add", "Add income"),
        ("transfer_add", "Add transfer"),
        ("refund_add", "Add refund"),
        ("bills", "Bills"),
        ("bill_add", "Add bill"),
        ("budgets", "Budgets"),
        ("savings", "Savings"),
        ("transactions", "History"),
        ("reports", "Outlook"),
    )

    label = forms.CharField(max_length=40)
    destination = forms.ChoiceField(choices=DESTINATIONS)


class BudgetUserCreationForm(UserCreationForm):
    email = forms.EmailField(required=False)
    is_staff = forms.BooleanField(
        required=False,
        label="Allow access to this admin page",
        help_text="Only grant this to people who should manage users and application settings.",
    )

    class Meta:
        model = get_user_model()
        fields = ("username", "email", "first_name", "last_name")


class BudgetPasswordResetForm(SetPasswordForm):
    """Staff-entered password reset using Django's configured validators."""

    pass


class UserAccessChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        full_name = obj.get_full_name().strip()
        role = "Administrator" if obj.is_staff or obj.is_superuser else "Standard user"
        return f"{obj.username}{f' — {full_name}' if full_name else ''} ({role})"


class ProfileAccessForm(forms.Form):
    users = UserAccessChoiceField(
        queryset=get_user_model().objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, profile, active_profile, actor, **kwargs):
        super().__init__(*args, **kwargs)
        self.profile = profile
        self.active_profile = active_profile
        self.actor = actor
        self.fields["users"].queryset = get_user_model().objects.order_by("username")
        self.fields["users"].initial = profile.memberships.values_list("user_id", flat=True)

    def clean_users(self):
        users = self.cleaned_data["users"]
        selected_ids = {user.pk for user in users}
        if self.profile == self.active_profile and self.actor.pk not in selected_ids:
            raise forms.ValidationError(
                "You cannot remove your own access from the active profile."
            )
        if not any(user.is_staff or user.is_superuser for user in users):
            raise forms.ValidationError(
                "At least one administrator must retain access to this profile."
            )
        return users


class ConfirmDeletionForm(forms.Form):
    confirmation = forms.CharField(max_length=150)

    def __init__(self, *args, expected_value, **kwargs):
        super().__init__(*args, **kwargs)
        self.expected_value = expected_value
        self.fields["confirmation"].label = f'Type "{expected_value}" to confirm'
        self.fields["confirmation"].widget.attrs.update(
            {"autocomplete": "off", "autocapitalize": "off"}
        )

    def clean_confirmation(self):
        confirmation = self.cleaned_data["confirmation"].strip()
        if confirmation != self.expected_value:
            raise forms.ValidationError("The confirmation text does not match.")
        return confirmation


class ProfileAuthenticationForm(AuthenticationForm):
    profile = forms.ModelChoiceField(
        queryset=Household.objects.order_by("name"),
        empty_label=None,
        help_text="Choose the budget profile you want to open.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # First run: no profile exists yet, so an administrator signs in without one and
        # creates the first profile in the app.
        self.first_run = not Household.objects.exists()
        if self.first_run:
            del self.fields["profile"]

    def clean(self):
        username = self.cleaned_data.get("username") or ""
        if self.request is not None:
            message = login_throttle.lock_message(self.request, username)
            if message:
                raise forms.ValidationError(message, code="locked")
        try:
            cleaned_data = super().clean()
        except forms.ValidationError as exc:
            if self.request is not None and exc.code == "invalid_login":
                login_throttle.record_failure(self.request, username)
            raise
        user = self.get_user()
        profile = cleaned_data.get("profile")
        if user and self.first_run:
            if not user.is_superuser:
                raise forms.ValidationError(
                    "No budget profile exists yet. An administrator must sign in first "
                    "and create one."
                )
        elif user and profile:
            if user.is_superuser:
                HouseholdMembership.objects.get_or_create(household=profile, user=user)
            elif not HouseholdMembership.objects.filter(household=profile, user=user).exists():
                raise forms.ValidationError(
                    "This user does not have access to the selected profile."
                )
        return cleaned_data


class BudgetProfileForm(forms.ModelForm):
    class Meta:
        model = Household
        fields = ("name",)

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if Household.objects.filter(name__iexact=name).exists():
            raise forms.ValidationError("A profile with this name already exists.")
        return name
