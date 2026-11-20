"""Forms for the Debts page and category budget editing."""

from datetime import date
from decimal import Decimal

from django import forms
from django.utils import timezone

from .models import Category, Debt, DebtBalance, RecurringBill
from .money import MoneyField


class DebtForm(forms.ModelForm):
    bills = forms.ModelMultipleChoiceField(
        label="Bills that pay this debt",
        queryset=RecurringBill.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text=(
            "Their amounts make up the monthly payment. A bill already linked to another "
            "debt moves to this one when you save."
        ),
    )

    class Meta:
        model = Debt
        fields = ["name", "kind", "apr", "minimum_payment", "notes"]
        field_classes = {"minimum_payment": MoneyField}
        help_texts = {
            "minimum_payment": "Used as the monthly payment when no bill is linked.",
        }

    def __init__(self, *args, household, **kwargs):
        super().__init__(*args, **kwargs)
        self.household = household
        bills = RecurringBill.objects.filter(household=household, active=True)
        if self.instance.pk:
            linked = RecurringBill.objects.filter(household=household, debt=self.instance)
            bills = bills | linked
            self.fields["bills"].initial = list(linked.values_list("pk", flat=True))
        self.fields["bills"].queryset = bills.distinct().order_by("name")

    def clean_apr(self):
        apr = self.cleaned_data["apr"]
        if apr < 0 or apr > 100:
            raise forms.ValidationError("APR must be between 0 and 100.")
        return apr

    def clean_minimum_payment(self):
        value = self.cleaned_data["minimum_payment"]
        if value < 0:
            raise forms.ValidationError("The minimum payment cannot be negative.")
        return value

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        clash = Debt.objects.filter(household=self.household, name__iexact=name)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("You already have a debt with this name.")
        return name


class DebtBalanceForm(forms.ModelForm):
    class Meta:
        model = DebtBalance
        fields = ["as_of", "balance", "notes"]
        field_classes = {"balance": MoneyField}
        labels = {"as_of": "Balance as of", "balance": "Balance owed"}
        widgets = {"as_of": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}
        help_texts = {"as_of": "Entering a second balance for the same day replaces the first."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["as_of"].initial = timezone.localdate()

    def clean_as_of(self):
        value = self.cleaned_data["as_of"]
        if value > timezone.localdate():
            raise forms.ValidationError("The balance date cannot be in the future.")
        return value

    def clean_balance(self):
        value = self.cleaned_data["balance"]
        if value < 0:
            raise forms.ValidationError("A balance cannot be negative.")
        return value

    def validate_unique(self):
        # (debt, as_of) is unique; the view updates that day's row instead of erroring.
        pass


class ExtraForm(forms.Form):
    extra = MoneyField(label="Extra per month", min_value=Decimal("0"), required=False)

    def clean_extra(self):
        return self.cleaned_data["extra"] or Decimal("0.00")


class CategoryBudgetForm(forms.Form):
    category = forms.ModelChoiceField(queryset=Category.objects.none())
    amount = MoneyField(label="Budget per pay period", min_value=Decimal("0"))
    effective_from = forms.DateField(
        label="Effective from", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
    )

    def __init__(self, *args, household, default_date: date | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = Category.objects.filter(
            household=household, type=Category.Type.SPENDING, archived_at=None
        ).order_by("name")
        if default_date:
            self.fields["effective_from"].initial = default_date
