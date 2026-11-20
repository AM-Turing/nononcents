"""Monthly statement check: stage bank statement lines and match them to manual entries.

Manual entry stays the record. A statement is only a completeness check:

* Lines are staged, never written to the ledger directly.
* Matching uses account, exact amount and a date window. It never looks at merchant text:
  the user's merchant and category always win, and the bank description is shown only
  for reference.
* The owner confirms a proposed match with one tap ("Same purchase"); that marks the entry
  cleared. Unmatched lines can be added as a new entry or dismissed. Entries on the account
  that no line matched are listed as "not on statement yet".
* Re-importing the same file, or an overlapping statement, never duplicates a line (each
  line has a per-account fingerprint).
"""

import hashlib
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Account, StatementImport, StatementLine
from .reconciliation import LedgerItem, _set_cleared, account_ledger
from .services import audit, money

# A manual entry is usually dated on or before the day the bank posts it.
MATCH_DAYS_BEFORE = 7  # entry up to 7 days before the bank date
MATCH_DAYS_AFTER = 3  # or up to 3 days after it
MAX_CANDIDATES = 3


@dataclass(frozen=True)
class ParsedLine:
    date: date
    amount: Decimal  # signed: + money in, - money out
    description: str = ""


@dataclass
class ParsedSection:
    """One account's part of a statement, as read from the PDF."""

    account_label: str
    period_start: date
    period_end: date
    lines: list[ParsedLine] = field(default_factory=list)
    closing_balance: Decimal | None = None
    opening_balance: Decimal | None = None
    account_suffix: str = ""  # last 4 digits of the bank account number
    # False when nothing on the statement could confirm the rows (no balances to add up).
    verified: bool = True


def _normalise(text: str) -> str:
    return " ".join(text.split()).casefold()


def fingerprints(lines: list[ParsedLine]) -> list[str]:
    """Stable per-line keys; identical lines on one day are told apart by their order."""
    seen: dict[tuple, int] = {}
    result = []
    for line in lines:
        base = (line.date.isoformat(), str(money(line.amount)), _normalise(line.description))
        index = seen.get(base, 0)
        seen[base] = index + 1
        result.append(hashlib.sha256("|".join((*base, str(index))).encode()).hexdigest())
    return result


@dataclass
class StageResult:
    statement: StatementImport
    created: int
    already_imported: int
    is_new_file: bool


def stage_statement(
    *, household, actor, account: Account, section: ParsedSection, file_sha256: str
) -> StageResult:
    """Store a statement section's lines for review. Safe to repeat with the same file."""
    if account.household_id != household.pk:
        raise ValidationError("Choose an account from this budget profile.")
    if section.period_end < section.period_start:
        raise ValidationError("The statement period ends before it starts.")
    for line in section.lines:
        if money(line.amount) == 0:
            raise ValidationError("A statement line has no amount.")
        if not section.period_start <= line.date <= section.period_end:
            raise ValidationError(
                f"A statement line dated {line.date:%b %-d, %Y} is outside the statement period."
            )
    with transaction.atomic():
        existing = StatementImport.objects.filter(account=account, file_sha256=file_sha256).first()
        if existing:
            return StageResult(existing, 0, existing.lines.count(), False)
        statement = StatementImport.objects.create(
            household=household,
            account=account,
            period_start=section.period_start,
            period_end=section.period_end,
            closing_balance=section.closing_balance,
            verified=section.verified,
            file_sha256=file_sha256,
            created_by=actor,
        )
        prints = fingerprints(section.lines)
        known = set(
            StatementLine.objects.filter(account=account, fingerprint__in=prints).values_list(
                "fingerprint", flat=True
            )
        )
        rows = [
            StatementLine(
                statement=statement,
                account=account,
                date=line.date,
                amount=money(line.amount),
                description=line.description[:255],
                fingerprint=fp,
            )
            for line, fp in zip(section.lines, prints, strict=True)
            if fp not in known
        ]
        try:
            StatementLine.objects.bulk_create(rows)
        except IntegrityError as exc:  # concurrent import of an overlapping statement
            raise ValidationError("This statement is being imported already; try again.") from exc
        audit(
            household,
            actor,
            statement,
            "create",
            {"account": account.name, "lines": len(rows), "already_imported": len(known)},
        )
    return StageResult(statement, len(rows), len(known), True)


