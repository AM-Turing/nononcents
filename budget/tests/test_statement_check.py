from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from budget.models import (
    Account,
    Category,
    Expense,
    Household,
    HouseholdMembership,
    IncomeAllocation,
    StatementLine,
)
from budget.services import account_balance, create_expense, create_income, create_transfer
from budget.statements import (
    ParsedLine,
    ParsedSection,
    build_review,
    confirm_all_proposed,
    confirm_match,
    dismiss_line,
    fingerprints,
    link_new_entry,
    reopen_line,
    stage_statement,
)

D = Decimal


@pytest.fixture
def s(db):
    user = get_user_model().objects.create_user("stmt-user", password="a-secure-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    spending = Account.objects.create(household=home, name="Spending", type="spending")
    bills = Account.objects.create(household=home, name="Bills", type="bills")
    food = Category.objects.create(household=home, name="Groceries", type="spending")
    return {"user": user, "home": home, "spending": spending, "bills": bills, "food": food}


def expense(s, day, amount, merchant="Grocer", account=None):
    return create_expense(
        household=s["home"],
        actor=s["user"],
        entry_date=date(2026, 10, day),
        account=account or s["spending"],
        category=s["food"],
        amount=amount,
        merchant=merchant,
    )


def section(*lines, start=1, end=31):
    return ParsedSection(
        account_label="Checking 1234",
        period_start=date(2026, 10, start),
        period_end=date(2026, 10, end),
        lines=[ParsedLine(date(2026, 10, d), D(a), text) for d, a, text in lines],
    )


def stage(s, sec, sha="a" * 64, account=None):
    return stage_statement(
        household=s["home"],
        actor=s["user"],
        account=account or s["spending"],
        section=sec,
        file_sha256=sha,
    )


@pytest.mark.django_db
def test_reimporting_same_file_or_overlap_never_duplicates(s):
    first = stage(s, section((3, "-12.50", "POS GROCER #123"), (4, "-12.50", "POS GROCER #123")))
    assert (first.created, first.is_new_file) == (2, True)
    again = stage(s, section((3, "-12.50", "POS GROCER #123")))
    assert (again.created, again.is_new_file) == (0, False)
    overlap = stage(
        s,
        section((4, "-12.50", "POS  grocer #123"), (20, "-9.00", "FUELCO")),
        sha="b" * 64,
    )
    assert (overlap.created, overlap.already_imported) == (1, 1)
    assert StatementLine.objects.count() == 3


def test_identical_lines_same_day_get_distinct_fingerprints():
    line = ParsedLine(date(2026, 10, 3), D("-5.00"), "SNACKBAR")
    a, b = fingerprints([line, line])
    assert a != b
    assert fingerprints([line]) == [a]


@pytest.mark.django_db
def test_staging_validates(s):
    with pytest.raises(ValidationError):
        stage(s, section((3, "0.00", "x")))
    with pytest.raises(ValidationError):
        stage(s, section((25, "-1.00", "x"), end=20))
    other = Account.objects.create(household=Household.objects.create(), name="X", type="spending")
    with pytest.raises(ValidationError):
        stage(s, section((3, "-1.00", "x")), account=other)


@pytest.mark.django_db
def test_matching_ignores_merchant_text_and_proposes_each_entry_once(s):
    a = expense(s, 2, "5.00", merchant="Coffee")
    b = expense(s, 3, "5.00", merchant="Something else")
    far = expense(s, 20, "5.00")
    wrong_amount = expense(s, 3, "5.01")
    result = stage(s, section((3, "-5.00", "TOTALLY DIFFERENT TEXT"), (4, "-5.00", "X")))
    review = build_review(result.statement)
    proposed = [row.proposed.row_pk for row in review.pending]
    assert proposed == [b.pk, a.pk]  # closest date first, never the same entry twice
    keys = {i.row_pk for row in review.pending for i in [row.proposed, *row.alternatives]}
    assert far.pk not in keys and wrong_amount.pk not in keys
    # Entries with no line (and not already proposed) are listed as not on the statement yet.
    assert {i.row_pk for i in review.not_on_statement} == {far.pk, wrong_amount.pk}


@pytest.mark.django_db
def test_date_window(s):
    before = expense(s, 1, "7.00")  # 7 days before the bank date: in window
    after = expense(s, 11, "8.00")  # 3 days after: in window
    too_early = expense(s, 1, "9.00")  # 8 days before: out
    result = stage(s, section((8, "-7.00", ""), (8, "-8.00", ""), (9, "-9.00", "")))
    rows = {row.line.amount: row for row in build_review(result.statement).pending}
    assert rows[D("-7.00")].proposed.row_pk == before.pk
    assert rows[D("-8.00")].proposed.row_pk == after.pk
    assert rows[D("-9.00")].proposed is None
    assert too_early.cleared_at is None


@pytest.mark.django_db
def test_confirm_marks_cleared_and_keeps_owner_values(s):
    entry = expense(s, 2, "5.00", merchant="Coffee")
    balance = account_balance(s["spending"])
    result = stage(s, section((3, "-5.00", "SQ *BEAN THERE 8885551212")))
    row = build_review(result.statement).pending[0]
    confirm_match(household=s["home"], actor=s["user"], line_id=row.line.pk, key=row.proposed.key)
    entry.refresh_from_db()
    assert entry.cleared_at is not None
    assert (entry.merchant, entry.amount, entry.date) == ("Coffee", D("5.00"), date(2026, 10, 2))
    assert account_balance(s["spending"]) == balance
    review = build_review(result.statement)
    assert not review.pending and review.matched[0].matched.row_pk == entry.pk
    assert entry.pk not in {i.row_pk for i in review.not_on_statement}
    with pytest.raises(ValidationError):  # already reviewed
        confirm_match(
            household=s["home"], actor=s["user"], line_id=row.line.pk, key=row.proposed.key
        )


@pytest.mark.django_db
def test_confirm_refuses_claimed_or_mismatched_entry(s):
    entry = expense(s, 2, "5.00")
    other = expense(s, 2, "6.00")
    result = stage(s, section((3, "-5.00", "A"), (3, "-5.00", "B")))
    first, second = build_review(result.statement).pending
    confirm_match(
        household=s["home"], actor=s["user"], line_id=first.line.pk, key=f"expense:{entry.pk}"
    )
    with pytest.raises(ValidationError, match="already matched"):
        confirm_match(
            household=s["home"], actor=s["user"], line_id=second.line.pk, key=f"expense:{entry.pk}"
        )
    with pytest.raises(ValidationError, match="amount"):
        confirm_match(
            household=s["home"], actor=s["user"], line_id=second.line.pk, key=f"expense:{other.pk}"
        )
    foreign = Household.objects.create()
    with pytest.raises(ValidationError):
        confirm_match(household=foreign, actor=s["user"], line_id=second.line.pk, key="expense:1")


@pytest.mark.django_db
def test_income_allocation_and_transfer_sides_match(s):
    income = create_income(
        household=s["home"],
        actor=s["user"],
        income_date=date(2026, 10, 10),
        source="Employer",
        amount="1000.00",
        allocations=[(s["bills"], "1000.00")],
    )
    transfer = create_transfer(
        household=s["home"],
        actor=s["user"],
        entry_date=date(2026, 10, 11),
        source=s["bills"],
        destination=s["spending"],
        amount="300.00",
    )
    result = stage(
        s,
        section((10, "1000.00", "EMPLOYER-CL"), (11, "-300.00", "TRANSFER TO")),
        account=s["bills"],
    )
    assert (
        confirm_all_proposed(household=s["home"], actor=s["user"], statement=result.statement) == 2
    )
    allocation = IncomeAllocation.objects.get(income=income)
    transfer.refresh_from_db()
    assert allocation.cleared_at is not None
    assert transfer.source_cleared_at is not None and transfer.destination_cleared_at is None


@pytest.mark.django_db
def test_dismiss_reopen_and_link_new_entry(s):
    result = stage(s, section((3, "-4.25", "VENDING")))
    line = result.statement.lines.get()
    dismiss_line(household=s["home"], actor=s["user"], line_id=line.pk)
    assert build_review(result.statement).dismissed == [line]
    reopen_line(household=s["home"], actor=s["user"], line_id=line.pk)
    new = expense(s, 3, "4.25", merchant="Snack")
    assert link_new_entry(
        household=s["home"],
        actor=s["user"],
        line_id=line.pk,
        account=s["spending"],
        key=f"expense:{new.pk}",
        amount=new.amount,
    )
    line.refresh_from_db()
    assert line.status == StatementLine.Status.MATCHED
    assert Expense.objects.get(pk=new.pk).cleared_at is not None
    # A wrong amount or account is not linked.
    result2 = stage(s, section((5, "-1.00", "X")), sha="c" * 64)
    line2 = result2.statement.lines.get()
    assert not link_new_entry(
        household=s["home"],
        actor=s["user"],
        line_id=line2.pk,
        account=s["spending"],
        key=f"expense:{new.pk}",
        amount=D("2.00"),
    )


@pytest.fixture
def logged_in(client, s):
    client.force_login(s["user"])
    session = client.session
    session["active_household_id"] = s["home"].pk
    session.save()
    return client


@pytest.mark.django_db
def test_review_page_and_one_tap_match(logged_in, s):
    entry = expense(s, 2, "5.00", merchant="Coffee")
    result = stage(s, section((3, "-5.00", "SQ *BEAN"), (9, "-40.00", "MISSED THING")))
    url = f"/statements/{result.statement.pk}/"
    page = logged_in.get(url).content.decode()
    assert "Same purchase" in page and "Coffee" in page and "SQ *BEAN" in page
    assert "Possibly missed" in page
    line = result.statement.lines.get(amount=D("-5.00"))
    response = logged_in.post(
        f"{url}line/", {"action": "match", "line": line.pk, "key": f"expense:{entry.pk}"}
    )
    assert response.status_code == 302
    line.refresh_from_db()
    assert line.status == StatementLine.Status.MATCHED
    assert "/statements/" in logged_in.get("/statements/").content.decode()


@pytest.mark.django_db
def test_other_profile_cannot_open_statement(client, s):
    result = stage(s, section((3, "-5.00", "X")))
    other = get_user_model().objects.create_user("other", password="a-secure-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=other)
    client.force_login(other)
    assert client.get(f"/statements/{result.statement.pk}/").status_code == 404
    assert client.post(f"/statements/{result.statement.pk}/delete/").status_code == 404


@pytest.mark.django_db
def test_add_as_new_entry_links_back(logged_in, s):
    result = stage(s, section((9, "-40.00", "MISSED THING")))
    line = result.statement.lines.get()
    form = logged_in.get(f"/transactions/expense/?statement_line={line.pk}").content.decode()
    assert 'value="40.00"' in form and 'value="2026-10-09"' in form
    token = form.split('name="form_token" value="')[1].split('"')[0]
    response = logged_in.post(
        "/transactions/expense/",
        {
            "form_token": token,
            "date": "2026-10-09",
            "account": s["spending"].pk,
            "category": s["food"].pk,
            "amount": "40.00",
            "merchant": "Hardware store",
            "splits-TOTAL_FORMS": "0",
            "splits-INITIAL_FORMS": "0",
            "savings-TOTAL_FORMS": "0",
            "savings-INITIAL_FORMS": "0",
        },
    )
    assert response.status_code == 302, response.content.decode()[:2000]
    assert response["Location"] == f"/statements/{result.statement.pk}/"
    line.refresh_from_db()
    new = Expense.objects.get(merchant="Hardware store")
    assert line.status == StatementLine.Status.MATCHED and line.matched_key == f"expense:{new.pk}"


@pytest.mark.django_db
def test_add_income_from_inflow_line(logged_in, s):
    result = stage(s, section((10, "25.00", "ZELLE FROM")), account=s["bills"])
    line = result.statement.lines.get()
    form = logged_in.get(f"/transactions/income/?statement_line={line.pk}").content.decode()
    token = form.split('name="form_token" value="')[1].split('"')[0]
    response = logged_in.post(
        "/transactions/income/",
        {
            "form_token": token,
            "date": "2026-10-10",
            "source": "Mylissa",
            "amount": "25.00",
            f"allocation_{s['bills'].pk}": "25.00",
            f"allocation_{s['spending'].pk}": "0",
        },
    )
    assert response.status_code == 302, response.content.decode()[:2000]
    line.refresh_from_db()
    assert line.status == StatementLine.Status.MATCHED
    assert line.matched_key.startswith("allocation:")


@pytest.mark.django_db
def test_remove_statement_keeps_cleared_marks(logged_in, s):
    entry = expense(s, 2, "5.00")
    result = stage(s, section((3, "-5.00", "X")))
    confirm_all_proposed(household=s["home"], actor=s["user"], statement=result.statement)
    logged_in.post(f"/statements/{result.statement.pk}/delete/")
    entry.refresh_from_db()
    assert entry.cleared_at is not None
    assert not StatementLine.objects.exists()
