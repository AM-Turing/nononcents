# Calculation rules

All money is `Decimal`, quantized to `0.01` with `ROUND_HALF_UP` at user-input and
calculated-target boundaries.

## Ledger

`balance = opening + allocations + incoming transfers + refunds + signed adjustments
- expenses - outgoing transfers`

Bill and Spending expenses are queried separately by category type. Split expenses
use their allocation rows for category totals while the parent amount affects the
account balance exactly once. The selected
account determines which ledger balance funds the expense, so a recurring bill can be
paid from either Bills or Spending without becoming discretionary spending. Transfers
never enter income/expense/category totals. Refunds reduce category net spending and
remain separately visible. Editing/deletion changes the underlying ledger row; no
balance cache can drift.

Balance corrections (`BalanceAdjustment`) are the only direct balance change. Reconciliation
posts them as positive amounts with a direction (`is_increase`) and a reason. They enter the
balance formula and account flows, but never income, Spending or Bills expense, category,
budget, or bill totals. A correction on a Savings-type account also writes a `RECONCILIATION`
savings-goal entry to Unallocated Savings. Legacy placeholder expenses and incomes can be
converted into corrections on the same account, date, and amount by the
`convert_placeholders` command. It is a dry run unless `--apply` is passed, and balances on
every date stay unchanged.

Reports compare the selected inclusive date range with the immediately preceding
inclusive range of equal length. Category tables and the pie chart use split
allocations when present and subtract category refunds. Account inflows are income
allocations, refunds, incoming transfers, and positive adjustments; outflows are
expenses, outgoing transfers, and negative adjustments. A transfer therefore appears
once on each side of the account-flow table while remaining excluded from income and
spending. Local-LLM narrative is derived from these aggregates and never replaces the
ledger calculations.

## Pay dates and pay periods

A pay period starts on the day its paycheck lands and ends the day before the next period
starts. Periods are contiguous: no gaps, no overlaps.

- **Paycheck.** An income marked "This is the regular paycheck" (`Income.is_paycheck`). It
  cannot also be irregular income.
- **Attribution of early (or late) deposits.** A paycheck funds the period whose nominal
  payday is nearest to the deposit date, looking from 10 days before to 5 days after the
  nominal payday; ties go to the later payday. That period's `actual_deposit_date` and
  `start_date` become the deposit date, and the previous period ends the day before. An
  early deposit therefore counts in the period it funds, never in the one it would
  otherwise have landed in, and each paycheck counts in exactly one period. If two
  paychecks map to one period, the earliest sets the start.
- **Recomputed on every paycheck change.** Recording, re-dating or deleting a paycheck
  re-runs the boundary sync. A period whose paycheck is removed returns to its expected
  deposit date. Bill occurrences are re-linked to the period containing their due date.
- **Locked periods never move.** A boundary next to a locked period is kept and the user
  is told. A deposit that would empty a neighbouring period is not applied either.
- **Expected deposit (future periods only).** For a nominal Monday–Friday payday the
  expected deposit is one calendar day earlier; for Saturday/Sunday it is the preceding
  Thursday. Generated periods start on the day after the latest period and end the day
  before the next expected deposit. A stale nominal payday is skipped, never producing an
  empty or overlapping period. Regeneration never rewrites existing periods.
- **Generated schedule (weekly, bi-weekly and monthly).** The next nominal payday counts on
  from the latest period's nominal payday, not from the day that period ends, so the expected
  deposit does not drift earlier with each batch. Weekly adds 7 days and bi-weekly adds 14
  days. Monthly keeps the schedule's first nominal day of the month (the lowest day number
  set), clipped to shorter months. If the latest period ends before the next expected deposit
  (a gap), the first new period is a short stub. It starts the day after the latest period and
  ends the day before that deposit, which closes the gap. Semi-monthly generation is unchanged
  and has no stub.
- **Manual edits.** Changing a period's start moves the previous period's end; changing
  its end moves the next period's start. A new period must start the day after the
  previous one ends and end the day before the next one starts.
- **Backfill.** `manage.py anchor_pay_periods --household ID --source NAME` marks existing
  non-irregular incomes from that source as paychecks and anchors the periods. It is a dry
  run unless `--apply` is passed and verifies that no account balance changes.

## Budgets and bills

