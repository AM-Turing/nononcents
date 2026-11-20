from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.urls import reverse
from django.utils import timezone

from budget.forms import RefundForm
from budget.models import (
    Account,
    Category,
    Expense,
    FormSubmission,
    Household,
    HouseholdMembership,
    Income,
    PayPeriod,
    Refund,
    SavingsGoal,
    Transfer,
)
from budget.services import create_refund

TODAY = timezone.localdate()


def days_ago(days):
    return TODAY - timedelta(days=days)


@pytest.fixture
def ledger(client, db):
    user = get_user_model().objects.create_user("ledger-user", password="a-secure-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    account = Account.objects.create(household=home, name="Spending", type="spending")
    savings = Account.objects.create(household=home, name="Savings", type="savings")
    groceries = Category.objects.create(household=home, name="Groceries", type="spending")
    pay = Category.objects.create(household=home, name="Paycheck", type="income")
    # One long period so ordinary dates are inside a pay period.
    PayPeriod.objects.create(
        household=home,
        nominal_payday=days_ago(120),
        expected_deposit_date=days_ago(120),
        start_date=days_ago(120),
        end_date=TODAY + timedelta(days=60),
    )
    expense = Expense.objects.create(
        household=home,
        created_by=user,
        date=days_ago(10),
        amount=Decimal("50.00"),
        account=account,
        category=groceries,
        merchant="Corner Market",
    )
    return {
        "client": client,
        "user": user,
        "home": home,
        "account": account,
        "savings": savings,
        "groceries": groceries,
        "income_category": pay,
        "expense": expense,
    }


def refund_data(ledger, amount="10.00", day=None, **extra):
    return {
        "amount": amount,
        "date": (day or days_ago(5)).isoformat(),
        "account": ledger["account"].pk,
        "category": ledger["groceries"].pk,
        "merchant": "Corner Market",
        "original_expense": ledger["expense"].pk,
        "notes": "",
        "form_token": str(uuid4()),
        **extra,
    }


def make_refund(ledger, amount, day=None):
    return create_refund(
        household=ledger["home"],
        actor=ledger["user"],
        entry_date=day or days_ago(5),
        account=ledger["account"],
        category=ledger["groceries"],
        amount=Decimal(amount),
        original_expense=ledger["expense"],
    )


def edit_refund_data(refund, amount, day=None, category=None):
    return {
        "date": (day or refund.date).isoformat(),
        "description": "Corner Market",
        "amount": amount,
        "category": (category or refund.category).pk,
        "notes": "",
    }


def edit_expense_data(ledger, amount="50.00", day=None):
    expense = ledger["expense"]
    return {
        "date": (day or expense.date).isoformat(),
        "description": "Corner Market",
        "amount": amount,
        "category": ledger["groceries"].pk,
        "notes": "",
    }


# --- Refund rules -------------------------------------------------------------------------


def test_refund_larger_than_original_is_rejected(ledger):
    response = ledger["client"].post(reverse("refund_add"), refund_data(ledger, "50.01"))
    assert response.status_code == 200
    assert "cannot exceed" in response.content.decode()
    assert not Refund.objects.exists()


def test_two_refunds_summing_over_the_original_are_rejected(ledger):
    make_refund(ledger, "30.00")
    over = ledger["client"].post(reverse("refund_add"), refund_data(ledger, "20.01"))
    assert over.status_code == 200
    assert Refund.objects.count() == 1
    ok = ledger["client"].post(reverse("refund_add"), refund_data(ledger, "20.00"))
    assert ok.status_code == 302
    assert Refund.objects.count() == 2


def test_editing_a_refund_cannot_push_total_over_the_original(ledger):
    make_refund(ledger, "20.00")
    second = make_refund(ledger, "20.00")
    url = reverse("transaction_edit", args=["refund", second.pk])
    over = ledger["client"].post(url, edit_refund_data(second, "30.01"))
    assert over.status_code == 200
    assert "cannot exceed" in over.content.decode()
    ok = ledger["client"].post(url, edit_refund_data(second, "30.00"))
    assert ok.status_code == 302
    second.refresh_from_db()
    assert second.amount == Decimal("30.00")


def test_refund_before_original_expense_date_is_rejected(ledger):
    response = ledger["client"].post(reverse("refund_add"), refund_data(ledger, day=days_ago(11)))
    assert response.status_code == 200
    assert "before the original expense date" in response.content.decode()
    assert not Refund.objects.exists()
    refund = make_refund(ledger, "5.00")
    edit = ledger["client"].post(
        reverse("transaction_edit", args=["refund", refund.pk]),
        edit_refund_data(refund, "5.00", day=days_ago(12)),
    )
    assert edit.status_code == 200
    refund.refresh_from_db()
    assert refund.date == days_ago(5)


def test_income_category_refund_is_rejected_by_form_and_service(ledger):
    data = refund_data(ledger) | {"category": ledger["income_category"].pk}
    form = RefundForm(data, household=ledger["home"])
    assert not form.is_valid()
    assert "category" in form.errors
    assert ledger["income_category"] not in form.fields["category"].queryset
    with pytest.raises(ValidationError, match="Spending or Bills"):
        create_refund(
            household=ledger["home"],
            actor=ledger["user"],
            entry_date=days_ago(1),
            account=ledger["account"],
            category=ledger["income_category"],
            amount=Decimal("5.00"),
        )
    assert not Refund.objects.exists()


def test_refund_edit_rejects_income_category(ledger):
    refund = make_refund(ledger, "5.00")
    response = ledger["client"].post(
        reverse("transaction_edit", args=["refund", refund.pk]),
        edit_refund_data(refund, "5.00", category=ledger["income_category"]),
    )
    assert response.status_code == 200
    refund.refresh_from_db()
    assert refund.category == ledger["groceries"]


def test_original_expense_from_another_household_is_rejected(ledger):
    other = Household.objects.create(name="Other")
    account = Account.objects.create(household=other, name="Spending", type="spending")
    category = Category.objects.create(household=other, name="Groceries", type="spending")
    foreign = Expense.objects.create(
        household=other, date=days_ago(3), amount="90.00", account=account, category=category
    )
    with pytest.raises(ValidationError, match="must belong"):
        create_refund(
            household=ledger["home"],
            actor=ledger["user"],
            entry_date=days_ago(1),
            account=ledger["account"],
            category=ledger["groceries"],
            amount=Decimal("5.00"),
            original_expense=foreign,
        )


def test_expense_cannot_drop_below_its_refunds(ledger):
    make_refund(ledger, "20.00")
    make_refund(ledger, "15.00")
    url = reverse("transaction_edit", args=["expense", ledger["expense"].pk])
    low = ledger["client"].post(url, edit_expense_data(ledger, "34.99"))
    assert low.status_code == 200
    assert "$35.00 in linked refunds" in low.content.decode()
    ok = ledger["client"].post(url, edit_expense_data(ledger, "35.00"))
    assert ok.status_code == 302


def test_expense_cannot_move_after_its_refund(ledger):
    make_refund(ledger, "20.00", day=days_ago(5))
    url = reverse("transaction_edit", args=["expense", ledger["expense"].pk])
    late = ledger["client"].post(url, edit_expense_data(ledger, day=days_ago(4)))
    assert late.status_code == 200
    assert "cannot be dated after it" in late.content.decode()
    ledger["expense"].refresh_from_db()
    assert ledger["expense"].date == days_ago(10)
    assert ledger["client"].post(url, edit_expense_data(ledger, day=days_ago(5))).status_code == 302


def test_deleting_an_expense_with_refunds_does_not_error(ledger):
    refund = make_refund(ledger, "20.00")
    url = reverse("transaction_delete", args=["expense", ledger["expense"].pk])
    page = ledger["client"].get(url)
    assert "linked refund(s) will be preserved and detached" in page.content.decode()
    response = ledger["client"].post(url, {"confirmation": "Expense: Corner Market"})
    assert response.status_code == 302
    refund.refresh_from_db()
    assert refund.original_expense is None


def test_original_expense_dropdown_is_recent_household_only_newest_first(ledger):
    old = Expense.objects.create(
        household=ledger["home"],
        date=days_ago(200),
        amount="9.00",
        account=ledger["account"],
        category=ledger["groceries"],
        merchant="Ancient",
    )
    newer = Expense.objects.create(
        household=ledger["home"],
        date=days_ago(2),
        amount="7.00",
        account=ledger["account"],
        category=ledger["groceries"],
        merchant="Newer",
    )
    other = Household.objects.create(name="Other")
    other_account = Account.objects.create(household=other, name="Spending", type="spending")
    other_category = Category.objects.create(household=other, name="G", type="spending")
    Expense.objects.create(
        household=other,
        date=days_ago(1),
        amount="1.00",
        account=other_account,
        category=other_category,
    )
    newer.refresh_from_db()
    field = RefundForm(household=ledger["home"]).fields["original_expense"]
    assert list(field.queryset) == [newer, ledger["expense"]]
    assert field.label_from_instance(newer) == f"{newer.date:%Y-%m-%d} — Newer — $7.00"
    linked = Refund.objects.create(
        household=ledger["home"],
        date=days_ago(1),
        amount="1.00",
        account=ledger["account"],
        category=ledger["groceries"],
        original_expense=old,
    )
    edit_field = RefundForm(instance=linked, household=ledger["home"]).fields["original_expense"]
    assert old in edit_field.queryset


# --- Date guard ---------------------------------------------------------------------------


def expense_data(ledger, day, **extra):
    return {
        "amount": "12.00",
        "date": day.isoformat(),
        "account": ledger["account"].pk,
        "category": ledger["groceries"].pk,
        "merchant": "Cafe",
        "notes": "",
        "bill": "",
        "form_token": str(uuid4()),
        **extra,
    }


def test_date_more_than_60_days_ago_needs_confirmation(ledger):
    client = ledger["client"]
    response = client.post(reverse("expense_add"), expense_data(ledger, days_ago(61)))
    content = response.content.decode()
    assert response.status_code == 200
    assert "more than 60 days ago" in content
    assert "Tick &#x27;This date is correct&#x27;" in content
    assert 'name="confirm_unusual_date"' in content
    assert 'aria-describedby="id_date_helptext id_date_error"' in content
    assert 'id="id_date_error"' in content
    assert not Expense.objects.filter(merchant="Cafe").exists()
    saved = client.post(
        reverse("expense_add"),
        expense_data(ledger, days_ago(61), confirm_unusual_date="on"),
    )
    assert saved.status_code == 302
    assert Expense.objects.filter(merchant="Cafe", date=days_ago(61)).count() == 1


def test_ordinary_date_does_not_render_the_confirmation_checkbox(ledger):
    page = ledger["client"].get(reverse("expense_add"))
    assert "confirm_unusual_date" not in page.content.decode()
    response = ledger["client"].post(reverse("expense_add"), expense_data(ledger, days_ago(2)))
    assert response.status_code == 302


def test_date_more_than_a_year_away_is_always_rejected(ledger):
    for day in (days_ago(400), TODAY + timedelta(days=400)):
        response = ledger["client"].post(
            reverse("expense_add"), expense_data(ledger, day, confirm_unusual_date="on")
        )
        assert response.status_code == 200
        assert "more than a year" in response.content.decode()
    assert not Expense.objects.filter(merchant="Cafe").exists()


def test_future_date_beyond_31_days_needs_confirmation(ledger):
    day = TODAY + timedelta(days=40)
    response = ledger["client"].post(reverse("expense_add"), expense_data(ledger, day))
    assert "days in the future" in response.content.decode()
    ok = ledger["client"].post(
        reverse("expense_add"), expense_data(ledger, day, confirm_unusual_date="on")
    )
    assert ok.status_code == 302


def test_date_outside_every_pay_period_needs_confirmation(ledger):
    PayPeriod.objects.all().delete()
    PayPeriod.objects.create(
        household=ledger["home"],
        nominal_payday=days_ago(30),
        expected_deposit_date=days_ago(30),
        start_date=days_ago(30),
        end_date=days_ago(20),
    )
    day = days_ago(5)
    response = ledger["client"].post(reverse("expense_add"), expense_data(ledger, day))
    assert "not in any pay period" in response.content.decode()
    ok = ledger["client"].post(
        reverse("expense_add"), expense_data(ledger, day, confirm_unusual_date="on")
    )
    assert ok.status_code == 302


def test_date_guard_applies_to_income_and_transfer(ledger):
    day = days_ago(61)
    income = ledger["client"].post(
        reverse("income_add"),
        {
            "date": day.isoformat(),
            "source": "Job",
            "amount": "100.00",
            f"allocation_{ledger['account'].pk}": "100.00",
            f"allocation_{ledger['savings'].pk}": "0",
            "form_token": str(uuid4()),
        },
    )
    assert income.status_code == 200
    assert "more than 60 days ago" in income.content.decode()
    assert not Income.objects.exists()
    transfer = ledger["client"].post(
        reverse("transfer_add"),
        {
            "date": day.isoformat(),
            "amount": "5.00",
            "source": ledger["account"].pk,
            "destination": ledger["savings"].pk,
            "form_token": str(uuid4()),
        },
    )
    assert transfer.status_code == 200
    assert not Transfer.objects.exists()


def test_unchanged_date_on_edit_needs_no_confirmation_but_changed_one_does(ledger):
    expense = ledger["expense"]
    expense.date = days_ago(200)
    expense.save()
    url = reverse("transaction_edit", args=["expense", expense.pk])
    unchanged = ledger["client"].post(url, edit_expense_data(ledger, "55.00"))
    assert unchanged.status_code == 302
    changed = ledger["client"].post(url, edit_expense_data(ledger, "55.00", day=days_ago(150)))
    assert changed.status_code == 200
    assert "confirm_unusual_date" in changed.content.decode()
    confirmed = ledger["client"].post(
        url, edit_expense_data(ledger, "55.00", day=days_ago(150)) | {"confirm_unusual_date": "on"}
    )
    assert confirmed.status_code == 302


# --- Duplicate submissions ----------------------------------------------------------------


def test_add_forms_render_a_hidden_token(ledger):
    for name in ("expense_add", "income_add", "transfer_add", "refund_add"):
        page = ledger["client"].get(reverse(name))
        assert page.context["form"]["form_token"].value()
        assert 'type="hidden" name="form_token"' in page.content.decode()


def test_duplicate_token_creates_one_row_and_redirects_with_info_message(ledger):
    data = expense_data(ledger, days_ago(2))
    first = ledger["client"].post(reverse("expense_add"), data)
    second = ledger["client"].post(reverse("expense_add"), data)
    assert first.status_code == second.status_code == 302
    assert second.url == first.url == reverse("dashboard")
    assert Expense.objects.filter(merchant="Cafe").count() == 1
    messages = [str(m) for m in ledger["client"].get(reverse("dashboard")).context["messages"]]
    assert "That entry was already saved." in messages


def test_duplicate_token_for_refund_income_and_transfer(ledger):
    goal = SavingsGoal.objects.create(household=ledger["home"], name="Trip", target_amount="100")
    refund = refund_data(ledger)
    income = {
        "date": days_ago(1).isoformat(),
        "source": "Job",
        "amount": "100.00",
        f"allocation_{ledger['account'].pk}": "100.00",
        f"allocation_{ledger['savings'].pk}": "0",
        "form_token": str(uuid4()),
    }
    transfer = {
        "date": days_ago(1).isoformat(),
        "amount": "5.00",
        "source": ledger["account"].pk,
        "destination": ledger["savings"].pk,
        "savings_destination_goal": goal.pk,
        "form_token": str(uuid4()),
    }
    for name, data in (("refund_add", refund), ("income_add", income), ("transfer_add", transfer)):
        assert ledger["client"].post(reverse(name), data).status_code == 302
        assert ledger["client"].post(reverse(name), data).status_code == 302
    assert Refund.objects.count() == 1
    assert Income.objects.count() == 1
    assert Transfer.objects.count() == 1


def test_missing_or_invalid_token_is_rejected(ledger):
    data = expense_data(ledger, days_ago(2))
    del data["form_token"]
    response = ledger["client"].post(reverse("expense_add"), data)
    assert response.status_code == 200
    assert "Reload the page" in response.content.decode()
    response = ledger["client"].post(reverse("expense_add"), data | {"form_token": "nope"})
    assert response.status_code == 200
    assert not Expense.objects.filter(merchant="Cafe").exists()
    assert not FormSubmission.objects.exists()


def test_token_survives_a_validation_error_and_failed_save_does_not_consume_it(ledger):
    token = str(uuid4())
    bad = expense_data(ledger, days_ago(2), amount="0", form_token=token)
    response = ledger["client"].post(reverse("expense_add"), bad)
    assert response.status_code == 200
    assert f'name="form_token" value="{token}"' in response.content.decode()
    good = expense_data(ledger, days_ago(2), form_token=token)
    assert ledger["client"].post(reverse("expense_add"), good).status_code == 302
    assert Expense.objects.filter(merchant="Cafe").count() == 1


def test_service_error_rolls_back_the_token(ledger):
    token = str(uuid4())
    PayPeriod.objects.update(is_locked=True)
    response = ledger["client"].post(
        reverse("expense_add"), expense_data(ledger, days_ago(2), form_token=token)
    )
    assert response.status_code == 200
    assert not FormSubmission.objects.filter(token=token).exists()


def test_old_form_submissions_are_pruned_on_insert(ledger):
    old = FormSubmission.objects.create(token=uuid4(), household=ledger["home"])
    FormSubmission.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=31))
    recent = FormSubmission.objects.create(token=uuid4(), household=ledger["home"])
    ledger["client"].post(reverse("expense_add"), expense_data(ledger, days_ago(2)))
    assert not FormSubmission.objects.filter(pk=old.pk).exists()
    assert FormSubmission.objects.filter(pk=recent.pk).exists()


# --- Row locking --------------------------------------------------------------------------


def test_edit_and_delete_lock_the_row_being_changed(ledger):
    original = QuerySet.select_for_update
    expense = ledger["expense"]
    with patch.object(QuerySet, "select_for_update", autospec=True, side_effect=original) as spy:
        edit = ledger["client"].post(
            reverse("transaction_edit", args=["expense", expense.pk]),
            edit_expense_data(ledger, "51.00"),
        )
        assert edit.status_code == 302
        assert spy.call_args_list[0].args[0].model is Expense
        spy.reset_mock()
        delete = ledger["client"].post(
            reverse("transaction_delete", args=["expense", expense.pk]),
            {"confirmation": "Expense: Corner Market"},
        )
        assert delete.status_code == 302
        assert spy.call_args_list[0].args[0].model is Expense
    assert not Expense.objects.filter(pk=expense.pk).exists()
