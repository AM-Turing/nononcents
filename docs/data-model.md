# Data model

Every financial table belongs to a `Household` (shown in the app as a **budget profile**), and
every query is filtered by the profile chosen at sign-in.

- `Household` is a budget profile: a name, and through foreign keys its accounts, categories,
  schedule, bills, transactions, budgets and settings.
- `HouseholdMembership` gives one user access to one profile, unique on `(household, user)`. A
  user may belong to several profiles. Superusers gain a membership when they choose a profile
  at sign-in.
- `Account` has a type, opening balance, display order, and archive timestamp. Its type and
  opening balance can change only while it has no activity. Removing an account deletes it
  when nothing refers to it; otherwise it is archived and can be restored.
- `Category` has a transaction/category type, order, and archive timestamp. Its kind (Spending
  or Bill) can change only while unused. Removing a category deletes it when unused, otherwise
  archives it, keeping its history. Archived categories are not offered on new entries.
- `PaySchedule` (one per profile) has a `frequency` (weekly, bi-weekly, semi-monthly or monthly;
  default semi-monthly) and `nominal_days` (default 1 and 15). `PayPeriod` stores the nominal payday, expected
  deposit date, actual deposit date, date range, exceptional flag, and lock state. When a
  paycheck is recorded, its date sets the period's `actual_deposit_date` and `start_date`,
  and the previous period's end; `expected_deposit_date` and `nominal_payday` stay as the schedule. No two
  pay periods of one profile may share a day. On Postgres an exclusion constraint
  (`no_overlapping_pay_periods`, checked when the transaction commits, so neighbouring periods
  can be moved one after the other) enforces this; migration 0023 creates the `btree_gist`
  extension it needs. SQLite, used by the unit tests, cannot enforce it, so there the form and
  service checks are the only guard.
- `Income` owns exact-sum `IncomeAllocation` rows. `Income.is_paycheck` marks the regular
  paycheck ("This is the regular paycheck"); it cannot also be irregular. A paycheck funds the
  pay period whose nominal payday is nearest its date.
- `Expense`, `Refund`, `Transfer`, and `BalanceAdjustment` are explicit positive-value
  ledger records. An expense may own exact-sum `ExpenseSplit` category rows while
  remaining one account ledger entry. Refunds optionally reference an expense.
  Transfers have distinct accounts. Bill expenses optionally reference an occurrence.
- `cleared_at` (nullable) on `Expense`, `Refund`, `IncomeAllocation`, and `BalanceAdjustment`,
  and `source_cleared_at` / `destination_cleared_at` on `Transfer`, record informational
  bank-statement matching only.
- `BalanceAdjustment` also carries `is_increase`, a required `reason` of up to 255 characters,
  and is the only direct balance change, posted by reconciliation or the placeholder
  conversion command.
- `RecurringBill` is a mutable future default. `every_months` is how often it is due: 1
  (every month, the default), 2, 3, or 12 (once a year). `schedule_start` is the first due
  month of the current schedule; empty means the bill's first month (`effective_start`).
  Migration `0017_bill_frequency` added both, so existing bills are monthly.
- `BillOccurrence` snapshots due date, amount, category, and payment account with a unique
  `(bill, year, month)` constraint.
  Its `pay_period` link is the period containing the due date and is recalculated when pay
  period boundaries move.
- `WeeklyCategoryBudgetTemplate` and `WeeklyCategoryBudget` were unused and are dropped by
  migration `0015_habits_debts`. Category budgets are `CategoryBudget` (below).
- `CategoryBudget` is one Spending category's amount per pay period, from
  `effective_from` to an optional `effective_to`. Unique on `(category, effective_from)`;
  the amount is not negative and the end is not before the start. Changing a budget closes
  the open row and starts a new one, so earlier periods keep their values. A second save on
  the same start date updates that row's amount (audited). Foreign keys: household, category
  (protected), creator.
- `HabitView` is a saved Habits page view: name (unique per household, up to 80
  characters), `small_threshold` (above zero, default 20.00), `basis` (pay periods or
  calendar months), `periods` (1 to 24, default 6), `min_repeat` (at least 2, default 3),
  an optional many-to-many list of categories (empty means all Spending categories), and
  `include_essential` (default on). Views only select and summarise; they never change a
  ledger row.
- `Debt` is a debt being paid off: name (unique per household; the form also refuses
  names that differ only by case), `kind` (loan, credit card, mortgage, other), `apr` as a
  percentage from 0 to 100 with three decimals, `minimum_payment` (not negative, used only
  when no active bill is linked), notes, and `archived_at`. It is not a ledger account. A debt that has balance history cannot be deleted
  (`DebtBalance.debt` is `PROTECT`), so debts are archived instead.
- `DebtBalance` is an owner-entered figure copied from a lender statement: the debt,
  `as_of` date, `balance` (not negative, two decimals), notes (up to 200 characters), and
  creator. Unique on `(debt, as_of)`; a second entry for the same date replaces that day's
  balance (audited). These rows are history, not ledger balances: no ledger, budget, bill,
  or report total reads them except the Debts page estimate and projection, and the app
  never derives them. The estimate between statements is computed on the fly from the latest
  row and is not stored.
