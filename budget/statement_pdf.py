"""Read bank statement PDFs into statement sections.

This module holds one specific layout, read by its own rules, and the PDF safety limits. A
statement in any other layout goes to the generic reader in ``statement_generic``.

Layout:

* "Statement Period" followed by ``MM/DD/YY - MM/DD/YY``.
* One section per account, headed ``<Account name> - <account number>``; a section that
  continues on the next page repeats the heading with "(Continued from previous page)".
* Rows: ``MM-DD <detail> <amount>[-] <balance>``; a trailing ``-`` marks money out. A long
  detail can wrap so the amounts sit on the next line, and some rows have an extra detail
  line after the amounts (ignored: the bank text is reference only).
* ``Beginning Balance`` and ``Ending Balance`` rows carry only a balance.
* After each account's rows come "Average Daily Balance", dividend text and an
  "Items Paid" table that repeats rows already listed; those are skipped.

Every row's running balance is checked, and opening + rows must equal the ending balance,
so a misread line refuses the whole section instead of staging wrong numbers.
"""

import logging
import re
from datetime import date
from decimal import Decimal
from io import BytesIO

from .statements import ParsedLine, ParsedSection

MAX_PDF_BYTES = 10 * 1024 * 1024
# A monthly statement is a handful of pages; this stops a small, page-heavy file (which a 10 MB
# limit allows) from tying up a worker in text extraction.
MAX_PAGES = 50

PERIOD = re.compile(r"(\d{2})/(\d{2})/(\d{2})\s*-\s*(\d{2})/(\d{2})/(\d{2})")
SECTION = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z0-9 &'./-]*?)\s+-\s+(?P<number>\d{6,17})"
    r"(?:\s*\(Continued from previous page\))?$"
)
ROW = re.compile(r"^(?P<month>\d{2})-(?P<day>\d{2})\s+(?P<rest>.*)$")
MONEY = r"\d{1,3}(?:,\d{3})*\.\d{2}"
# The money-out minus may touch the amount ("2.99-") or stand apart ("2.99 - 546.18").
AMOUNTS = re.compile(rf"(?P<amount>{MONEY})(?:\s*(?P<out>-))?\s+(?P<balance>-?{MONEY}-?)\s*$")
KNOWN_ROW = re.compile(r"^\d{2}-\d{2}\s+beginning balance\b", re.I | re.M)
SINGLE = re.compile(rf"(?P<balance>-?{MONEY}-?)\s*$")
TABLE_END = (
    "average daily balance",
    "items paid",
    "your account earned",
    "disclosure information",
)


class StatementParseError(ValueError):
    """The PDF is not a statement this parser can read reliably."""


def _money(text: str) -> Decimal:
    negative = text.startswith("-") or text.endswith("-")
    value = Decimal(text.strip("-").replace(",", ""))
    return -value if negative else value


class _Section:
    def __init__(self, name: str, number: str):
        self.name = name
        self.number = number
        self.opening: Decimal | None = None
        self.closing: Decimal | None = None
        self.rows: list[tuple[int, int, str, Decimal, Decimal]] = []


def _is_known_layout(pages: list[str]) -> bool:
    text = "\n".join(pages)
    if not (PERIOD.search(text) and KNOWN_ROW.search(text)):
        return False
    return any(SECTION.match(" ".join(line.split())) for line in text.splitlines())