A period budget has a Spending target and an expected paycheck. A new period copies both
from the most recent earlier period's budget, or starts at zero; there are no built-in
amounts. The legacy columns weekly target, mode, bills target, bills cushion, temporary
adjustment and carry-over flag are kept for history but no calculation reads them and
the form no longer shows them.

### Spending

`net_spending` is the one spending definition used by the dashboard, the outlook and the
reports. Spending is the sum of expenses in Spending-type categories (split allocations
count per category when present, otherwise the full amount) minus refunds in Spending-type
categories, dated within the inclusive range. The paying account does not matter.
Bills-type categories, income, transfers and balance corrections are excluded. Bills
spending uses the same function with the Bill category type.

Spending remaining is the Spending target minus net Spending for the pay period. It is not
clamped: a negative value means the period is over target.

### Bill obligations and forecast

- **Live obligations** (`live_bill_occurrences`) are occurrences that are not skipped, whose
  due date is on or after the bill's effective start, and that fall inside the bill's life:
  an active bill with no end date, or a due date on or before its effective end. An inactive
  bill with no end date contributes nothing.
- **Forecast amount** (`bill_forecast`): a month marked Paid is settled at the amount actually
  paid. Its basis is "paid", and its remaining is 0, even when a recent average or the expected
  amount is higher. Otherwise a confirmed amount is used when set. Otherwise the forecast is
  the larger of the expected amount and the average of the bill's last three fully paid
  earlier occurrences. The basis is "confirmed amount", "expected amount", or "average of last
  N payments". Remaining is `max(forecast - linked payments, 0)`. Nothing is written back to
  the occurrence.
- **Status** (`computed_bill_status`) is recomputed whenever a payment or bill changes, and
  by `cleanup_bill_occurrences`. A skipped occurrence stays skipped. Otherwise it is Paid
  when linked payments reach the target (confirmed amount if set, else expected amount),
  Partial when some is paid, Confirmed when a confirmed amount is set, and Unconfirmed
  otherwise.
- **Payment month** (`choose_occurrence`): an explicitly chosen bill month must belong to
  that bill and be live and not skipped, or the payment is refused. Without a choice, the
  payment goes to the bill's live occurrence due closest to the payment date, within 45
  days, that still has a balance (ties go to the earlier due date). The balance check ignores
  the payment being edited. If no occurrence qualifies, the bill's occurrence in the payment's
  calendar month is used, created if missing. If that occurrence is skipped, the payment is
  refused with "A skipped bill month cannot receive a payment." Skipped months are never
  candidates.
- **Removing a bill** (`remove_bill`): the bill becomes inactive with an effective end the day
  before the chosen month. Later occurrences with no payments are deleted; later occurrences
  with payments are kept. The whole removal is refused if any affected due date is in a
  locked pay period. Unpaid months that are still live (due on or before the effective end)
  stay payable after removal: from the Bills **Paid $…** button, from the expense form's bill
  month list, and by Undo of a payment. Months after the effective end are not live and cannot
  be paid.
- **Bill frequency** (`RecurringBill.every_months`): every month (1), every 2 months (2),
  every 3 months (3), or once a year (12). The schedule starts at `schedule_start`, the first
  due month of the current schedule, or at the bill's first month (`effective_start`) when it
  is empty. It then repeats every `every_months` months. Months before the start are never
  part of the schedule (`bill_due_in_month`). Existing bills default to every month.
- **Changing frequency** (`change_bill_frequency`, from Edit bill): sets the new frequency
  and the chosen first due month as the new schedule start. From today on, each unpaid
  occurrence that no longer fits the schedule is deleted. An occurrence is kept when it has a
  payment, is skipped, or sits in a locked pay period. Past months are never changed. Missing
  months on the new schedule are then generated three months ahead. The change is audited with
  the old and new values and the number of months removed.
- **Generation**: one occurrence per live bill for each month its schedule covers, due on the
  bill's due day (clipped to the length of the month), linked to the pay period containing its
  due date. Occurrences are created up to three months ahead by `generate_bill_occurrences`
  (safe to run repeatedly), when a bill is added or edited, and for the month shown on Bills.
  Generation never changes an existing occurrence.
- **Cleanup**: `cleanup_bill_occurrences` (a dry run unless `--apply` is passed) deletes
  unpaid occurrences of removed bills dated after their end and corrects stale statuses.
  Locked periods are skipped.

### Payday forecast

