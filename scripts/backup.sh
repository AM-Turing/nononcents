#!/bin/sh
# Verified PostgreSQL backup for NoNonCents.
#
# 1. Dumps the live database to a temporary file.
# 2. Proves the dump is restorable by fully restoring it into a scratch database
#    inside the db container, comparing row counts, then dropping the scratch copy.
# 3. Publishes it atomically (rename) to BACKUP_DIR and to every BACKUP_COPY_DIRS entry.
# 4. Applies retention, always keeping at least BACKUP_KEEP_MIN newest dumps.
#
# Safe to run from cron: it locates the project itself and reads only the backup
# settings it needs from .env. Exits non-zero on any failure.
set -eu

project_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$project_dir"

env_value() {
    # Read KEY=value from .env without sourcing (and without touching secrets).
    [ -f .env ] || return 0
    sed -n "s/^$1=//p" .env | tail -n 1 | sed -e 's/^"//' -e 's/"$//'
}

backup_dir="${BACKUP_DIR:-$(env_value BACKUP_DIR)}"
backup_dir="${backup_dir:-$project_dir/backups}"
# A relative BACKUP_DIR (such as ./backups) is relative to the project folder.
case "$backup_dir" in /*) ;; *) backup_dir="$project_dir/${backup_dir#./}" ;; esac
copy_dirs="${BACKUP_COPY_DIRS:-$(env_value BACKUP_COPY_DIRS)}"
retention="${BACKUP_RETENTION_DAYS:-$(env_value BACKUP_RETENTION_DAYS)}"
retention="${retention:-30}"
keep_min="${BACKUP_KEEP_MIN:-$(env_value BACKUP_KEEP_MIN)}"
keep_min="${keep_min:-7}"
ping_url="${BACKUP_PING_URL:-$(env_value BACKUP_PING_URL)}"
# Small, secret-free status file read by the web app (mounted read-only) to show an
# administrator alert when backups fail or go stale.
status_dir="${BACKUP_STATUS_DIR:-$(env_value BACKUP_STATUS_DIR)}"
status_dir="${status_dir:-$backup_dir/status}"

# sha256sum on Linux, shasum on macOS.
sha256() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi
}

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

finished=""
dump_name=""
copies_ok=0
copies_expected=0
for _dir in $copy_dirs; do copies_expected=$((copies_expected + 1)); done

json_string() {
    # Quote a value for JSON: drop control characters, escape backslash and quote.
    printf '"%s"' "$(printf '%s' "$1" | tr -d '\000-\037' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
}

write_status() {
    # write_status ok|failed MESSAGE
    last_success="null"
    if [ -f "$backup_dir/LAST_SUCCESS" ]; then
        iso="$(cut -d' ' -f1 "$backup_dir/LAST_SUCCESS" \
            | sed -n 's/^\([0-9]\{4\}\)\([0-9][0-9]\)\([0-9][0-9]\)T\([0-9][0-9]\)\([0-9][0-9]\)\([0-9][0-9]\)Z$/\1-\2-\3T\4:\5:\6Z/p')"
        [ -z "$iso" ] || last_success="$(json_string "$iso")"
    fi
    dump_json="null"
    [ -z "$dump_name" ] || dump_json="$(json_string "$dump_name")"
    (
        umask 022
        mkdir -p "$status_dir" && chmod 755 "$status_dir" || exit 0
        tmp="$status_dir/.backup-status.json.tmp"
        printf '{"state": %s, "finished_at": %s, "message": %s, "last_success_at": %s, "dump": %s, "copies_ok": %d, "copies_expected": %d}\n' \
            "$(json_string "$1")" "$(json_string "$(date -u +%Y-%m-%dT%H:%M:%SZ)")" \
            "$(json_string "$2")" "$last_success" "$dump_json" \
            "$copies_ok" "$copies_expected" > "$tmp" \
            && chmod 644 "$tmp" && mv "$tmp" "$status_dir/backup-status.json"
    ) || log "Could not write backup status file" >&2
}

fail() {
    log "BACKUP FAILED: $*" >&2
    write_status failed "$*"
    finished="yes"
    exit 1
}

umask 077
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
name="nononcents-$stamp.dump"
partial="$backup_dir/.$name.partial"
scratch="backup_verify_$(date +%s)_$$"

db() { docker compose exec -T db sh -c "$1" </dev/null; }
# Same, but feeds this script's stdin (a dump file) to the command.
db_in() { docker compose exec -T db sh -c "$1"; }

cleanup() {
    rc=$?
    rm -f "$partial"
    db "dropdb -U \"\$POSTGRES_USER\" --if-exists $scratch" >/dev/null 2>&1 || true
    if [ -z "$finished" ]; then
        write_status failed "Backup stopped unexpectedly (exit status $rc)"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

log "Dumping database"
db 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$partial" || fail "pg_dump failed"
[ -s "$partial" ] || fail "dump is empty"

log "Verifying by full restore into scratch database $scratch"
db "createdb -U \"\$POSTGRES_USER\" $scratch" || fail "could not create scratch database"
db_in "pg_restore -U \"\$POSTGRES_USER\" -d $scratch --no-owner --exit-on-error" < "$partial" \
    || fail "dump did not restore cleanly"

count_sql="select (select count(*) from budget_expense)||' '||(select count(*) from budget_income)||' '||(select count(*) from budget_transfer)||' '||(select count(*) from auth_user)"
live_counts="$(db "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -Atc \"$count_sql\"")" \
    || fail "could not count live rows"
restored_counts="$(db "psql -U \"\$POSTGRES_USER\" -d $scratch -Atc \"$count_sql\"")" \
    || fail "could not count restored rows"
# Rows written between the dump and this check can only make the live side larger.
set -- $live_counts
live_expense=$1 live_income=$2 live_transfer=$3 live_users=$4
set -- $restored_counts
[ "$1" -le "$live_expense" ] && [ "$2" -le "$live_income" ] \
    && [ "$3" -le "$live_transfer" ] && [ "$4" -le "$live_users" ] && [ "$4" -gt 0 ] \
    || fail "restored row counts ($restored_counts) do not match live ($live_counts)"
log "Restore verified (expenses incomes transfers users: $restored_counts)"

chmod 600 "$partial"
mv "$partial" "$backup_dir/$name"
dump_name="$name"
checksum="$(sha256 "$backup_dir/$name" | cut -d' ' -f1)"
log "Saved $backup_dir/$name ($checksum)"

prune() {
    # Delete dumps older than the retention window, but never the newest $keep_min.
    ls -1t "$1"/nononcents-*.dump 2>/dev/null | tail -n "+$((keep_min + 1))" | while read -r old; do
        if [ -n "$(find "$old" -mtime "+$retention" 2>/dev/null)" ]; then
            rm -f "$old" && log "Pruned $old"
        fi
    done
}
prune "$backup_dir"

copy_failed=""
for dir in $copy_dirs; do
    if [ ! -d "$dir" ]; then
        copy_failed="$copy_failed; $dir is missing (is the disk mounted?)"
        # Never create the copy directory: if an external disk is unplugged, creating
        # it would silently write the "extra" copy onto the main disk.
        log "Copy directory $dir is missing (is the disk mounted?)" >&2
        continue
    fi
    if cp "$backup_dir/$name" "$dir/.$name.partial" \
        && [ "$(sha256 "$dir/.$name.partial" | cut -d' ' -f1)" = "$checksum" ] \
        && mv "$dir/.$name.partial" "$dir/$name"; then
        chmod 600 "$dir/$name" 2>/dev/null || true
        log "Copied to $dir"
        copies_ok=$((copies_ok + 1))
        prune "$dir"
    else
        rm -f "$dir/.$name.partial"
        log "Copy to $dir failed" >&2
        copy_failed="$copy_failed; copy to $dir failed"
    fi
done

[ -z "$copy_failed" ] || fail "Local backup saved, but a copy failed${copy_failed}"
# Written only when the local dump and every copy succeeded.
printf '%s %s\n' "$stamp" "$name" > "$backup_dir/LAST_SUCCESS"

if [ -n "$ping_url" ]; then
    curl -fsS -m 10 --retry 3 "$ping_url" >/dev/null || log "Backup ping failed" >&2
fi
write_status ok "Backup complete"
finished="yes"
log "Backup complete"
