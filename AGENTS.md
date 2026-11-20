# NoNonCents contributor guide

This file is for contributors and coding agents working on NoNonCents. It applies to every change in the repository.

## Non-negotiable rules

- Use `Decimal` and two-decimal database fields for money; never use floats.
- Balances are derived from the ledger. Never add an editable balance column.
- Transfers are neither income nor expense. Refunds reduce spending and are not income.
- All financial writes use server-side validation and `transaction.atomic()`.
- Locked pay periods reject financial changes unless an administrator reopens them.
- Keep Spending and Bills separate in calculations and reports.
- Preserve historical values when defaults or schedules change.
- Add migrations and tests with model or calculation changes.
- Never log secrets or financial form payloads.

## Quality gate

Run `make format lint typecheck test` before handing off. Browser tests are run with `make browser-test` after installing Playwright browsers (`npx playwright install chromium`).

Keep templates semantic, keyboard accessible, usable at 320px, and correct in light and dark themes.

## Data and privacy

- Fixtures, tests, demo data and documentation use made-up names and amounts only.
- Do not commit `.env` files, database dumps, statements, screenshots of real data, logs or crash dumps.
- Do not add references to a particular person, household, machine, network, host name or file path.

## Documentation

- The in-app Help renders `docs/guide/*.md` and the chapters listed in `budget/guide.py`. Keep their links and anchors valid; `budget/tests/test_docs.py` checks them.
- Member chapters must not contain server commands.

## Layout

- `budget/`: the Django app (models, services, views, templates, static files, tests).
- `nononcents/`: project settings and URLs.
- `tests/browser/`: Playwright browser tests and the screenshot script.
- `docs/`: documentation. `docs/guide/` is the in-app guide.
