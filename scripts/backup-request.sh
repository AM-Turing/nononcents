#!/bin/sh
# Optional: runs "Back up now" requests left by the web app (Admin -> Backups).
# Needs a cron entry (or any scheduler) on the host; without it the button reports that it
# is not set up.
#
# From cron, every minute (it exits at once when nothing is waiting):
#   * * * * * /path/to/nononcents/scripts/backup-request.sh >> /path/to/nononcents/backups/backup.log 2>&1
# First time: scripts/backup-request.sh --setup   (creates the request folder)
#
# The web container can only drop a request file into BACKUP_REQUEST_DIR (mode 1733: it may
# create files there but not list or remove anything). This script takes the nightly job's
# lock, so a requested backup and the nightly backup never overlap, then runs
# scripts/backup.sh, the same verified backup. The result reaches Admin through the usual
# backup status file.
set -eu

project_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$project_dir"

env_value() {
    [ -f .env ] || return 0
    sed -n "s/^$1=//p" .env | tail -n 1 | sed -e 's/^"//' -e 's/"$//'
}

request_dir="${BACKUP_REQUEST_DIR:-$(env_value BACKUP_REQUEST_DIR)}"
request_dir="${request_dir:-$project_dir/backups/requests}"
case "$request_dir" in /*) ;; *) request_dir="$project_dir/${request_dir#./}" ;; esac
# The lock lives in the (owner-only) backup folder, not at a predictable /tmp path that any
# local user could create or hold. Keep it the same for every job that runs backup.sh.
backup_dir="${BACKUP_DIR:-$(env_value BACKUP_DIR)}"
backup_dir="${backup_dir:-$project_dir/backups}"
case "$backup_dir" in /*) ;; *) backup_dir="$project_dir/${backup_dir#./}" ;; esac
lock_file="${BACKUP_LOCK_FILE:-$backup_dir/.backup.lock}"
backup_command="${BACKUP_COMMAND:-$project_dir/scripts/backup.sh}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

if [ "${1:-}" = "--setup" ]; then
    mkdir -p "$request_dir"
    chmod 1733 "$request_dir"
    echo "Request folder ready: $request_dir (mode 1733)"
    exit 0
fi

request="$request_dir/backup-now.json"
[ -f "$request" ] || exit 0

[ -n "${BACKUP_LOCK_FILE:-}" ] || (umask 077 && mkdir -p "$backup_dir")
exec 9>"$lock_file"
if ! flock -n 9; then
    exit 0 # a backup is already running; the request waits for the next minute
fi

claimed="$request_dir/.claimed-$$"
mv "$request" "$claimed" 2>/dev/null || exit 0
who="$(sed -n 's/.*"requested_by": *"\([A-Za-z0-9_.@-]*\)".*/\1/p' "$claimed" | head -c 150)"
rm -f "$claimed"

log "Backup requested in the app by ${who:-an administrator}"
"$backup_command"
