#!/usr/bin/env bash
# Regenerate the user guide screenshots (docs/guide/images/*.png) from the made-up
# Demo Household profile, on a scratch SQLite database. Your real database is never used.
#   make guide-screenshots
set -euo pipefail
cd "$(dirname "$0")/.."
port="${BROWSER_TEST_PORT:-8775}"
work="$(mktemp -d)"
db="$work/guide-screenshots.sqlite3"
# A made-up healthy backup report and a writable request folder, so the Admin page shows what
# a working server shows (the scratch server has no backup job of its own).
mkdir -m 700 "$work/requests"
export BROWSER_TEST_TODAY="${BROWSER_TEST_TODAY:-$(cat tests/browser/FIXED_TODAY)}"
# Two hours before the fixed clock's noon start (UTC), so the Admin page never shows the real time.
finished="$(.venv/bin/python -c "import sys; print(sys.argv[1] + 'T10:00:00+00:00')" "$BROWSER_TEST_TODAY")"
cat > "$work/backup-status.json" <<JSON
{"state": "ok", "finished_at": "$finished", "last_success_at": "$finished",
 "copies_ok": 2, "copies_expected": 2, "dump": "nononcents-demo.dump", "message": ""}
JSON
export BROWSER_TEST_MODE=true DJANGO_DEBUG=true SQLITE_PATH="$db"
export BACKUP_STATUS_FILE="$work/backup-status.json" BACKUP_REQUEST_DIR_IN_APP="$work/requests"
.venv/bin/python manage.py migrate --noinput -v0
.venv/bin/python manage.py seed_demo_profile --yes
.venv/bin/python manage.py create_browser_user
.venv/bin/python manage.py runserver "127.0.0.1:$port" --noreload >/dev/null 2>&1 &
server=$!
trap 'kill "$server" 2>/dev/null; rm -rf "$work"' EXIT
for _ in $(seq 60); do
  curl -fs -o /dev/null "http://127.0.0.1:$port/login/" && break
  sleep 1
done
BROWSER_TEST_PORT="$port" node tests/browser/screenshots.js