- `RecurringBill.debt` (new, nullable) links a bill to the `Debt` it pays down. On delete
  of the debt the link is cleared (`SET_NULL`). Saving a debt's bill list moves any bill
  already linked to another debt. Each active, open-ended linked bill's `default_amount`
  makes up the debt's monthly payment.
- `PayPeriodBudget` (one per pay period) stores `spending_target` and `expected_paycheck`,
  both defaulting to `0.00`. A new period's budget copies both from the most recent earlier
  period's budget. Database checks (migration 0022) reject a negative `weekly_target`,
  `spending_target`, `expected_paycheck`, `bills_target` or `bills_cushion`. The legacy columns `weekly_target`, `mode`, `bills_target`,
  `bills_cushion`, `temporary_adjustment`, and `carryover_enabled` are kept for history only;
  no calculation reads them and the form does not show them.
- `ReportAnalysisRun` (migration `0018_report_analysis_runs`) is one Deep pattern analysis
  of a report range: household, `start` and `end` dates, `status` (running, done or failed),
  the checked `result` JSON, a short `error`, the creator, and `created_at` / `finished_at`.
  Only the checked result is stored. The report data sent to the local model is not.
  A run still running after 10 minutes is shown as failed. Runs are not ledger rows.
- The old import table was dropped by migration `0016`. Nothing stores uploaded images.
- `Reconciliation` keeps one history row per account reconciliation: statement date,
  bank and ledger balances, notes, and an optional one-to-one `adjustment`. It is never a
  balance source.
- `StatementImport` is one account's section of a bank statement: account, period
  start and end, optional closing balance, `file_sha256`, creator and time. Unique on
  `(account, file_sha256)`; period end is not before period start. The PDF is not stored.
- `Account.statement_suffix` (blank or 4 digits, unique within a profile; the database
  enforces this for non-blank endings through `unique_account_statement_suffix`, as well as the
  form; editable on the account form as **Statement account ending**): last 4 digits of the bank account
  number, set the first time a statement section is filed to the account; later uploads use it.
- `StatementLine` is one bank line: statement, account, date, signed `amount`
  (non-zero; positive is money in), `description` (bank wording, reference only, up to 255),
  a per-account `fingerprint` unique on `(account, fingerprint)`, `status` (pending, matched or
  dismissed), `matched_key` (ledger item key such as `expense:12`, set when matched), and
  `resolved_by` / `resolved_at`. Lines never create ledger rows and never change a balance.
- `SavingsGoal` has a target, priority, status, and an `is_system` flag for Unallocated Savings.
- `SavingsGoalEntry` holds signed amounts by date and kind, with optional links to the income
  allocation, expense, refund, transfer, or `adjustment` that created it. At most one of these links is set on an entry
  (`savings_entry_one_source`, migration 0022).
- `FormSubmission` stores a one-time `token` UUID per create-form submission, with its
  household and time. Rows older than 30 days are deleted whenever a create form is saved.
- `ApplicationSetting` stores per-profile behaviour as JSON by key: page names, Home alerts and
  quick actions. `UserPreference` stores each user's appearance (system, light or dark); every signed-in page
  applies it (`budget.context_processors.appearance`) and copies it into the browser, whose own
  copy is used only on signed-out pages.
- `AuditEvent` records the profile (nullable), actor (nullable: automatic changes and unknown
  usernames), time, model label, object identifier, action and a redacted JSON object of the
  changes. Actions are create, update, delete, `login` and `login_failed` (migration
  `0019_audit_sign_ins`). Sign-in rows store the profile name and the device address; failed
  ones also the username typed and the reason. Rows are indexed on `(household, occurred_at)` (migration 0022). Passwords and form payloads are
  never stored.
- `LoginFailure` (migration `0020_login_throttle`) is one wrong-password attempt: a casefolded
  `username_key`, the device `ip` (nullable) and the time `at`. Rows older than a day are deleted
  on the next failure, and a successful sign-in deletes that username's rows. It drives the
  sign-in lockout (5 per username or 20 per address within 15 minutes).

Money uses `numeric(14,2)` and positive-value checks. Date ranges, unique bill months,
allocation line limits, one split per expense/category, and different transfer
endpoints are database constrained. Exact allocation and split totals and aggregate
payment limits are enforced transactionally.

## Migrations

Migrations live in `budget/migrations/` and run automatically when the web container starts.
The latest is `0023_no_overlapping_pay_periods`. Notable ones: `0002` memberships, `0007` pay schedule
frequency, `0009` expense splits and bill snapshots, `0010` savings goals, `0012`
reconciliation and cleared state, `0014` statement check, `0015` habits, debts and category
budgets, `0016` drops the old receipt import table, `0017` bill frequency, `0018` report
analysis runs, `0019` sign-ins in the audit log, `0020` sign-in throttling, `0021` scrubs failed sign-in usernames, `0022` the P8 database
constraints (statement ending uniqueness, non-negative pay-period budgets, one source per savings
entry, debt balance protection, the audit index) and `0023` no overlapping pay periods (Postgres
only).