def parse_statement_pages(pages: list[str]) -> list[ParsedSection]:
    """Parse the text of each page (in order) into one section per account.

    The known layout is tried first; any other statement uses the generic reader.
    """
    if not _is_known_layout(pages):
        from .statement_generic import parse_generic_pages

        return parse_generic_pages(pages)
    period = None
    for text in pages:
        match = PERIOD.search(text)
        if match:
            m1, d1, y1, m2, d2, y2 = (int(v) for v in match.groups())
            period = (date(2000 + y1, m1, d1), date(2000 + y2, m2, d2))
            break
    if period is None:
        raise StatementParseError("No statement period was found. Is this a bank statement?")
    start, end = period
    if end < start:
        raise StatementParseError("The statement period is not readable.")

    sections: dict[str, _Section] = {}
    for text in pages:
        current: _Section | None = None
        in_rows = False
        pending: tuple[int, int, str] | None = None  # a row whose amounts wrapped
        for raw in text.splitlines():
            line = " ".join(raw.split())
            if not line:
                continue
            header = SECTION.match(line)
            if header:
                number = header["number"]
                current = sections.setdefault(number, _Section(header["name"].strip(), number))
                in_rows, pending = True, None
                continue
            if current is None or not in_rows:
                continue
            if line.casefold().startswith(TABLE_END):
                in_rows, pending = False, None
                continue
            row = ROW.match(line)
            if row:
                pending = None
                month, day, rest = int(row["month"]), int(row["day"]), row["rest"]
            elif pending:
                month, day, previous = pending
                rest = f"{previous} {line}"
            else:
                continue  # extra detail after a complete row, or page furniture
            lowered = rest.casefold()
            if lowered.startswith(("beginning balance", "ending balance")):
                single = SINGLE.search(rest)
                if single is None:
                    raise StatementParseError(f"Could not read a balance in {current.name}.")
                if lowered.startswith("beginning"):
                    current.opening = _money(single["balance"])
                else:
                    current.closing = _money(single["balance"])
                pending = None
                continue
            amounts = AMOUNTS.search(rest)
            if amounts is None:
                pending = (month, day, rest)
                continue
            amount = _money(amounts["amount"])
            if amounts["out"]:
                amount = -amount
            detail = rest[: amounts.start()].strip()
            current.rows.append((month, day, detail, amount, _money(amounts["balance"])))
            pending = None

    if not sections:
        raise StatementParseError("No account sections were found in this statement.")
    return [_finish(section, start, end) for section in sections.values()]


def _row_date(month: int, day: int, start: date, end: date) -> date:
    try:
        candidate = date(end.year, month, day)
        if candidate > end:
            candidate = date(end.year - 1, month, day)
    except ValueError as exc:
        raise StatementParseError(f"A row date {month:02d}-{day:02d} is not valid.") from exc
    if not start <= candidate <= end:
        raise StatementParseError(f"A row dated {candidate:%b %-d, %Y} is outside the period.")
    return candidate


def _finish(section: _Section, start: date, end: date) -> ParsedSection:
    label = f"{section.name} ending {section.number[-4:]}"
    if section.opening is None or section.closing is None:
        raise StatementParseError(f"{label}: the beginning or ending balance is missing.")
    running = section.opening
    lines = []
    for month, day, detail, amount, balance in section.rows:
        running += amount
        when = _row_date(month, day, start, end)
        if running != balance:
            raise StatementParseError(
                f"{label}: the row on {when:%b %-d} does not add up to the balance shown. "
                "Nothing was imported."
            )
        lines.append(ParsedLine(when, amount, detail))
    if running != section.closing:
        raise StatementParseError(
            f"{label}: the rows do not add up to the ending balance. Nothing was imported."
        )
    return ParsedSection(
        account_label=label,
        account_suffix=section.number[-4:],
        period_start=start,
        period_end=end,
        lines=lines,
        opening_balance=section.opening,
        closing_balance=section.closing,
    )


def parse_statement_pdf(data: bytes) -> list[ParsedSection]:
    if len(data) > MAX_PDF_BYTES:
        raise StatementParseError("The file is larger than 10 MB.")
    if not data.startswith(b"%PDF-"):
        raise StatementParseError("That file is not a PDF.")
    from pypdf import PdfReader

    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise StatementParseError("The PDF is password-protected; download it again unlocked.")
        if len(reader.pages) > MAX_PAGES:
            raise StatementParseError(
                f"The PDF has more than {MAX_PAGES} pages; upload one statement at a time."
            )
        # Layout mode keeps each printed row on one line; the default mode reads the detail
        # and amount columns separately on these statements.
        pages = [page.extract_text(extraction_mode="layout") or "" for page in reader.pages]
    except StatementParseError:
        raise
    except Exception as exc:  # pypdf raises many kinds of error on damaged or hostile files
        logging.getLogger("budget.security").warning(
            "Statement PDF could not be read: %s", type(exc).__name__
        )
        raise StatementParseError("The PDF could not be read.") from exc
    if not any(page.strip() for page in pages):
        raise StatementParseError(
            "This PDF has no readable text. It may be a scan or a photo; download the "
            "statement itself from your bank (not a scan) and upload that."
        )
    return parse_statement_pages(pages)
