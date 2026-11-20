"""Generic bank statement reader: the fallback when no known layout matches.

It works on the text pypdf extracts (layout mode, so columns keep their spacing) and
looks for the things almost every statement has, not for any one bank's layout:

* a statement period (or a closing date), which also gives the year of year-less row dates;
* account headings with a masked or last-4 number ("ending in 1234", "xxxx1234", ...);
* beginning/ending balance labels;
* rows that start with a date and end with one to three amounts.

An amount's direction (money in or out) comes from, in order: an explicit sign, "CR"/"DR"
or parentheses; the debit or credit column it sits in; the change in the bank's running
balance; the nearest "Deposits"/"Withdrawals" style heading. Then the result is checked:
every running balance must follow from the row before it, and opening balance + rows must
equal the closing balance. A check that fails refuses the statement. When there is nothing
to check against, the section is returned with ``verified=False`` so the user is told to
check each line.

Known limits are listed in docs/calculation-rules.md: rows that share one date heading,
two transactions side by side on one line, columns whose headers wrap over two lines,
statements in other languages or currencies, and scanned PDFs.
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from .statement_pdf import StatementParseError
from .statements import ParsedLine, ParsedSection

MON = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
MONTHS = {
    name: number
    for number, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
# Each pattern captures its parts by name; "a" and "b" are the two numeric parts whose order
# (month/day or day/month) is decided once per statement.
DATE_PATTERNS = [
    re.compile(r"(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})(?![\d/-])"),
    re.compile(r"(?P<a>\d{1,2})(?P<s>[/-])(?P<b>\d{1,2})(?P=s)(?P<y>\d{4}|\d{2})(?![\d/-])"),
    re.compile(r"(?P<a>\d{1,2})\.(?P<b>\d{1,2})\.(?P<y>\d{4})(?![\d/-])"),
    re.compile(r"(?P<a>\d{1,2})[/-](?P<b>\d{1,2})(?![\d/.,-])"),
    re.compile(
        rf"(?P<mon>{MON})\.?\s+(?P<d>\d{{1,2}})(?:,?\s+(?P<y>(?:19|20)\d{{2}}))?(?![\d/])", re.I
    ),
    re.compile(
        rf"(?P<d>\d{{1,2}})\s+(?P<mon>{MON})\.?(?:,?\s+(?P<y>(?:19|20)\d{{2}}))?(?![\d/])", re.I
    ),
]
DATE_AT_START = [re.compile(r"^" + p.pattern, p.flags) for p in DATE_PATTERNS]
SPLIT = r"(?:-|–|—|to|through|thru)"

AMOUNT = re.compile(
    r"(?<![\w.,/])(?P<open>\()?\s?(?P<lead>[-+])?\s?\$?\s?(?P<lead2>[-+])?"
    r"(?P<num>\d{1,3}(?:,\d{3})+|\d+)\.(?P<cents>\d{2})(?P<close>\))?(?P<trail>[-+])?"
    r"(?:\s?(?P<cr>CR|DR|Cr|Dr))?(?![\w.,%])"
)

PERIOD_WORDS = re.compile(r"period|statement|cycle|billing|summary", re.I)
CLOSING_DATE = re.compile(r"(?:statement|closing|billing)\s+date|as of|closing", re.I)
OPENING_LABEL = re.compile(
    r"(?:beginning|opening|previous|starting|prior)\s+(?:account\s+)?balance"
    r"|balance\s+(?:brought\s+)?forward",
    re.I,
)
CLOSING_LABEL = re.compile(
    r"(?:ending|closing|new|final)\s+(?:account\s+)?balance|balance\s+carried\s+forward", re.I
)
BALANCE_LABEL = re.compile(f"({OPENING_LABEL.pattern})|({CLOSING_LABEL.pattern})", re.I)
TOTAL_LINE = re.compile(r"^(?:total|subtotal|sum of)\b", re.I)
PAGE_NOISE = re.compile(
    r"^(?:page\s+\d+(?:\s+of\s+\d+)?|\d+\s+of\s+\d+|continued(?: on next page)?"
    r"|\(?continued(?: from previous page)?\)?)$",
    re.I,
)
# Tables that repeat rows or list other figures: no rows or balances are read from them
# until a new account heading, column header or Deposits/Withdrawals heading appears.
SKIP_HEADING = re.compile(
    r"daily\s+(?:ending\s+)?balances?|items\s+paid|check\s+images?|images?\s+of\s+checks?"
    r"|average\s+daily\s+balance|interest\s+charge|fees?\s+(?:and\s+interest\s+)?summary"
    r"|total\s+fees|year[- ]to[- ]date|important\s+information|disclosures?|rewards?\s+summary",
    re.I,
)
ACTIVATE_HEADING = re.compile(
    r"transaction\s+(?:detail|history|activity)|account\s+(?:activity|detail)|^activity", re.I
)
CREDIT_HINT = re.compile(r"deposits?|credits?|money\s+in|additions?|paid\s+in", re.I)
DEBIT_HINT = re.compile(
    r"withdrawals?|debits?|checks?(?:\s+paid)?|money\s+out|purchases?|subtractions?"
    r"|electronic\s+payments?|paid\s+out",
    re.I,
)
COLUMN_ROLES = [
    (
        "debit",
        re.compile(r"withdrawals?|debits?|money\s+out|paid\s+out|charges|payments?\s+out", re.I),
    ),
    ("credit", re.compile(r"deposits?|credits?|money\s+in|paid\s+in|payments?\s+in", re.I)),
    ("balance", re.compile(r"balance", re.I)),
    ("amount", re.compile(r"amount", re.I)),
]
HEADING_KEY = re.compile(
    r"(?:ending\s+in|ending|account\s+(?:number|no\.?|#)|acct\.?(?:\s*(?:number|no\.?|#))?|account)"
    r"\s*:?\s*(?:[xX*•.#-]+\s*)*(?P<n>\d{4})\b"
    r"|(?:[xX*•]{2,}[\s-]*)+(?P<m>\d{4})\b|\.{3}(?P<d>\d{4})\b"
    r"|\b\d{6,13}(?P<f>\d{4})\b",
    re.I,
)
NOT_HEADING = re.compile(r"routing|phone|call|customer|member|www\.|http|@|\bfax\b", re.I)
MAX_PERIOD_DAYS = 400
MAX_PENDING_LINES = 3


@dataclass
class _Row:
    dates: list[tuple[int, int, int | None]]
    description: str
    amount: Decimal  # absolute value
    explicit: int | None = None  # +1 in, -1 out, from sign, CR/DR or parentheses
    column: int | None = None  # +1 / -1 from the debit or credit column
    hint: int | None = None  # +1 / -1 from the section heading
    balance: Decimal | None = None


@dataclass
class _Sec:
    suffix: str = ""
    name: str = ""
    opening: Decimal | None = None
    closing: Decimal | None = None
    opening_seen: bool = False
    closing_seen: bool = False
    rows: list[_Row] = field(default_factory=list)
    skipped: int = 0  # row-like lines that could not be read

    def set_balance(self, which: str, value: Decimal):
        # A label seen twice with different figures is ambiguous, so it is not trusted.
        if getattr(self, f"{which}_seen") and getattr(self, which) != value:
            setattr(self, which, None)
        elif not getattr(self, f"{which}_seen"):
            setattr(self, which, value)
        setattr(self, f"{which}_seen", True)


def _sign_of(match) -> int | None:
    if match["lead"] == "-" or match["lead2"] == "-" or match["trail"] == "-":
        return -1
    if match["open"] and match["close"]:
        return -1
    if (match["cr"] or "").upper() == "DR":
        return -1
    if match["lead"] == "+" or match["lead2"] == "+" or match["trail"] == "+":
        return 1
    if (match["cr"] or "").upper() == "CR":
        return 1
    return None


def _value(match) -> Decimal:
    return Decimal(f"{match['num'].replace(',', '')}.{match['cents']}")


def _signed(match) -> Decimal:
    return -_value(match) if _sign_of(match) == -1 else _value(match)


def _trailing_amounts(raw: str):
    """The run of amounts at the end of a line (at most 3), with the text before it."""
    # A lone dash after an amount ("50.00 - 1,539.52") marks money out; same line length.
    raw = re.sub(r"(?<=\d\.\d\d)( +)-( +)(?=[\d$(])", lambda m: "-" + m[1] + m[2], raw)
    end = len(raw.rstrip())
    run: list[re.Match[str]] = []
    for match in reversed(list(AMOUNT.finditer(raw))):
        if match.end() != end or len(run) == 3:
            break
        run.append(match)
        end = len(raw[: match.start()].rstrip())
    run.reverse()
    return run, (raw[: run[0].start()].strip() if run else raw.strip())


def _date_at_start(text: str):
    """Parse one date at the start of ``text``: (parts, rest of text) or None."""
    for pattern in DATE_AT_START:
        match = pattern.match(text)
        if match and (match.end() == len(text) or text[match.end()].isspace()):
            return match.groupdict(), text[match.end() :].lstrip()
    return None


def _parts(groups: dict, day_first: bool):
    """(month, day, year or None) from a pattern's named groups."""
    year = groups.get("y")
    year = (int(year) + 2000 if len(year) == 2 else int(year)) if year else None
    if groups.get("mon"):
        return MONTHS[groups["mon"][:3].lower()], int(groups["d"]), year
    if groups.get("m"):
        return int(groups["m"]), int(groups["d"]), year
    a, b = int(groups["a"]), int(groups["b"])
    return (b, a, year) if day_first else (a, b, year)


