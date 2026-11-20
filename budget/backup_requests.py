"""The "Back up now" button in Admin.

The web container cannot run the backup itself (it has no access to the host or the database
container's tools, by design). It leaves a small request file in a shared folder; the host job
scripts/backup-request.sh checks that folder every minute and runs scripts/backup.sh, the
same verified backup as a scheduled one (sharing its lock, so they never overlap). The result
appears in the Backups card through the usual status file.
"""

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.utils import timezone

REQUEST_FILE = "backup-now.json"


def request_dir() -> Path:
    return Path(settings.BACKUP_REQUEST_DIR)


def available() -> bool:
    """The shared folder is mounted and this process may write to it."""
    folder = request_dir()
    return folder.is_dir() and os.access(folder, os.W_OK | os.X_OK)


def pending() -> dict | None:
    """The waiting request, if any: {"requested_by": ..., "requested_at": datetime}."""
    path = request_dir() / REQUEST_FILE
    try:
        data = json.loads(path.read_text())
        return {
            "requested_by": str(data.get("requested_by", "")),
            "requested_at": datetime.fromisoformat(data["requested_at"]),
        }
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        # Present but unreadable (the host job may be picking it up): still waiting.
        return {"requested_by": "", "requested_at": None} if path.exists() else None


def request_backup(username: str) -> bool:
    """Leave a request for the host job. False if one is already waiting or none can be left."""
    if not available() or pending():
        return False
    folder = request_dir()
    safe_name = re.sub(r"[^A-Za-z0-9_.@-]", "", username)[:150]
    body = json.dumps({"requested_by": safe_name, "requested_at": timezone.now().isoformat()})
    handle, temporary = tempfile.mkstemp(prefix=".request-", dir=folder)
    with os.fdopen(handle, "w") as stream:
        stream.write(body)
    os.chmod(temporary, 0o644)
    os.replace(temporary, folder / REQUEST_FILE)
    return True
