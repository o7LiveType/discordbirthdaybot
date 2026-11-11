# Birthday bot

Authored by [o7LiveType](https://github.com/o7LiveType) with [Codex](https://openai.com/codex/).

A Python 3.12 Discord bot with a public birthday calendar, configurable moderator-only browsing, and optional birthday celebrations. Uses Discord application commands, simple month/day/timezone menus, timezone autocomplete, modern labelled modal forms, channel selectors, confirmation buttons, and ephemeral replies.

## Quick start

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.txt
cp .env.example .env
chmod 600 .env
# Edit .env locally: set DISCORD_TOKEN. Never paste it into a command or commit it.
.venv/bin/python -u main.py
```

Create an application in the [Discord Developer Portal](https://discord.com/developers/applications), add its bot, and copy the bot token into `.env`. Leave **all privileged intents disabled**. Use a Guild Install with `bot` and `applications.commands` scopes. Request only **View Channel, Send Messages, Embed Links, Read Message History** (permission integer `84992`). Administrator permission is unnecessary. Channel overrides must permit those four permissions in the birthday channel and optional alert channel. The birthday channel must also be visible to the members you intend to celebrate there.

Invite URL (replace `APPLICATION_ID`):

```text
https://discord.com/oauth2/authorize?client_id=APPLICATION_ID&scope=bot%20applications.commands&permissions=84992&integration_type=0
```

Set `TEST_GUILD_ID` to your test server ID for immediate guild command registration. Leave it blank for global registration when deploying; Discord may take time to refresh global commands. During the switch, remove old test-server commands to avoid duplicate cached entries (see below). This bot uses a Gateway connection; no public HTTP endpoint, inbound port, interactions URL, client secret, or public key is required.

Run `/birthday-admin setup` and use its **Choose a setting** dropdown. **Announcement time** and **Birthday GIF link** open separate forms; accepted GIF links are shown as **Open GIF**. Choose the birthday channel and optional private moderator alert channel, choose the announcement time and leap-day behavior, and save. Then use `/birthday-admin preview` and `/birthday-admin status`. Members use `/birthday set`, choose month/day/timezone, and press **Save**. Use `opt_out: True` or turn announcements off in the menu to save a private record without public posts. Nothing is saved until Save is pressed.

## Commands

| Command | Who | Result |
| --- | --- | --- |
| `/birthdaybot` | Member | Main menu: calendar, monthly summary, and birthday management |
| `/birthday list` | Member by default | Public month/day calendar in chronological order; month filter and pagination |
| `/birthday summary` | Member by default | Server configured total, shared total, and twelve monthly counts |
| `/birthday set [opt_out] [timezone]` | Member | Simple month/day/timezone menu, public-list/announcement opt-out, Save and Cancel |
| `/birthday update [opt_out] [timezone]` | Member | Edit the same details; preserves existing opt-out if omitted |
| `/birthday me` | Member | Only the caller's record and upcoming calendar occurrence |
| `/birthday delete` | Member | Confirmation, then erase caller's record and delivery metadata in this server |
| `/birthday help` | Member | Privacy and scheduling explanation |
| `/birthday-admin setup` | Manage Server | Channel selectors, public/moderator-only list access, announcement time, leap-day rule and GIF form |
| `/birthday-admin list` | Manage Server | Private paginated birthday records for this server |
| `/birthday-admin forget member` | Manage Server | Confirm deletion of a member's record |
| `/birthday-admin pause paused` | Manage Server | Pause/resume this server's announcements |
| `/birthday-admin preview` | Manage Server | Private embed preview and permission checks |
| `/birthday-admin status` | Manage Server | Scheduler health, channel checks, SQLite mode, delivery counts |
| `/birthday-admin deliveries` | Manage Server | Up to 15 unresolved deliveries, with review markers |
| `/birthday-admin resolve member year action checked_channel [message_id]` | Manage Server | Mark delivered, retry, or skip after manual channel inspection |

Setup, personal records, moderation, and the `/birthdaybot` launcher are ephemeral. Public calendars and monthly summaries appear in a shared channel post; the requester gets a private link. When browsing is restricted to moderators, calendars and summaries are also ephemeral. The admin group defaults to Manage Server (`manage_guild`) and every admin command enforces that permission at runtime. Menus and modals recheck ownership and moderator permissions on every interaction, including after permission revocation. The public calendar is navigable by any server member; private menus check ownership. Server administrators can use moderator commands. Grant Manage Server only to trusted moderators.

## Privacy and security

Birthdays belong to one server. **Public birthday browsing is ON by default**, including existing servers after migration. `/birthday-admin setup` → **Birthday list access** → **Private — moderators only** restricts browsing to Manage Server moderators. Public calendars show member mentions and month/day only, ordered January through December; timezones, announcement preferences, and other record details stay private. Opted-out records are excluded from public pages and monthly counts, even when a moderator opens the public calendar. The overall configured count includes all saved records; the shared count and monthly summary count only publicly shared records. Moderators can privately read every record using `/birthday-admin list`, including opted-out dates. The server operator can access SQLite and backups. Ephemeral replies are delivered through Discord and are not end-to-end encrypted.

Only month/day is collected, never birth year or age. The private menu explains that saving enables birthday announcements unless the user opts out. Both set/update offer an optional `opt_out` flag and the menu has a sharing toggle. Opting out hides the date from public directories and birthday announcements; moderators can still view the record. Updating preserves the existing opt-out when the flag is omitted. Enabling announcements reveals that it is the user's birthday on the announcement day and mentions that user once. Posts omit their stored date, timezone, and age. No mass mentions, role pings, or unsolicited DMs. Timezone and birthday values are never included in normal audit logs. Audit events contain Discord IDs, actions, delivery markers, and outcomes; restrict access to logs and backups.

`.env` is the **first line of `.gitignore`**. Runtime databases, WAL/SHM sidecars, data directories, logs, and private keys are ignored. Config tokens are excluded from repr and redacted from application log output. SQL parameters are bound. GIF links reject credentials, unsafe schemes/hosts/ports, whitespace, control characters, malformed percent escapes, query strings and fragments. Accepted URLs are canonicalized and Markdown-sensitive path characters are percent-encoded before they are displayed as clickable links. GIF URLs accept only HTTPS `.gif` files on an explicit set of KLIPY/GIPHY media hosts; the bot never fetches user-supplied URLs on the server. Discord fetches media for the embed. Tokens must stay in `.env` or Pterodactyl secret environment variables, never an egg export, URL, startup command, issue, or commit. Panel environment variables override `.env`.

Commands have a bounded per-user rate limit (10 per 30 seconds). Interactive menus also throttle rapid clicks and expire after 10 minutes. Database writes and sends coordinate with opt-outs and deletions. Records for departed members are removed when a due delivery checks membership; the bot does not subscribe to privileged member events. Removing the bot from a server erases that server's data when the removal event is received. If it was removed while offline, the operator should delete the stale guild data manually. User deletion removes the live birthday and completed delivery rows. Unresolved send markers and pending cleanup metadata are retained to find and expire any public post, even after an interrupted send. Deletion does not remove operator backups or log entries containing IDs; existing posts follow their normal expiry. A minimal annual anti-spam receipt (server ID, user ID, local year; no birthday or timezone) remains so deletion and re-registration cannot trigger repeated celebrations. Removing the server deletes those receipts too.

Public directories reuse **one message per server text channel** instead of posting a new list for every request. They support month filters, next/previous pages, refresh, and a twelve-month summary. Public posts expire after 24 hours using the same durable cleanup queue as announcements. Updating/deleting a birthday or changing list visibility queues existing public directory posts for removal on the next scheduler pass (normally within 30 seconds, plus API/backlog delay). Outages or missing permissions can delay removal. Existing messages cannot be retroactively hidden from screenshots or quotations. Old controls stop showing data after invalidation; `/birthdaybot` opens a fresh view. Private moderator menus recheck permissions on every click.

## Spam control and scale

Application commands and normal menu actions enter a bounded FIFO queue with **64 waiting jobs and two workers**. Each user can have only one pending action, and each server can occupy at most eight slots, so a busy server cannot fill the queue alone. Queue overload gets a private busy reply. Waiting jobs expire after 20 seconds; processing times out after 15 seconds. If a save times out, check `/birthday me` before retrying because its database commit may already have completed. Moderator authorization and menu ownership are checked before accepting work.

Commands allow 10 per user per 30 seconds; a shared limiter across menus allows 12 actions per user per 10 seconds. Rapid repeated clicks are also throttled. Modal opening and submission need Discord's initial interaction response, so they are handled directly with the same ownership checks; modal submissions only edit the in-memory form, not SQLite. One active private menu per server/user replaces the previous menu. Private menus and shared directory views each have a global limit of 1,000 retained views. Repeated identical directory requests within three seconds reuse the existing link without another edit. Rate-limit maps are bounded to 10,000 users. Busy-queue logs are sampled to avoid log flooding. Discord.py also handles Discord's route and global HTTP rate limits.

Birthday announcements use a separate durable delivery ledger, with batches capped at 25 per scheduler pass and faster follow-up passes when a backlog exists. An index on active month/day retrieves only the civil dates currently possible worldwide. Completed annual receipts are excluded in SQL. The scheduler rechecks consent and membership before sending. Moderator lists fetch ten rows per page through an index instead of loading a server's entire list. Automatic Gateway sharding supports larger installations; Discord application verification and privileged-intent limits still apply. Capacity depends on the host and Discord API limits: unit stress tests establish queue bounds, not a measured maximum production server count.

## Time and delivery rules

New servers use `America/New_York` as the birthday-form default; each person can choose their own IANA timezone. Announcements default to **00:00 (midnight) in that member's timezone**, rather than midnight UTC or the panel host's timezone. Moderators can change this in `/birthday-admin setup` → **Announcement time**, or select **Midnight (00:00)**, then **Save settings**. The time uses 24-hour `HH:MM`; changes apply to all birthday records in that server. Moderators also control the February 29 rule; members only set month, day and timezone. Upgrading from schema 4 or earlier changes the old 09:00 default to midnight and updates existing records in those servers. Other configured times are preserved; any time, including 09:00, remains selectable after upgrading. Fixed offsets and ambiguous abbreviations should be avoided. Daylight saving changes follow installed IANA rules; keep the host timezone database and pinned `tzdata` current. Linux uses its system timezone data first, with the Python `tzdata` package as fallback. The host's clock must be synchronized; its own local timezone does not affect scheduling.

- The scheduler checks every **30 seconds** while connected. Delivery normally follows the target by up to a poll interval plus API/queue latency; Discord rate limits and outages can increase delay. It is not a hard realtime guarantee.
- Spring-forward gaps use the first real minute after the requested wall time. Fall-back repeats use the first occurrence. A skipped entire calendar date is not moved to a different birthday.
- February 29 birthdays can use February 28, March 1, or leap years only. Dates such as April 31 are unavailable in the menus and rejected by calendar validation.
- A restart or same-day interruption catches up after the target time, while it is still that person's local birthday. Earlier local days are not announced late. Enabling consent or resuming announcements after the target may trigger same-day catch-up.
- A unique `(server, user, local year)` ledger prevents repeat announcements on repeated polls, restarts, birthday edits, and ordinary timezone edits. The record's local calendar year is used, including around UTC year boundaries.
- Send intent is committed before the Discord request. Discord receives a stable nonce with `enforce_nonce` through discord.py 2.7.1, providing additional recent-request deduplication. A 403/404 schedules a durable retry with backoff up to one hour. Ambiguous network/server failures become **uncertain** instead of blindly resending. The bot checks up to 500 channel messages for its unique footer marker; found messages are marked delivered. Unresolved cases are checked at most hourly and require a moderator's decision. This favors avoiding duplicates; no claim of perfect exactly-once external delivery is made.
- Use `/birthday-admin deliveries` to find the user, year, marker and error. Inspect the **original** delivery channel. Then resolve with `checked_channel=True`: `mark_delivered` with its `message_id` if present (the bot verifies author and delivery marker), `retry` only if absent, or `skip`. Retry still requires consent, enabled announcements, and that user's local birthday.

Birthday announcements are automatically deleted **24 hours after Discord created the message**. The durable cleanup queue survives restarts and user-record deletion, checks every scheduler pass, and retries failures with backoff. Already-deleted messages count as success. Outages, missing channel access, rate limits or a stopped bot can delay removal; overdue posts are removed after recovery. No Manage Messages permission is needed to delete the bot's own posts. Deleting a birthday record does not immediately remove an existing post; its normal 24-hour expiry still applies.

Operational warnings appear in structured JSON logs and repeat at most hourly in the configured moderator alert channel. They cover missing setup, permission failures, rejected sends, and uncertain deliveries. No private dates are put into those alerts. Heartbeats log every five minutes. Use `/birthday-admin status` to spot a stalled scheduler. Failed ticks log tracebacks and try again next tick. Normal command audit logs include the command actor; detailed HTTP, Gateway and SQLite debug logging stays disabled even with `LOG_LEVEL=DEBUG`.

## GIFs

The default is a direct animated [KLIPY birthday cake GIF](https://klipy.com/gifs/happy-birthday-837). KLIPY [lists Discord among its integrations](https://klipy.com/apps). No GIF API key is required. Use `/birthday-admin setup` → **Birthday GIF link** to choose another direct media URL, or set `BIRTHDAY_GIF_URL`. Supported hosts: `static.klipy.com`, `static2.klipy.com`, `media.klipy.com`, `i.klipy.com`, `media.giphy.com`, `i.giphy.com`. Copy the image's `.gif` address, not the provider's HTML share page. Preview it after changing. Third-party media availability and client GIF autoplay preferences affect playback; replacing a removed GIF is an operator task.

## Pterodactyl Python egg

Use a generic Python egg with **Python 3.12**, SQLite support, outbound HTTPS and Discord Gateway WebSocket access. Configure its Git repository URL to this public repository, the branch to `main`, its requirements filename to `requirements.txt`, and its Python/startup file to `main.py`. Exact field labels vary by egg. Dependency install must run `python -m pip install --require-hashes -r requirements.txt`; eggs that use `pip install -r requirements.txt` also enter hash-checking mode because this file contains hashes. If overriding the complete startup command, use `python -u main.py` or `sh start.sh` after dependencies are installed.

Create `.env` through the panel file manager or SFTP using `.env.example` as a template. Alternatively set `DISCORD_TOKEN` as a hidden panel variable. Do not put secrets in a public egg template. Leave `TEST_GUILD_ID` empty for production. No network port needs to be exposed.

Keep `.env`, `data/`, and `logs/` persistent under `/home/container` across updates. Repository sync must update tracked source files only; **do not use `git clean -fdx` or wipe/reinstall the server volume**. Use fast-forward Git pulls and stop/restart the bot around updates. A single bot process must own the database: local process locking prevents a duplicate process using the same lock file, but does not coordinate replicas with separate volumes. Stop the test instance before starting production with the same bot token. Give shutdown time to cancel the scheduler and close/checkpoint SQLite; SIGTERM is handled. Health is visible as `bot_ready` and `scheduler_heartbeat` in the panel console.

Runtime defaults: `DATABASE_PATH=data/birthdays.sqlite3`, `LOG_DIR=logs`, `LOG_LEVEL=INFO`. Log files rotate at 5 MB with five backups, plus panel stdout logs. Configure the panel's own log retention. Start with a modest memory allocation (for example 256 MiB) and observe actual usage before sizing larger servers.

## SQLite durability and backup

Every connection used by the bot explicitly enables and verifies **`journal_mode=WAL` and `synchronous=FULL`**, with foreign keys, a five-second busy timeout, serialized `BEGIN IMMEDIATE` transactions, rollback on cancellation, schema version checks, and graceful WAL checkpointing. `FULL` causes a WAL sync per transaction commit; see [SQLite's documentation](https://sqlite.org/pragma.html#pragma_synchronous). Use a local persistent filesystem with reliable fsync semantics. Durability depends on the panel host, filesystem, and storage honoring flushes; SQLite cannot compensate for a lost volume or dishonest storage. Network shares are unsuitable for this WAL database.

Make consistent live backups with SQLite's backup API:

```sh
python scripts/backup.py data/backups/birthdays-2026-11-10.sqlite3
```

The helper refuses to overwrite files, creates private permissions, and checks backup integrity. Copy backups to protected storage outside the server volume and set a retention policy. Do not copy only a live `.sqlite3` file while its WAL exists. To restore, stop the bot, preserve the current database and sidecars, put a verified snapshot at `DATABASE_PATH`, remove stale sidecars belonging to the replaced database after preserving them, and restart. Never replace the database while the process is running.

## Development and verification

```sh
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m pytest -q
python scripts/check_secrets.py
```

Tests cover public/private directory access, chronological pagination, monthly counts, opt-out filtering, old-message invalidation, schema migration, launcher navigation, queue saturation and per-server fairness, permission enforcement, private menu ownership, revoked moderator permissions, server isolation, opt-out preservation, menu limits, URL validation, log redaction, WAL/FULL, transaction cancellation, concurrent delivery claims, restart deduplication, membership removal, ambiguous-send recovery, 24-hour deletion and retry across restarts, midnight crossing, DST gaps/folds, leap years including 2100, UTC year boundaries, and quarter-hour offsets. CI runs these checks without a Discord token. Live Discord testing is separate: confirm private setup from a regular member and a moderator, forbidden moderator access from the regular member, GIF rendering, one real scheduled post with announcements enabled, and no repeated post after restart. API-only checks do not prove client UI behavior.

This lock set targets Python 3.12. Dependencies and transitive dependencies are version-pinned and hash-locked. Regenerate on Python 3.12 with `pip-tools==7.6.2` after reviewing updates:

```sh
pip-compile --generate-hashes --strip-extras --output-file=requirements.txt requirements.in
pip-compile --generate-hashes --strip-extras --output-file=requirements-dev.txt requirements-dev.in
```

For a test-to-global migration, stop the bot, blank `TEST_GUILD_ID`, then run once (replace the non-secret server ID). This removes this application's guild commands in that server; restarting registers global commands:

```python
import asyncio
import discord
from birthdaybot.config import Config


async def clear_test_commands():
    config = Config.load()
    async with discord.Client(intents=discord.Intents.none()) as client:
        await client.login(config.token)
        tree = discord.app_commands.CommandTree(client)
        await tree.sync(guild=discord.Object(id=YOUR_TEST_SERVER_ID))


asyncio.run(clear_test_commands())
```

The repository uses the MIT license. GIFs remain subject to their providers' and creators' terms.
