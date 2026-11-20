# Contributing to NoNonCents

Thank you for helping. NoNonCents is an MIT-licensed, self-hosted budgeting application. Contributions are accepted under the same licence (see [LICENSE](LICENSE)).

Read [README.md](README.md) for setup and [docs/architecture.md](docs/architecture.md) for how the code is organised. Coding agents should also read [AGENTS.md](AGENTS.md).

## The money rules

These rules are not optional. A change that breaks one will not be merged.

- Use `Decimal` and two-decimal database fields for money. Never use floats.
- Balances are derived from the ledger. Never add an editable balance column.
- Transfers are neither income nor expense. Refunds reduce spending and are not income.
- Every financial write validates on the server and runs inside `transaction.atomic()`.
- Locked pay periods reject financial changes unless an administrator reopens them.
- Keep Spending and Bills separate in calculations and reports.
- Preserve historical values when defaults or schedules change.
- Add migrations and tests with any model or calculation change.
- Never log secrets or financial form payloads.

[docs/calculation-rules.md](docs/calculation-rules.md) describes how each figure is calculated.

## Setting up

```bash
make setup
make migrate
make demo        # optional: a made-up profile to look at
make dev
```

## Quality gate

Before you open a pull request, run:

```bash
make format lint typecheck test
```

All four must pass. Add tests for new behaviour and for every bug you fix.

## Browser tests

Browser tests check the real pages in Chromium:

```bash
npm ci
npx playwright install chromium
make browser-test
```

`make browser-test-webkit` runs the phone-layout audit in WebKit through Docker. Run the browser tests when you change templates, styles or scripts.

## Pages, accessibility and themes

Templates must be usable and correct for everyone:

- Use semantic HTML: real headings, lists, labels, buttons and tables.
- Every control must work with the keyboard, with a visible focus state.
- Pages must work at 320 pixels wide, with no horizontal scrolling.
- Check both the light and the dark theme. Colours come from the design tokens in the stylesheets.
- Money shown on screen must follow the same rules as the stored values.

If you change a page that appears in the in-app guide, update the matching chapter in `docs/guide/` as well.

## Test data

- Test and demo data must be made up. Use names such as "Demo Household", "Fresh Market" or "Test Person", and round or obviously fictional amounts.
- Never commit a real statement, backup, `.env` file, screenshot of real data or personal detail, in code, tests, fixtures, documentation or images.
- The guide screenshots are generated from the demo profile with `make guide-screenshots`; regenerate them rather than editing them by hand.

## Pull requests

- Keep each pull request focused on one change, and say in the description what it changes for people using the money figures.
- Describe how you tested it, and list any migrations.
- If the change touches the UI, include before-and-after screenshots that use demo data.

## Reporting security problems

Do not open a public issue for a security problem. Follow [SECURITY.md](SECURITY.md).
