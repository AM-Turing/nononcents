"""Generic statement reader tests on made-up statements in several styles (no real data).

Each expected figure is worked out by hand in the comment beside it.
"""

from datetime import date
from decimal import Decimal

import pytest

from budget.statement_pdf import StatementParseError, parse_statement_pages, parse_statement_pdf

D = Decimal


def row(day, text, *cells):
    """One printed table line: date, description, then right-aligned amount cells."""
    return f"{day:<14}{text:<30}" + "".join(f"{cell:>14}" for cell in cells)


def parse(*pages):
    return parse_statement_pages(list(pages))


def amounts(section):
    return [(line.date, line.amount) for line in section.lines]


SIGNED = "\n".join(
    [
        "Example Credit Union",
        "Checking Account",
        "Account number: xxxx4821",
        "Statement period: January 1, 2026 - January 31, 2026",
        "",
        "Account summary",
        "Beginning balance            $1,000.00",
        "Total deposits               $2,000.00",
        "Ending balance               $2,630.50",
        "",
        "Transaction history",
        row("Date", "Description", "Amount", "Balance"),
        row("01/03", "Payroll Deposit", "2,000.00", "3,000.00"),
        row("01/05", "Grocery Mart #12", "-45.50", "2,954.50"),
        row("01/09", "Electric Co", "(124.00)", "2,830.50"),
        row("01/20", "Transfer to Savings", "-200.00", "2,630.50"),
    ]
)


def test_signed_amounts_with_running_balance():
    (section,) = parse(SIGNED)
    assert section.account_suffix == "4821"
    assert (section.period_start, section.period_end) == (date(2026, 1, 1), date(2026, 1, 31))
    # opening 1,000.00; ending 2,630.50 (labels in the summary)
    assert (section.opening_balance, section.closing_balance) == (D("1000.00"), D("2630.50"))
    assert amounts(section) == [
        (
            date(2026, 1, 3),
            D("2000.00"),
        ),  # 3,000.00 - 1,000.00 = +2,000.00 (unsigned: balance says in)
        (date(2026, 1, 5), D("-45.50")),  # "-45.50" is money out; 3,000.00 - 45.50 = 2,954.50
        (date(2026, 1, 9), D("-124.00")),  # "(124.00)" is money out; 2,954.50 - 124.00 = 2,830.50
        (date(2026, 1, 20), D("-200.00")),  # 2,830.50 - 200.00 = 2,630.50
    ]
    assert section.lines[0].description == "Payroll Deposit"
    assert (
        section.verified
    )  # every balance follows: 1,000.00 + 2,000.00 - 45.50 - 124.00 - 200.00 = 2,630.50


DEBIT_CREDIT = "\n".join(
    [
        "Statement Period 03/01/2026 to 03/31/2026",
        "Account ending in 7766",
        "Opening balance  500.00",
        row("Date", "Description", "Withdrawals", "Deposits"),
        row("03/02", "Rent", "800.00", ""),
        row("03/04", "Refund Store", "", "25.00"),
        row("03/15", "Paycheck", "", "1,500.00"),
        row("03/28", "Coffee", "6.75", ""),
        "Closing balance  1,218.25",
    ]
)


def test_debit_and_credit_columns_without_running_balance():
    (section,) = parse(DEBIT_CREDIT)
    assert section.account_suffix == "7766"
    assert amounts(section) == [
        (date(2026, 3, 2), D("-800.00")),  # Withdrawals column: out
        (date(2026, 3, 4), D("25.00")),  # Deposits column: in
        (date(2026, 3, 15), D("1500.00")),  # Deposits column: in
        (date(2026, 3, 28), D("-6.75")),  # Withdrawals column: out
    ]
    # 500.00 - 800.00 + 25.00 + 1,500.00 - 6.75 = 1,218.25 = the closing balance
    assert section.verified


MULTI = "\n".join(
    [
        "Statement period: 02/01/2026 - 02/28/2026",
        "",
        "Everyday Checking - Account number ending in 1234",
        "Beginning balance 300.00",
        "",
        "Deposits and other credits",
        "02/03 Payroll 1,000.00",
        "Withdrawals and other debits",
        "02/05 Utility Co 80.00",
        "02/12 Gas Station 40.25",
        "Ending balance 1,179.75",
        "",
        "Savings Account ending in 5678",
        "Beginning balance 2,000.00",
        "Deposits and other credits",
        "02/15 Transfer from checking 100.00",
        "02/28 Dividend 0.40",
        "Ending balance 2,100.40",
    ]
)


