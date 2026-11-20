# Decisions and assumptions

These are the technical decisions behind NoNonCents, and the assumptions they rest on.

1. Django 5.2 is the supported LTS release, and Python 3.12 is the runtime baseline.
2. Several budget profiles share one installation. Every financial row belongs to a profile, and users are given access per profile. All members of a profile can view and enter its finances; only administrators manage users and settings and reopen locked periods. Administrators who are not superusers manage only their own profiles and standard users.
3. There is no currency setting. Amounts are plain decimal values shown with a dollar sign and two decimal places, and the app never converts between currencies. Dates and pay periods are shown in the time zone set by `TIME_ZONE` (default `UTC`).
4. Bill due days beyond a month's length clamp to that month's last day.
5. A linked expense is the only way to count a bill payment. Skipped occurrences need a reason and do not require funding.
6. There is no server-side PDF export: reports print, or save as PDF, from the browser's Print / PDF. The optional `pdf` extra (WeasyPrint) in `pyproject.toml` is not installed or used.
7. Frontend assets are local: no CDNs, analytics or telemetry.
8. Usernames, not email addresses, sign people in; no personal data beyond a name is needed.
9. Sign-in throttling is stored in the database (5 failures per username or 20 per address in 15 minutes). It is enough for a small private deployment; keeping the app off the public internet remains the main exposure control.
10. Money fields accept thousands commas and `$`, normalised on the server; ambiguous text such as `12,50` is refused rather than guessed.
11. HTTPS is terminated by a reverse proxy that the operator runs, so the app never handles certificates itself. HSTS is off by default and is turned on only once HTTPS works on every device that uses the app.
12. Backups run on the host, not in the web container: a scheduled job (cron or a systemd timer) runs `scripts/backup.sh`. **Back up now** leaves a request file that a host job picks up, sharing the scheduled job's lock so the two never overlap.
13. The in-app Help pages render the repository's own Markdown documents (Python-Markdown, raw HTML off), so the app and the repository never disagree.
14. Statement PDFs are not kept after they are read. Only the figures the user confirms are stored.
