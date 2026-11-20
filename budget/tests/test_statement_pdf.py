"""Statement parser tests on a made-up statement in the bank's layout (no real data)."""

from datetime import date
from decimal import Decimal

import pytest

from budget.statement_pdf import StatementParseError, parse_statement_pages, parse_statement_pdf

D = Decimal

PAGE_1 = """Page 1 of 3
Statement of Account
Statement Period
12/12/25 - 01/11/26
Access No. 1234567
Summary of your deposit accounts
Previous Balance Deposits/ Credits Withdrawals/ Debits Ending Balance YTD Dividends
e-Checking
1111111111 $100.00 $1,500.00 $1,060.49 $539.51 $0.25
Premier Checking
2222222222 $1,000.00 $4,000.00 $1,200.00 $3,800.00 $0.80
1111111111 Checking
2222222222 Checking
"""

PAGE_2 = """Page 2 of 3
Statement Period
12/12/25 - 01/11/26
Statement of Account
For TEST PERSON
Checking
e-Checking - 1111111111
Joint Owner(s): OTHER PERSON
Date Transaction Detail Amount($) Balance($)
12-12 Beginning Balance 100.00
12-13 Transfer From Checking 1,500.00 1,600.00
12-13 POS Debit - Debit Card 0000 Transaction 12-11-25 Food Store #12 133 Main
Smalltown 10.48- 1,589.52
12-31 POS Debit- Debit Card 0000 12-30-25 Fuel Stop 555 Smalltown XX 50.00 - 1,539.52
CHANGE OF ADDRESS
PLEASE PRINT. USE BLUE OR BLACK BALL POINT PEN.
NAME (FIRST MI LAST)
"""

PAGE_3 = """Page 3 of 3
Statement Period
12/12/25 - 01/11/26
Access No. 1234567
e-Checking - 1111111111 (Continued from previous page)
Joint Owner(s): OTHER PERSON
Date Transaction Detail Amount($) Balance($)
01-02 POS Debit- Debit Card 0000 01-01-26 Fuel Stop 555 Smalltown XX 50.00- 1,489.52
01-02 POS Debit- Debit Card 0000 01-01-26 Fuel Stop 555 Smalltown XX 50.00- 1,439.52
01-05 POS Credit Adjustment 0000 Transaction 01-03-26 Big Store 0.01 1,439.53
01-09 Transfer To Checking 900.02- 539.51
01-11 Ending Balance 539.51
Average Daily Balance - Current Cycle: $744.85
Your account earned $0.04, with an annual percentage yield earned of 0.05%
Items Paid
Date Item Amount($) Date Item Amount($)
12-13 POS 10.48 01-02 POS 50.00
12-31 POS 50.00 01-02 POS 50.00
Premier Checking - 2222222222
Joint Owner(s): OTHER PERSON
Date Transaction Detail Amount($) Balance($)
12-12 Beginning Balance 1,000.00
12-13 Deposit - ACH Paid From EMPLOYER Pay 4,000.00 5,000.00
12-13 Transfer From Shares 50.00 5,050.00
Some Person
12-14 Paid To - Power Co Chk 123 1,250.00- 3,800.00
01-11 Ending Balance 3,800.00
Items Paid
Date Item Amount($)
12-14 ACH 1,250.00
"""


def test_parses_sections_rows_and_years():
    checking, duty = parse_statement_pages([PAGE_1, PAGE_2, PAGE_3])
    assert checking.account_suffix == "1111" and duty.account_suffix == "2222"
    assert (checking.period_start, checking.period_end) == (date(2025, 12, 12), date(2026, 1, 11))
    assert (checking.opening_balance, checking.closing_balance) == (D("100.00"), D("539.51"))
    assert [(line.date, line.amount) for line in checking.lines] == [
        (date(2025, 12, 13), D("1500.00")),
        (date(2025, 12, 13), D("-10.48")),
        (date(2025, 12, 31), D("-50.00")),
        (date(2026, 1, 2), D("-50.00")),
        (date(2026, 1, 2), D("-50.00")),
        (date(2026, 1, 5), D("0.01")),
        (date(2026, 1, 9), D("-900.02")),
    ]
    # Wrapped detail is joined; the Items Paid repeats and the address form are ignored.
    assert checking.lines[1].description.endswith("133 Main Smalltown")
    assert [line.amount for line in duty.lines] == [D("4000.00"), D("50.00"), D("-1250.00")]
    assert duty.lines[1].description == "Transfer From Shares"


def test_misread_row_refuses_the_statement():
    broken = PAGE_3.replace("900.02- 539.51", "900.20- 539.51")
    with pytest.raises(StatementParseError, match="does not add up"):
        parse_statement_pages([PAGE_1, PAGE_2, broken])


