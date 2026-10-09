# HuskySync

A local Python application for reviewing HuskyCT coursework, McGraw-Hill Connect
launches, and UConn email action items, then adding deadlines to Google Calendar.
The Streamlit dashboard and SQLite database run on your computer.

## Install and run

Use Python **3.11 or newer**. Run these commands from this repository:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
python -m streamlit run app/dashboard.py --server.address 127.0.0.1
```

On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell instead.
On Linux, if Chromium reports missing system libraries, use Playwright's supported
`python -m playwright install --with-deps chromium` command, which may require
administrator privileges. Browser automation runs in its own worker event loop
so Streamlit can use Playwright on Windows too.

The app creates `.husky_sync/husky_sync.sqlite3` automatically. Keep the dashboard
bound to your local computer: it contains private coursework and email snippets.
The `.husky_sync/` directory, OAuth files, virtual environment, and local secrets
are excluded from Git. Browser profiles and OAuth tokens grant account access;
keep them on your own computer and out of shared folders.

## Public demo (sample data, no accounts)

`demo_app.py` runs the same dashboard on the sample coursework in
`assignments.json` and the sample messages in `emails.json`. It contacts no
HuskyCT, Outlook, or Google account: those buttons are disabled, and visitors can
filter coursework, load the sample emails, turn them into tasks, and reset the
data. Sample deadlines are shifted so the file's reference date falls on today.

```bash
python -m streamlit run demo_app.py
```

To give other people a link, deploy it on
[Streamlit Community Cloud](https://share.streamlit.io/): sign in with GitHub,
choose **Create app**, select this repository and branch, and set the main file
path to `demo_app.py`. Everyone who opens the link shares one sample database,
which is recreated whenever the app restarts.

Deploy only `demo_app.py`. Publishing `app/dashboard.py`, or exposing a local
copy that is connected to real accounts, would show your coursework to anyone
with the link and let them write to your Google account. Real use stays on each
person's own computer, as described below.

## Connect HuskyCT and McGraw-Hill

With the virtual environment activated:

```bash
python -m app.scraper login
```

1. Complete UConn NetID SSO and Duo yourself in the Chromium window.
2. Wait until the HuskyCT Ultra activity stream is visible.
3. Open relevant McGraw-Hill Connect course launches from HuskyCT and complete any
   separate publisher sign-in. This establishes the publisher session in the same
   browser profile.
4. Press Enter in the terminal to verify and save the session, then close the
   login workflow before scraping.

Use **Sync HuskyCT** in the dashboard or:

```bash
python -m app.scraper sync
# Show the browser while diagnosing page-layout issues:
python -m app.scraper sync --headed
# Optionally inspect an actual authenticated publisher assignment-list page:
python -m app.scraper sync --connect-url 'https://connect.mheducation.com/your-course'
```

Replace the example publisher URL with the assignment-list URL from your course.
You can repeat `--connect-url`, or set `HUSKYSYNC_CONNECT_URLS` to a comma-separated
list. Only HTTPS URLs under `mheducation.com`, `mcgraw-hill.com`, or `mhhe.com` are
accepted. Do not supply one-use LTI launch tokens.

Scraping visits `/ultra/stream`, `/ultra/grades`, and `/ultra/calendar`. An OS lock
prevents two HuskySync processes from opening the profile at once. Saved sessions
can expire; rerun `login` if the app requests authentication. Duo is never bypassed
or automated.

**Live integration limits:** Blackboard Ultra and Connect have institution-specific,
changing DOMs, and their lists may be paginated or virtualized. The included parser
is fixture-tested; it has not been verified against a logged-in UConn account.
Review the import against your actual courses. Rows without a parseable date and
clock time are skipped and reported, rather than given an invented LMS deadline.
External publisher cards are flagged as `MCGRAW_CONNECT`; a launch card alone
cannot establish publisher-side completion or an internal due date. Optional
publisher-page scraping uses the same DOM parser and may require selector updates.

If your page uses different containers, set `HUSKYSYNC_ASSIGNMENT_SELECTORS` to
comma-separated CSS selectors, and adjust the scoped title/course/due/status
selectors in `app/scraper.py` to match your school's live markup. The browser reads
three scroll snapshots by default; `HUSKYSYNC_SCROLL_PASSES` accepts 1–10. A zero-item
import warns that empty coursework and unsupported markup cannot be distinguished.
Source IDs are preferred for deduplication; where unavailable, course/title identity
is used, so a renamed assignment may need manual reconciliation.

## Connect UConn Outlook

The app uses TLS IMAP at `outlook.office365.com:993`, opens INBOX read-only, and
fetches `BODY.PEEK[]`, leaving messages unread. By default it reads the newest 100
unread messages from the past 14 days. Sender domains must exactly match `uconn.edu`
or the configured Blackboard notification-domain allowlist in `app/email_parser.py`.
This filtering is a relevance check, not sender-authentication verification.

Microsoft 365 normally disables IMAP password authentication. Obtain a delegated
Microsoft OAuth2 access token through a UConn-approved Microsoft Entra application
with the scope:

```text
https://outlook.office.com/IMAP.AccessAsUser.All
```

The app registration and consent must permit access to your mailbox, and IMAP must
be enabled for it. Ask UConn IT if either is unavailable. Microsoft's
[IMAP OAuth documentation](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth)
describes app registration, delegated token acquisition, and XOAUTH2. Request
`offline_access` in the external authorization flow if that client needs to renew
tokens. HuskySync accepts an access token; it does **not** acquire or refresh
Microsoft tokens itself.

Set the mailbox address and token in the environment **before** starting Streamlit.
For Bash, read the token without echoing it or recording it in shell history:

```bash
export HUSKYSYNC_IMAP_USERNAME='your.netid@uconn.edu'
read -rsp 'Outlook IMAP access token: ' HUSKYSYNC_IMAP_ACCESS_TOKEN
export HUSKYSYNC_IMAP_ACCESS_TOKEN
python -m streamlit run app/dashboard.py --server.address 127.0.0.1
```

In PowerShell, set these environment variables through your local secure credential
workflow. Renew the access token and restart Streamlit when it expires. An optional
`HUSKYSYNC_IMAP_PASSWORD` is used only if explicitly supplied and your tenant allows
basic IMAP authentication; a normal UConn password usually will not work here.

Click **Scan Emails**. Deadlines such as “due by Friday” and “deadline moved to
October 20 at 5 PM” become reviewable action items. Relative dates use message
receipt time. A deadline with a date but no time defaults to **11:59 PM in
America/New_York**, and is labeled as suggested. Event times are retained when
present. Parsing is heuristic and retains one action per message; it does not
understand every sentence or reconcile multiple deadline changes automatically.
Verify dates before clicking **Add to Tasks**. Undated announcements remain
reviewable and dismissible, but cannot be converted to a timed assignment.
Converted email tasks can be marked completed or reopened in the assignment
tracker's individual controls.

## Connect Google Calendar

1. Create a project in [Google Cloud Console](https://console.cloud.google.com/).
2. Enable the **Google Calendar API**.
3. Configure the OAuth consent screen. If the app is in testing mode, add your
   Google account as a test user.
4. Create an OAuth client with application type **Desktop app**. Download its JSON
   to `.husky_sync/credentials.json` (create the directory if needed).
5. Run this command on the same local computer as your browser:

```bash
python -m app.calendar_sync auth
```

Approve Calendar access. The loopback callback caches `.husky_sync/token.json`;
the app refreshes this Google token when possible. Testing-mode consent can make
refresh tokens expire, requiring authorization again. The requested
`calendar.events` scope permits event management; HuskySync writes its own events
to the configured calendar.

Use **Add to Google Calendar** beside an urgent assignment, **Sync all urgent**, or
the assignment tracker's individual calendar controls. Events have:

- Title: `[CSE 3100] Assignment title`
- Start: one hour before the deadline
- End: the exact deadline
- Description: course name, platform, and direct assignment URL

The returned event ID is saved to SQLite. Resyncing an assignment updates that
event; deterministic event IDs recover a successful insert after a local save
failure. Deleted events can be recreated. **Sync all urgent** adds only unsynced,
unsubmitted assignments due from now until strictly less than 48 hours away.
It does not update previously synced deadlines automatically: use **Update Google
Calendar** after a deadline moves. Events are not automatically removed when an
assignment is submitted.

### Google Tasks (optional, per assignment)

Enable the **Google Tasks API** in the same Google Cloud project, then run
`python -m app.calendar_sync auth` again so the saved token includes Tasks access
(a token authorized before this feature must be renewed once). In the dashboard's
**Google Tasks** section, tick the assignments you want and press **Sync selected
to Google Tasks**; nothing is sent for unticked rows. The **Preselect** control
starts from unsubmitted assignments, all, or none.

Tasks are titled `[CSE 3100] Assignment title` and appear in Google Calendar's
Tasks view. Google Tasks stores a due date without a time, so the exact deadline
is written in the task notes. Submitted assignments are added as completed tasks.
Syncing again updates the same task; unticking a row later does not delete a task
that was already created. `HUSKYSYNC_GOOGLE_TASKLIST_ID` selects a list other than
the default one.

Set `HUSKYSYNC_GOOGLE_CALENDAR_ID` to use a separate writable calendar instead of
`primary`. Keep this target stable after syncing; stored event IDs refer to their
original calendar.

## Configuration

The app reads process environment variables at startup. `.env.example` is a
reference; `.env` files are not loaded automatically. Defaults use the repository's
`.husky_sync/` directory.

| Variable | Default / purpose |
| --- | --- |
| `HUSKYSYNC_DATA_DIR` | `.husky_sync/`; local state root |
| `HUSKYSYNC_DATABASE_PATH` | `<data_dir>/husky_sync.sqlite3` |
| `HUSKYSYNC_TIMEZONE` | `America/New_York`; display and date interpretation |
| `HUSKYSYNC_BLACKBOARD_BASE_URL` | `https://lms.uconn.edu` (where `huskyct.uconn.edu` redirects) |
| `HUSKYSYNC_BROWSER_PROFILE_DIR` | `<data_dir>/browser_profile` |
| `HUSKYSYNC_BROWSER_LOCK_PATH` | `<data_dir>/browser.lock` |
| `HUSKYSYNC_BROWSER_EXECUTABLE_PATH` | Optional existing Chromium executable; otherwise Playwright's bundled browser |
| `HUSKYSYNC_BROWSER_CHANNEL` | Optional installed Chromium channel, e.g. `chrome` |
| `HUSKYSYNC_ASSIGNMENT_SELECTORS` | Optional comma-separated assignment container selectors |
| `HUSKYSYNC_SCROLL_PASSES` | `3`; bounded at 1–10 |
| `HUSKYSYNC_CONNECT_URLS` | Optional comma-separated authenticated publisher URLs |
| `HUSKYSYNC_GOOGLE_CREDENTIALS_PATH` | `<data_dir>/credentials.json` |
| `HUSKYSYNC_GOOGLE_TOKEN_PATH` | `<data_dir>/token.json` |
| `HUSKYSYNC_GOOGLE_CALENDAR_ID` | `primary` |
| `HUSKYSYNC_IMAP_HOST` | `outlook.office365.com` |
| `HUSKYSYNC_IMAP_USERNAME` | Your mailbox address |
| `HUSKYSYNC_IMAP_ACCESS_TOKEN` | Delegated Microsoft IMAP access token |
| `HUSKYSYNC_IMAP_PASSWORD` | Optional tenant-supported basic-auth fallback |

