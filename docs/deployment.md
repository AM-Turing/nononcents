# Deployment

This guide explains how NoNonCents runs with Docker Compose: the containers, the settings in `.env`, updates and rollback, scheduled jobs, HTTPS behind a reverse proxy, and the optional AI analysis. Commands run from the folder that holds `compose.yml` (the NoNonCents folder) unless they say otherwise.

## Containers

`compose.yml` runs two containers and nothing else.

| Container | What it runs | Notes |
| --- | --- | --- |
| `web` | Gunicorn (3 workers) serving the Django app on port 8000 | Runs as an unprivileged user. The code is read-only at run time. Applies database migrations when it starts. |
| `db` | PostgreSQL 17 | Data lives in the named volume `postgres_data`. It is on an internal network and has no published port. |

Only `web` publishes a port, set by `WEB_BIND` (default `127.0.0.1:8000`, reachable from this computer only). Docker publishes ports through its own firewall rules, which can bypass firewall rules you set with UFW or similar tools. Keep the loopback default unless you intend to expose the app, and then use HTTPS.

The backup folders are mounted into `web` so the app can show backup health and receive "Back up now" requests. The backup scripts run on the host, not in a container.

## Before you start

- Docker Engine with the Compose v2 plugin (Linux), or Docker Desktop (macOS and Windows).
- A DNS name or a local name for the computer if other devices will use it.
- Time to choose your `TIME_ZONE`, which you set once and should not change.

## First install

The simplest route is the start script from the README: `./start.sh` on Linux and macOS, or `start.cmd` / `start.ps1` on Windows. It does the steps below for you.

To do it by hand:

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env`:

- `DJANGO_SECRET_KEY`: at least 50 random characters. For example, `openssl rand -base64 64 | tr -dc 'A-Za-z0-9' | head -c 64`.
- `POSTGRES_PASSWORD`: a long random password. Use letters and digits only if you prefer, so it is safe in every context.
- `TIME_ZONE`: an IANA name such as `Europe/London` or `America/Chicago`. The default `UTC` is only a fallback.

Then build and start:

```bash
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
```

The web container applies the database migrations when it starts. Check that the app answers:

```bash
curl -fsS http://127.0.0.1:8000/health/
```

It prints `ok`. Sign in at `http://127.0.0.1:8000` with the administrator account. You will be asked to create your first budget profile; see the README's first-run section.

`make up` is the same as `docker compose up -d --build`, and it also tags the image with the short git commit so you can roll back to it later.

## Updating

Update to the latest version of the code:

```bash
git pull
make backup
make up
```

`make backup` takes a verified backup first, which is recommended before every update. `make up` rebuilds the image, replaces the containers and migrates the database. Then check the containers:

```bash
docker compose ps
```

Both containers should show `Up ... (healthy)`. Changes to `.env` take effect the same way: run `make up`. It recreates both containers, which takes under a minute, and the data stays in the volume.

Do not use `docker compose down -v`. The `-v` flag deletes the database volume.

## Rolling back

The program and the database are separate, so there are two cases:

1. **The new version changed nothing in the database** (no migration ran): check out the previous commit and run `make up`, or start an image that is still on the computer:

   ```bash
   docker image ls nononcents-web
   APP_VERSION=<tag> docker compose up -d
   ```

   Use the tag of the earlier image, which is the short commit it was built from.

