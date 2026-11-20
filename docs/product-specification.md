# Product specification

NoNonCents is a private, self-hosted household financial system of record. One installation
holds several separate budget profiles, each used by a few signed-in people. It is built for fast
everyday entry on a phone while keeping an accurate, auditable record of every entry.

## Users and access

- **Superusers** manage every profile and user and decide who is an administrator.
- **Administrators** (staff) manage the profiles they belong to: users, profile access, page
  names, Home alerts and quick actions, backups, the activity log, and reopening locked pay
  periods.
- **Standard users** enter and view the financial data of the profiles they have access to.
- The profile is chosen at sign-in. There is no registration and no anonymous access. Sign-in is
  throttled and every sign-in and failed sign-in is logged.

## Primary workflows

1. Set up a profile: accounts, categories, pay schedule and pay periods, bills, Spending target,
   optional category budgets, savings goals and debts.
2. Record income and allocate its exact total across accounts.
3. Record an expense (optionally split across categories, optionally paying a bill), or a refund
   linked to the original expense.
4. Add a transfer between accounts. Transfers are neither income nor spending.
5. Review, confirm, skip and pay recurring bills each month.
6. See what is safe to spend until payday, what remains after bills this period and next, and
   what must carry over to the next pay period.
7. Check a bank statement PDF against the app's entries (Statement check), and reconcile each
   account's balance with the bank on its Reconcile page.
8. Review Outlook projections and charts, spending habits and debt payoff; export CSV or print.
9. Read the guides in the app (Help), with search.

Accounts and categories are extensible and archivable. New profiles start with Spending, Bills
and Savings accounts and a few categories. A new installation has no profiles until the first
administrator creates one; `seed_demo_profile` (`make demo`) can add a made-up "Demo Household"
for trying the app.

## Quality attributes

All financial writes are validated on the server, audited, transactional and use decimal
arithmetic. The deployment has no public-cloud dependency and is reachable only on the household
network, over HTTPS.

## Design standards

### Money

- Money uses decimals, never floats. Balances are worked out from entries; none is typed in.
- Transfers are neither income nor spending. Refunds reduce spending and are not income.
- Spending and Bills stay separate in calculations and reports.
- A locked pay period refuses financial changes until an administrator reopens it.
- Amounts show "$", thousands commas and two decimals. Entry accepts 1234.56, 1,234.56 and
  $1,234.56; ambiguous text such as "12,50" is refused, not guessed at.

### Words and messages

- Page headings match menu names. Owners can rename pages under Admin → Page names.
- Plain words, not accounting jargon, in labels, messages and guides.
- Errors start with "Problem:" and use role="alert"; warnings start with "Check:". Errors and
  warnings are therefore not told apart by colour alone.

### Layout and interaction

- Usable from 320px wide up, with a 16px side gutter and no sideways scrolling.
- Tables that do not fit become stacked rows on small screens. Card content fits at every width.
- Pages do not shift after load, and keep their scroll position after a list action.
- Amount fields take focus on load only with a fine pointer (mouse or trackpad), not on touch.
- Undo covers the latest one-tap delete and the latest "Paid" press, for 10 minutes.

### Look, type and access

- Light and dark themes use the `.dark` class. Display Mode (Light, Dark or System) is saved
  per person.
- Text contrast meets WCAG AA, checked with axe in light and dark.
- Plus Jakarta Sans is the interface face; Space Grotesk is used for the wordmark, headings and
  balances. Both are self-hosted, fall back to usable system faces, and figures use tabular
  numerals.
- Templates use semantic HTML. Every control works from the keyboard and shows a visible focus
  ring.
- Charts can be used from the keyboard. They are drawn on the server, so they show without
  script; tooltips and zoom need script.

### Accessibility details

- Every `aria-describedby` points to an element that exists (help text and error lists).
- On the entry forms (expense, refund, transfer, income, splits, bills, budgets and savings),
  required-field messages name the field ("Enter an amount.", "Choose an account.") and select
  lists show a meaningful empty choice instead of "---------".
- The current page is marked in the sidebar and the phone menu, not by colour alone.
- Buttons, menu entries and small text links have a tap height of at least 44px.

### Quality gate

- `make format lint typecheck test` before handoff. Model and calculation changes come with
  migrations and tests.
- `make browser-test` (Playwright: layout, contrast, visual and page-stability checks, after
  installing the Playwright browsers) and `make browser-test-webkit` (WebKit phone audit).

## Version 1 boundary

No bank sync (statements are uploaded by hand and never change balances on their own), automated
payment, public hosting or registration, native app, multiple currencies, investment or tax
tools, telemetry, or offline entry.