def _real_date(month: int, day: int, year: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _find_dates(text: str, day_first: bool):
    """Every date in a line, in order, as (month, day, year or None)."""
    found = []
    taken: list[tuple[int, int]] = []
    for pattern in DATE_PATTERNS:
        for match in pattern.finditer(text):
            if any(match.start() < end and start < match.end() for start, end in taken):
                continue
            taken.append((match.start(), match.end()))
            found.append((match.start(), _parts(match.groupdict(), day_first)))
    return [parts for _, parts in sorted(found, key=lambda item: item[0])]


def _day_first(text: str) -> bool:
    """Numeric dates are month/day unless a first part above 12 shows day/month."""
    first_big = second_big = False
    for match in re.finditer(
        r"(?<![\d/.-])(\d{1,2})(?:[/-](\d{1,2})(?:[/-]\d{2,4})?|\.(\d{1,2})\.\d{4})(?![\d/])",
        text,
    ):
        a, b = int(match[1]), int(match[2] or match[3])
        first_big |= a > 12 >= b
        second_big |= b > 12 >= a
    if first_big and second_big:
        raise StatementParseError("The dates on this statement mix day-first and month-first.")
    return first_big


def _with_year(parts, anchor_year: int, ceiling: date | None = None):
    month, day, year = parts
    if year is not None:
        return _real_date(month, day, year)
    candidate = _real_date(month, day, anchor_year)
    if candidate is not None and ceiling is not None and candidate > ceiling:
        return _real_date(month, day, anchor_year - 1)
    return candidate


def _find_period(lines: list[str], day_first: bool):
    """(start, end) from the best line holding two dates, else a lone closing date."""
    best = None
    single = None
    month_range = re.compile(
        rf"(?P<mon>{MON})\.?\s+(?P<d1>\d{{1,2}})\s*{SPLIT}\s*(?P<d2>\d{{1,2}}),?\s+(?P<y>\d{{4}})",
        re.I,
    )
    for raw in lines:
        text = " ".join(raw.split())
        if not text or _trailing_amounts(raw)[0]:
            continue
        ranged = month_range.search(text)
        if ranged:
            month = MONTHS[ranged["mon"][:3].lower()]
            year = int(ranged["y"])
            start = _real_date(month, int(ranged["d1"]), year)
            end = _real_date(month, int(ranged["d2"]), year)
        else:
            dates = _find_dates(text, day_first)
            start = end = None
            if len(dates) >= 2 and dates[1][2]:
                end = _real_date(dates[1][0], dates[1][1], dates[1][2])
                if end is not None:
                    start = _with_year(dates[0], end.year, ceiling=end)
            elif len(dates) == 1 and dates[0][2] and CLOSING_DATE.search(text):
                single = single or _real_date(dates[0][0], dates[0][1], dates[0][2])
        if start and end and start <= end:
            if PERIOD_WORDS.search(text):
                return start, end
            best = best or (start, end)
    return best or ((None, single) if single else None)


def _heading(text: str):
    """(name, last 4 digits) when the line heads an account section, else None."""
    if _trailing_amounts(text)[0] or NOT_HEADING.search(text) or _date_at_start(text):
        return None
    match = HEADING_KEY.search(text)
    if not match:
        return None
    suffix = match["n"] or match["m"] or match["d"] or match["f"]
    name = re.sub(r"\(?continued[^)]*\)?", "", text[: match.start()], flags=re.I)
    name = re.sub(r"[\s:,\-–|#]+$", "", name)
    name = re.sub(r"^[\s:,\-–|]+", "", name).strip()
    name = re.sub(
        r"(?:[,:\-\u2013|]\s*(?:account|acct\.?)|account\s*(?:number|no\.?|#))\s*$",
        "",
        name,
        flags=re.I,
    )
    name = re.sub(r"[\s:,\-\u2013|#]+$", "", name)
    if re.fullmatch(r"(?:account|acct\.?)?(?:\s*(?:number|no\.?|#))?", name, re.I):
        name = ""
    return name, suffix


def _columns(raw: str):
    """Column roles and their positions when the line is a table header, else None."""
    if _trailing_amounts(raw)[0] or _date_at_start(raw.strip()):
        return None
    cols = []
    for role, pattern in COLUMN_ROLES:
        for match in pattern.finditer(raw):
            cols.append((match.start(), match.end(), role))
    cols.sort()
    roles = {role for _, _, role in cols}
    words = len(raw.split())
    has_date = re.search(r"\bdate\b", raw, re.I) is not None
    if (len(roles) >= 2 or (roles and has_date)) and (words <= 14 or has_date):
        return cols
    return None


def _role_of(match, cols):
    """The header role nearest to where an amount sits on the line."""
    best: tuple[tuple[int, int], str] | None = None
    for start, end, role in cols:
        gap = (
            0
            if match.start() < end and start < match.end()
            else min(abs(match.end() - end), abs(match.start() - start))
        )
        centre = abs((match.start() + match.end()) - (start + end))
        key = (gap, centre)
        if best is None or key < best[0]:
            best = (key, role)
    return best[1] if best else "amount"


def _hint(text: str):
    """+1 / -1 for a short heading such as 'Deposits and other credits', else None."""
    if len(text.split()) > 7 or text.endswith(".") or any(c.isdigit() for c in text):
        return None
    credit, debit = CREDIT_HINT.search(text), DEBIT_HINT.search(text)
    if credit and debit:
        return None
    return 1 if credit else -1 if debit else None


def _build_row(dates, description, run, cols, hint):
    """Turn a date, description and amount run into a row (None if unreadable)."""
    if cols is not None and len({role for _, _, role in cols}) < 2:
        cols = None  # a lone "Amount" header says nothing about direction
    amounts, balance = [], None
    for match in run:
        role = _role_of(match, cols) if cols else None
        if role == "balance":
            balance = _signed(match)
        else:
            amounts.append((match, +1 if role == "credit" else -1 if role == "debit" else None))
    if not cols:
        if len(run) == 2:
            amounts, balance = [(run[0], None)], _signed(run[1])
        elif len(run) > 2:
            return None
    if len(amounts) != 1 or _value(amounts[0][0]) == 0:
        return None
    match, column = amounts[0]
    return _Row(
        dates, " ".join(description.split()), _value(match), _sign_of(match), column, hint, balance
    )


def _resolve(rows: list[_Row], opening: Decimal | None):
    """Signed amount per row: explicit sign, column, running-balance change, heading."""
    signed = []
    previous = opening
    for row in rows:
        sign = row.explicit or row.column
        if sign is None and row.balance is not None and previous is not None:
            diff = row.balance - previous
            if abs(diff) == row.amount:
                sign = 1 if diff > 0 else -1
        if sign is None:
            sign = row.hint
        if sign is None:
            return None
        signed.append(sign * row.amount)
        previous = (
            row.balance
            if row.balance is not None
            else (previous + sign * row.amount if previous is not None else None)
        )
    return signed


def _check(rows, signed, opening, closing):
    """(ok, checked, message): every running balance and the totals must reconcile."""
    checked = False
    running = opening
    for row, amount in zip(rows, signed, strict=True):
        if row.balance is not None:
            if running is not None:
                if running + amount != row.balance:
                    return False, True, "a row does not add up to the balance shown"
                checked = True
            running = row.balance
        elif running is not None:
            running += amount
    if closing is not None and running is not None:
        if running != closing:
            return False, True, "the rows do not add up to the ending balance"
        checked = True
    return True, checked, ""


def parse_generic_pages(pages: list[str]) -> list[ParsedSection]:
    lines = [line for text in pages for line in text.splitlines()]
    day_first = _day_first("\n".join(lines))
    period = _find_period(lines, day_first)

    sections: dict[str, _Sec] = {"": _Sec()}
    order = [""]
    current = sections[""]
    previous = ""  # the last non-blank line, which can name an account ("Checking")
    for text in pages:
        cols = None
        hint = None
        skip = False
        pending = None  # (dates, description, lines used): the amounts are on a later line
        last: _Row | None = None  # the row just read, which a wrapped line can extend
        extra = 0
        row_indent = 0
        for raw in text.splitlines():
            raw = raw.rstrip()
            line = " ".join(raw.split())
            if not line:
                last = None
                continue
            before_line, previous = previous, line
            indent = len(raw) - len(raw.lstrip())
            if PAGE_NOISE.match(line) or TOTAL_LINE.match(line):
                pending = None
                continue
            run, text_before = _trailing_amounts(raw)
            start = _date_at_start(line)
            wrapped = (
                (pending is not None or last is not None) and not start and indent > row_indent
            )
            head = None if wrapped else _heading(line)
            if head:
                name, suffix = head
                if not name and before_line and not re.search(r"\d", before_line):
                    name = before_line
                if suffix not in sections:
                    sections[suffix] = _Sec(suffix, name)
                    order.append(suffix)
                elif name and not sections[suffix].name:
                    sections[suffix].name = name
                if suffix != current.suffix:
                    cols, hint = None, None
                current = sections[suffix]
                skip, pending, last = False, None, None
                continue
            body = start[1] if start else line
            label = None if wrapped else BALANCE_LABEL.match(body)
            if label:
                if len(run) == 1 and not skip:
                    current.set_balance("opening" if label[1] else "closing", _signed(run[0]))
                pending = last = None
                continue
            columns = None if wrapped else _columns(raw)
            if columns:
                cols, skip, pending, last = columns, False, None, None
                continue
            if not start and not run and not wrapped:
                if SKIP_HEADING.search(line):
                    skip, pending, last = True, None, None
                    continue
                if ACTIVATE_HEADING.search(line):
                    skip, pending, last = False, None, None
                    continue
                found = _hint(line)
                if found is not None:
                    hint, skip, pending, last = found, False, None, None
                    continue
            if skip:
                continue
            if start:
                groups, rest = start
                dates = [_parts(groups, day_first)]
                second = _date_at_start(rest)
                if second:  # transaction date then posting date
                    dates.append(_parts(second[0], day_first))
                    rest = second[1]
                row_indent = indent
                if run:
                    row = _build_row(dates, _trailing_amounts(rest)[1], run, cols, hint)
                    if row:
                        current.rows.append(row)
                    else:
                        current.skipped += 1
                    last, pending, extra = row, None, 0
                elif sum(c.isalpha() for c in rest) >= 3:
                    pending, last = (dates, rest, 1), None
                else:
                    pending = last = None
            elif run and pending:
                dates, description, _used = pending
                row = _build_row(dates, f"{description} {text_before}", run, cols, hint)
                if row:
                    current.rows.append(row)
                else:
                    current.skipped += 1
                last, pending, extra = row, None, 0
            elif not run and pending:
                dates, description, used = pending
                pending = (dates, f"{description} {line}", used + 1)
                if used >= MAX_PENDING_LINES:
                    pending = None
            elif not run and last is not None and extra < MAX_PENDING_LINES:
                last.description = f"{last.description} {line}".strip()
                extra += 1

    return _finish(sections, order, period, day_first)


def _finish(sections, order, period, day_first) -> list[ParsedSection]:
    default = sections[""]
    real = [key for key in order if key and sections[key].rows]
    if not default.rows and len(real) == 1:
        target = sections[real[0]]
        for which in ("opening", "closing"):
            if getattr(target, which) is None and not getattr(target, f"{which}_seen"):
                setattr(target, which, getattr(default, which))
    used = [sections[key] for key in order if sections[key].rows]
    if not used:
        if period is None:
            raise StatementParseError("No statement period was found. Is this a bank statement?")
        raise StatementParseError(
            "No transaction rows were recognised in this statement. Nothing was imported."
        )
    all_dates = [d for sec in used for row in sec.rows for d in row.dates]
    start, end = period if period else (None, None)
    if end is None:
        if not all(d[2] for d in all_dates):
            raise StatementParseError("No statement period was found. Is this a bank statement?")
    result = []
    for number, sec in enumerate(used, 1):
        result.append(_section(sec, number, len(used), start, end, day_first))
    return result


def _row_date(row: _Row, start, end):
    """The row's date inside the period (posting date preferred), else None."""
    for parts in reversed(row.dates):
        if parts[2] is not None:
            candidate = _real_date(parts[0], parts[1], parts[2])
            if candidate and (end is None or start is None or start <= candidate <= end):
                return candidate
            continue
        if end is None:
            continue
        for year in (end.year, end.year - 1):
            candidate = _real_date(parts[0], parts[1], year)
            if candidate and (start is None or start <= candidate) and candidate <= end:
                return candidate
    return None


def _section(sec: _Sec, number: int, total: int, start, end, day_first) -> ParsedSection:
    base = sec.name or ("Statement" if total == 1 else f"Account {number}")
    label = f"{base} ending {sec.suffix}" if sec.suffix else base
    if end is not None and start is None:
        # Only a closing date was found: the period starts at the earliest row.
        starts = [d for row in sec.rows for d in [_row_date(row, None, end)] if d]
        start = min(starts) if starts else end - timedelta(days=30)
    kept, dated = [], []
    for row in sec.rows:
        when = _row_date(row, start, end)
        if when is None:
            sec.skipped += 1
            continue
        kept.append(row)
        dated.append(when)
    if not kept:
        raise StatementParseError(
            f"{label}: no transaction rows could be read. Nothing was imported."
        )
    if start is None:
        start, end = min(dated), max(dated)
    if end < start or (end - start).days > MAX_PERIOD_DAYS:
        raise StatementParseError("The statement period is not readable.")
    signed = _resolve(kept, sec.opening)
    if signed is None:
        raise StatementParseError(
            f"{label}: could not tell which lines are money in and which are money out. "
            "Nothing was imported."
        )
    ok, checked, why = _check(kept, signed, sec.opening, sec.closing)
    opening, closing = sec.opening, sec.closing
    both = opening is not None and closing is not None
    net = sum(signed, Decimal(0))
    if opening is not None and closing is not None:
        if ok and opening + net != closing:
            ok, why = False, "the rows do not add up to the ending balance"
        if not ok and not any(row.balance is not None for row in kept):
            # Balances that count up with purchases (card statements): money out raises them.
            if opening - net == closing:
                ok, checked, opening, closing = True, True, -opening, -closing
    if not ok:
        raise StatementParseError(f"{label}: {why}. Nothing was imported.")
    verified = checked and (not sec.skipped or both)
    lines = [
        ParsedLine(when, amount, row.description)
        for when, amount, row in zip(dated, signed, kept, strict=True)
    ]
    return ParsedSection(
        account_label=label,
        account_suffix=sec.suffix,
        period_start=start,
        period_end=end,
        lines=lines,
        opening_balance=opening,
        closing_balance=closing,
        verified=verified,
    )