def claimed_keys(account: Account) -> set[str]:
    """Ledger keys already confirmed against some statement line on this account."""
    return set(
        StatementLine.objects.filter(account=account, status=StatementLine.Status.MATCHED)
        .exclude(matched_key="")
        .values_list("matched_key", flat=True)
    )


def candidates_for(
    line: StatementLine, ledger: list[LedgerItem], claimed: set[str]
) -> list[LedgerItem]:
    """Entries that could be the same movement: same signed amount, close date, unclaimed."""
    earliest = line.date - timedelta(days=MATCH_DAYS_BEFORE)
    latest = line.date + timedelta(days=MATCH_DAYS_AFTER)
    options = [
        item
        for item in ledger
        if item.base != "adjustment"
        and item.amount == line.amount
        and earliest <= item.date <= latest
        and item.key not in claimed
    ]
    # Closest date first; on a tie prefer the entry dated before the bank date, then oldest.
    options.sort(key=lambda i: (abs((line.date - i.date).days), i.date > line.date, i.sort_key))
    return options[:MAX_CANDIDATES]


@dataclass
class ReviewRow:
    line: StatementLine
    proposed: LedgerItem | None
    alternatives: list[LedgerItem]
    matched: LedgerItem | None = None


@dataclass
class Review:
    statement: StatementImport
    pending: list[ReviewRow]
    matched: list[ReviewRow]
    dismissed: list[StatementLine]
    not_on_statement: list[LedgerItem]
    ledger_at_end: Decimal = Decimal("0.00")

    @property
    def balance_difference(self) -> Decimal | None:
        """Bank ending balance minus the app's balance on the same day."""
        closing = self.statement.closing_balance
        return None if closing is None else money(closing - self.ledger_at_end)

    @property
    def proposed_count(self) -> int:
        return sum(1 for row in self.pending if row.proposed)


def build_review(statement: StatementImport) -> Review:
    """Proposals for every pending line, assigned so no entry is proposed twice."""
    account = statement.account
    ledger = account_ledger(account)
    by_key = {item.key: item for item in ledger}
    claimed = claimed_keys(account)
    lines = list(statement.lines.order_by("date", "pk"))
    pending, matched, dismissed = [], [], []
    proposed_keys: set[str] = set()
    for line in lines:
        if line.status == StatementLine.Status.MATCHED:
            matched.append(ReviewRow(line, None, [], by_key.get(line.matched_key)))
        elif line.status == StatementLine.Status.DISMISSED:
            dismissed.append(line)
        else:
            options = candidates_for(line, ledger, claimed)
            free = [o for o in options if o.key not in proposed_keys]
            proposed = free[0] if free else None
            if proposed:
                proposed_keys.add(proposed.key)
            pending.append(ReviewRow(line, proposed, [o for o in options if o is not proposed]))
    not_on_statement = [
        item
        for item in ledger
        if item.base != "adjustment"
        and item.key not in claimed
        and item.key not in proposed_keys
        and item.cleared_at is None
        and item.date <= statement.period_end
        and item.date >= statement.period_start - timedelta(days=MATCH_DAYS_AFTER)
    ]
    through = [item for item in ledger if item.date <= statement.period_end]
    ledger_at_end = through[-1].balance_after if through else money(account.opening_balance)
    return Review(statement, pending, matched, dismissed, not_on_statement, ledger_at_end)


def _locked_line(household, line_id) -> StatementLine:
    line = (
        StatementLine.objects.select_for_update()
        .select_related("statement", "account")
        .filter(pk=line_id, statement__household=household)
        .first()
    )
    if line is None:
        raise ValidationError("That statement line no longer exists.")
    return line