def test_several_accounts_with_masked_numbers_and_unsigned_sections():
    checking, savings = parse(MULTI)
    assert (checking.account_suffix, savings.account_suffix) == ("1234", "5678")
    assert checking.account_label == "Everyday Checking ending 1234"
    assert amounts(checking) == [
        (date(2026, 2, 3), D("1000.00")),  # under "Deposits": in
        (date(2026, 2, 5), D("-80.00")),  # under "Withdrawals": out
        (date(2026, 2, 12), D("-40.25")),
    ]
    # 300.00 + 1,000.00 - 80.00 - 40.25 = 1,179.75 = ending balance
    assert [line.amount for line in savings.lines] == [D("100.00"), D("0.40")]
    # 2,000.00 + 100.00 + 0.40 = 2,100.40 = ending balance
    assert checking.verified and savings.verified


def test_summary_daily_balances_and_items_paid_are_ignored():
    text = "\n".join(
        [
            "Statement period 06/01/2026 - 06/30/2026",
            "Account ending in 9012",
            "Account summary",
            "Beginning balance 1,000.00",
            "Deposits and credits 2,200.00",
            "Withdrawals and debits 800.00",
            "Ending balance 2,400.00",
            "",
            "Deposits and other credits",
            "06/02 Payroll 2,000.00",
            "06/17 Tax refund 200.00",
            "Total deposits 2,200.00",
            "",
            "Withdrawals and other debits",
            "06/05 Rent 600.00",
            "06/10 Phone 100.00",
            "06/20 Insurance 100.00",
            "Total withdrawals 800.00",
            "",
            "Daily balance summary",
            "06/02 3,000.00",
            "06/05 2,400.00",
            "Items paid",
            "06/05 Rent 600.00",
        ]
    )
    (section,) = parse(text)
    # Only the five listed rows: 2,000.00 + 200.00 in, 600.00 + 100.00 + 100.00 out.
    assert [line.amount for line in section.lines] == [
        D("2000.00"),
        D("200.00"),
        D("-600.00"),
        D("-100.00"),
        D("-100.00"),
    ]
    # 1,000.00 + 2,200.00 - 800.00 = 2,400.00; the daily table and Items paid add nothing.
    assert section.verified and section.closing_balance == D("2400.00")


def test_december_to_january_rollover_and_month_name_dates():
    text = "\n".join(
        [
            "Statement period: Dec 15, 2025 - Jan 14, 2026",
            "Account number: xxxx0042",
            "Opening balance 100.00",
            row("Dec 28", "Paycheck", "500.00", "600.00"),
            row("Dec 31", "Monthly fee", "-12.00", "588.00"),
            row("Jan 3", "Market", "-38.00", "550.00"),
            "Closing balance 550.00",
        ]
    )
    (section,) = parse(text)
    assert amounts(section) == [
        (date(2025, 12, 28), D("500.00")),  # December belongs to 2025 (period starts Dec 15, 2025)
        (date(2025, 12, 31), D("-12.00")),
        (date(2026, 1, 3), D("-38.00")),  # January belongs to 2026
    ]
    # 100.00 + 500.00 - 12.00 - 38.00 = 550.00
    assert section.verified


def test_two_pages_with_debit_credit_and_balance_columns_and_repeated_headers():
    head = row("Date", "Description", "Debit", "Credit", "Balance")
    page_1 = "\n".join(
        [
            "Page 1 of 2   Statement Date: 09/30/2026",
            "Account Number: ****5521",
            "Statement Period 09/01/2026 through 09/30/2026",
            "Beginning Balance  $2,000.00",
            "Ending Balance     $2,100.00",
            head,
            row("09/03", "POS Purchase Fuel", "40.00", "", "1,960.00"),
            row("09/04", "ACH Deposit", "", "200.00", "2,160.00"),
        ]
    )
    page_2 = "\n".join(
        [
            "Page 2 of 2",
            "Account Number: ****5521",
            head,
            row("09/20", "Check 1001", "60.00", "", "2,100.00"),
            "",
            "Daily Ending Balance",
            "09/03 1,960.00",
            "09/20 2,100.00",
        ]
    )
    (section,) = parse(page_1, page_2)  # one account: the repeated heading adds no second section
    assert amounts(section) == [
        (date(2026, 9, 3), D("-40.00")),  # Debit column; 2,000.00 - 40.00 = 1,960.00
        (date(2026, 9, 4), D("200.00")),  # Credit column; 1,960.00 + 200.00 = 2,160.00
        (date(2026, 9, 20), D("-60.00")),  # page 2, Debit column; 2,160.00 - 60.00 = 2,100.00
    ]
    assert section.verified and section.closing_balance == D(
        "2100.00"
    )  # the daily table is skipped


