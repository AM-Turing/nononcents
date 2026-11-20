"""Money may be typed as 1234.56, 1,234.56 or $1,234.56 and is shown with commas."""

import importlib
import inspect
import pkgutil
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django import forms
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

import budget
from budget.forms import IncomeForm
from budget.models import (
    Account,
    Category,
    Expense,
    Household,
    HouseholdMembership,
    Income,
    IncomeAllocation,
)
from budget.money import INVALID_AMOUNT, MoneyField, MoneyInput, normalize_money_text
from budget.services import create_expense

TODAY = timezone.localdate()


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("1234.56", "1234.56"),
        ("1,234.56", "1234.56"),
        ("$1,234.56", "1234.56"),
        (" $ 12,345,678.9 ", "12345678.9"),
        ("12", "12"),
        (".5", ".5"),
        ("-1,234.56", "-1234.56"),
        ("-$1,234.56", "-1234.56"),
        ("$-1,234.56", "-1234.56"),
    ],
)
def test_amounts_are_normalised(typed, stored):
    assert normalize_money_text(typed) == stored


@pytest.mark.parametrize(
    "typed",
    ["12,50", "1,2345", "1,23,456", "1,,234", "$", "--5", "-$-5", "12.34.56", "1 234", "abc"],
)
def test_ambiguous_or_malformed_amounts_are_rejected(typed):
    with pytest.raises(forms.ValidationError) as error:
        MoneyField().clean(typed)
    assert error.value.messages == [INVALID_AMOUNT]


def test_european_decimal_comma_is_never_read_as_thousands():
    with pytest.raises(forms.ValidationError) as error:
        MoneyField().clean("12,50")
    assert error.value.messages == [INVALID_AMOUNT]


def test_money_field_keeps_two_decimal_and_minimum_rules():
    assert MoneyField().clean("$1,234.5") == Decimal("1234.5")
    with pytest.raises(forms.ValidationError):
        MoneyField().clean("1,234.567")
    with pytest.raises(forms.ValidationError):
        MoneyField(min_value=Decimal("0.01")).clean("0")


def test_money_input_is_text_with_decimal_keypad():
    html = MoneyField().widget.render("amount", Decimal("1234.50"))
    assert 'type="text"' in html
    assert 'inputmode="decimal"' in html
    assert 'value="1234.50"' in html
    assert "step=" not in html


def _form_classes():
    for module_info in pkgutil.iter_modules(budget.__path__):
        if module_info.name in ("tests", "migrations"):
            continue
        module = importlib.import_module(f"budget.{module_info.name}")
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, forms.BaseForm) and cls.__module__ == module.__name__:
                yield cls


def test_every_two_decimal_form_field_is_a_money_field():
    plain = [
        f"{cls.__module__}.{cls.__name__}.{name}"
        for cls in _form_classes()
        for name, field in cls.base_fields.items()
        if isinstance(field, forms.DecimalField)
        and field.decimal_places == 2
        and not (isinstance(field, MoneyField) and isinstance(field.widget, MoneyInput))
    ]
    assert plain == []


@pytest.fixture
def setup(client, db):
    user = get_user_model().objects.create_user("money-user", password="a-secure-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    spending = Account.objects.create(household=home, name="Spending", type="spending")
    bills = Account.objects.create(household=home, name="Bills", type="bills")
    groceries = Category.objects.create(household=home, name="Groceries", type="spending")
    return {
        "client": client,
        "user": user,
        "home": home,
        "spending": spending,
        "bills": bills,
        "groceries": groceries,
    }


def _expense(setup, amount):
    return setup["client"].post(
        reverse("expense_add"),
        {
            "amount": amount,
            "date": TODAY.isoformat(),
            "account": setup["spending"].pk,
            "category": setup["groceries"].pk,
            "merchant": f"Store {amount}",
            "notes": "",
            "bill": "",
            "form_token": str(uuid4()),
        },
    )


@pytest.mark.parametrize("typed", ["1234.56", "1,234.56", "$1,234.56"])
def test_expense_saves_the_same_amount_however_it_is_typed(setup, typed):
    response = _expense(setup, typed)
    assert response.status_code == 302
    assert Expense.objects.get(merchant=f"Store {typed}").amount == Decimal("1234.56")


def test_expense_with_decimal_comma_is_refused_and_nothing_saved(setup):
    response = _expense(setup, "12,50")
    assert response.status_code == 200
    assert INVALID_AMOUNT in response.content.decode()
    assert not Expense.objects.exists()


def test_income_allocations_accept_commas(setup):
    data = {
        "date": TODAY.isoformat(),
        "source": "Acme",
        "amount": "$2,000.00",
        "notes": "",
        "form_token": str(uuid4()),
        f"allocation_{setup['spending'].pk}": "500",
        f"allocation_{setup['bills'].pk}": "1,500.00",
    }
    response = setup["client"].post(reverse("income_add"), data)
    assert response.status_code == 302
    income = Income.objects.get()
    assert income.amount == Decimal("2000.00")
    shares = {a.account_id: a.amount for a in IncomeAllocation.objects.filter(income=income)}
    assert shares == {
        setup["spending"].pk: Decimal("500.00"),
        setup["bills"].pk: Decimal("1500.00"),
    }


def test_dynamic_allocation_fields_are_money_fields(setup):
    form = IncomeForm(accounts=[setup["spending"]], household=setup["home"])
    assert isinstance(form.fields[f"allocation_{setup['spending'].pk}"], MoneyField)


def test_pages_show_amounts_with_thousands_commas(setup):
    create_expense(
        household=setup["home"],
        actor=setup["user"],
        entry_date=TODAY - timedelta(days=1),
        account=setup["spending"],
        category=setup["groceries"],
        amount=Decimal("8925.31"),
        merchant="Big purchase",
    )
    history = setup["client"].get(reverse("transactions")).content.decode()
    assert "8,925.31" in history
    assert "8925.31" not in history.replace('value="8925.31"', "")
