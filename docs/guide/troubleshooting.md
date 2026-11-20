# Troubleshooting

Find the problem that matches what you see. Each entry says what it means and what to do.
When a fix needs the server, the entry says to ask your administrator.

A message at the top of a page starts with **Problem:** when something failed, and with
**Check:** when something needs a look. The words after it say what to do.

## Budget problems

### A balance doesn't match the bank

**What it means:** an entry is missing, entered twice, or has the wrong amount or account, or
the bank has something you have not recorded yet.

**What to do:**

1. Open **History**, choose the account under **Account**, and select **Search**. The
   **Balance after** column shows the balance after each entry. Find the first day it differs
   from your bank.
2. Look for missing income or refunds, a purchase saved twice, or one saved to the wrong
   account.
3. Run the [monthly statement check](everyday-use.md#do-the-monthly-statement-check): lines
   marked "Possibly missed" are the usual cause.
4. If the difference still can't be found, reconcile the account and post a balance correction
   with a reason (see
   [Check an account against your bank statement](getting-started.md#check-an-account-against-your-bank-statement)).

Balances cannot be typed in directly; that is on purpose, so every change has a record.

### The Home numbers look wrong

**What it means:** Home uses your bills, your next pay period's paycheck and spending target,
and today's balances. A wrong figure in any of them changes the result.

**What to do:**

1. Tap **How this is worked out** on the card and check each line.
2. If the paycheck or spending target is wrong, use **Edit paycheck or spending target** on
   the Bills card.
3. If a bill amount is wrong, fix that month on **Bills** with **Edit bill**.
4. Check for "older unpaid bills need review": they are left out of the figures until you deal
   with them.
5. Read [Understanding the numbers](understanding-the-numbers.md) for what each line means.

### Remaining after bills next period is negative, but money is carrying over

**What it means:** this is normal when the line under it says the shortfall is "held back on
the Spending card". Safe to spend is already lowered by that amount.

**What to do:** nothing, as long as you stay within **Safe to spend**. See
[Why a negative number can appear when money is carrying over](understanding-the-numbers.md#why-a-negative-number-can-appear-when-money-is-carrying-over).

### The carry-over amount looks too high or too low

**What it means:** **Needs to carry over** is next period's bills plus the spending target
(or your recent average, if no target is set), minus the expected paycheck.

**What to do:** open **How this is worked out** on the carry-over card. If the expected
paycheck is missing or old, set it on **Budgets** for the next pay period. If it says
"expected spending (recent average; no target set)", set a spending target for the next period.
Check next month's bills on **Bills** for a bill counted twice or an amount that has changed.

### A bill shows twice, or is missing

**What it means:** two recurring bills were set up for the same thing, or a bill's
**How often**, **First month** or **Last month** doesn't match reality, or the bill is not
**Active**.

**What to do:**

- **Twice:** on **Bills**, select **Remove** on the duplicate bill from the first month it is
  wrong. Months that already have a payment are kept.
- **Missing:** select **Edit bill** on a month where it does show, and check **How often** and
  **First due month**. If it shows nowhere, add it again with **＋ Add bill**.
- **Still unpaid after paying:** use **Paid $…** for a full payment, or edit the payment in
  **History** and choose the bill under **Apply payment to bill (optional)** and the right
  **Bill month (optional)**.

### An entry is in the wrong pay period

**What it means:** its date is wrong, or the pay period dates are wrong.

**What to do:**

- Fix the date: **History → Edit**.
- Fix the period: **Budgets → Manage pay periods → Edit**. Changing an end date also moves the
  next period's start.
- If a paycheck arrived early or late, record it with **This is the regular paycheck** ticked.
  The pay period then starts on the day it landed.

### "This pay period is locked"

**What it means:** the entry, bill, budget or reconciliation falls in a pay period that was
locked when it was finished. Locked periods can't be changed.

**What to do:** ask your administrator to **Reopen** the period. Make the correction, then lock
it again on **Budgets → Manage pay periods**.

### Income won't save

**What it means:** the allocations to each account don't add up exactly to the amount.

**What to do:** check each **… allocation** box so they total the income amount to the cent.
If any money goes to Savings, choose the **Savings destination goal**.

### A category is refused on an expense

**What it means:** the category doesn't fit the account. Spending accounts use Spending
categories; the Bills account uses Bill categories. Utilities is the one exception and can be
paid from Spending.

**What to do:** change either the account or the category. If the category you need is not in
the list, it may be archived; restore it from **Categories → Archived**.

### An amount is refused

**What it means:** the amount uses a comma for cents, such as `12,50`, or has letters in it.

**What to do:** type it as `12.50`. Commas between thousands are fine: `1,250.00`.

### A date is refused or needs confirming

**What it means:** the date is more than 60 days ago, more than 31 days ahead, or outside every
pay period. More than a year away is always refused.

**What to do:** check the year first. If the date is right, tick **This date is correct** and
save again.

### The same transaction was entered twice

**What to do:** open **History**, search for the merchant or amount, and **Delete** the extra
one. **Undo** in the banner brings it back if you delete the wrong one. Undo works within 10
minutes, for the latest delete only. If one
of the pair is already cleared against a statement, delete the one that is not.

### I deleted something by mistake

**What to do:** select **Undo** in the banner at the top of the page. Undo works within 10 minutes, for the
latest delete only. Entries that involve Savings, and balance corrections, can't be
undone; add them again.

### A statement won't import

The page says why. The usual messages:

| Message | What to do |
| --- | --- |
| "That file is not a PDF." | Download the statement again as a PDF, not a picture or web page. |
| "The PDF is password-protected; download it again unlocked." | Download it again without a password. |
| "The file is larger than 10 MB." | Download just the one month's statement. |
| "No statement period was found. Is this a bank statement?" or "No account sections were found" | Make sure it is the monthly *statement of account* from your bank, not a receipt or tax form. |
| A message naming an account and a date, ending "Nothing was imported." | A row didn't add up to the balance the bank printed. Download the PDF again and retry; if it fails again, ask your administrator. |

### The statement went to the wrong account

**What it means:** the wrong app account was picked the first time, and the app remembered it
by the last 4 digits of the bank account number.

**What to do:**

1. Open **Statement check** from the menu (on a phone: **More → Statement check**), find the
   statement and select **Remove statement** (your
   cleared marks stay).
2. Open **Accounts**, select **Edit** beside the account that got the statement by mistake, empty
   **Statement account ending (optional)** and select **Save**. (Or type the correct 4 digits on
   the right account instead; the app refuses an ending that another account already uses, so
   clear the wrong one first.)
3. Upload the statement again. If an account has no ending, the app asks **Which account is
   which?** once more.

### A statement line has no match

**What it means:** "Possibly missed" means no entry on that account has the same amount within
7 days before or 3 days after the bank date.

**What to do:** if you forgot it, select **Add as new entry**. If you entered it with a
different amount or on another account, fix that entry in **History**, then come back. If it is
not something to record (for example, a line from before you started using the app), select
**Dismiss**.

### The debt estimate looks wrong

**What to do:** enter the newest statement balance with its statement date
(**Enter balance**), check the **APR (%)**, and check that the right bills are ticked under
**Bills that pay this debt**.

### The AI analysis shows an error or never finishes

**What to do:** read the message on the **Deep Pattern Analysis** card. A run still going after
10 minutes is reported as failed; select **Generate AI analysis** again. The figures and charts
on Outlook don't depend on it. If it keeps failing, ask your administrator.

## Sign-in and connection problems

### Which address to use

Use the address your administrator gave you. It usually starts with `https://`. An old bookmark that starts with `http://` may be sent on to the secure address, but update the bookmark so the app always opens the same way.

If the app is only available on the computer that runs it, the address is usually `http://127.0.0.1:8000` on that computer.

### The browser says the connection is not private

**What it means:** the browser does not trust the site's security certificate. This usually means the certificate comes from a private certificate authority that your device does not know yet.

**What to do:** do not select "continue anyway". Ask your administrator for the certificate authority file, and install it once on each device. The steps depend on the device, and your administrator can give you the exact steps for yours. If the warning still appears after you install it, ask your administrator to check the certificate.

### "Too many sign-in attempts"

**What it means:** after 5 wrong passwords for one username within 15 minutes, that username is
paused. (After 20 failed attempts from one device in 15 minutes, every sign-in from that device
is paused.) The message says how many minutes are left; it is never longer than 15 minutes.

**What to do:** wait the number of minutes shown, then sign in carefully. Check the right
**Profile** is chosen and that Caps Lock is off. If you have forgotten your password, ask your
administrator to reset it. Failed sign-ins are shown to administrators in the activity log.

### "Please enter a correct username and password"

**What to do:** check the username and password. Both can be case-sensitive, so check Caps
Lock too.

### "This user does not have access to the selected profile"

**What to do:** choose a different **Profile**. A username only works for the profiles your
administrator has given it. Ask your administrator if you need access to another one.

### I was signed out

**What it means:** for safety, you are signed out 8 hours after you sign in, even if you were
using the app.

**What to do:** sign in again. Anything you had saved is kept. A form you had not saved yet
needs to be filled in again.

### "Forbidden (403)" or "CSRF verification failed", or a button does nothing

**What it means:** the page was open for a long time, you were signed out in another tab, or the
page was opened at an old address.

**What to do:** go back, refresh the page, and try again. If it keeps happening, sign out, close
the tab, and open the app's address fresh. If it still happens, ask your
administrator: it can mean a server setting needs updating.

### Dark mode doesn't stay on

**What it means:** your choice is saved to your sign-in and applied as soon as you sign in, on any
device. Before you sign in (the sign-in page), the device shows the last choice it saw. If it
still changes, your choice may be 🖥 **System**, which follows the phone's or computer's own
light or dark setting.

**What to do:** sign in, then choose ☾ under **Display Mode** (on a phone: **More → Display
mode**). It then stays dark everywhere you sign in. If someone else signs in on the same device,
their own choice is used.

### The page looks broken or out of date

**What it means:** the browser is still showing an older copy of the page, often just after an
update.

**What to do:** refresh the page (pull down on a phone; **Ctrl+R** on Windows, **⌘R** on a Mac).
If it still looks wrong, close the tab and open the app again.

### Something doesn't fit on my phone

**What to do:** wide tables turn into stacked cards on a phone, one card per row; scroll down
rather than sideways. Charts can be zoomed with two fingers. Turning the phone sideways gives
more room. If text is cut off or something runs off the edge, take a screenshot and send it to
your administrator.

### I opened the wrong profile

**What to do:** select **Sign out** (at the bottom of the menu on a computer, or at the bottom of
**More** on a phone), then choose the right
**Profile** on the sign-in page.

### A page or button mentioned in this guide is missing

**What it means:** your administrator may have renamed the page, or the page is for
administrators only (**Admin**).

**What to do:** look in the same place in the menu; the position stays the same when the name
changes. Ask your administrator if you need access.