def test_random_text_never_crashes_the_reader():
    import random

    words = [
        "01/05", "Jan 5", "2026-02-30", "(12.00)", "1,234.56", "-5.00", "CR", "Deposits",
        "Withdrawals", "Beginning balance", "Ending balance", "account ending in 1234",
        "xxxx9999", "Date", "Debit", "Credit", "Statement period 01/01/2026 - 01/31/2026",
        "$", "99999999999.99", "0.00", "Total", "Items paid", "31/02/2026", "00/00/00",
    ]  # fmt: skip
    rng = random.Random(7)
    for _ in range(1500):
        lines = [
            " " * rng.randint(0, 20) + " ".join(rng.choices(words, k=rng.randint(1, 6)))
            for _ in range(rng.randint(1, 12))
        ]
        try:
            parse("\n".join(lines))
        except StatementParseError:
            pass  # refusing is fine; any other exception type would fail the test


def test_year_less_rows_day_month_names_and_four_date_styles():
    text = "\n".join(
        [
            "Statement period 01 Jan 2026 - 31 Jan 2026",
            "Account ending 3030",
            "Opening balance 50.00",
            row("Date", "Details", "Debit", "Credit"),
            row("05 Jan", "Cafe", "5.00", ""),
            row("2026-01-12", "Gift", "", "20.00"),
            row("13/01/2026", "Bus pass", "15.00", ""),
            row("Jan 20, 2026", "Refund", "", "7.50"),
            "Closing balance 57.50",
        ]
    )
    (section,) = parse(text)
    assert amounts(section) == [
        (date(2026, 1, 5), D("-5.00")),  # "05 Jan": year from the period
        (date(2026, 1, 12), D("20.00")),  # ISO date
        (date(2026, 1, 13), D("-15.00")),  # 13 > 12 shows day/month order
        (date(2026, 1, 20), D("7.50")),  # "Jan 20, 2026"
    ]
    # 50.00 - 5.00 + 20.00 - 15.00 + 7.50 = 57.50
    assert section.verified


def test_amount_styles_cr_dr_trailing_minus_and_dollar_signs():
    text = "\n".join(
        [
            "Statement period 2026-04-01 to 2026-04-30",
            "Account number xxxx3344",
            "Opening balance 0.00",
            "2026-04-02 Salary $1,234.56 CR",
            "2026-04-03 Store $34.56 DR",
            "2026-04-04 Fee 1.00-",
            "Closing balance 1,199.00",
        ]
    )
    (section,) = parse(text)
    assert [line.amount for line in section.lines] == [D("1234.56"), D("-34.56"), D("-1.00")]
    # 0.00 + 1,234.56 - 34.56 - 1.00 = 1,199.00
    assert section.verified


def test_wrapped_descriptions_before_and_after_the_amounts():
    text = "\n".join(
        [
            "Statement period 01/01/2026 - 01/31/2026",
            "Account ending in 5555",
            "Opening balance 1,000.00",
            "01/06  CARD PURCHASE LONG MERCHANT NAME",
            "         CITY ST                   -30.00      970.00",
            "01/08  Online transfer from savings 200.00     1,170.00",
            "         reference 998877",
            "Closing balance 1,170.00",
        ]
    )
    (section,) = parse(text)
    assert section.lines[0].description == "CARD PURCHASE LONG MERCHANT NAME CITY ST"
    assert section.lines[0].amount == D("-30.00")  # 1,000.00 - 30.00 = 970.00
    assert section.lines[1].description == "Online transfer from savings reference 998877"
    assert section.lines[1].amount == D("200.00")  # 970.00 + 200.00 = 1,170.00


