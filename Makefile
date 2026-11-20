.PHONY: setup dev format lint typecheck test migrate demo backup restore restore-drill up down logs browser-test browser-test-webkit guide-screenshots
VERSION ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo latest)

# --- Development (a local Python virtual environment; SQLite unless POSTGRES_DB is set) ---
setup:
	python3 -m venv .venv
	.venv/bin/pip install --require-hashes -r requirements-dev.lock
dev:
	.venv/bin/python manage.py runserver
format:
	.venv/bin/ruff format .
lint:
	.venv/bin/ruff check .
typecheck:
	.venv/bin/mypy budget nononcents
test:
	.venv/bin/pytest
# Local development database only. The container migrates automatically on start.
migrate:
	.venv/bin/python manage.py migrate
# Load the made-up demo profile into the local development database.
demo:
	.venv/bin/python manage.py seed_demo_profile --yes
browser-test:
	npm run test:browser
# Phone layout audit in WebKit through Playwright's Docker image (see scripts/webkit-check.sh).
browser-test-webkit:
	./scripts/webkit-check.sh
# User guide screenshots from the made-up demo profile on a scratch database.
guide-screenshots:
	./scripts/guide-screenshots.sh

# --- Running with Docker (see start.sh for the guided first start) ---
up:
	APP_VERSION=$(VERSION) docker compose up -d --build
# Stops the containers. Never add -v: that deletes the database volume.
down:
	docker compose down
logs:
	docker compose logs --tail=200 -f web

# --- Backups (BACKUP_DIR in .env, default ./backups) ---
backup:
	./scripts/backup.sh
restore:
	./scripts/restore.sh $(FILE)
# Restore FILE into a throwaway database to prove it works; live data is untouched.
restore-drill:
	RESTORE_TARGET_DB=restore_drill ./scripts/restore.sh $(FILE)
