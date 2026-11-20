# Backup and restore

NoNonCents backs up its PostgreSQL database with `pg_dump`. Each backup is checked before it counts: it is restored into a scratch database, and the row counts are compared with the live database. Restores are done by `scripts/restore.sh`, which never leaves the app with an empty database.

## Where backups are

| Item | Default | Setting |
| --- | --- | --- |
| Backup folder | `backups/` in the NoNonCents folder | `BACKUP_DIR` |
| Dumps | `nononcents-<UTC timestamp>.dump`, one file per backup | (name is fixed) |
| Permissions | the folder is owner-only (`700`), each dump is `600` | (fixed) |
| Newest good backup | `LAST_SUCCESS` in the backup folder | (fixed) |
| Status shown in Admin | `status/backup-status.json` in a status folder (no secrets) | `BACKUP_STATUS_DIR` |
| Extra copies | none, unless folders are listed | `BACKUP_COPY_DIRS` |
| "Back up now" requests | `requests/` in a request folder | `BACKUP_REQUEST_DIR` |

A copy on the same disk does not protect against disk failure. For real safety, list at least one folder on another disk in `BACKUP_COPY_DIRS`, and keep one copy somewhere else entirely.

## Known limitations

- A dump contains everything in the database, including password hashes and sign-in sessions. Keep dumps private, and encrypt any copy that leaves your control.
- Dumps do not include `.env`. Keep a copy of `.env` somewhere safe, because the secret key and the database settings are needed to run a restored installation.
- A restore replaces the data as it was when the backup was taken. Anything entered after that is lost.
- Uploaded statement PDFs are not stored, so they are not in backups. Only the figures you confirmed are.

## How a backup works

`scripts/backup.sh` does the following, and exits with an error if any step fails:

1. Dumps the live database to a temporary file inside the database container (`pg_dump`, custom format).
2. Restores that dump into a scratch database and compares row counts with the live database, then drops the scratch database.
3. Publishes the dump by atomic rename to `BACKUP_DIR`, and to each folder in `BACKUP_COPY_DIRS`. Each copy is checked against the dump's checksum.
4. Applies retention: deletes dumps older than `BACKUP_RETENTION_DAYS`, but always keeps the `BACKUP_KEEP_MIN` newest.
5. Writes the status file that Admin reads, and requests `BACKUP_PING_URL` if it is set.

It writes no secrets to the status file, and it reads only the backup settings from `.env`. Run it from cron or a timer, as described in [Deployment](deployment.md#scheduled-jobs). Use the lock file `backups/.backup.lock` in the job, so a scheduled backup and a "Back up now" backup never overlap.

### Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `BACKUP_DIR` | `./backups` | Primary folder. A relative path is relative to the NoNonCents folder. |
| `BACKUP_COPY_DIRS` | empty | Space-separated folders for copies. Each must already exist; the script never creates them. Do not put spaces in the paths. |
| `BACKUP_RETENTION_DAYS` | `30` | Days to keep old dumps. |
| `BACKUP_KEEP_MIN` | `7` | Number of newest dumps always kept, however old. |
| `BACKUP_PING_URL` | empty | Requested after every successful backup. |
| `BACKUP_STATUS_DIR` | `BACKUP_DIR/status` | Folder for the status file that Admin reads. Mounted read-only into the app. |
| `BACKUP_REQUEST_DIR` | `BACKUP_DIR/requests` | Folder for "Back up now" requests. |
| `BACKUP_MAX_AGE_HOURS` | `26` | Hours without a successful backup before Admin shows a problem. |

## Checking backups

- On **Admin**, the **Backups** card shows **Healthy** or **Problem**, the time of the last successful backup and the copies made.
- From the NoNonCents folder:

  ```bash
  cat backups/LAST_SUCCESS
  ls -lt backups/*.dump | head -n 5
  ```

- Rehearse a restore now and then with the [restore drill](#restore-drill). A backup you have never restored is not yet proven.

### Alert in the web app

If the last run failed, or no backup has succeeded within `BACKUP_MAX_AGE_HOURS`, the Backups card shows **Problem**. Administrators also see a red banner on every page until a fully successful run.

## Restore the live database

Restoring replaces the live database. Use it only when you have to, and stop anyone from changing data while you do.

```bash
make restore FILE=backups/nononcents-20260101T021500Z.dump
```

The same command works without `make`: `./scripts/restore.sh BACKUP_FILE`. The script does this:

1. Asks you to type the live database name (by default `nononcents`). Anything else cancels with no change.
2. Takes a fresh safety backup of the current database. If that fails, nothing is changed.
3. Restores the file into a new staging database and reports how many expenses, incomes and users it contains.
4. Stops the `web` container and ends connections to the live database.
5. Renames the live database to `<name>_pre_restore_<timestamp>` and the staging database to the live name, in one transaction, so both renames happen or neither does.
6. Starts `web` again. The app applies any migrations that the backup predates.

The previous database is kept under its new name. Keep it until you are satisfied with the restore.

### Undo a restore

Stop the app, rename the databases back, and start the app again. Replace `<name>` with your database name, `<timestamp>` with the one the restore printed, and `<user>` with your `POSTGRES_USER`:

```bash
docker compose stop web
docker compose exec -T db psql -U <user> -d postgres -v ON_ERROR_STOP=1 -c 'alter database "<name>" rename to "<name>_unwanted"; alter database "<name>_pre_restore_<timestamp>" rename to "<name>";'
docker compose start web
```

When you no longer need a database, drop it:

```bash
docker compose exec db dropdb -U <user> <name>_unwanted
```

### Restoring on a new server

Install NoNonCents as in the [deployment guide](deployment.md#first-install), using the same `POSTGRES_DB`, `POSTGRES_USER` and `POSTGRES_PASSWORD` as the old installation (taken from its `.env`). Start only the database with `docker compose up -d db`, then run `make restore FILE=...` with a dump from the old server. Then start the app with `make up`.

## Restore drill

A drill restores a backup into a separate database and leaves the live database alone:

```bash
make restore-drill FILE=backups/nononcents-20260101T021500Z.dump
```

It restores into a database named `restore_drill` and reports the counts. Check that they look right, then drop the database:

```bash
docker compose exec db dropdb -U <user> restore_drill
```

Use the drill to prove that a backup is usable. Do it after changing backup settings, after a large update, and on a regular schedule.
