"""Read the status file written by the backup job. Every field is untrusted."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from django.conf import settings
from django.http import HttpRequest
from django.utils import timezone

MAX_STATUS_BYTES = 64 * 1024
MAX_MESSAGE_CHARS = 300
MAX_DUMP_CHARS = 200


@dataclass(frozen=True)
class BackupStatus:
    level: str  # "ok" | "error" | "unknown"
    headline: str
    detail: str = ""
    last_success_at: datetime | None = None
    finished_at: datetime | None = None
    copies_ok: int | None = None
    copies_expected: int | None = None
    dump: str | None = None


def _unknown(headline: str, detail: str = "") -> BackupStatus:
    return BackupStatus("unknown", headline, detail)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip()[:limit]


def _load(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_STATUS_BYTES + 1)
        if len(raw) > MAX_STATUS_BYTES:
            return None
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_backup_status(now: datetime | None = None) -> BackupStatus:
    now = now or timezone.now()
    path = Path(settings.BACKUP_STATUS_FILE)
    try:
        exists = path.exists()
    except OSError:
        exists = True
    if not exists:
        return _unknown(
            "Backup status is not available",
            "No backup has reported yet. Run scripts/backup.sh (or make backup) on the server "
            "to take the first one, or check that the backup status folder is mounted.",
        )
    data = _load(path)
    if data is None:
        return _unknown("Backup status could not be read")

    state = data.get("state")
    finished_at = _timestamp(data.get("finished_at"))
    last_success_at = _timestamp(data.get("last_success_at"))
    message = _text(data.get("message"), MAX_MESSAGE_CHARS) or ""
    fields: dict[str, Any] = {
        "finished_at": finished_at,
        "last_success_at": last_success_at,
        "copies_ok": _count(data.get("copies_ok")),
        "copies_expected": _count(data.get("copies_expected")),
        "dump": _text(data.get("dump"), MAX_DUMP_CHARS) or None,
    }
    if state not in ("ok", "failed"):
        return _unknown("Backup status could not be read")

    max_hours = settings.BACKUP_MAX_AGE_HOURS
    if last_success_at is None:
        return BackupStatus(
            "error",
            "No successful backup has been recorded",
            message if state == "failed" else "",
            **fields,
        )
    if now - last_success_at > timedelta(hours=max_hours):
        return BackupStatus(
            "error",
            f"No successful backup in the last {max_hours} hours",
            message if state == "failed" else "",
            **fields,
        )
    if state == "failed":
        return BackupStatus("error", "The last backup failed", message, **fields)
    return BackupStatus("ok", "Backups are healthy", message, **fields)


def request_backup_status(request: HttpRequest) -> BackupStatus:
    """Read the status at most once per request."""
    cached = getattr(request, "_backup_status", None)
    if cached is None:
        cached = read_backup_status()
        request._backup_status = cached  # type: ignore[attr-defined]
    return cached