2. **The new version ran migrations:** the older code may not work with the newer database. Roll the code back as above, then restore the backup taken just before the update. See [Backup and restore](backup-and-restore.md#restore-the-live-database). Anything entered after that backup is lost, so take a fresh backup before you restore anything.

## Settings

All settings are read from `.env` by both containers. After a change, run `make up`. See `.env.example` for comments on each one.

| Setting | Default | Meaning |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | (required) | Signing key. Unique, at least 50 random characters. Changing it signs everyone out. |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | `nononcents`, `nononcents`, (required) | Database name, user and password. |
| `POSTGRES_HOST` | `db` | Database host; leave it as `db` with the bundled `compose.yml`. |
| `WEB_BIND` | `127.0.0.1:8000` | Address and port the app is published on. |
| `ALLOWED_HOSTS` | `127.0.0.1,localhost` | Host names the app answers to. Requests for any other name are refused. |
| `CSRF_TRUSTED_ORIGINS` | empty | Full origins with `https://`, comma-separated, for the addresses you browse from through a proxy. |
| `TIME_ZONE` | `UTC` | Time zone for dates, pay periods and reports. |
| `DJANGO_DEBUG` | `false` | Shows detailed errors. Keep it `false` wherever real data is stored. |
| `SESSION_COOKIE_AGE` | `28800` | Seconds a sign-in lasts (8 hours). |
| `SECURE_SSL_REDIRECT`, `TRUST_PROXY_HTTPS`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` | `false` | HTTPS options, see [HTTPS](#https-behind-a-reverse-proxy). |
| `SECURE_HSTS_SECONDS` | `0` | Tells browsers to use HTTPS only, for this many seconds. Turn on only when HTTPS works on every device. |
| `TRUSTED_PROXY_NETWORKS` | `127.0.0.1/32,::1/128,172.16.0.0/12` | Networks the proxy connects from. Only these may pass the visitor's address in `X-Real-IP`, which sign-in throttling and the activity log use. |
| `BACKUP_DIR` | `./backups` | Folder for database dumps. Created owner-only. |
| `BACKUP_COPY_DIRS` | empty | Space-separated existing folders that receive a verified copy of every dump, for example a mounted external disk. |
| `BACKUP_RETENTION_DAYS`, `BACKUP_KEEP_MIN` | `30`, `7` | Delete dumps older than this many days, but always keep at least this many of the newest. |
| `BACKUP_PING_URL` | empty | Optional URL requested after each successful backup, for a "dead man's switch" monitor. |
| `BACKUP_MAX_AGE_HOURS` | `26` | Hours without a successful backup before administrators see a warning. |
| `BACKUP_STATUS_DIR`, `BACKUP_REQUEST_DIR` | folders inside `BACKUP_DIR` | Where the status file and "Back up now" requests are kept. |
| `REPORT_LLM_PROVIDER` | `disabled` | `openai_compatible` turns on the AI analysis. See [AI analysis](#ai-analysis). |
| `REPORT_LLM_URL`, `REPORT_LLM_MODEL`, `REPORT_LLM_API_KEY` | empty | Endpoint, model name and optional bearer token. |
| `REPORT_LLM_TIMEOUT_SECONDS`, `REPORT_LLM_MAX_TOKENS`, `REPORT_LLM_DISABLE_THINKING` | `120`, `2200`, `false` | Limits and reasoning options for the model. |
| `DJANGO_LOG_LEVEL` | `WARNING` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. Financial form data is never logged. |

## Scheduled jobs

Three jobs run on the host, outside the containers. Use cron or a systemd timer. The examples use a placeholder path; replace `/path/to/nononcents` with the folder that holds `compose.yml`.

| Job | When | Command |
| --- | --- | --- |
| Nightly backup | Once a night, for example 02:15 | `scripts/backup.sh` |
| Bill occurrences | Once a day, for example 02:40 | `docker compose exec -T web python manage.py generate_bill_occurrences` |
| Back up now | Every minute (optional) | `scripts/backup-request.sh` |

`generate_bill_occurrences` creates upcoming monthly bill entries ahead of time, and it is safe to run every day.

### Cron example

Edit your crontab with `crontab -e`:

```cron
15 2 * * * cd /path/to/nononcents && flock -n backups/.backup.lock scripts/backup.sh >> backups/backup.log 2>&1
40 2 * * * cd /path/to/nononcents && docker compose exec -T web python manage.py generate_bill_occurrences >/dev/null 2>&1
* * * * * /path/to/nononcents/scripts/backup-request.sh >> /path/to/nononcents/backups/backup.log 2>&1
```

Use the same lock file (`backups/.backup.lock`) for the nightly backup and the "Back up now" job, so two backups never run at once. `flock` is available on Linux.

### systemd timer example

Create `/etc/systemd/system/nononcents-backup.service`:

```ini
[Unit]
Description=NoNonCents verified backup

[Service]
Type=oneshot
WorkingDirectory=/path/to/nononcents
ExecStart=/usr/bin/flock -n /path/to/nononcents/backups/.backup.lock /path/to/nononcents/scripts/backup.sh
```

And `/etc/systemd/system/nononcents-backup.timer`:

```ini
[Unit]
Description=Nightly NoNonCents backup

[Timer]
OnCalendar=*-*-* 02:15:00
Persistent=true

[Install]
WantedBy=timers.target
```

Then run `sudo systemctl daemon-reload && sudo systemctl enable --now nononcents-backup.timer`. `Persistent=true` runs a missed night as soon as the computer starts again.

## Back up now (Admin)

Administrators can press **Back up now** on the Admin page. The web container cannot run host commands, so the button leaves a request file, and a host job picks it up within a minute.

One-time setup on Linux:

1. Create the request folder: `scripts/backup-request.sh --setup`. It creates `BACKUP_REQUEST_DIR` (by default `backups/requests`) with the permissions the container needs.
2. Make sure `BACKUP_REQUEST_DIR` in `.env` matches that folder, then run `make up`.
3. Add the every-minute cron line from the [cron example](#cron-example).

`scripts/backup-request.sh` uses `flock`, so it runs on Linux only. On macOS or Windows, take backups with `make backup` or a scheduled job instead; the button then shows that it is not set up.

## Browser security

- The app sends a Content Security Policy that allows only its own scripts, styles and images, so no third-party content can load.
- Pages cannot be framed by other sites (`X-Frame-Options: DENY`).
- Sign-ins are throttled per username and per address; see the [administrator guide](guide/administrator.md#sign-in-lockout).
- Passwords are hashed with Argon2 and must be at least 12 characters.
- The app uses no analytics, telemetry or third-party CDN.

## Logging and health

- `/health/` returns `ok` when the app can reach its database, and `503` with `database unavailable` when it cannot. The container health check uses it.
- Logs go to the container output: `docker compose logs --tail=200 web`, or `make logs` to follow them.
- Financial form data and secrets are never logged. Docker keeps up to 5 log files of 10 MB for each container.

## In-app documentation

The **Help** pages in the app are built from the Markdown files in `docs/guide/` and from the administrator chapters in `docs/`. The files are copied into the image, so a change to the documentation reaches users after `make up`.

## AI analysis

The AI analysis is off unless you set `REPORT_LLM_PROVIDER=openai_compatible` and give it a URL and model name. When it is on, a summary of your aggregate totals, category and account names, and pay dates is sent to `REPORT_LLM_URL`. Individual transactions, merchant names and notes are not sent. Use a model server you run yourself or one you trust.

```dotenv
REPORT_LLM_PROVIDER=openai_compatible
REPORT_LLM_URL=http://host.docker.internal:11434/v1/chat/completions
REPORT_LLM_MODEL=your-model-name
REPORT_LLM_DISABLE_THINKING=true
```

The address in this example is an Ollama server on the Docker host. The container must be able to reach the model server:

- `compose.yml` maps `host.docker.internal` to the Docker host, so this address works on Linux as well as Docker Desktop.
- The model server must listen on an address the container can reach, such as `0.0.0.0` (Ollama uses `OLLAMA_HOST=0.0.0.0`). Restrict it with a firewall so that only the Docker network and this computer can connect.
- A model server on another machine is reached by its own address, for example `http://192.0.2.10:8080/v1/chat/completions`.

After changing these settings, run `make up`. The full list of settings, and exactly what is sent, are in `docs/report-analysis.md`.

## HTTPS behind a reverse proxy

NoNonCents does not terminate HTTPS. Put a reverse proxy in front of it on the same computer, so it can reach the app on the loopback address:

```text
Browser --HTTPS--> reverse proxy (budget.example.com) --HTTP--> 127.0.0.1:8000 (web container)
```

Caddy is the simplest, because it obtains and renews certificates for public names automatically. Its `Caddyfile` is:

```text
budget.example.com {
    reverse_proxy 127.0.0.1:8000 {
        header_up X-Real-IP {remote_host}
    }
}
```

Caddy sets `X-Forwarded-Proto` itself. With nginx, the server block is:

```nginx
server {
    listen 443 ssl;
    server_name budget.example.com;

    ssl_certificate     /path/to/fullchain.pem;   # placeholder: your certificate
    ssl_certificate_key /path/to/privkey.pem;     # placeholder: your key

    client_max_body_size 12m;   # statement PDFs are up to 10 MB

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 180s;
    }
}
```

Add a second server block on port 80 that redirects to HTTPS if you want plain-HTTP visitors redirected. Get the certificate from a public authority (for example with `certbot` for nginx), or from a private certificate authority you run for your home network. A private authority means each device must trust its certificate.

Then set these in `.env`:

```dotenv
ALLOWED_HOSTS=budget.example.com,127.0.0.1,localhost
CSRF_TRUSTED_ORIGINS=https://budget.example.com
TRUST_PROXY_HTTPS=true
SESSION_COOKIE_SECURE=true
CSRF_COOKIE_SECURE=true
SECURE_SSL_REDIRECT=true
SECURE_HSTS_SECONDS=0
```

Run `make up`, then open `https://budget.example.com` and check that you can sign in and that `https://budget.example.com/health/` prints `ok`.

Notes:

- `SECURE_SSL_REDIRECT=true` needs `TRUST_PROXY_HTTPS=true`, and the proxy must send `X-Forwarded-Proto`. Without it every request would redirect to itself, and the app refuses to start with that combination.
- Set `SECURE_HSTS_SECONDS=31536000` (one year) only after HTTPS works on every device you use. Browsers then refuse plain HTTP for the site until that time passes.
- If the proxy runs on another machine or network, add its address to `TRUSTED_PROXY_NETWORKS`, and bind the app so the proxy can reach it (`WEB_BIND`).
- Do not publish the app port on all interfaces (`0.0.0.0:8000`) without HTTPS in front of it.

## Troubleshooting

Common problems and their fixes are in the [administrator guide](guide/administrator.md#administrator-troubleshooting).
