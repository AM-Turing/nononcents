from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

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
from budget.services import create_expense, create_income, default_income_split

TODAY = timezone.localdate()


@pytest.fixture
def home_setup(client, db):
    user = get_user_model().objects.create_user("entry-user", password="a-secure-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    spending = Account.objects.create(household=home, name="Spending", type="spending")
    bills = Account.objects.create(household=home, name="Bills", type="bills")
    other = Account.objects.create(household=home, name="Other", type="other")
    groceries = Category.objects.create(household=home, name="Groceries", type="spending")
    fuel = Category.objects.create(household=home, name="Fuel", type="spending")
    return {
        "client": client,
        "user": user,
        "home": home,
        "spending": spending,
        "bills": bills,
        "other": other,
        "groceries": groceries,
        "fuel": fuel,
    }


def spend(setup, merchant, day=None, category="groceries", account="spending", home=None):
    return create_expense(
        household=home or setup["home"],
        actor=setup["user"],
        entry_date=day or TODAY,
        account=setup[account],
        category=setup[category],
        amount=Decimal("5.00"),
        merchant=merchant,
    )


def expense_post(setup, **extra):
    return {
        "amount": "12.00",
        "date": TODAY.isoformat(),
        "account": setup["spending"].pk,
        "category": setup["groceries"].pk,
        "merchant": "Cafe",
        "notes": "",
        "bill": "",
        "form_token": str(uuid4()),
        **extra,
    }


# --- Merchant memory ------------------------------------------------------------------------


def test_merchant_datalist_dedupes_case(home_setup):
    for name in ("corner market", "Corner Market", "Corner Market", "Fuel Stop", "  ", ""):
        spend(home_setup, name)
    page = home_setup["client"].get(reverse("expense_add"))
    assert page.context["merchant_names"] == ["Corner Market", "Fuel Stop"]
    html = page.content.decode()
    assert '<datalist id="merchant-options">' in html
    assert 'list="merchant-options"' in html
    assert html.count('<option value="Corner Market">') == 1
    assert "corner market</option>" not in html


def test_merchant_datalist_is_alphabetical_ignoring_case(home_setup):
    # Use counts no longer decide the order: zebra x3, Mango x2, apple x1 -> A to Z.
    for name in (
        "Zebra Mart",
        "Zebra Mart",
        "Zebra Mart",
        "Mango Cafe",
        "Mango Cafe",
        "apple stand",
    ):
        spend(home_setup, name)
    names = home_setup["client"].get(reverse("expense_add")).context["merchant_names"]
    assert names == ["apple stand", "Mango Cafe", "Zebra Mart"]


def test_merchant_datalist_keeps_the_most_used_when_capped(home_setup, monkeypatch):
    from budget import views

    monkeypatch.setattr(views, "MERCHANT_CAP", 2)
    for name in ("Bakery", "Bakery", "Yarn Shop", "Yarn Shop", "Antiques"):
        spend(home_setup, name)
    # The two most used are kept (Antiques, used once, is dropped), then listed A to Z.
    names = home_setup["client"].get(reverse("expense_add")).context["merchant_names"]
    assert names == ["Bakery", "Yarn Shop"]


def test_merchant_datalist_is_capped(home_setup):
    for index in range(305):
        spend(home_setup, f"Shop {index:03d}")
    names = home_setup["client"].get(reverse("expense_add")).context["merchant_names"]
    assert len(names) == 300


def test_merchant_map_uses_most_recent_expense_and_excludes_other_households(home_setup):
    spend(home_setup, "Corner Market", TODAY - timedelta(days=5), "groceries", "spending")
    spend(home_setup, "CORNER market", TODAY - timedelta(days=1), "fuel", "other")
    stranger = Household.objects.create(name="Elsewhere")
    account = Account.objects.create(household=stranger, name="Spending", type="spending")
    category = Category.objects.create(household=stranger, name="Secret", type="spending")
    create_expense(
        household=stranger,
        actor=home_setup["user"],
        entry_date=TODAY,
        account=account,
        category=category,
        amount=Decimal("1.00"),
        merchant="Secret Shop",
    )
    page = home_setup["client"].get(reverse("expense_add"))
    assert page.context["merchant_map"] == {
        "corner market": {"category": home_setup["fuel"].pk, "account": home_setup["other"].pk}
    }
    assert "Secret Shop" not in page.content.decode()
    assert 'id="merchant-map"' in page.content.decode()


def test_refund_form_also_gets_merchant_memory(home_setup):
    spend(home_setup, "Corner Market")
    page = home_setup["client"].get(reverse("refund_add"))
    assert page.context["merchant_names"] == ["Corner Market"]
    assert 'list="merchant-options"' in page.content.decode()


# --- Remembered date and account ------------------------------------------------------------


def test_saving_remembers_date_and_account_for_next_add(home_setup):
    client = home_setup["client"]
    day = TODAY - timedelta(days=3)
    client.post(
        reverse("expense_add"),
        expense_post(home_setup, date=day.isoformat(), account=home_setup["other"].pk),
    )
    assert client.session["last_entry_date"] == day.isoformat()
    assert client.session["last_entry_account"] == home_setup["other"].pk
    form = client.get(reverse("expense_add")).context["form"]
    assert form["date"].value() == day
    assert form["account"].value() == home_setup["other"].pk


def test_refund_save_also_remembers(home_setup):
    client = home_setup["client"]
    day = TODAY - timedelta(days=2)
    response = client.post(
        reverse("refund_add"),
        expense_post(home_setup, date=day.isoformat(), account=home_setup["other"].pk),
    )
    assert response.status_code == 302
    assert client.session["last_entry_date"] == day.isoformat()
    assert client.session["last_entry_account"] == home_setup["other"].pk


def test_remembered_date_older_than_seven_days_falls_back_to_today(home_setup):
    session = home_setup["client"].session
    session["last_entry_date"] = (TODAY - timedelta(days=8)).isoformat()
    session.save()
    assert home_setup["client"].get(reverse("expense_add")).context["form"]["date"].value() == (
        TODAY
    )
    session["last_entry_date"] = (TODAY - timedelta(days=7)).isoformat()
    session.save()
    assert home_setup["client"].get(reverse("expense_add")).context["form"]["date"].value() == (
        TODAY - timedelta(days=7)
    )


def test_archived_or_foreign_remembered_account_is_ignored(home_setup):
    client = home_setup["client"]
    session = client.session
    session["last_entry_account"] = home_setup["other"].pk
    session.save()
    home_setup["other"].archived_at = timezone.now()
    home_setup["other"].save()
    assert client.get(reverse("expense_add")).context["form"]["account"].value() is None
    stranger = Account.objects.create(
        household=Household.objects.create(name="Elsewhere"), name="Spending", type="spending"
    )
    session["last_entry_account"] = stranger.pk
    session.save()
    assert client.get(reverse("expense_add")).context["form"]["account"].value() is None


# --- Save / save and add another ------------------------------------------------------------


def test_save_goes_to_dashboard_and_add_another_returns_to_form(home_setup):
    client = home_setup["client"]
    saved = client.post(reverse("expense_add"), expense_post(home_setup))
    assert saved.url == reverse("dashboard")
    again = client.post(reverse("expense_add"), expense_post(home_setup, add_another="1"))
    assert again.url == reverse("expense_add")
    page = client.get(again.url)
    # Spending opens at 0.00; one 12.00 expense leaves -12.00.
    assert [str(m) for m in page.context["messages"]][0] == (
        "Expense of $12.00 saved to Spending. Resulting balance: -$12.00."
    )
    assert "Save and add another" in page.content.decode()
    refund = client.post(reverse("refund_add"), expense_post(home_setup, add_another="1"))
    assert refund.url == reverse("refund_add")


def test_expense_saved_url_redirects_to_the_form(home_setup):
    response = home_setup["client"].get(reverse("expense_saved"))
    assert response.status_code == 302
    assert response.url == reverse("expense_add")


def test_duplicate_token_redirects_to_the_same_destination(home_setup):
    client = home_setup["client"]
    for extra, destination in (({}, "dashboard"), ({"add_another": "1"}, "expense_add")):
        data = expense_post(home_setup, merchant=f"Dup {destination}", **extra)
        first = client.post(reverse("expense_add"), data)
        second = client.post(reverse("expense_add"), data)
        assert first.url == second.url == reverse(destination)
        assert Expense.objects.filter(merchant=f"Dup {destination}").count() == 1


# --- Recurring income defaults --------------------------------------------------------------


def income(setup, source, amounts, day=None, **flags):
    accounts = {"spending": setup["spending"], "bills": setup["bills"], "other": setup["other"]}
    return create_income(
        household=setup["home"],
        actor=setup["user"],
        income_date=day or TODAY,
        source=source,
        amount=sum(amounts.values(), Decimal("0")),
        allocations=[(accounts[key], value) for key, value in amounts.items()],
        **flags,
    )


def test_default_split_single_account_gets_everything(home_setup):
    income(home_setup, "Employer", {"bills": Decimal("1000.00")})
    split = default_income_split(home_setup["home"], "employer", Decimal("1234.56"))
    assert split == {home_setup["bills"]: Decimal("1234.56")}


def test_default_split_fixed_amounts_and_remainder(home_setup):
    income(
        home_setup,
        "Acme",
        {
            "spending": Decimal("500.00"),
            "bills": Decimal("1200.00"),
            "other": Decimal("300.00"),
        },
    )
    split = default_income_split(home_setup["home"], " ACME ", Decimal("2300.50"))
    assert split == {
        home_setup["spending"]: Decimal("500.00"),
        home_setup["other"]: Decimal("300.00"),
        home_setup["bills"]: Decimal("1500.50"),
    }
    assert sum(split.values()) == Decimal("2300.50")


def test_default_split_negative_remainder_falls_back_to_remainder_account(home_setup):
    income(home_setup, "Acme", {"spending": Decimal("500.00"), "bills": Decimal("1200.00")})
    assert default_income_split(home_setup["home"], "Acme", Decimal("400.00")) == {
        home_setup["bills"]: Decimal("400.00")
    }


def test_default_split_uses_most_recent_income_and_rejects_unknown(home_setup):
    income(home_setup, "Acme", {"spending": Decimal("100.00")}, TODAY - timedelta(days=30))
    income(home_setup, "acme", {"other": Decimal("100.00")}, TODAY - timedelta(days=2))
    assert default_income_split(home_setup["home"], "ACME", Decimal("50.00")) == {
        home_setup["other"]: Decimal("50.00")
    }
    assert default_income_split(home_setup["home"], "Nobody", Decimal("50.00")) is None
    assert default_income_split(home_setup["home"], "", Decimal("50.00")) is None
    assert default_income_split(home_setup["home"], "Acme", Decimal("0")) is None


def test_default_split_ignores_other_households_and_archived_accounts(home_setup):
    stranger = Household.objects.create(name="Elsewhere")
    account = Account.objects.create(household=stranger, name="Spending", type="spending")
    create_income(
        household=stranger,
        actor=home_setup["user"],
        income_date=TODAY,
        source="Elsewhere Pay",
        amount=Decimal("10.00"),
        allocations=[(account, Decimal("10.00"))],
    )
    assert default_income_split(home_setup["home"], "Elsewhere Pay", Decimal("10.00")) is None
    income(home_setup, "Acme", {"bills": Decimal("100.00")})
    home_setup["bills"].archived_at = timezone.now()
    home_setup["bills"].save()
    assert default_income_split(home_setup["home"], "Acme", Decimal("10.00")) is None


def test_income_form_embeds_sources_and_profiles(home_setup):
    income(
        home_setup,
        "Acme",
        {"spending": Decimal("500.00"), "bills": Decimal("1200.00")},
        is_paycheck=True,
    )
    page = home_setup["client"].get(reverse("income_add"))
    assert page.context["income_sources"] == ["Acme"]
    assert page.context["income_defaults"] == {
        "acme": {
            "is_paycheck": True,
            "is_irregular": False,
            "remainder_account": home_setup["bills"].pk,
            "fixed": {str(home_setup["spending"].pk): "500.00"},
        }
    }
    html = page.content.decode()
    assert '<datalist id="income-sources">' in html
    assert 'id="income-defaults"' in html


def income_post(setup, **extra):
    data = {
        "date": TODAY.isoformat(),
        "source": "Acme",
        "amount": "2000.00",
        "notes": "",
        "form_token": str(uuid4()),
    }
    for account in ("spending", "bills", "other"):
        data[f"allocation_{setup[account].pk}"] = ""
    return data | extra


def test_blank_allocations_get_the_default_split_server_side(home_setup):
    income(home_setup, "Acme", {"spending": Decimal("500.00"), "bills": Decimal("1200.00")})
    response = home_setup["client"].post(reverse("income_add"), income_post(home_setup))
    assert response.status_code == 302
    created = Income.objects.filter(amount=Decimal("2000.00")).get()
    shares = {a.account_id: a.amount for a in IncomeAllocation.objects.filter(income=created)}
    assert shares == {
        home_setup["spending"].pk: Decimal("500.00"),
        home_setup["bills"].pk: Decimal("1500.00"),
    }


def test_entered_allocations_are_never_altered(home_setup):
    income(home_setup, "Acme", {"spending": Decimal("500.00"), "bills": Decimal("1200.00")})
    data = income_post(home_setup, **{f"allocation_{home_setup['other'].pk}": "2000.00"})
    response = home_setup["client"].post(reverse("income_add"), data)
    assert response.status_code == 302
    created = Income.objects.filter(amount=Decimal("2000.00")).get()
    shares = {a.account_id: a.amount for a in IncomeAllocation.objects.filter(income=created)}
    assert shares == {home_setup["other"].pk: Decimal("2000.00")}


def test_short_allocations_show_the_existing_error_and_are_not_changed(home_setup):
    income(home_setup, "Acme", {"spending": Decimal("500.00"), "bills": Decimal("1200.00")})
    data = income_post(home_setup, **{f"allocation_{home_setup['bills'].pk}": "100.00"})
    response = home_setup["client"].post(reverse("income_add"), data)
    assert response.status_code == 200
    assert "Account allocations must add up exactly" in response.content.decode()
    assert Income.objects.filter(amount=Decimal("2000.00")).count() == 0
    form = response.context["form"]
    assert form[f"allocation_{home_setup['bills'].pk}"].value() == "100.00"
    assert form[f"allocation_{home_setup['spending'].pk}"].value() in ("", None)


def test_unknown_source_with_blank_allocations_is_rejected(home_setup):
    response = home_setup["client"].post(
        reverse("income_add"), income_post(home_setup, source="Brand New")
    )
    assert response.status_code == 200
    assert Income.objects.filter(source="Brand New").count() == 0


def test_income_form_clean_leaves_flags_alone(home_setup):
    income(home_setup, "Acme", {"bills": Decimal("100.00")}, is_paycheck=True)
    accounts = [home_setup["spending"], home_setup["bills"]]
    form = IncomeForm(
        {"date": TODAY, "source": "Acme", "amount": "10.00"},
        accounts=accounts,
        household=home_setup["home"],
    )
    assert form.is_valid()
    assert form.cleaned_data["is_paycheck"] is False
    assert form.cleaned_data[f"allocation_{home_setup['bills'].pk}"] == Decimal("10.00")
