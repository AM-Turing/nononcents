# Security policy

## Reporting a vulnerability

Please do not report security problems in a public issue, pull request or discussion.

Report them privately with GitHub's private vulnerability reporting: on the repository's **Security** tab, choose **Report a vulnerability**. Include the steps to reproduce the problem, the version or commit you tested, and what you think an attacker could do. Keep any real financial data out of the report; a made-up example is enough.

We will confirm receipt and work on a fix as soon as we can. Please give us a reasonable time to fix the problem before you disclose it publicly.

## Supported versions

Only the latest commit on the `main` branch is supported. Security fixes go there. If you run an older version, update to the latest commit (see [docs/deployment.md](docs/deployment.md)).

## Security design

- **Server-side validation.** Every financial write is validated on the server, inside a database transaction. Money is stored as two-decimal values and calculated with `Decimal`, never with floats.
- **Access control.** Users see only the budget profiles they belong to. Locked pay periods reject changes unless an administrator reopens them. Administrator pages are restricted to administrators.
- **Sign-in protection.** Passwords are hashed with Argon2 and must be at least 12 characters. Sign-ins are throttled: five wrong passwords for one username, or twenty from one address, within 15 minutes.
- **Web protections.** Cross-site request forgery protection is on for every form. The Content Security Policy allows only the application's own scripts, styles and images. Pages cannot be framed (`X-Frame-Options: DENY`). Cookies can be marked secure for HTTPS, and HSTS is off until you turn it on.
- **Activity log.** Changes and sign-ins are recorded with who made them and when. Passwords are never recorded.
- **Secrets and data in logs.** Secrets from `.env` and financial form data are not written to logs.
- **Container.** The application runs as an unprivileged user, its code is read-only at run time, and the database is on an internal Docker network that the outside cannot reach.
- **No telemetry.** The application sends no analytics and loads nothing from third-party content delivery networks.
- **AI analysis is off by default.** When you enable it, it sends an aggregate summary to the endpoint you configure and nothing else. Use a model server you trust.

## Operator responsibilities

- Keep `.env` private and back it up separately from the database dumps. Never commit it.
- Serve the application over HTTPS when anyone other than the host computer uses it. Do not publish the app port to the internet without HTTPS and a sign-in policy you trust.
- Keep Docker, the base images and the application updated.
- Keep the backups private: a dump contains every table, including password hashes.
