#!/bin/sh
# Phone layout audit in WebKit (Safari's engine), the browser on iPhones.
# Starts a test server with the demo profile on the host, runs tests/webkit in Playwright's Docker image as the
# current user (no sudo), then stops the server.
set -eu
project_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$project_dir"
port="${BROWSER_TEST_PORT:-8776}"
version="$(node -e 'console.log(require("@playwright/test/package.json").version)')"
db="$(mktemp -d)/webkit.sqlite3"
export BROWSER_TEST_TODAY="${BROWSER_TEST_TODAY:-$(cat tests/browser/FIXED_TODAY)}"
export DJANGO_DEBUG=true BROWSER_TEST_MODE=true SQLITE_PATH="$db"
.venv/bin/python manage.py migrate --noinput >/dev/null
.venv/bin/python manage.py seed_demo_profile --yes >/dev/null
.venv/bin/python manage.py create_browser_user >/dev/null
.venv/bin/python manage.py runserver "127.0.0.1:$port" --noreload >/dev/null 2>&1 &
server=$!
trap 'kill $server 2>/dev/null || true; rm -f "$db"' EXIT
curl -s --retry 20 --retry-connrefused --retry-delay 1 -o /dev/null "http://127.0.0.1:$port/login/"
docker run --rm --ulimit core=0 --user "$(id -u):$(id -g)" -e HOME=/tmp -e BROWSER_TEST_PORT="$port" \
    --network host -v "$project_dir:/work" -w /work/tests/webkit \
    "mcr.microsoft.com/playwright:v$version-noble" npx playwright test