def test_card_statement_where_purchases_raise_the_balance():
    text = "\n".join(
        [
            "Statement period 05/01/2026 - 05/31/2026",
            "Card account ending in 8800",
            "Previous balance $200.00",
            "Payments and other credits",
            "05/10 Payment thank you 100.00",
            "Purchases",
            "05/12 Store one 50.00",
            "05/20 Store two 30.00",
            "New balance $180.00",
        ]
    )
    (section,) = parse(text)
    assert [line.amount for line in section.lines] == [D("100.00"), D("-50.00"), D("-30.00")]
    # Owed 200.00 + 50.00 + 30.00 - 100.00 = 180.00. Stored the way the app counts money:
    # a debt of 200.00 is -200.00, so the balances are negated.
    assert (section.opening_balance, section.closing_balance) == (D("-200.00"), D("-180.00"))
    assert section.verified


def test_totals_that_do_not_add_up_refuse_the_statement():
    broken = DEBIT_CREDIT.replace("1,218.25", "1,218.52")
    # 500.00 - 800.00 + 25.00 + 1,500.00 - 6.75 = 1,218.25, not 1,218.52
    with pytest.raises(StatementParseError, match="do not add up to the ending balance"):
        parse(broken)


def test_a_wrong_running_balance_refuses_the_statement():
    broken = SIGNED.replace("2,954.50", "2,945.50", 1)
    with pytest.raises(StatementParseError, match="does not add up to the balance shown"):
        parse(broken)


def test_no_balances_stages_the_rows_as_not_verified():
    text = "\n".join(
        [
            "Statement period 07/01/2026 - 07/31/2026",
            "Account ending in 1010",
            row("Date", "Description", "Amount"),
            row("07/03", "Groceries", "-25.00"),
            row("07/09", "Paycheck", "+100.00"),
        ]
    )
    (section,) = parse(text)
    assert [line.amount for line in section.lines] == [D("-25.00"), D("100.00")]
    assert section.verified is False
    assert section.opening_balance is None and section.closing_balance is None


def test_unsigned_amounts_with_nothing_to_tell_direction_are_refused():
    text = "\n".join(
        [
            "Statement period 07/01/2026 - 07/31/2026",
            "Account ending in 1010",
            "07/03 Groceries 25.00",
            "07/09 Paycheck 100.00",
        ]
    )
    with pytest.raises(StatementParseError, match="money in and which are money out"):
        parse(text)


def test_account_without_a_number_gets_no_suffix():
    text = "\n".join(
        [
            "Statement period 07/01/2026 - 07/31/2026",
            "Opening balance 10.00",
            "07/03 Groceries -2.00 8.00",
            "Closing balance 8.00",
        ]
    )
    (section,) = parse(text)
    assert section.account_suffix == "" and section.verified


def test_text_that_is_not_a_statement():
    with pytest.raises(StatementParseError, match="statement period"):
        parse("Welcome to our new website\nCall us any time")
    with pytest.raises(StatementParseError, match="No transaction rows"):
        parse("Statement period 07/01/2026 - 07/31/2026\nNothing happened")


def test_known_layout_is_still_chosen_first():
    from budget.tests.test_statement_pdf import PAGE_1, PAGE_2, PAGE_3

    first, second = parse(PAGE_1, PAGE_2, PAGE_3)
    assert (first.account_suffix, second.account_suffix) == ("1111", "2222")


# --- real PDF files -------------------------------------------------------------------------


def positioned_pdf(pages):
    """A minimal PDF; each page is a list of lines, each line a list of (x, text) cells."""
    objects = [None, None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for lines in pages:
        ops = ["BT", "/F1 9 Tf"]
        y = 760
        for cells in lines:
            for x, text in cells:
                escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                ops.append(f"1 0 0 1 {x} {y} Tm ({escaped}) Tj")
            y -= 14
        ops.append("ET")
        stream = "\n".join(ops)
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        content = len(objects)
        objects.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content} 0 R >>"
        )
        kids.append(f"{len(objects)} 0 R")
    objects[0] = "<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


DATE_X, DESC_X, DEBIT_X, CREDIT_X = 40, 110, 330, 440


