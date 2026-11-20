"""Forms for saved habit views and the quick adjust bar."""

from decimal import Decimal

from django import forms

from .models import Category, HabitView
from .money import MoneyField


class HabitViewForm(forms.ModelForm):
    class Meta:
        model = HabitView
        fields = [
            "name",
            "small_threshold",
            "basis",
            "periods",
            "min_repeat",
            "categories",
            "include_essential",
        ]
        field_classes = {"small_threshold": MoneyField}
        widgets = {"categories": forms.CheckboxSelectMultiple}

    def __init__(self, *args, household, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.household = household
        self.user = user
        self.fields["categories"].queryset = Category.objects.filter(
            household=household, type=Category.Type.SPENDING, archived_at=None
        ).order_by("name")
        self.fields["categories"].required = False

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        clash = HabitView.objects.filter(household=self.household, name=name)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("You already have a view with this name.")
        return name

    def clean_small_threshold(self):
        value = self.cleaned_data["small_threshold"]
        if value <= 0:
            raise forms.ValidationError("Enter an amount greater than zero.")
        return value

    def clean_periods(self):
        value = self.cleaned_data["periods"]
        if not 1 <= value <= 24:
            raise forms.ValidationError("Choose between 1 and 24.")
        return value

    def clean_min_repeat(self):
        value = self.cleaned_data["min_repeat"]
        if value < 2:
            raise forms.ValidationError("A habit needs at least 2 purchases.")
        return value

    def save(self, commit=True):
        view = super().save(commit=False)
        view.household = self.household
        if not view.pk:
            view.created_by = self.user
        view.save()
        self.save_m2m()
        return view


class AdjustForm(forms.Form):
    """Quick look at other settings without saving a view."""

    small_threshold = MoneyField(min_value=Decimal("0.01"))
    basis = forms.ChoiceField(choices=HabitView.Basis.choices)
    periods = forms.IntegerField(min_value=1, max_value=24)