def confirm_match(*, household, actor, line_id: int, key: str) -> LedgerItem:
    """'Same purchase': link the line to the entry and mark the entry cleared."""
    with transaction.atomic():
        line = _locked_line(household, line_id)
        if line.status != StatementLine.Status.PENDING:
            raise ValidationError("That statement line was already reviewed.")
        item = next((i for i in account_ledger(line.account) if i.key == key), None)
        if item is None or item.base == "adjustment":
            raise ValidationError("That entry is no longer on this account.")
        if item.amount != line.amount:
            raise ValidationError("That entry's amount does not match the statement line.")
        if key in claimed_keys(line.account):
            raise ValidationError("That entry is already matched to another statement line.")
        now = timezone.now()
        if item.cleared_at is None:
            _set_cleared(item, now)
        line.status = StatementLine.Status.MATCHED
        line.matched_key = key
        line.resolved_by = actor
        line.resolved_at = now
        line.save(update_fields=["status", "matched_key", "resolved_by", "resolved_at"])
        audit(household, actor, line, "update", {"status": line.status, "matched_key": key})
    return item


def confirm_all_proposed(*, household, actor, statement: StatementImport) -> int:
    """Confirm every proposed match on the review page in one step."""
    review = build_review(statement)
    count = 0
    with transaction.atomic():
        for row in review.pending:
            if row.proposed:
                confirm_match(
                    household=household, actor=actor, line_id=row.line.pk, key=row.proposed.key
                )
                count += 1
    return count


def dismiss_line(*, household, actor, line_id: int) -> None:
    with transaction.atomic():
        line = _locked_line(household, line_id)
        if line.status != StatementLine.Status.PENDING:
            raise ValidationError("That statement line was already reviewed.")
        line.status = StatementLine.Status.DISMISSED
        line.resolved_by = actor
        line.resolved_at = timezone.now()
        line.save(update_fields=["status", "resolved_by", "resolved_at"])
        audit(household, actor, line, "update", {"status": line.status})


def reopen_line(*, household, actor, line_id: int) -> None:
    """Undo a match or dismissal. The entry keeps its cleared mark (clear it from Accounts)."""
    with transaction.atomic():
        line = _locked_line(household, line_id)
        line.status = StatementLine.Status.PENDING
        line.matched_key = ""
        line.resolved_by = None
        line.resolved_at = None
        line.save(update_fields=["status", "matched_key", "resolved_by", "resolved_at"])
        audit(household, actor, line, "update", {"status": line.status})


def release_statement_match(household, actor, account: Account, key: str) -> int:
    """Return lines matched to ``key`` to review after the entry's amount changed or it
    was removed: the match no longer holds. Returns the number of lines reopened."""
    lines = StatementLine.objects.select_for_update().filter(
        account=account,
        statement__household=household,
        status=StatementLine.Status.MATCHED,
        matched_key=key,
    )
    count = 0
    for line in lines:
        line.status = StatementLine.Status.PENDING
        line.matched_key = ""
        line.resolved_by = None
        line.resolved_at = None
        line.save(update_fields=["status", "matched_key", "resolved_by", "resolved_at"])
        audit(household, actor, line, "update", {"status": line.status, "reason": "entry changed"})
        count += 1
    return count


def release_entry_matches(household, actor, kind: str, obj) -> int:
    """Release every statement line matched to ``obj`` (an expense, refund, transfer or
    income); call before an amount edit saves or before a delete. Returns lines reopened."""
    if kind == "expense" or kind == "refund":
        targets = [(obj.account, f"{kind}:{obj.pk}")]
    elif kind == "transfer":
        targets = [
            (obj.source, f"transfer_out:{obj.pk}"),
            (obj.destination, f"transfer_in:{obj.pk}"),
        ]
    else:
        targets = [(a.account, f"allocation:{a.pk}") for a in obj.allocations.all()]
    return sum(release_statement_match(household, actor, account, key) for account, key in targets)


def link_new_entry(*, household, actor, line_id: int, account: Account, key: str, amount) -> bool:
    """After 'Add as new entry' saves, link it to the line if it still fits; True if linked."""
    try:
        line = StatementLine.objects.get(pk=line_id, statement__household=household)
    except StatementLine.DoesNotExist:
        return False
    if (
        line.status != StatementLine.Status.PENDING
        or line.account_id != account.pk
        or abs(line.amount) != money(amount)
    ):
        return False
    try:
        confirm_match(household=household, actor=actor, line_id=line_id, key=key)
    except ValidationError:
        return False
    return True
