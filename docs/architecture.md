# Architecture

## Shape

A server-rendered Django 5.2 LTS monolith: Django templates, hand-written CSS, small vanilla
JavaScript files (no framework, no build step, no CDN) and PostgreSQL. Everything is in one
Django app, `budget`. Most calculations live in service and query modules (`services.py`,
`forecast.py`, `outlook.py`, `pay_periods.py` and the others in the table below), but `views.py`
also holds some calculation of its own, such as the Home figures and the spending chart data.
Models enforce row-level rules; services enforce cross-row rules inside database transactions.

```text
Phone / computer browser
  -> reverse proxy on the host (HTTPS, for example Caddy or nginx; see docs/deployment.md)
  -> Gunicorn + Django in the web container (127.0.0.1:8000)
       -> views -> service / query modules -> Django ORM -> PostgreSQL (internal network)
       -> templates, WhiteNoise static files
       -> audit events, CSV export
       -> aggregate report summary -> optional model server (OpenAI-compatible, off by default)
Host jobs: nightly backup timer, Back up now cron, bill generation cron
```

### Main modules (`budget/`)

| Module | Responsibility |
| --- | --- |
| `models.py` | All tables (see [data model](data-model.md)) |
| `services.py` | Ledger balances, posting and editing transactions, bills, pay periods |
| `forecast.py` | Home **Until payday** figures, including carry-over |
| `outlook.py` | Outlook actuals, projections, pace and chart data |
| `pay_periods.py` | Pay schedule and pay period generation |
| `statements.py`, `statement_pdf.py`, `statement_views.py` | Bank statement PDF reading and the statement check |
| `reconciliation.py`, `reconciliation_views.py` | Account reconciliation and balance corrections |
| `habits.py` | Spending habit analysis (small purchases, merchant frequency, trends) |
| `debts.py` | Debt estimates and projections from the latest balance entered |
| `views.py` | Page views for the main budget pages, with some calculation (Home figures, spending chart data) |
| `forms.py` | Forms for expenses, refunds, transfers, income, savings goals, budgets, recurring bills, pay periods, Outlook settings, quick actions and profile access |
| `urls.py` | URL routes for the whole app |
| `manage_views.py`, `manage_forms.py` | The Manage page: add, edit, archive and remove accounts and categories |
| `habit_views.py`, `habit_forms.py` | The Habits page, saved habit views and the quick adjust bar |
| `debt_views.py`, `debt_forms.py` | The Debts page: debt editing, balance entry and category budget setting |
| `docs_views.py` | Help pages (`/docs/`) that render the in-app guides |
| `setup_checklist.py` | The first-run checklist on Home |
| `context_processors.py` | Values added to every page: the backup alert for staff and the saved appearance |
| `csp.py` | Content-Security-Policy header |
| `admin.py`, `apps.py` | Django admin registrations; app setup, including the browser-test clock |
| `testclock.py` | Fixed "today" for browser tests, the WebKit audit and the guide screenshots |
| `money.py` | Money entry fields that accept `$` and thousands commas |
| `activity_log.py`, `sign_ins.py` | Admin activity log, sign-in and failed sign-in records |
| `login_throttle.py` | Sign-in throttling by username and device address |
| `admin_scope.py` | Which profiles and users a non-superuser administrator may manage |
| `backup_status.py`, `backup_requests.py` | Backup health card and banner; Back up now requests |
| `report_analysis.py`, `analysis_jobs.py` | Optional local-model narrative for reports, run in the background |
| `guide.py` | The in-app documentation (Help) |
| `undo.py` | Undo after deleting a transaction |

Authentication uses Django sessions with Argon2 password hashing. The sign-in form also chooses
the budget profile; the active profile is kept in the session and every query is filtered by it.
Sign-in attempts are throttled in the database (`LoginFailure`), and sign-ins and failures are
written to the audit log.

### Reports and the local model

Report generation follows a trust split. Django calculates category totals, equal-length
comparison trends and per-account inflows and outflows from the ledger. A configured local
OpenAI-compatible model receives only those aggregates and returns a schema-validated narrative;
it cannot change report figures or write financial data, and a narrative that cites figures not
in the report is replaced by a plain summary. The analysis runs in a background thread
(`analysis_jobs.py`) so the request returns at once; its checked result is stored in a
`ReportAnalysisRun` row.

### In-app documentation

`/docs/` (**Help** in the menu) renders the Markdown guides in this repository with
Python-Markdown (raw HTML disabled), builds a table of contents and a search index from their
headings, and links between chapters. The chapter list is `CHAPTERS` in `budget/guide.py`:
`docs/guide/getting-started.md`, `everyday-use.md`, `understanding-the-numbers.md` and
`troubleshooting.md` for everyone; `docs/guide/administrator.md`, `docs/deployment.md`,
`docs/backup-and-restore.md` and `docs/calculation-rules.md` for administrators (staff) only.
The files ship inside the image, so the app always shows the documentation of the deployed
version. Pictures used only by administrator chapters are served only to administrators.

## Frontend

Mobile-first CSS with semantic colour tokens for light and dark palettes. The media queries in
`budget/static/budget/*.css` use many widths rather than one scale: 390, 420, 440, 479, 520, 639,
640, 700, 760, 800, 1023, 1024, 1199, 1360 and 1399px. The desktop sidebar appears from 1024px
(`sidebar.css`, `app.css`); below 1024px phones get a bottom navigation bar with a **More** menu
(`mobile-layout.css`, `typography.css`). The sidebar's collapsed state is remembered in the
browser. Wide tables become stacked, labelled rows when they do not fit
(`stack-tables.js`). Primary controls are at least 44px, focus is visible, motion is reduced on
request, and print forces a light theme.

Every write is a standard form POST with CSRF protection and server-side validation; JavaScript
only enhances pages (chart read-outs and zoom, split and allocation helpers, the More menu,
stacked tables, Help search).

Appearance (light, dark or system) is saved per user (`UserPreference`) and mirrored in the
browser's local storage, which a small script in `templates/base.html` reads before the page is
drawn so it never flashes the wrong theme. Local storage is per address, so a device starts on
"system" the first time it opens a new address.

Charts are inline SVG drawn on the server: `outlook.py` computes the Outlook pie slices and
line points with `Decimal` amounts, and the Home spending pie uses the same slices from the
figures in `views.spending_chart_data`. `charts.js` adds hover and tap read-outs and zoom (buttons, wheel, pinch) on
the daily chart.

## Operations and trust boundaries

- Gunicorn listens on the port set by `WEB_BIND` (default `127.0.0.1:8000`). A reverse proxy on
  the host terminates HTTPS and passes the visitor's address in `X-Real-IP` (trusted only from
  `TRUSTED_PROXY_NETWORKS`).
- PostgreSQL is on an internal Compose network with no published port.
- Secrets live only in `.env`.
- The web container cannot run backups or reach the database's tools: Back up now drops a
  request file that a host job picks up. Backup status reaches the app as a read-only,
  secret-free JSON file.
- The network setup decides who can reach the site, not Django: keep the app on the loopback
  address or behind HTTPS and a firewall, as described in the deployment guide.

Details: [deployment](deployment.md), [backup and restore](backup-and-restore.md).
