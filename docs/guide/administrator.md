# Administrator guide

This chapter is for the people who look after NoNonCents: they create budget profiles and
logins, keep backups healthy and update the server. Only administrators can see it.

Two kinds of administrator exist:

- **Superuser**: can manage every profile and every user, and can make other people
  administrators. The first account created on the server is a superuser.
- **Administrator (staff)**: sees the **Admin** page, but only for the profiles they belong to
  (see [What an administrator can see](#what-an-administrator-can-see)).

Everyone else is a **standard user**: they can use every budget page of the profiles they have
access to, but they never see Admin.

## The Admin page

Open **Admin** from the left-hand menu on a computer, or **More → Admin** on a phone. The page
works on the profile you signed in to; the profile name is shown at the top of every page.
(Admin can be renamed, so it may have a different name in your menus.)

The page has these sections, from top to bottom:

| Section | What it is for |
| --- | --- |
| Backups | Health of the nightly backup, and the **Back up now** button |
| Page names | Rename any page in the menus |
| Profile alerts | Turn the Home page warnings on or off for this profile |
| Quick actions | Shortcut buttons on the Home page |
| Budget profiles | Create profiles, manage who can open them, delete empty ones |
| Users | Create logins, reset passwords, remove people |
| Activity log | Who did what, and every sign-in |

### Page names

1. Under **Page names**, choose a page in the **Page** list.
2. Type the new name in **New name** and select **Save name**.
3. To go back to the original name, choose the page, leave **New name** empty and save.

The new name appears in the menus and as the page heading for everyone using this profile.
Other profiles keep their own names.

### Profile alerts

These switches control the warnings on the Home page for this profile only:

- **Show the Spending low-balance alert**, with the amount it warns below.
- **Show the current-period Bills funding shortage alert**.
- **Show the bill-confirmation alert** (bills whose amount still needs confirming).

Select **Save alerts**.

### Quick actions

Quick actions are buttons on the Home page that jump straight to a task, such as **Add
expense** or **Add transfer**. Under **Quick actions**, pick a destination, type the button label
and select **Add quick action**. Remove one with its **Remove** button.

## Budget profiles

A **budget profile** is a completely separate budget: its own accounts, bills, categories,
transactions and users. People choose the profile on the sign-in screen.

### Create a profile

1. On **Admin**, under **Budget profiles**, select **＋ Add profile**.
2. Type the profile's name (for example "Household Budget") and select **Create profile**.

The new profile starts with:

- three accounts: **Spending**, **Bills** and **Savings**;
- four categories: **General** and **ATM Withdrawal** (Spending), **Other Bills** (Bills) and
  **Income**;
- a pay schedule on the 1st and 15th.

You become a member of it. To set it up, sign out, choose the new profile on the sign-in screen,
and follow [Getting started](getting-started.md).

### Give someone access to a profile

1. On **Admin**, under **Budget profiles**, select **Manage access** next to the profile.
2. Tick everyone who should be able to open it and select **Save profile access**.

Rules the app enforces:

- You cannot remove yourself from the profile you are signed in to.
- At least one administrator must keep access to every profile.
- A user created on Admin is automatically a member of the profile you are signed in to.
- A superuser can open any profile; choosing it at sign-in adds the membership.

### Delete a profile

Only an empty profile (no transactions, bills or pay periods) can be deleted, and never the one
you are signed in to. Sign in to a different profile, then on **Admin → Budget profiles**,
select **Delete profile** and type the profile's name to confirm.

## Users

### Create a login

1. On **Admin**, under **Users**, select **＋ Add user**.
2. Fill in the username, name, email (optional) and a password twice.
3. A superuser also sees **Allow access to this admin page**. Tick it only for people who
   should manage users and settings.
4. Select **Create user**.

The person can now sign in by choosing this profile on the sign-in screen. To let them open
other profiles too, use **Manage access** on those profiles.

### Reset a forgotten password

1. On **Admin**, under **Users**, select **Reset password** next to the person.
2. Type the new password twice and save.
3. Tell them the new password in person; they sign in with it straight away.

If nobody can sign in at all (for example the only administrator forgot their password), reset
it on the server:

```bash
docker compose exec web python manage.py changepassword USERNAME
```

Replace `USERNAME` with the login name. To create a new superuser instead:

```bash
docker compose exec web python manage.py createsuperuser
```

### Remove someone

On **Admin → Users**, select **Delete user** and type their username to confirm. If they also
belong to other profiles, they only lose access to this one; otherwise their login is deleted.
You cannot delete your own login. Their past entries stay in the budget and in the activity log.

### What an administrator can see

A superuser sees every profile and every user. An administrator who is not a superuser sees:

- only the profiles they are a member of;
- only users who belong to no profile outside those, and never superusers or other
  administrators (they always see themselves, to reset their own password);
- in **Manage access**, members they cannot see are kept as they are.

Only a superuser can make someone an administrator. To do that for an existing login, use the
Django admin: open `/admin/` on the app's address (for example `https://budget.example.com/admin/`), choose **Users**, open the person, tick
**Staff status** and save.

Signing in to the Django admin at `/admin/` uses the app's own sign-in page, so the same
[sign-in lockout](#sign-in-lockout) and activity log apply. After signing in, a staff account
opens the admin page (or the page it asked for). Other accounts are refused.

The Django admin at `/admin/` edits database rows directly and skips the app's checks (locked
pay periods, balanced splits). Use it only for login settings like the one above, never to
change transactions or balances.

## Locked pay periods

Anyone can lock a finished pay period on **Budgets → Manage pay periods** so it can no longer be
changed. Only an administrator can reopen one:

1. Open **Budgets** (on a phone: **More → Budgets**) and select **Manage pay periods**.
2. Find the period and select **Reopen**.
3. Make the correction, then select **Lock** again.

Reopening and locking are recorded in the activity log.

## Activity log

The **Activity log** at the bottom of Admin shows the 100 most recent entries for the profile
you are signed in to: when, who, what was done, what it was done to, and the details of the
change (for example "merchant: fresh market → Fresh Market"). Passwords are never recorded.

Use the buttons above it to filter:

| Filter | Shows |
| --- | --- |
| All | Everything |
| Sign-ins | Successful sign-ins, with the profile and the device's network address |
| Failed sign-ins | Wrong password or no access. Shows the username that was typed when it is a real user, otherwise **Unknown user**. Attempts blocked by the lockout are not recorded |
| Transactions | Expenses, income, refunds, transfers and balance corrections |
| Other changes | Bills, pay periods, accounts, categories, settings, users, backup requests |

One action that changed many records at once (for example combining merchant names on Habits)
is shown as one row such as "10 expenses". "System (automatic)" means no person made the change: the app or a
server command did it, for example the bill clean-up command.

The activity log keeps entries for 60 days. Older entries are removed automatically, at most once
a day, the next time someone signs in. Nothing needs to be scheduled. The Activity log section on
Admin says how long entries are kept.

The number of days is set with `ACTIVITY_LOG_RETENTION_DAYS` in the `.env` file (see
[Deployment and updates](../deployment.md#activity-log)). `0` keeps entries forever.

To remove old entries by hand, run this in the NoNonCents folder (add `--dry-run` to see how many would be
removed without removing them):

```bash
docker compose exec web python manage.py prune_activity_log
```

Removed entries cannot be restored except from a backup taken before the removal. Undo, balances
and reports do not depend on the log.

People sometimes type a password into the username box. So a failed sign-in never shows a typed
name that is not a real user; the row says **Unknown user** instead.

A failed sign-in for a profile that does not exist, or with no profile chosen, is not shown
in any profile's log.

## Sign-in lockout

To slow down password guessing, the sign-in screen blocks:

- a **username** on one device after **5 wrong passwords within 15 minutes**;
- a **device address** after **20 wrong passwords within 15 minutes**, for any usernames.

Mistakes on one device do not lock that person out on another device.

The screen then says "Too many sign-in attempts. Try again in N minutes." The block lifts by
itself once 15 minutes have passed since the earliest of those attempts. A successful sign-in
clears that username's count on that device. Blocked attempts are not added to the activity log.

To lift a block at once (for example after resetting the person's password), run this,
replacing `USERNAME`:

```bash
docker compose exec -T web python manage.py shell -c "from budget.models import LoginFailure; print(LoginFailure.objects.filter(username_key='USERNAME'.casefold()).delete())"
```

To lift a block on a device address shown in the log (replace `203.0.113.10`):

```bash
docker compose exec -T web python manage.py shell -c "from budget.models import LoginFailure; print(LoginFailure.objects.filter(ip='203.0.113.10').delete())"
```

## Backups

### Backups card

The **Backups** card on Admin reads the status left by the last backup run:

- **Healthy**: the last run worked and every copy was made.
- **Problem**: the last run failed, no backup has succeeded in the last 26 hours, or the status
  could not be read. Administrators then also see a red banner on every page.
- **Last successful backup**, **Last run finished**, **Copies** (for example "2 of 2", when every
  folder in `BACKUP_COPY_DIRS` received a copy) and **Latest dump** (the file name).

### Back up now

Select **Back up now** to take a backup straight away, for example before a big clean-up. The
app leaves a request; the server picks it up within a minute and runs the same verified backup
as the nightly job. Reload the page after a minute to see the new time on the card. The request
is recorded in the activity log.

If the card says "Back up now needs a one-time setup on the server", see
[Back up now (Admin)](../deployment.md#back-up-now-admin).

### When backups run

| What | When | How |
| --- | --- | --- |
| Nightly backup | Once a night (for example 02:15) | A host timer or cron job running `scripts/backup.sh` |
| Back up now | Within a minute of the button | The host job `scripts/backup-request.sh`, if it is set up |
| Before an update | Each time you update, before `make up` | `make backup` |
| Bill due dates ahead | Once a day | `generate_bill_occurrences` |

The schedules are set up on the server; see [Scheduled jobs](../deployment.md#scheduled-jobs). Each backup is restored into a scratch database and checked before it counts as successful. The full details are in [Backup and restore](../backup-and-restore.md).

## Server tasks

These commands run on the server, from the NoNonCents folder (where `compose.yml` is), in a terminal. Each line can be copied as it is.

### Check that everything is running

```bash
docker compose ps
```

Both `nononcents-web-1` and `nononcents-db-1` should say `Up ... (healthy)`.

```bash
curl -fsS http://127.0.0.1:8000/health/
```

This prints `ok` when the app and its database are working. Through your HTTPS address, use `https://budget.example.com/health/` instead.

### Read the logs

```bash
docker compose logs --tail=200 web
```

To follow new lines as they arrive, stop with Ctrl+C: `make logs`.

### Deploy an update

```bash
git pull
make backup
make up
```

`make backup` takes a verified backup first. `make up` rebuilds the app, replaces the containers and applies any database changes. Then check with `docker compose ps`. The full routine is in [Deployment and updates](../deployment.md#updating).

### Go back to the previous version

List the versions kept on the server, newest first:

```bash
docker image ls nononcents-web
```

Start an earlier one (replace `<tag>` with a tag from that list):

```bash
APP_VERSION=<tag> docker compose up -d
```

This changes only the program, not the data. If the newer version changed the database, also restore the backup taken just before the update (see [Rollback](../deployment.md#rolling-back)).

### Back up from the server

```bash
make backup
```

It ends with `Backup complete` and the file name.

### Check the backups

```bash
cat backups/LAST_SUCCESS
ls -lt backups/*.dump | head -n 5
```

### Check the scheduled jobs

If you use the systemd timer from the deployment guide:

```bash
systemctl list-timers nononcents-backup.timer --no-pager
journalctl -u nononcents-backup.service -n 30 --no-pager
```

`NEXT` should be the next night at the scheduled time, and `LAST` the most recent run. With cron, check the `backups/backup.log` file that the job writes to.

### Practise a restore (restore drill)

This restores a backup into a throwaway database and never touches the live budget. Replace the file name with a real one from `backups/`:

```bash
make restore-drill FILE=backups/nononcents-20260101T021500Z.dump
```

It prints the number of expenses, incomes and users it restored. Remove the throwaway database afterwards:

```bash
docker compose exec db dropdb -U <user> restore_drill
```

Replace `<user>` with your `POSTGRES_USER`. A real restore is described in [Backup and restore](../backup-and-restore.md#restore-the-live-database).

## Administrator troubleshooting

### The site does not open at all

1. Check the containers: `docker compose ps`.
2. If they are not running, start them: `docker compose up -d`.
3. If `web` keeps restarting, read why: `docker compose logs --tail=100 web`. A line with `ImproperlyConfigured` names the `.env` setting that is wrong.
4. Check the reverse proxy (Caddy, nginx or whatever you use) with its own status and log commands.

### "502 Bad Gateway"

The reverse proxy is running but cannot reach the app on `127.0.0.1:8000`. The app is stopped, still starting (wait 30 seconds after an update) or crashing. Follow the steps above, then reload the page.

### Health check fails

`curl -fsS http://127.0.0.1:8000/health/` prints `database unavailable` when the app cannot reach the database. Check the database container:

```bash
docker compose ps db
docker compose logs --tail=50 db
```

If it is stopped, start it with `docker compose up -d db`. Never run `docker compose down -v`: that deletes the database.

### The red backup banner is showing

Open **Admin → Backups** and read the message, then look at the backup output:

```bash
grep -E "BACKUP FAILED|Backup complete" backups/backup.log | tail -n 10
```

Common causes:

- **A copy folder is not available.** The local backup was kept, but the copy failed. Check that each folder in `BACKUP_COPY_DIRS` exists and is mounted, then select **Back up now**.
- **The computer was off at the scheduled time.** A systemd timer with `Persistent=true` runs the missed backup at start-up; a cron job does not. If the banner stays, select **Back up now**.
- **The database container was down.** Start it (see above) and select **Back up now**.

The banner clears after the next fully successful run.

### A backup failed

Run one by hand to see the error on screen:

```bash
make backup
```

The line starting `BACKUP FAILED` says which step failed: the dump, the verification restore, the copy or the retention step.

### The disk is full

```bash
df -h
```

Old dumps are deleted automatically (dumps older than `BACKUP_RETENTION_DAYS` days, always keeping the newest `BACKUP_KEEP_MIN`). To free space from unused Docker images and build cache:

```bash
docker image prune -f
docker builder prune -f
```

Keep at least the last two `nononcents-web` images for rollback. Never delete the `nononcents_postgres_data` volume.

### Certificate expiring or expired

If your reverse proxy obtains certificates automatically (Caddy does for public names), check its log for renewal errors. Otherwise renew the certificate with the tool you used, then reload the proxy. To see the current end date:

```bash
echo | openssl s_client -connect budget.example.com:443 -servername budget.example.com 2>/dev/null | openssl x509 -noout -enddate
```

### "Forbidden (403) CSRF verification failed"

The address in the browser is not in `CSRF_TRUSTED_ORIGINS`, or the HTTPS settings do not match how the site is served. Add the address with `https://` to `CSRF_TRUSTED_ORIGINS`, and the name alone to `ALLOWED_HOSTS`, in `.env`, then run `make up`. Changing `.env` recreates the database container as well as the app; that takes under a minute.

### "Bad Request (400)"

The address used is not in `ALLOWED_HOSTS` in `.env`. Add the name and run `make up`.

### Someone is locked out

- **"Too many sign-in attempts"**: wait 15 minutes, or lift the block at once (see [Sign-in lockout](#sign-in-lockout)).
- **"This user does not have access to the selected profile"**: they chose a profile they are not a member of. Give them access with **Manage access**, or ask them to choose the right profile.
- **Forgotten password**: [reset it](#reset-a-forgotten-password).
- **The browser warns about the certificate**: the certificate is not trusted on that device. Check the proxy's certificate, or install your private certificate authority on the device.