Required network access includes HuskyCT and its SSO/Duo redirects, your publisher,
Outlook IMAP on TCP 993, and Google's OAuth and Calendar endpoints. Downloading
Playwright browsers also requires the official Playwright CDN and its redirect
destinations. Corporate or cloud egress policies may need explicit allowances.

## Modules and persistence

```text
app/
  config.py          Environment settings and local directories
  models.py          SQLAlchemy enums and UTC-aware models
  database.py        SQLite engine, WAL, sessions, schema initialization
  services.py        Pydantic validation, upserts, email conversion/dismissal
  scraper.py         Persistent Playwright session and DOM parsers
  email_parser.py    Outlook IMAP and email date/action extraction
  calendar_sync.py   Google OAuth and repeatable calendar operations
  dashboard.py       Streamlit dashboard
tests/              Offline integration, parser, database, and UI tests
```

`Assignment.is_urgent` is computed on access rather than stored as a stale Boolean.
The dashboard similarly computes overdue state from the current time. All database
timestamps round-trip as UTC-aware Python datetimes and display in the configured
timezone, including daylight-saving changes. Unique source keys prevent repeated
assignment/email imports, and email conversion retains its assignment ID.
Schema initialization uses `create_all`; future schema changes will need migrations
or a separately backed-up new database. Back up the SQLite database when the app is
stopped, or use SQLite's backup API for a live database.

## Development checks

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Tests exercise real SQLite transactions, fixture DOM/email parsing, mocked IMAP and
Calendar calls, and Streamlit's interactive test runner. They do not require real
accounts or send messages. Live UConn, Connect, Outlook, and Google authorization
must be verified on your own computer with your credentials.