`payday_forecast` (`budget/forecast.py`) feeds the Home page's **Until payday** panel. It
needs a pay period containing today and a next pay period. Without the next period, the
panel shows only the reason it is unavailable and the older unpaid bills. The Bills coverage
figure is still calculated but not displayed.

Inputs:

- **Payday**: the start of the next pay period. Days until payday is `max(payday - today, 1)`.
- **Spending balance** and **Bills balance**: the sums of Spending-type and Bills-type account
  balances through today.
- **Bills due before payday**: remaining amounts of live unpaid occurrences due from the
  current period start through the day before payday. They are split into those paid from a
  Spending-type account (`spending_total`) and those paid from any other account
  (`bills_total`, counted against Bills).
- **Next-period bills**: remaining amounts of live unpaid occurrences due from the next period
  start through the day before the period after it starts (or the next period's end), from
  any account.
- **Expected paycheck**: the earliest paycheck recorded in the next period; otherwise the next
  period's expected paycheck if above zero; otherwise the most recent recorded paycheck;
  otherwise zero. The whole amount counts: the hold-back subtracts all of it, and nothing is
  set aside for Savings or another account. A period's expected paycheck is used only while
  that period is the next one. Once its start date arrives, the period is current and the
  expected paycheck stops being used.
- **Anticipated spending** (next period's spending): the next period's **Spending target**
  when it is above zero. A new period copies the previous target, so it is normally set once
  and one-off purchases do not change it. Only when no target is set is the average net
  Spending of the last three completed pay periods used, to the cent. With no completed period
  and no target, it is zero. The average is still calculated and shown in the formula details
  for comparison, marked "not used", whenever a target is set.

Formulas, as computed:

- Bills left this period = Bills balance - Bills due before payday from non-Spending accounts.
  Home labels this **Remaining after Bills this period**.
- Hold back for next period (the carry-over needed) = max(0, next-period bills + anticipated
  spending - expected paycheck). Home labels this **Needs to carry over**.
- Hold-back not covered = max(0, hold back - Bills left this period). Home labels this "held for
  next period".
- Safe to spend until payday = Spending balance - Bills due before payday from Spending
  accounts - hold-back not covered.
- Per day until payday = safe to spend / days until payday.
- Next period left over = Bills left this period + expected paycheck - next-period bills -
  anticipated spending. Home labels this **Remaining after bills next period**.

Only the hold-back figures are clamped at zero. The other figures can be negative, meaning short
by that amount.

**Carry-over split** (`carry_over` in the forecast):

- From Bills = min(hold back, max(Bills left this period, 0)): Bills covers as much of the
  carry-over as it has left after this period's bills.
- From Spending = hold back - from Bills. This is the part Spending holds back.
- When Bills is short this period (Bills left this period below zero), Spending also covers that
  shortfall: Bills shortfall from Spending = hold-back not covered - from Spending. Held for next
  period therefore exceeds From Spending by exactly that shortfall.
- Spare = max(0, expected paycheck - next-period bills - anticipated spending), shown when
  nothing needs to carry over.

Home shows the figures as three cards. Each headline number comes first, and its calculation is
in a "How this is worked out" panel that opens on tap; the lines reconcile exactly:

- **Spending account**: **Safe to spend** = Spending balance - bills paid from Spending before
  payday - held for next period. Below it: about $X a day until payday, or "Short by $X before
  payday" when negative; and this period's spending against the Spending target.
- **Bills account**: **Remaining after Bills this period** = Bills balance - unpaid bills before
  payday. Then, for the next pay period, **Remaining after bills next period** = Remaining after
  Bills this period + expected paycheck - next period's bills - spending target. The last line
  reads "expected spending (recent average; no target set)" when the anticipated figure is the
  three-period average. When it is negative, the card says how much next period is short and how
  much of it is held back on the Spending card.
- **Carry over to next pay period**: **Needs to carry over** = next period's bills + spending
  target (or expected spending) - expected paycheck, never below $0.00, then how much comes
  **From Bills** and **From Spending** (and any extra Spending holds back because Bills is short).
  When nothing needs to carry over, it says so and shows the spare amount.

**Older unpaid bills** are unpaid live occurrences due before the current period starts.
They are listed with a count and total so they can be reviewed, marked paid or skipped on
Bills, and never counted in any figure above.

### Outlook projection

`outlook_data` (`budget/outlook.py`) splits each selected range into actuals through the
as-of date (today, or the range end when that is earlier) and projections for the rest:

- **Spending still to come** = daily pace x remaining days. Daily pace is net Spending over
  the last three completed pay periods divided by their total days. With no completed period,
  it is the range's prorated Spending target divided by the range's days.
- **Bills still to come** = remaining amounts of live unpaid occurrences due from the later of
  the range start and the current pay period start through the range end.
- **Paychecks still to come** = one expected paycheck for each pay period that starts after the
  as-of date and on or before the range end, valued as in the payday forecast. A period whose
  start date has arrived is no longer expected; its recorded paycheck, if any, is the paycheck.
  The whole amount counts, with nothing set aside for Savings or another account.
- **Projected end balance** (Spending + Bills) = balance at the as-of date + paychecks to come
  - spending to come - bills to come - spending before the range - bills before the range. The
  last two apply only when the range starts at least two days after today; otherwise they are
  zero. **Projected balance change** = projected end balance - balance before the range.
- **A range that starts after today.** The days strictly between today and the range start
  still spend money and still owe bills (`gap_days`):
  - **Spending before the range** = daily pace x those days.
  - **Bills before the range** = remaining amounts of live unpaid occurrences due from the
    current pay period's start (or from the day after today when no pay period contains today)
    through the day before the range starts. Because of the pay period start, this can include
    unpaid bills due earlier in the current period than today.
  - **Balance before the range** (the opening balance) = ledger balance through the day before
    the range + expected paychecks dated before the range - spending before the range - bills
    before the range. The projected balance change is measured from this figure.
  - The two "before the range" amounts appear in the projected end balance's formula and inputs
    as **Spending before the range** and **Bills before the range**.
- **Projected total expenses** = actual Spending + actual Bills + spending to come + bills to
  come.
- **Pace note** (under Projected end balance): "Based on your spending over the last N pay
  periods, you are averaging $X a day" (or, with no completed period, the range's spending target
  per day). If the projected end balance is negative it adds that Spending and Bills together
  would be that much short by the range end, an additional amount the next pay period would need
  to cover; otherwise how much would be left. It always ends by saying this is a projection from
  past spending and actual spending can make it smaller or larger.
- **Category pie**: one slice per category with a positive net amount, clockwise from 12
  o'clock, sized by its share of the total (`pie_slices`). On Outlook it covers the
  selected range; on Home (`spending_chart_data`) it covers Spending categories in the current
  pay period, net of refunds, largest first. The amounts and shares shown are the `Decimal`
  totals; only the drawing uses floating point.
- **Daily chart**: Spending and Bills expenses for each day of the range, net of refunds, never
  below zero (`daily_series`). Zooming only changes the days shown; it never recalculates a
  figure.

### Reports

- Every actual figure in Reports (tables, CSV and the AI narrative) uses one as-of date:
  today, or the range end when that is earlier. Nothing dated after it is counted. The CSV
  header records it as "As of".
- The AI narrative is generated only from figures through the as-of date. Before it is shown,
  `enforce_report_grounding` checks it. Every dollar amount it cites must appear in the report
  data (a whole-dollar figure may round a report amount). It must not describe income as lost,
  declining or missing when an in-progress period's projected income is at least 95% of the
  completed-history average and income is still scheduled, and it must not recommend
  increasing, replacing or supplementing income on that basis. If a check fails, the narrative
  is replaced by a plain summary built from the same figures.


### Deep pattern analysis

- A run starts only from the **Generate AI analysis** button on Outlook (or **Run the
  analysis again** after a run). The button sends a form POST. A link, a page refresh or a
  browser preview cannot start a run.
- One run at a time per budget profile, for any date range. Starting another range while a run
  is going does not start a second run. The page shows the running one, for its own range,
  with the note "An analysis for another date range is still running."
- A run still going after 10 minutes counts as failed. Starting again then creates a new run.

## Money entry

Every money field accepts `1234.56`, `1,234.56` or `$1,234.56` (and a leading minus where the
field allows negatives). The server removes the dollar sign and thousands commas before the
usual two-decimal validation (`budget/money.py`), so the stored value is the same whichever way
it was typed. Exponent forms (`1e3`) and underscores (`1_000`) are refused. A comma is accepted only as a thousands separator: `12,50` is refused rather than
guessed. Amounts are displayed with thousands separators and two decimals.

## Reconciliation

- The statement date cannot be in the future, the account must be active, and a statement
  date inside a locked pay period is refused.
- `difference = statement balance - ledger balance through the statement date`. Zero means
  the account matches.
- Every ticked item must still be uncleared and dated on or before the statement date.
  Otherwise the request is refused and must be compared again.
- A non-zero difference requires posting a correction with a reason of 1 to 255 characters.
  The correction equals the absolute difference, and its direction follows the sign.
- Ticked items and any correction share one timestamp. A `Reconciliation` row records the
  statement date, bank and ledger balances, and the correction. It is history only.

## Cleared state

`cleared_at` on expenses, refunds, income allocations, and balance corrections, and
`source_cleared_at` / `destination_cleared_at` on transfers, record that an item was
matched to a bank statement. The state is informational. No balance, total, report, budget,
or bill calculation reads it. Changing an expense, refund, or transfer amount clears it
again. An income allocation is cleared again only when its own amount changes. History has
no Mark cleared or Mark not cleared buttons. The Reconcile page sets the mark for each ticked
item; ticking is allowed in locked pay periods because clearing changes no figure.

## Refunds

- A refund's category must be of type Spending or Bills in the same budget profile.
- An optional original expense must belong to the same profile. A refund cannot be dated
  before it. The refunds linked to one expense cannot total more than that expense's amount.
  On edit, the refund's previous amount is excluded from that total.
- An expense edit is refused if its amount falls below its linked refunds or its date moves
  after the earliest linked refund.
- Refunds reduce category net spending and increase the account. They are never income.
- Deleting an expense detaches its refunds. They keep their amounts and dates and still count
  in Spending, since spending totals do not depend on the link. Undo of that delete links the
  refunds that are still unlinked.
- A refund into a Savings-type account requires an active, non-system goal, which receives
  an inflow entry.

## Savings goals

- Invariant: the sum of all `SavingsGoalEntry.amount` values for the household, across every
  goal including the system Unallocated Savings goal, equals the balance of the Savings-type
  accounts. `savings_drift = savings ledger balance - sum(goal entries)`.
- Entries are signed. Money entering Savings is positive and money leaving is negative.
- An income split across several Savings-type accounts records one goal entry for each of
  those accounts, in the chosen goal.
- Moving a Savings income to a later date is refused if the goal would be short on an earlier
  spending date from that goal. The goal's running total must stay at zero or above on each
  such date (the error reads "would not have enough allocated funds as of …").
- **Move savings** uses a form token like the other create forms. A repeat submit of the same
  form records nothing and shows "That entry was already saved."
- Date-aware outflow check: money leaving a goal (expense, transfer out, or reallocation) must
  be covered by the goal's total and by its entries dated on or before the transaction date.
  Fulfilling and archiving a goal additionally requires its target to be met and the full
  amount to be used.
- Drift is never corrected automatically. The Savings page shows any drift. An explicit POST
  assigns it to Unallocated Savings as a `RECONCILIATION` entry dated today. A Savings-account
  balance correction writes its own `RECONCILIATION` entry, so reconciliation keeps the
  invariant.
- First use: when a household has no goals, an Emergency Fund and Unallocated Savings are
  created. The existing Savings balance is recorded as an `INITIAL` entry. A positive balance
  goes to the Emergency Fund and any other balance to Unallocated Savings. The entry is dated
  from the earliest Savings activity, today if there is none, and `1900-01-01` when the account
  has an opening balance.

## Unusual dates

Money entry forms and history edits use the household's local date:

- More than 60 days in the past, more than 31 days in the future, or outside every pay period
  requires confirmation (`This date is correct`). An unchanged date on edit is exempt.
- More than 365 days from today is rejected.

## Mark paid

`pay_bill_occurrence` (the **Paid $X** button on Bills) pays one bill occurrence in one
transaction.

- Refused when the occurrence is skipped, or when today is in a locked pay period. The due
  date's period is not checked: a payment dated today in an open period may settle a month due
  in a locked period (see Locked pay periods).
- An unconfirmed occurrence is confirmed first at its expected amount (`confirmed_amount` is
  set and the status becomes Confirmed).
- Amount = target minus amount already paid, where target is the confirmed amount if set,
  otherwise the expected amount. Zero or less is refused as already paid.
- The expense is dated today, uses the occurrence's effective account and category, has the
  bill name as merchant and the note "Marked paid from Bills", and is linked to that
  occurrence. The month is not looked up; the occurrence on the row is paid.
- Status is then recomputed (Paid when the remainder is covered).

**Undo of Paid** (`bill_paid_undo`). The banner's **Undo** takes back only the latest Paid press,
and only for 10 minutes (`undo.PAID_SESSION_KEY`, separate from the delete snapshot). It
deletes the payment with the normal delete rules: the lock on the payment's date applies and
any bank statement match on the payment is released. It then sets the occurrence's confirmed
amount back to what it was before the press (none, when the month was unconfirmed) and
recomputes the status, so the month is as it was. The snapshot is claimed once, and the action
is written to the audit trail with `undo_mark_paid`. The banner's **Change date** only opens the
payment for editing; the bill stays paid, and the edit is checked on both dates.

`confirm_unchanged_bills` sets each unconfirmed occurrence in the month to Confirmed at its
expected amount. It creates no expense. Occurrences due in a locked period are skipped and
counted.

## Locked pay periods

A financial write is checked against the pay period of its own date, not against a related
date.

- **Entries.** Creating, editing, deleting or undoing an expense, refund, transfer or income
  is refused when the entry's date falls in a locked period. An edit is checked on both its
  old and its new date. A payment's lock is its own date, so a late payment dated in an open
  period may settle a bill month whose due date is in a locked period.
- **Bill months.** Confirm, skip, remove and edit-amount on a bill month check that month's due
  date. Editing a bill's category or account refuses the edit if any later month of that bill
  is in a locked period, because that change would be applied to the later months.
- **Undo.** Undo of a payment or delete checks the date of the restored entry, which is the
  date it had when deleted.

## Undo of a delete

After a delete, the app keeps a snapshot of the entry for 10 minutes (`budget/undo.py`). Only
the latest delete can be undone. The snapshot is claimed once, even across workers, so a second
Undo restores nothing.

- Undo re-creates the entry with a new id and runs the normal create rules: the lock on its
  date, the category and account rules, and the bill month rules. A removed bill's live month
  is accepted. If the bill month no longer exists, Undo is refused.
- Refunds that were detached by the delete are linked to the restored expense again, when they
  are still unlinked.
- A statement line matched to the deleted entry stays in review after Undo.
- Entries that involve a Savings account cannot be undone.

## Income source defaults (automatic remainder)

Applies to the income form only. Nothing is stored; the defaults come from the most recent
income with the same source.

- Source match: trimmed and case-insensitive. Only the most recent income for that source is
  used.
- Profile: the remainder account is the account with the largest allocation on that income
  (ties go to the lowest account id). Every other account keeps its previous fixed amount.
  A previous split that used an archived account gives no profile.
- For a total `A` and fixed sum `F`: when `A - F >= 0`, each fixed account gets its amount and
  the remainder account gets `A - F` (omitted when zero). When `A - F < 0`, the remainder
  account gets `A` and the others get nothing.
- The split always sums exactly to `A`, as with any income.
- The paycheck and irregular choices from that income are suggested too.
- Suggestions never overwrite typed values. Amounts fill only while no allocation has been
  typed; the source and total update them. Without JavaScript, a submission with every
  allocation blank gets the same split on the server before validation.

## Statement PDF reading

Implemented in `budget/statement_pdf.py` for the supported statement layout (other banks may need their own parser). The
period comes from the `MM/DD/YY - MM/DD/YY` header; each `<name> - <number>` heading starts
(or continues) an account section; rows are `MM-DD detail amount[-] balance`, a trailing
`-` meaning money out; wrapped details are joined; "Items Paid" tables and text after
"Average Daily Balance" are skipped. A row's year is the period end's year, or the year
before if that date would fall after the period end; a row dated outside the period is
refused. Each account section needs both a Beginning and an Ending Balance row. **Every row
must satisfy previous balance + amount = printed balance, and the last must equal the ending
balance**; otherwise the whole upload is refused. Files over 10 MB, files that do not start
with `%PDF-`, and password-protected PDFs are refused before reading. Each section's account
ending is the last 4 digits of its account number. `check_statement_pdf FILE` runs the same
reading without saving.

## Statement check matching

Implemented in `budget/statements.py` and `budget/statement_views.py`. A statement belongs to
one account, and a line only ever matches entries on that account.

- Account choice: each non-archived account may hold a 4-digit `statement_suffix`
  (**Statement account ending**), unique within the profile (the database enforces this as well as the form). When every section
  of an upload
  has a matching ending, it is staged straight away. Otherwise **Which account is which?**
  asks, suggesting the remembered account or, for a bank section named "saving…", the
  Savings-type account. Each section needs a different account or **skip**. Confirming saves
  the ending on the chosen account and clears it from any other account in the profile. The
  ending can also be set or cleared on **Accounts → Edit**; the audit log records only that it
  was set, changed or cleared.

- Candidates: expenses, refunds, income allocations and transfer legs on the same account
  whose signed amount equals the line's (money in positive, money out negative) and whose
  date is from 7 days before to 3 days after the bank date, inclusive. Balance corrections are
  never candidates. Entries already matched to another line are excluded.
- Order: nearest date first; on a tie, an entry dated before the bank date; then the oldest.
  At most three candidates are offered.
- Proposal: the first candidate not already proposed to an earlier line (lines in date order).
  An entry is proposed at most once.
- Merchant, notes and category never take part. The bank text is stored for reference only.
- **Same purchase** re-checks the entry (still on the account, same amount, not matched), sets
  `matched_key` on the line and sets the entry's cleared mark if it is not set. **Confirm all
  proposed pairs** applies this to each proposal in turn.
- **Dismiss** sets the line aside. No ledger row changes. **Undo** returns a matched or
  dismissed line to review; the entry keeps its cleared mark.
- **Add as new entry** opens the expense or income form prefilled from the line. After saving,
  the entry is linked only when its account and absolute amount equal the line and the line is
  still pending. Otherwise it is saved unlinked.
- **Not on a statement yet**: uncleared entries on the account (not balance corrections)
  that no line matched and that are not proposed for a pending line, dated on or before the
  statement end and on or after the period start minus 3 days. Informational.
- **Balance difference**: the statement's ending balance minus the app's running balance after
  the last entry dated on or before the statement end (the opening balance when there is none).
- Import: a file with the same SHA-256 for the account returns the existing statement. A line
  with a zero amount, or dated outside the statement period, refuses the import. Each
  line has a per-account fingerprint of date, signed amount, normalized description and its
  order among identical lines that day; a fingerprint already present is not staged again.
  All sections of one upload are staged in one transaction.
- Removing a statement removes its lines only; cleared marks stay.
- Changing an income's split (the amount for each account on **Edit** for an income): an
  account whose amount changes loses its cleared mark, and a statement line matched to that
  account's split returns to review. Accounts whose amount is unchanged keep both their cleared
  mark and their match. Setting an account's amount to 0 removes that split, and its line
  returns to review. Changing only the date or the notes keeps the matches.
- Changing the amount of an expense, refund or transfer, or deleting any entry, returns its
  matched line to review. A transfer releases both its outgoing and incoming lines. Undo of
  that delete does not match the line again, because the restored entry has a new id.
- Statement lines never create ledger rows and never change balances, budgets, bills or
  reports.

## Spending habits

Implemented in `budget/habits.py` on the Habits page. Spending is the `net_spending`
definition above: expenses and refunds in Spending categories, on any account. Bills,
transfers and balance corrections are excluded.

- **Range.** A view looks back `periods` pay periods (those starting on or before today,
  newest first, including the current one) or calendar months (including the current,
  partly elapsed month). The range runs from the first column's start to the last column's
  end.
- **Small purchase.** An expense whose whole purchase total is at or below the view's
  `small_threshold`. A split expense is judged by its total and counted in each split's
  category. Refunds reduce totals but are never small purchases.
- **Merchant.** Grouped by name with spaces collapsed and case ignored. The most-used
  spelling is displayed. Entries with a blank merchant are left out of merchant lists.
  Purchase count = distinct expenses (refunds not counted); a merchant with only refunds is
  not listed. Total = expenses minus refunds at that merchant. Average = total ÷ count.
- **Habit.** A merchant with at least `min_repeat` small purchases in the range. Its total
  is its small purchases only (refunds excluded). Per month = that total × 30.44 ÷ days from
  the range start to the earlier of the range end and today, rounded to the cent. Per year =
  total × 30.44 ÷ days × 12, rounded once (so it can differ from per month × 12 by a few
  cents).
- **Essential.** An expense uses its own `is_essential_override` when set, otherwise its
  category's `is_essential`. A refund uses its category's setting. With `include_essential`
  off, essential items are removed from every figure in the view.
- **Categories.** When a view lists categories, only those count. An empty list means
  every Spending category.
- **Category trend.** Per category and column, net spending (expenses minus refunds).
  Average = total ÷ number of columns, zero columns included.
- **Small share** = small-purchase total ÷ total spending, rounded to a whole percent. Zero
  when total spending is zero or less.
- **Adjust** changes the page only. It is not saved and covers every Spending category.

### Cleanup queue

`cleanup_queue` lists the 50 newest expenses (any category type) with a blank merchant, and
up to 50 merchant groups whose expenses are spelled more than one way (most used first).

`unify_merchant` renames every expense and refund whose merchant key matches to the chosen
spelling. The chosen name must have the same key (same letters, ignoring case and spacing).
Only the merchant text changes. Rows in locked pay periods are skipped and counted, and
each change is audited.

## Category budgets

Implemented in `budget/habits.py` (`set_category_budget`, `budget_for`,
`category_budget_status`). Category budgets are optional: a category never needs one, and a
category without a budget in a period is not compared. A `CategoryBudget` row gives one
Spending category an amount per pay period from `effective_from` until `effective_to` (open
when null).

- **Period budget.** A pay period uses the row in effect on its start date. A category with
  no row, or a row of zero, has no budget for that period.
- **Setting a budget** from date D: refused if the category already has a row starting after
  D. A row starting on D has its amount replaced. Otherwise the open row ends the day before
  D and a new row starts on D. Earlier periods keep their budget. Zero is recorded as a row
  that removes the budget from D. Negative amounts are refused.
- **Status for one period.** For each active Spending category with a budget above zero:
  - Spent = net spending in the period, any account, essential items included.
  - Remaining = budget - spent. It is not clamped, so a negative value means over budget.
  - Percent = spent ÷ budget, rounded to a whole percent, and may exceed 100.
  - Lines are sorted by remaining, lowest first.

## Debt balances and payoff

Implemented in `budget/debts.py` on the Debts page. Balances come only from `DebtBalance`
rows that the owner enters from lender statements. Nothing is inferred from the ledger or
from the APR and minimum payment.

- **Monthly payment.** Sum of `default_amount` of the debt's linked bills that are active
  with no end date. With none, the debt's `minimum_payment`. The page shows which basis
  applies.
- **Estimated balance** (only when a balance has been entered on or before today). Base is
  the latest entered row with `as_of` on or before today (same date: the one entered last).
  Months count whole months: the month difference, less one if today's day of the month is
  before the `as_of` day. Amounts round half up to the cent.

  ```
  months   = whole calendar months from as_of to today
  interest = round(balance × APR ÷ 12 × months)                 (simple interest)
  paid     = expenses linked to this debt's bill occurrences,
             dated after as_of and on or before today
  estimate = max(balance + interest - paid, 0)
  ```

- **Projection** starts from the estimate and compounds monthly. Each month:

  ```
  interest  = round(balance × APR ÷ 1200)
  due       = balance + interest
  payment   = min(monthly payment, due)
  balance   = due - payment
  ```

  The payoff date is today plus the number of months. If the monthly payment is not above
  the first month's interest, the debt never pays off and the projection says so. If it is
  not paid off within 600 months, it is reported as not within 50 years.
- **Payoff plan** (Debts page comparison). Includes non-archived debts with an estimate and
  a monthly payment above zero. A debt already at zero is shown as paid off and frees no
  payment; only a debt paid off during the plan passes its payment on.
  - *Current payments* (baseline): each debt is paid its own monthly payment. No extra and no
    rollover.
  - *With extra* (avalanche): each month every owed debt accrues interest and receives its
    payment (capped at what it owes). The pool is the extra per month plus, with rollover,
    the payments of debts already paid off and any unused part of this month's final
    payments. The pool pays the highest-APR debt still owed first (ties: smaller balance,
    then older debt), then the next.
  - Each plan reports debt-free month and total interest across debts. "Sooner" and
    "less interest" are shown only when both plans finish.
- The extra amount is typed on the page and is not saved. Blank input is zero; invalid or
  negative input shows a message and is treated as zero.

## Test-first rules

Before implementation, tests must cover ledger signs/exclusions, exact allocation,
rounding, refund netting, transfer neutrality, all seven nominal weekdays, overrides,
historical stability, period assignment, paycheck anchoring, payday forecast, locked writes, partial bill payment,
bill variance, seed counts/totals, date boundaries, report separation, and all six
acceptance scenarios.
