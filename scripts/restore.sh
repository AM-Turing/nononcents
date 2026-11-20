#!/bin/sh
# Restore a NoNonCents backup without ever leaving the app with an empty database.
#
#   scripts/restore.sh BACKUP.dump
#
# 1. Takes a fresh safety backup of the current database (scripts/backup.sh).
# 2. Restores the file into a new staging database and checks it.
# 3. Stops the web container, renames the current database to <name>_pre_restore_<stamp>
#    and the staging database to the live name, then starts web (which migrates).
# The previous database is kept so the swap can be undone; drop it once satisfied.
#
# Drill mode: RESTORE_TARGET_DB=some_other_name restores into that database only and
# never touches the live database or the web container.
set -eu

project_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
file="${1:?Usage: scripts/restore.sh BACKUP.dump}"
case "$file" in /*) ;; *) file="$PWD/$file" ;; esac
[ -s "$file" ] || { echo "No such backup file: $file" >&2; exit 1; }
cd "$project_dir"

db() { docker compose exec -T db sh -c "$1" </dev/null; }
# Same, but feeds this script's stdin (a dump file) to the command.
db_in() { docker compose exec -T db sh -c "$1"; }
live_db="$(db 'printf %s "$POSTGRES_DB"')"
target="${RESTORE_TARGET_DB:-$live_db}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
staging="restore_staging_$(date +%s)_$$"

case "$target" in *[!A-Za-z0-9_]*|'') echo "Invalid target database name" >&2; exit 1 ;; esac

drop_staging() { db "dropdb -U \"\$POSTGRES_USER\" --if-exists $staging" >/dev/null 2>&1 || true; }
web_stopped=0
# On any failure: drop the staging database and, if this script stopped web, start it again.
cleanup() {
    drop_staging
    if [ "$web_stopped" = 1 ]; then docker compose start web || true; fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

if [ "$target" = "$live_db" ]; then
    printf 'This replaces the live database %s with %s.\nType the database name to continue: ' \
        "$live_db" "$(basename "$file")"
    read -r confirmation
    [ "$confirmation" = "$live_db" ] || { echo "Restore cancelled"; exit 1; }
    echo "Taking a safety backup of the current database first"
    ./scripts/backup.sh || { echo "Safety backup failed; nothing was changed" >&2; exit 1; }
fi

echo "Restoring into staging database $staging"
db "createdb -U \"\$POSTGRES_USER\" $staging"
db_in "pg_restore -U \"\$POSTGRES_USER\" -d $staging --no-owner --exit-on-error" < "$file"
counts="$(db "psql -U \"\$POSTGRES_USER\" -d $staging -Atc \"select (select count(*) from budget_expense)||' expenses, '||(select count(*) from budget_income)||' incomes, '||(select count(*) from auth_user)||' users'\"")"
echo "Staging database contains: $counts"

swap_sql() {
    db "psql -U \"\$POSTGRES_USER\" -d postgres -v ON_ERROR_STOP=1 -c \"$1\""
}

if [ "$target" = "$live_db" ]; then
    previous="${live_db}_pre_restore_${stamp}"
    web_stopped=1
    docker compose stop web
    swap_sql "select pg_terminate_backend(pid) from pg_stat_activity where datname='$live_db' and pid <> pg_backend_pid();" >/dev/null
    # One psql -c string runs as a single transaction: both renames happen or neither does.
    swap_sql "alter database \\\"$live_db\\\" rename to \\\"$previous\\\"; alter database \\\"$staging\\\" rename to \\\"$live_db\\\";"
    trap - EXIT INT TERM
    docker compose start web
    echo "Restore complete. The previous database is kept as $previous."
    echo "To undo: stop web, rename $live_db away and $previous back to $live_db, start web."
    echo "When satisfied: docker compose exec db dropdb -U <user> $previous"
else
    db "dropdb -U \"\$POSTGRES_USER\" --if-exists $target"
    swap_sql "alter database \\\"$staging\\\" rename to \\\"$target\\\";"
    trap - EXIT INT TERM
    echo "Drill restore complete into $target ($counts). The live database was not touched."
    echo "Drop it when done: docker compose exec db dropdb -U <user> $target"
fi
