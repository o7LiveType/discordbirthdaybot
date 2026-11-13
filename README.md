# Birthday bot

Authored by [o7LiveType](https://github.com/o7LiveType) with [Codex](https://openai.com/codex/).

A Python 3.12 Discord bot with a public birthday calendar, configurable moderator-only browsing, and optional birthday celebrations. Uses one slash launcher, month/day/timezone menus, validated timezone search, modern labelled modal forms, channel/member selectors, confirmation buttons, and ephemeral replies.

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

Run `/birthdaybot` → **Server tools** → **Server settings**. Choose a birthday channel, optional moderator alert channel, announcement time, list access and leap-day behavior, then **Save settings**. Time uses hour/minute selectors; **Birthday GIF link** opens a validated URL form and shows an **Open GIF** link. Use **Server tools** → **Preview** and **Status** to check setup. Members choose **Set birthday**, select month/day/timezone, open **Sharing** to choose permissions, then **Save**. Draft changes take effect only after saving.

**Everyone ping** is off by default. To enable it, allow the bot **Mention @everyone, @here, and All Roles** in the birthday channel, then select **Server settings** → **Everyone ping** → **On** → **Save settings**. Saving checks this permission; Administrator is unnecessary. Only scheduled server birthday posts can ping `@everyone`; previews, menus, alerts, directories and DMs cannot. Role pings stay blocked. If the permission is later removed, greetings still post without `@everyone` and a throttled moderator alert explains the problem. Each greeting invites everyone to wish the member a happy birthday. Member **Server posts** must still be on; **Post mentions** independently controls the birthday member's direct mention.

## Menu actions

| Menu action | Permission | Behavior |
|---|---|---|
| `/birthdaybot` | Member | The only slash command; opens the private menu, with no command options |
| View birthdays / Monthly summary | Member by default | Chronological calendar, month filter, pages, configured/shared totals and twelve monthly counts |
| Set birthday / Update birthday | Member | Month, day and timezone; Sharing submenu; Save and Cancel |
| Sharing | Member | Independent public-list, server-post, post-mention and birthday-DM permissions; Save and Cancel |
| My birthday / Delete birthday | Member | View your own record or delete it after confirmation |
| Server tools → Server settings | Manage Server | Channels, public/moderator-only lists, everyone ping, hour/minute selectors, leap-day rule and validated GIF form |
| Server tools → All birthday records | Manage Server | Private paginated records, including opted-out dates |
| Server tools → Delete member birthday | Manage Server | Native member selector, then deletion confirmation |
| Server tools → Pause / resume | Manage Server | Buttons to pause or resume announcements and birthday DMs |
| Server tools → Preview / Status | Manage Server | Private embed preview, permissions, scheduler health, SQLite durability and delivery counts |
| Server tools → Import birthdays | Manage Server | Upload CSV, verify members and data, preview and explicitly confirm; existing records are skipped |
| Server tools → Channel deliveries / DM deliveries | Manage Server | Paginated delivery selector, bounded message search, verified delivery recovery, retry/skip confirmation |

All actions use buttons and selectors after opening `/birthdaybot`. Timezone text search is optional: **Browse all timezones** works entirely through selectors and buttons. Only **Search timezones** and **Birthday GIF link** accept text. Private screens include return navigation. Setup, personal records and moderation are ephemeral. Public calendars and summaries appear in a shared channel post; the requester gets a private link. With moderator-only browsing configured, calendars and summaries are ephemeral too. Every server tool enforces Manage Server (`manage_guild`) at runtime; menus and modals recheck ownership and moderator access, including permission revocation. Public calendar controls are available to any server member. Grant Manage Server only to trusted moderators.

Private manager screens use pink embed cards and a gift thumbnail. Birthday cards show the member's avatar/name, **Date**, **Time Zone** with its current UTC offset, **Countdown**, and sharing status. Drafts and save confirmations use the same layout. The countdown follows the configured announcement time, IANA timezone and leap-day rule; birthdays already due today show **Today**. Discord renders the relative countdown and full timestamp in the viewer's timezone, while the Date field always shows the stored month/day. Settings, timezone search, import previews and moderator tools also stay inside embed cards with their existing buttons and menus.

## Import birthdays

Open `/birthdaybot` → **Server tools** → **Import birthdays**. Press **CSV template** to download the header, fill it in, and use **Upload CSV** to select the file in Discord's native upload form. No extra slash command or public channel upload is needed. Supply a comma-separated UTF-8 CSV (a UTF-8 BOM is accepted), with at most **500 records / 256 KiB**.

Required columns: `user_id`, `month`, `day`. `member_id`, `discord_id` or `id` can replace `user_id`; IDs must be exact digits or Discord user mentions. Month accepts 1–12, an English abbreviation such as `Jan`, or a full name such as `January`. Optional columns are `timezone` (an IANA name, such as `Europe/Bucharest`), `opt_out`, and the four independent sharing flags below. Supported booleans: true/false, yes/no, on/off, 1/0. Alternatively use `consent`, `sharing` or `show_birthday` with the opposite meaning to `opt_out`; do not supply both. Duplicate headers, including aliases for the same flag, are rejected. Birth years, ages, names and other extra columns are discarded and never stored.

| CSV flag | Import sharing control | Alias |
| --- | --- | --- |
| `list_public` | Public list | `public_list` |
| `announce` | Server posts | `server_posts` |
| `dm_enabled` | Birthday DMs | `birthday_dms` |
| `mention_enabled` | Post mentions | `post_mentions` |

```csv
user_id,month,day,timezone,opt_out,list_public,announce,dm_enabled,mention_enabled
123456789012345678,1,25,Europe/Bucharest,false,true,true,false,true
```

This row allows public listing and server posts with a direct mention, while birthday DMs are off.

The importer validates calendar dates (including February 29), IDs, timezone names, sharing values and duplicate rows. CSV errors must be fixed before confirmation. It checks current server membership through Discord without a privileged members intent, skips departed members and existing saved/pending birthdays, and reports API failures as unverified. **Retry verification** retries incomplete checks; no import is allowed until verification finishes. Verification runs in the background, using at most two simultaneous member lookups across the bot, so it does not occupy an interaction worker. There is one import per server and at most three open imports across the process. **Refresh preview** shows progress and paginates rows/errors. Closing/replacing the menu, cancellation, expiry or shutdown cancels verification and clears the draft. Upload a corrected file to retry any parse error.

Press **Review import**, inspect the counts, then **Confirm import**. Nothing is written before that confirmation. The final commit uses one WAL/FULL transaction, rechecks for records added since the preview, and never overwrites existing birthdays or annual delivery receipts. A repeated import skips records already imported. The server's current announcement time and leap-day rule apply. Audit logs record the moderator, server, counts and outcomes; file names, raw CSV and birthday values are not logged.

Open **Sharing** in the import menu to set each flag to **Off**, **On**, or **From CSV**, then **Apply**. These settings can be chosen before or after upload and remain a draft until **Confirm import**. **Back** discards sharing-menu edits; **All off** prepares a private import. New import sessions default to **From CSV** for all four flags, replacing the previous CSV sharing switch, so supplied preferences are honored without a second opt-in switch. Missing or blank sharing values remain off. Explicit per-flag CSV values take precedence over the legacy all-on choice from `opt_out=false` or `consent=true`. **On** supplies an opt-in when a value is missing, but an explicit CSV false still stays off. **Off** always disables that flag. **`opt_out=true` (or `consent=false`) always disables all four**, even if other columns or menu defaults say on. Post mentions do not enable server posts by themselves.

The preview shows each row's resulting flags; final review shows separate enabled counts for each permission, separating complete records from pending timezones. It also shows the server's Everyone ping setting and any server-wide pause or moderator-only list policy. Existing saved and pending records are **skipped**, including their sharing settings: changing `opt_out` in a repeated import does not update them. Members can change existing records through **Sharing**. Enabling posts or DMs can cause same-day birthday catch-up.

Missing timezones are saved separately as **pending imports**, hidden from public lists and excluded from announcements/DMs, while retaining their chosen flags. The member opens **Set birthday**, **Update birthday** or **My birthday** to see their prefilled date, explicitly choose a timezone, review sharing and Save. No timezone is silently assigned. Pending records are included in **All birthday records** for moderators and can be deleted normally; removing the server also deletes them. This import change uses the existing sharing columns and requires no new schema migration.

The bot does not write uploaded files to disk. Parsed drafts expire with the private menu; confirmed pending data remains until the member confirms it, someone deletes it, or the server is removed. Discord handles the uploaded attachment itself. Existing database backups retain data under the operator's retention policy.

## Privacy and security

Birthdays belong to one server. **Public birthday browsing is ON by default**, including existing servers after migration. `/birthdaybot` → **Server tools** → **Server settings** → **Birthday list access** → **Private — moderators only** restricts browsing to Manage Server moderators. Public calendars show member mentions and month/day only, ordered January through December; timezones, announcement preferences, and other record details stay private. Records with Public list off are excluded from public pages and monthly counts, even when a moderator opens the public calendar. The overall configured count includes all saved records; the shared count and monthly summary count only publicly shared records. Moderators can privately read every record using **Server tools** → **All birthday records**, including opted-out dates. The server operator can access SQLite and backups. Ephemeral replies are delivered through Discord and are not end-to-end encrypted.

Only month/day is collected, never birth year or age. **Sharing** is a dedicated private menu with independent **Public list**, **Server posts**, **Birthday DMs**, and **Post mentions** controls. Open it directly from `/birthdaybot` to change permissions without editing the birthday. Save commits only those permissions; Cancel leaves them unchanged. Within Set/Update, Sharing → Apply changes the birthday draft; the birthday form's Save commits the date and all permissions together. No typing is required.

Public-list visibility is independent of posts and DMs: members may hide their date from the calendar while still receiving greetings, or share their date while disabling greetings. Post mentions affect only server announcements; with mentions off the post uses an escaped display name and suppresses the member's direct mention. The server's Everyone ping setting is independent. With server posts off, no announcement or everyone ping is sent for that birthday. Server-wide moderator-only list access and the announcements pause still apply. Moderators may read all stored records; users can change only their own preferences. Turning off a permission prevents future work, while already-posted messages follow their existing 24-hour expiry.

Schema 9 copies every existing all-on/all-off choice to the four permissions, including pending imports; upgrading never enables a previously opted-out permission. CSV imports support the four flags independently; the legacy `opt_out`/`consent` choice remains a fallback, with explicit opt-outs always taking precedence. Import menu flags set to Off remain off regardless of CSV opt-ins. Date/timezone edits preserve independent choices. Changing only DMs or posts does not invalidate public calendar snapshots; changing public-list visibility does. Sent and uncertain annual delivery records survive sharing changes, preserving duplicate prevention. Timezone and birthday values are never included in normal audit logs. Sharing audit events contain Discord IDs and permission booleans.

`.env` is the **first line of `.gitignore`**. Runtime databases, WAL/SHM sidecars, data directories, logs, and private keys are ignored. Config tokens are excluded from repr and redacted from application log output. SQL parameters are bound. GIF links reject credentials, unsafe schemes/hosts/ports, whitespace, control characters, malformed percent escapes, query strings and fragments. Accepted URLs are canonicalized and Markdown-sensitive path characters are percent-encoded before they are displayed as clickable links. GIF URLs accept only HTTPS `.gif` files on an explicit set of KLIPY/GIPHY media hosts; the bot never fetches user-supplied URLs on the server. Discord fetches media for the embed. Tokens must stay in `.env` or Pterodactyl secret environment variables, never an egg export, URL, startup command, issue, or commit. Panel environment variables override `.env`.

Commands have a bounded per-user rate limit (10 per 30 seconds). Interactive menus also throttle rapid clicks and expire after 10 minutes. Database writes and sends coordinate with opt-outs and deletions. Records for departed members are removed when a due delivery checks membership; the bot does not subscribe to privileged member events. Removing the bot from a server erases that server's data when the removal event is received. If it was removed while offline, the operator should delete the stale guild data manually. User deletion removes the live birthday and completed delivery rows. Unresolved send markers and pending cleanup metadata are retained to find and expire any public post, even after an interrupted send. Deletion does not remove operator backups or log entries containing IDs; existing posts follow their normal expiry. Minimal annual anti-spam receipts for channel posts and DMs (server ID, user ID, local year; no birthday or timezone) remain so deletion and re-registration cannot trigger repeated celebrations. Removing the server deletes those receipts too.

Public directories reuse **one message per server text channel** instead of posting a new list for every request. They support month filters, next/previous pages, refresh, and a twelve-month summary. Public posts expire after 24 hours using the same durable cleanup queue as announcements. Updating/deleting a birthday or changing list visibility queues existing public directory posts for removal on the next scheduler pass (normally within 30 seconds, plus API/backlog delay). Outages or missing permissions can delay removal. Existing messages cannot be retroactively hidden from screenshots or quotations. Old controls stop showing data after invalidation; `/birthdaybot` opens a fresh view. Private moderator menus recheck permissions on every click.

## Spam control and scale

Application commands and normal menu actions enter a bounded FIFO queue with **64 waiting jobs and two workers**. Each user can have only one pending action, and each server can occupy at most eight slots, so a busy server cannot fill the queue alone. Queue overload gets a private busy reply. Waiting jobs expire after 20 seconds; processing times out after 15 seconds. If a save times out, open `/birthdaybot` → **My birthday** before retrying because its database commit may already have completed. Moderator authorization and menu ownership are checked before accepting work; moderator access is rechecked when queued actions run. Timezone and GIF form submissions also enter this queue.

Commands allow 10 per user per 30 seconds; a shared limiter across menus allows 12 actions per user per 10 seconds. Rapid repeated clicks are also throttled. Opening a modal must be Discord's initial interaction response, so only opening bypasses the work queue. Submissions enter the queue, recheck ownership, menu expiry and moderator permissions, and edit the in-memory draft; Save commits to SQLite. One active private menu per server/user replaces the previous menu. Private menus and shared directory views each have a global limit of 1,000 retained views. Repeated identical directory requests within three seconds reuse the existing link without another edit. Rate-limit maps are bounded to 10,000 users. Busy-queue logs are sampled to avoid log flooding. Discord.py also handles Discord's route and global HTTP rate limits.

Birthday announcements use a separate durable delivery ledger, with batches capped at 25 per scheduler pass and faster follow-up passes when a backlog exists. An index on active month/day retrieves only the civil dates currently possible worldwide. Completed annual receipts are excluded in SQL. The scheduler rechecks consent and membership before sending. Moderator lists fetch ten rows per page through an index instead of loading a server's entire list. Automatic Gateway sharding supports larger installations; Discord application verification and privileged-intent limits still apply. Capacity depends on the host and Discord API limits: unit stress tests establish queue bounds, not a measured maximum production server count.

## Time and delivery rules

New servers use **UTC** as the birthday-form default; each person can choose their own IANA timezone. The menu offers quick choices and **Browse all timezones**, covering every installed IANA name (including aliases and fixed-offset names) in pages of 25. Choose a region, optionally filter by the **current UTC offset**, select a city/name, and press **Use this timezone** to return to the birthday draft. A local clock preview helps confirm the choice. **Back to birthday** keeps the previous timezone and all month/day/sharing edits. No device timezone detection, browser fingerprinting, geolocation, external lookup site, or extra permission is involved.

**Search timezones** accepts cities, regions, full IANA names, or UTC offsets, such as `New York`, `Europe`, `UTC-05:00`, `UTC+05:45`, or `Europe UTC+01:00`. Broad results are paginated rather than truncated. Input is limited to 80 characters and validated against local timezone data; invalid or empty-result searches leave the birthday draft intact. A dedicated **Search** button is always available in the picker, so you can retry immediately without changing a dropdown first. Region/offset selectors and Reset filters also recover from invalid searches. Matching a UTC offset filters timezone choices; the user still confirms an IANA name, so city daylight-saving rules remain intact. The displayed offset and filters reflect the current minute, and each selection shows **UTC±HH:MM**. Catalog snapshots are shared across servers and cached with a bounded size.

Announcements default to **00:00 (midnight) in that member's timezone**, rather than midnight UTC or the panel host's timezone. Moderators can change this in `/birthdaybot` → **Server tools** → **Server settings** → **Announcement time**, or select **Midnight (00:00)**, then **Save settings**. The hour (00–23) and minute (00–59) are chosen with selectors; changes apply to all birthday records in that server. Moderators also control the February 29 rule; members only set month, day and timezone. Upgrading from schema 4 or earlier changes the old 09:00 default to midnight and updates existing records in those servers. Other configured times are preserved; any time, including 09:00, remains selectable after upgrading. Fixed offsets and ambiguous abbreviations should be avoided. Daylight saving changes follow installed IANA rules; keep the host timezone database and pinned `tzdata` current. Linux uses its system timezone data first, with the Python `tzdata` package as fallback. Existing member timezone choices are preserved when upgrading; the prior New York server prefill changes back to UTC. The host's clock must be synchronized; its own local timezone does not affect scheduling.

- The scheduler checks every **30 seconds** while connected. Delivery normally follows the target by up to a poll interval plus API/queue latency; Discord rate limits and outages can increase delay. It is not a hard realtime guarantee.
- Spring-forward gaps use the first real minute after the requested wall time. Fall-back repeats use the first occurrence. A skipped entire calendar date is not moved to a different birthday.
- February 29 birthdays can use February 28, March 1, or leap years only. Dates such as April 31 are unavailable in the menus and rejected by calendar validation.
- A restart or same-day interruption catches up after the target time, while it is still that person's local birthday. Earlier local days are not announced late. Enabling consent or resuming announcements after the target may trigger same-day catch-up.
- A unique `(server, user, local year)` ledger prevents repeat announcements on repeated polls, restarts, birthday edits, and ordinary timezone edits. The record's local calendar year is used, including around UTC year boundaries.
- Send intent is committed before the Discord request. Discord receives a stable nonce with `enforce_nonce` through discord.py 2.7.1, providing additional recent-request deduplication. A 403/404 schedules a durable retry with backoff up to one hour. Ambiguous network/server failures become **uncertain** instead of blindly resending. The bot checks up to 500 channel messages for its unique footer marker; found messages are marked delivered. Unresolved cases are checked at most hourly and require a moderator's decision. This favors avoiding duplicates; no claim of perfect exactly-once external delivery is made.
- Open **Server tools** → **Channel deliveries**, choose a record, then **Find posted message**. Each click searches at most 500 original-channel messages; **Search older messages** continues earlier history. A matching bot author and unique footer are required before marking delivered and scheduling its 24-hour deletion. If the message is absent, **Retry or skip…** opens a separate confirmation. Only retry after checking for duplicates. Retry still requires sharing, enabled announcements, and the user's local birthday. No typed member IDs, years or message IDs are needed.

Birthday announcements are automatically deleted **24 hours after Discord created the message**. The durable cleanup queue survives restarts and user-record deletion, checks every scheduler pass, and retries failures with backoff. Already-deleted messages count as success. Outages, missing channel access, rate limits or a stopped bot can delay removal; overdue posts are removed after recovery. No Manage Messages permission is needed to delete the bot's own posts. Deleting a birthday record does not immediately remove an existing post; its normal 24-hour expiry still applies.

Members with **Birthday DMs** enabled also receive **one birthday DM per server and local calendar year**, using their configured timezone and the same announcement time. The greeting names the server and includes its configured birthday GIF. Server names are escaped, and DMs suppress mentions. The DM permission, server pause and membership checks apply independently of public-list visibility and server posts. A person in several participating servers can receive a separate greeting naming each server.

DM delivery has its own indexed, durable queue, capped at 25 new greetings and 25 delivery attempts per pass. A failed channel post does not prevent the DM; blocked DMs do not stop the public announcement. Discord privacy restrictions (`403`) are recorded as `blocked` and are not retried that year. Safe transient failures back off, while ambiguous sends are reconciled against the bot's own DM history before any manual retry. Retries only send while it is still the member's local birthday. **Server tools** → **Status** includes DM outcomes; **Server tools** → **DM deliveries** inspects and resolves uncertain greetings using the same selectors and confirmation buttons; DM bodies are never shown to moderators. Recipient DMs are also deleted after 24 hours through the durable cleanup queue. Sent, blocked and ambiguous DM metadata is retained to prevent delete/re-add spam and recover interrupted sends; no birthday date or DM body is stored there.

Operational warnings appear in structured JSON logs and repeat at most hourly in the configured moderator alert channel. They cover missing setup, permission failures, rejected sends, and uncertain deliveries. No private dates are put into those alerts. Heartbeats log every five minutes. Use **Server tools** → **Status** to spot a stalled scheduler. Failed ticks log tracebacks and try again next tick. Normal command audit logs include the command actor; detailed HTTP, Gateway and SQLite debug logging stays disabled even with `LOG_LEVEL=DEBUG`.

## GIFs

The default is a direct animated [KLIPY birthday cake GIF](https://klipy.com/gifs/happy-birthday-837). KLIPY [lists Discord among its integrations](https://klipy.com/apps). No GIF API key is required. Use `/birthdaybot` → **Server tools** → **Server settings** → **Birthday GIF link** to choose another direct media URL, or set `BIRTHDAY_GIF_URL`. Supported hosts: `static.klipy.com`, `static2.klipy.com`, `media.klipy.com`, `i.klipy.com`, `media.giphy.com`, `i.giphy.com`. Copy the image's `.gif` address, not the provider's HTML share page. Preview it after changing. Third-party media availability and client GIF autoplay preferences affect playback; replacing a removed GIF is an operator task.

## Pterodactyl Python egg

Use a generic Python egg with **Python 3.12**, SQLite support, outbound HTTPS and Discord Gateway WebSocket access. Configure its Git repository URL to this public repository, the branch to `main`, its requirements filename to `requirements.txt`, and its Python/startup file to `main.py`. Exact field labels vary by egg. Dependency install must run `python -m pip install --require-hashes -r requirements.txt`; eggs that use `pip install -r requirements.txt` also enter hash-checking mode because this file contains hashes. If overriding the complete startup command, use `python -u main.py` or `sh start.sh` after dependencies are installed.

Create `.env` through the panel file manager or SFTP using `.env.example` as a template. Alternatively set `DISCORD_TOKEN` as a hidden panel variable. Do not put secrets in a public egg template. Leave `TEST_GUILD_ID` empty for production. No network port needs to be exposed.

Keep `.env`, `data/`, and `logs/` persistent under `/home/container` across updates. Repository sync must update tracked source files only; **do not use `git clean -fdx` or wipe/reinstall the server volume**. Use fast-forward Git pulls and stop/restart the bot around updates. A single bot process must own the database: local process locking prevents a duplicate process using the same lock file, but does not coordinate replicas with separate volumes. Stop the test instance before starting production with the same bot token. Give shutdown time to cancel the scheduler and close/checkpoint SQLite; SIGTERM is handled. Health is visible as `bot_ready` and `scheduler_heartbeat` in the panel console.

For panel updates, leave `AUTO_UPDATE=1` and restart the server. The egg pulls code and installs requirements; startup checks the schema of the database at `DATABASE_PATH` on that server. Before upgrading an existing schema, the bot creates a private, integrity-checked snapshot in `data/backups/` (or `backups/` beside a custom database path), then migrates the database in one transaction. Schema 10 adds Everyone ping with a default of off and preserves birthdays, sharing choices and delivery history. A failed backup or migration stops startup; a newer unsupported schema is refused. Watch for `database_backup_created`, `database_migrated`, then `bot_ready`. No manual migration command or local database upload is needed. Pushing code alone does not change the running panel instance.

Runtime defaults: `DATABASE_PATH=data/birthdays.sqlite3`, `LOG_DIR=logs`, `LOG_LEVEL=INFO`. Log files rotate at 5 MB with five backups, plus panel stdout logs. Configure the panel's own log retention. Start with a modest memory allocation (for example 256 MiB) and observe actual usage before sizing larger servers.

## Standalone Linux service

For a checkout at `~/discordbirthdaybot`, the included user service keeps the bot independent of an interactive terminal and automatically restarts it after an unexpected exit. Adjust the paths in `deploy/discordbirthdaybot.service` for another checkout location. Pterodactyl provides its own process supervision; use its Python startup command inside the panel instead of this service.

```sh
mkdir -p ~/.config/systemd/user
cp deploy/discordbirthdaybot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now discordbirthdaybot.service
loginctl enable-linger "$USER"
```

Inspect status with `systemctl --user status discordbirthdaybot.service` and exit details with `journalctl --user -u discordbirthdaybot.service`. Restart after code updates using `systemctl --user restart discordbirthdaybot.service`. Use `systemctl --user stop discordbirthdaybot.service` before starting a panel instance with the same bot token. The service reads the checkout's ignored `.env` through the application; credentials are not embedded in the unit. Process start, shutdown signals, connection exit and completed shutdown are logged alongside scheduler heartbeats. The journal retains exit status when the process cannot write an application traceback, such as a forced termination.

## SQLite durability and backup

Every connection used by the bot explicitly enables and verifies **`journal_mode=WAL` and `synchronous=FULL`**, with foreign keys, a five-second busy timeout, serialized `BEGIN IMMEDIATE` transactions, rollback on cancellation, schema version checks, and graceful WAL checkpointing. `FULL` causes a WAL sync per transaction commit; see [SQLite's documentation](https://sqlite.org/pragma.html#pragma_synchronous). Use a local persistent filesystem with reliable fsync semantics. Durability depends on the panel host, filesystem, and storage honoring flushes; SQLite cannot compensate for a lost volume or dishonest storage. Network shares are unsuitable for this WAL database.

Schema upgrades automatically create a verified SQLite backup before changing an existing database, including committed WAL contents. These snapshots remain on the panel volume; they are not created on ordinary restarts and do not replace regular off-server backups. Backups are never automatically deleted. Keep protected copies elsewhere and manage retention.

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

Tests cover CSV bounds, validation, duplicate detection, member verification and retry, import permissions, confirmation, transactional rollback, concurrent imports, pending timezone confirmation, cancellation and privacy, complete timezone and offset pagination, immediate invalid-search retry, region/city/offset searches, DST-aware offset filtering, timezone draft confirmation/cancellation, server-named DMs, blocked recipients, independent channel/DM failure handling, retry and crash recovery, annual DM deduplication, bounded DM batches, public/private directory access, chronological pagination, monthly counts, opt-out filtering, old-message invalidation, schema migration, launcher navigation, queue saturation and per-server fairness, permission enforcement, private menu ownership, revoked moderator permissions, server isolation, opt-out preservation, menu limits, URL validation, log redaction, WAL/FULL, transaction cancellation, concurrent delivery claims, restart deduplication, membership removal, ambiguous-send recovery, 24-hour deletion and retry across restarts, midnight crossing, DST gaps/folds, leap years including 2100, UTC year boundaries, and quarter-hour offsets. CI runs these checks without a Discord token. Live Discord testing is separate: confirm private setup from a regular member and a moderator, forbidden moderator access from the regular member, GIF rendering, one real scheduled post with announcements enabled, and no repeated post after restart. API-only checks do not prove client UI behavior.

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