def _statement_pdf():
    def cells(day="", desc="", debit="", credit=""):
        return [
            (x, t)
            for x, t in ((DATE_X, day), (DESC_X, desc), (DEBIT_X, debit), (CREDIT_X, credit))
            if t
        ]

    return positioned_pdf(
        [
            [
                [(40, "Sample Savings Bank")],
                [(40, "Statement period: 08/01/2026 - 08/31/2026")],
                [(40, "Everyday Checking, account ending in 4455")],
                [(40, "Opening balance"), (330, "$400.00")],
                [],
                cells("Date", "Description", "Withdrawals", "Deposits"),
                cells("08/03", "Hardware Store", "60.00"),
                cells("08/14", "Paycheck", "", "900.00"),
                cells("08/21", "Water Utility", "45.50"),
                [],
                [(40, "Closing balance"), (330, "$1,194.50")],
            ]
        ]
    )


def test_reads_a_real_pdf_with_debit_and_credit_columns():
    (section,) = parse_statement_pdf(_statement_pdf())
    assert section.account_suffix == "4455"
    assert amounts(section) == [
        (date(2026, 8, 3), D("-60.00")),  # Withdrawals column
        (date(2026, 8, 14), D("900.00")),  # Deposits column
        (date(2026, 8, 21), D("-45.50")),  # Withdrawals column
    ]
    # 400.00 - 60.00 + 900.00 - 45.50 = 1,194.50
    assert section.verified


def test_pdf_without_text_is_explained():
    with pytest.raises(StatementParseError, match="no readable text"):
        parse_statement_pdf(positioned_pdf([[]]))


@pytest.fixture
def owner(client, db):
    from django.contrib.auth import get_user_model

    from budget.models import Account, Household, HouseholdMembership

    user = get_user_model().objects.create_user("generic-user", password="a-secure-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    session = client.session
    session["active_household_id"] = home.pk
    session.save()
    return client, home, Account.objects.create(household=home, name="Spending", type="spending")


def _upload(client, data):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return client.post(
        "/statements/upload/",
        {"statement": SimpleUploadedFile("s.pdf", data, content_type="application/pdf")},
    )


@pytest.mark.django_db
def test_upload_map_and_review_a_generic_pdf(owner):
    from budget.models import Account, StatementImport, StatementLine

    client, home, spending = owner
    response = _upload(client, _statement_pdf())
    assert response["Location"] == "/statements/upload/accounts/"
    page = client.get("/statements/upload/accounts/").content.decode()
    assert "Everyday Checking ending 4455" in page and "not verified" not in page
    response = client.post("/statements/upload/accounts/", {"account_0": spending.pk})
    assert response["Location"] == "/statements/"
    assert Account.objects.get(pk=spending.pk).statement_suffix == "4455"
    statement = StatementImport.objects.get()
    assert statement.verified and statement.closing_balance == D("1194.50")
    assert StatementLine.objects.filter(account=spending).count() == 3
    review = client.get(f"/statements/{statement.pk}/").content.decode()
    assert "Bank: $1,194.50" in review and "Not verified" not in review
    # The same file again goes straight through: the account ending is remembered.
    assert _upload(client, _statement_pdf())["Location"] == "/statements/"
    assert StatementLine.objects.count() == 3


@pytest.mark.django_db
def test_unverified_statement_is_flagged_on_every_screen(owner):
    from budget.models import StatementImport

    client, home, spending = owner
    pdf = positioned_pdf(
        [
            [
                [(40, "Statement period: 08/01/2026 - 08/31/2026")],
                [(40, "Account ending in 6677")],
                [(40, "Date"), (110, "Description"), (400, "Amount")],
                [(40, "08/03"), (110, "Hardware Store"), (400, "-60.00")],
            ]
        ]
    )
    _upload(client, pdf)
    page = client.get("/statements/upload/accounts/").content.decode()
    assert "not verified: check each line" in page
    response = client.post("/statements/upload/accounts/", {"account_0": spending.pk}, follow=True)
    text = response.content.decode()
    assert "Not verified for Spending" in text and "not verified" in text
    statement = StatementImport.objects.get()
    assert statement.verified is False
    assert "Not verified." in client.get(f"/statements/{statement.pk}/").content.decode()


@pytest.mark.django_db
def test_upload_explains_a_pdf_without_text_and_a_refused_statement(owner):
    client, *_ = owner
    assert "no readable text" in _upload(client, positioned_pdf([[]])).content.decode()
    refused = _upload(client, _statement_pdf().replace(b"$1,194.50", b"$1,194.51")).content.decode()
    assert "do not add up to the ending balance" in refused and "Nothing was imported" in refused