def test_dropped_row_refuses_the_statement():
    missing = PAGE_3.replace(
        "01-05 POS Credit Adjustment 0000 Transaction 01-03-26 Big Store 0.01 1,439.53\n", ""
    )
    with pytest.raises(StatementParseError):
        parse_statement_pages([PAGE_1, PAGE_2, missing])


def test_not_a_statement():
    with pytest.raises(StatementParseError, match="statement period"):
        parse_statement_pages(["Hello world"])
    with pytest.raises(StatementParseError, match="not a PDF"):
        parse_statement_pdf(b"GIF89a")


def _pdf(pages: list[str]) -> bytes:
    """A minimal text PDF: one Helvetica text line per statement line."""
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        None,
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    kids = []
    for text in pages:
        lines = text.splitlines()
        ops = ["BT", "/F1 9 Tf", "11 TL", "20 780 Td"]
        for line in lines:
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({escaped}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n" + stream.decode("latin-1") + "\nendstream"
        )
        content = len(objects)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content} 0 R >>"
        )
        kids.append(f"{len(objects)} 0 R")
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


def test_reads_a_real_pdf_file():
    sections = parse_statement_pdf(_pdf([PAGE_1, PAGE_2, PAGE_3]))
    assert [s.account_suffix for s in sections] == ["1111", "2222"]
    assert sum(len(s.lines) for s in sections) == 10


@pytest.fixture
def owner(client, db):
    from django.contrib.auth import get_user_model

    from budget.models import Account, Household, HouseholdMembership

    user = get_user_model().objects.create_user("pdf-user", password="a-secure-password-123")
    home = Household.objects.create()
    HouseholdMembership.objects.create(household=home, user=user)
    client.force_login(user)
    session = client.session
    session["active_household_id"] = home.pk
    session.save()
    spending = Account.objects.create(household=home, name="Spending", type="spending")
    bills = Account.objects.create(household=home, name="Bills", type="bills")
    return client, home, spending, bills


def _upload(client, data, name="statement.pdf"):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return client.post(
        "/statements/upload/",
        {"statement": SimpleUploadedFile(name, data, content_type="application/pdf")},
    )


@pytest.mark.django_db
def test_upload_asks_once_which_account_is_which(owner):
    from budget.models import Account, StatementImport, StatementLine

    client, home, spending, bills = owner
    pdf = _pdf([PAGE_1, PAGE_2, PAGE_3])
    response = _upload(client, pdf)
    assert response["Location"] == "/statements/upload/accounts/"
    page = client.get("/statements/upload/accounts/").content.decode()
    assert "e-Checking ending 1111" in page and "Premier Checking ending 2222" in page
    assert "1111111111" not in page  # never the full account number
    response = client.post(
        "/statements/upload/accounts/", {"account_0": spending.pk, "account_1": bills.pk}
    )
    assert response["Location"] == "/statements/"
    assert Account.objects.get(pk=spending.pk).statement_suffix == "1111"
    assert StatementLine.objects.filter(account=spending).count() == 7
    assert StatementLine.objects.filter(account=bills).count() == 3
    # Second upload of the same file: accounts known, nothing duplicated.
    response = _upload(client, pdf)
    assert response["Location"] == "/statements/"
    assert StatementImport.objects.count() == 2 and StatementLine.objects.count() == 10
    review = client.get(f"/statements/{StatementImport.objects.get(account=bills).pk}/")
    assert "Bank: $3,800.00" in review.content.decode()


@pytest.mark.django_db
def test_upload_skip_and_duplicate_choice(owner):
    from budget.models import StatementLine

    client, home, spending, bills = owner
    _upload(client, _pdf([PAGE_1, PAGE_2, PAGE_3]))
    response = client.post(
        "/statements/upload/accounts/", {"account_0": spending.pk, "account_1": spending.pk}
    )
    assert response["Location"] == "/statements/upload/accounts/"
    assert not StatementLine.objects.exists()
    client.post("/statements/upload/accounts/", {"account_0": spending.pk, "account_1": "skip"})
    assert StatementLine.objects.filter(account=spending).count() == 7
    assert not StatementLine.objects.filter(account=bills).exists()


@pytest.mark.django_db
def test_upload_rejects_bad_files(owner):
    client, *_ = owner
    assert "not a PDF" in _upload(client, b"hello").content.decode()
    broken = _pdf([PAGE_1, PAGE_2, PAGE_3.replace("900.02- 539.51", "900.20- 539.51")])
    page = _upload(client, broken).content.decode()
    assert "does not add up" in page and "Nothing was imported" in page


def test_check_command_reads_without_saving(tmp_path, db):
    from io import StringIO

    from django.core.management import call_command

    path = tmp_path / "s.pdf"
    path.write_bytes(_pdf([PAGE_1, PAGE_2, PAGE_3]))
    out = StringIO()
    call_command("check_statement_pdf", str(path), stdout=out)
    text = out.getvalue()
    assert "e-Checking ending 1111: 7 rows, in $1,500.01, out $1,060.50" in text
    assert "Fuel Stop" not in text
