#!/usr/bin/env bash
# NoNonCents guided start for Linux and macOS.
#
#   ./start.sh
#
# 1. Checks that Docker and Docker Compose are installed and running.
# 2. Creates .env from .env.example with fresh random secrets (an existing .env is never
#    changed).
# 3. Builds and starts the app and its database with Docker Compose.
# 4. Waits until the app answers its health check.
# 5. Offers to create the first administrator, unless one already exists.
# 6. Prints the address to open.
#
# It is safe to run again at any time: it only builds, starts and checks.
#
# Optional environment variables (read from your shell, or from .env once it exists):
#   WEB_BIND             ADDRESS:PORT to publish the app on (default 127.0.0.1:8000)
#   COMPOSE_PROJECT_NAME Docker Compose project name (default nononcents)
set -eu

cd "$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"

say() { printf '%s\n' "$*"; }
fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }

# --- 1. Docker ------------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 ||
    fail "Docker is not installed. Install Docker Desktop (macOS) or Docker Engine (Linux): https://docs.docker.com/get-docker/"
docker info >/dev/null 2>&1 ||
    fail "Docker is installed but not running. Start Docker Desktop (or the Docker service) and run ./start.sh again."
docker compose version >/dev/null 2>&1 ||
    fail "The 'docker compose' command (Compose v2) is not available. Update Docker or install the Compose plugin: https://docs.docker.com/compose/install/"

# --- 2. .env --------------------------------------------------------------------------------
random_string() {
    # random_string LENGTH: letters and digits only, so the value is safe in .env and in SQL.
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 192 | LC_ALL=C tr -dc 'A-Za-z0-9' | cut -c "1-$1"
    else
        LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1"
    fi
}

set_env_value() {
    # set_env_value FILE KEY VALUE: replace the KEY=... line without sed -i (BSD and GNU differ).
    tmp="$1.tmp.$$"
    awk -v key="$2" -v value="$3" 'BEGIN { FS = OFS = "=" }
        $1 == key { print key "=" value; next } { print }' "$1" > "$tmp" && mv "$tmp" "$1"
}

env_value() {
    # env_value KEY DEFAULT: the shell's value wins, then .env, then DEFAULT.
    eval "shell_value=\${$1:-}"
    if [ -n "$shell_value" ]; then printf '%s' "$shell_value"; return; fi
    file_value="$(sed -n "s/^$1=//p" .env 2>/dev/null | tail -n 1 | sed -e 's/^"//' -e 's/"$//')"
    printf '%s' "${file_value:-$2}"
}

if [ -f .env ]; then
    say "Using your existing .env (not changed)."
else
    [ -f .env.example ] || fail ".env.example is missing; run this from the NoNonCents folder."
    say "Creating .env with new random secrets."
    (umask 077 && cp .env.example .env)
    set_env_value .env DJANGO_SECRET_KEY "$(random_string 64)"
    set_env_value .env POSTGRES_PASSWORD "$(random_string 40)"
    if [ -n "${WEB_BIND:-}" ]; then set_env_value .env WEB_BIND "$WEB_BIND"; fi
    chmod 600 .env
fi

# Folders the containers share with the backup scripts (created now so Docker does not create
# them as root).
mkdir -p backups/status
chmod 755 backups/status
./scripts/backup-request.sh --setup >/dev/null 2>&1 || mkdir -p backups/requests

# --- 3. Build and start ---------------------------------------------------------------------
say "Building and starting NoNonCents (the first build takes a few minutes)..."
docker compose up -d --build ||
    fail "docker compose could not start the app. Read the messages above, or run: docker compose logs"

# --- 4. Wait for the health check -----------------------------------------------------------
bind="$(env_value WEB_BIND 127.0.0.1:8000)"
host="${bind%:*}"
port="${bind##*:}"
case "$host" in ""|"0.0.0.0"|"::"|"[::]"|"*") host="127.0.0.1" ;; esac
url="http://$host:$port"

healthy() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsS -o /dev/null --max-time 5 "$url/health/" 2>/dev/null
    else
        docker compose exec -T web python -c \
            "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/')" \
            >/dev/null 2>&1
    fi
}

say "Waiting for the app to answer at $url/health/ ..."
waited=0
until healthy; do
    waited=$((waited + 2))
    if [ "$waited" -ge 180 ]; then
        docker compose logs --tail=40 web 2>&1 || true
        fail "The app did not become healthy within 3 minutes. See the log above (docker compose logs web)."
    fi
    sleep 2
done
say "The app is running."

# --- 5. First administrator -----------------------------------------------------------------
has_admin() {
    docker compose exec -T web python manage.py shell -c \
        "import sys; from django.contrib.auth import get_user_model as g; sys.exit(0 if g().objects.filter(is_superuser=True).exists() else 1)" \
        >/dev/null 2>&1
}

if has_admin; then
    say "An administrator already exists."
elif [ -t 0 ] && [ -t 1 ]; then
    printf 'Create the first administrator now? [Y/n] '
    read -r answer || answer=n
    case "$answer" in
        ""|[Yy]*)
            say "Choose a username and a password of at least 12 characters (an email is optional)."
            docker compose exec web python manage.py createsuperuser ||
                say "No administrator was created. Run ./start.sh again, or: docker compose exec web python manage.py createsuperuser"
            ;;
        *)
            say "Skipped. Create one later with: docker compose exec web python manage.py createsuperuser"
            ;;
    esac
else
    say "No administrator exists yet. Create one with: docker compose exec web python manage.py createsuperuser"
fi

# --- 6. Done --------------------------------------------------------------------------------
say ""
say "NoNonCents is ready: $url"
say "Sign in with your administrator account; you will create your first budget profile there."
say "Stop it with: docker compose down      (never add -v: that deletes your data)"
