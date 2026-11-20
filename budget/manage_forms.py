"""Forms for adding and editing accounts and categories."""

from django import forms

from .models import Account, Category
from .money import MoneyField

KIND_CHOICES = [
    (Category.Type.SPENDING, "Spending"),
    (Category.Type.BILL, "Bill"),
]


class AccountForm(forms.ModelForm):
    opening_balance = MoneyField(initial=0)

    class Meta:
        model = Account
        fields = ["name", "type", "opening_balance", "display_order", "statement_suffix"]
        labels = {
            "type": "Type",
            "display_order": "Display order",
            "statement_suffix": "Statement account ending (optional)",
        }

    def __init__(self, *args, household, locked: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.household = household
        self.locked = locked
        self.fields["display_order"].help_text = "Lower numbers are listed first."
        suffix = self.fields["statement_suffix"]
        suffix.help_text = (
            "The last 4 digits of this account's number as printed on your bank statement, so "
            "statement uploads go to the right account. Leave blank and the next upload asks."
        )
        suffix.widget.attrs.update(
            {"inputmode": "numeric", "autocomplete": "off", "pattern": "[0-9]{4}", "maxlength": 4}
        )
        if locked:
            self.fields["type"].disabled = True
            self.fields["type"].help_text = "Type can't change once the account has activity."
            self.fields["opening_balance"].disabled = True
            self.fields["opening_balance"].help_text = (
                "Opening balance can't change once the account has activity. "
                "Use a balance correction from Reconcile instead."
            )

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        others = Account.objects.filter(household=self.household, name__iexact=name)
        if self.instance.pk:
            others = others.exclude(pk=self.instance.pk)
        if others.exists():
            raise forms.ValidationError("You already have an account with this name.")
        return name

    def clean_statement_suffix(self):
        suffix = self.cleaned_data["statement_suffix"].strip()
        if not suffix:
            return ""
        if not (len(suffix) == 4 and suffix.isdigit()):
            raise forms.ValidationError("Enter exactly 4 digits, such as 1234, or leave it blank.")
        other = Account.objects.filter(household=self.household, statement_suffix=suffix)
        if self.instance.pk:
            other = other.exclude(pk=self.instance.pk)
        if (account := other.first()) is not None:
            raise forms.ValidationError(
                f"{account.name} already uses this ending. Clear it there first."
            )
        return suffix


class CategoryForm(forms.Form):
    name = forms.CharField(max_length=100)
    kind = forms.ChoiceField(choices=KIND_CHOICES)
    is_essential = forms.BooleanField(required=False, label="Essential")

    def __init__(
        self,
        *args,
        household,
        instance: Category | None = None,
        locked_kind: bool = False,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.household = household
        self.instance = instance
        if locked_kind:
            self.fields["kind"].disabled = True
            self.fields["kind"].help_text = "Kind can't change once the category is in use."
        if instance is not None:
            self.initial = {
                "name": instance.name,
                "kind": instance.type,
                "is_essential": instance.is_essential,
            }
        self.fields[
            "is_essential"
        ].help_text = "Essential items are left out of the cut-spending ideas."

    def clean(self):
        data = super().clean() or {}
        name, kind = data.get("name"), data.get("kind")
        if name and kind:
            name = " ".join(name.split())
            data["name"] = name
            others = Category.objects.filter(household=self.household, name__iexact=name, type=kind)
            if self.instance is not None:
                others = others.exclude(pk=self.instance.pk)
            if others.exists():
                self.add_error("name", "You already have a category with this name and kind.")
        return data
