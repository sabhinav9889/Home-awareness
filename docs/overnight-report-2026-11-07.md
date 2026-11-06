# Overnight report — 7 Nov 2026

Started 00:20 IST. You authorised autonomous coding, testing, improvements and competitor-driven features until 10:00 AM.
Limits I kept: no push, no GitHub/cloud accounts, no deploys, no paid services, other projects untouched.

## Progress

### ✅ Plan 1 — server core: done, reviewed, merged into `main` (local only, not pushed)
- 13 tasks built by subagents, each checked by an independent reviewer; fix rounds were needed on Tasks 3, 8, 9, 10, 11, 13.
- Final whole-branch review (strongest model) → one critical + five important findings → fixed in one wave (22 commits),
  then a re-review caught two more real bugs in that wave → fixed in a small follow-up (4 commits) → clean.
- **175 tests pass** (I re-ran them myself against real Postgres with warnings-as-errors). Lint clean. CI runs migrations + `alembic check`.
- **Real end-to-end run on Docker passed:** sign up → confirm → pair the fake sensor → scream → alert delivered → sensor reported offline after it stopped.
- Notable things the reviews caught that tests alone would not have:
  - the example `.env` shipped a placeholder login-token secret that would let anyone forge logins (now rejected at startup);
  - a worker crash or broker outage could double-send or silently lose a scream alert (now at-most-once with a safe fallback);
  - the Telegram bot token was being written to logs by the HTTP library (now redacted in every process);
  - a member removed at the exact moment they connected could keep receiving their old household's live events;
  - after a server restart, every sensor would have been falsely reported "offline" to every member;
  - when Telegram and ntfy were sent in parallel, one channel's "sent" could be lost from the record.

How to try it: `cd ~/home-awareness && cp -n .env.example .env`, put a long random `JWT_SECRET` in `.env`
(`python3 -c "import secrets;print(secrets.token_urlsafe(48))"`), then `REDIS_HOST_PORT=6380 docker compose up --build -d`
and follow "Manual end-to-end" in `README.md`. (6380 because your Heidi project's Redis uses 6379.)

### ✅ Competitor research — `docs/research/2026-11-07-competitors.md`
The top 8 features users value are already in our design. New candidates: vibration/flash alerts (deaf/hard-of-hearing),
per-member quiet hours, a Home Assistant/MQTT bridge. Rejected as conflicting with the design: uploading audio clips for review,
emergency-service calling, radar/camera fall detection, a paid cloud tier.

### ✅ Plan 2 — laptop sensor + alert agent: done, reviewed, merged into `main` (local only)
- 9 tasks + final review + one fix wave. **221 edge tests + 175 server tests pass** (re-run by me with warnings-as-errors). Built: package skeleton + config (0600 files), server client with an in-memory retry queue, YAMNet label map + detector, **YAMNet running on this laptop (3.4 ms per window, model checksum-verified)**, microphone capture pinned to one mic (PipeWire), and the "is media playing through speakers?" probe (headphones correctly ignored).
- PortAudio isn't installed and I can't sudo, so audio goes through PipeWire's own tools (`pw-record`, `pw-dump`, `pw-play`) — verified on your built-in mic.
- I removed six generic YAMNet labels ("Door", "Dog", "Breathing", "Breaking", "Bang", "Grunt") because they'd fire on everyday sounds and flood you with alerts.
- **Measured accuracy (ESC-50, 2,000 real clips) — `docs/accuracy.md`:** F1 at threshold 0.30 — dog barking 0.88, glass breaking 0.84, crying 0.81, siren 0.81, knocking 0.74, coughing 0.66. These are clean-recording upper bounds. Every current reliability label in the catalog matches. Siren and coughing do better at 0.50 → per-sound thresholds are a good next enhancement. Scream, smoke alarm, doorbell, thud and 7 others have no ESC-50 class, so they stay unmeasured.
- **`ha-sensor`** (pair / devices / fetch-model / run): real smoke against the server — paired, heartbeats, a synthetic smoke-alarm recording produced 4 `smoke_alarm` events at the server, revoking the device stopped it with "pair again".
- **`ha-agent`** (login / run / test-alarm): real smoke — a test alert arrived live; popup + alarm through the default output (= your headphones when plugged in); "I'm on it" acknowledges; the alarm is capped (30 s per alert, restarts on a new alarm alert, 3 min overall).
- Reviews caught and fixed: a housemate's "I'm on it" (or a test alert) could have silenced *your* smoke alarm; one bad message could drop a popup; a mic unplugged mid-night could have silently switched to another input; if the laptop clock drifts >10 min the server rejects every event — now logged loudly; queued events for a sound you just turned off were still sent after an outage.
- Setup guide: `edge/README.md` (Linux + PipeWire). Note: your speaker is **muted** and mic gain is **0.18** in system settings — the live mic heard only silence during testing. I didn't change your settings.
- A usage limit paused everything from about 02:30 to 04:00; work resumed automatically.

## Rulings I made on your behalf (Plan 2)
1. Audio uses PipeWire's tools (`pw-record`, `pw-dump`, `pw-play`) because PortAudio isn't installed and I can't sudo. [Linux/PipeWire only for now]
2, 6. Some tasks ran two builders in parallel on separate files to save time. [none observed]
3. The sensor's send queue keeps events through server outages (up to 10 min, max 500) in memory only — never written to disk.
4. Removed six generic YAMNet labels that fire on everyday sounds. [some real events missed for door slam / gasping / glass / explosion / moaning — accuracy doc shows the trade]
5. The model is checksum-verified every time the sensor starts.
7. A pinned mic that disappears stops the sensor (exit 4) — belt-and-braces: PipeWire "don't fall back" flags plus a 3 s no-audio watchdog.
8. The alarm is streamed straight to `pw-play`; no temp files.
9. "I'm on it" from someone else only silences the alarm for that same event (server now sends the event id).
10. Pressing "Stop alarm"/"I'm on it" on your own laptop silences everything that's ringing.
11. Alarm: 30 s per alert, restarts for a new alarm alert, hard cap 3 min of continuous ringing.

## Rulings I made on your behalf (Plan 1)
Each is something the plan/spec didn't settle; "cost if wrong" in brackets.
1. Built on a branch in the main checkout, not a separate worktree. [none]
2–5, 11. Small scaffolding stubs/files the plan implied but didn't list. [trivial]
6. Local ports configurable; this project uses Redis on 6380 because Heidi holds 6379. [trivial]
7. SMTP port is a number, not text. [trivial]
8. Each builder's commits carry its own model's Co-Authored-By line (some say Sonnet 5.5, some Opus 5.5). [cosmetic; fixable before you push]
9. Pairing a sensor looks up the code outside the tenant layer (the caller isn't logged in yet). [low; one audited query]
10. Per-sensor rate limits are keyed on the hashed device key with no DB lookup. [fake keys get their own bucket but are still rejected]
12. Alerts are queued only after they're saved. [small drift from the plan's function signature]
13, 15. Merge window uses an atomic claim so two sensors reporting the same scream at once count as one. [none]
14. Delivery records hang off their alert rather than carrying the household id themselves. [future code must read them via the alert]
16. **Alert texts show time in UTC (now labelled "UTC").** No household timezone in the design. [confusing in India: 2:14 AM shows as 20:44 UTC — see "Needs you"]
17. A deleted sensor is named "a removed sensor" in messages.
18. The background worker uses its own database connection per job. [none]
19. A member with no saved preferences gets the defaults (popup on).
20. **Alerts are delivered at most once.** If a worker crashes mid-send, that alert may be lost rather than repeated. [rare missed alert vs. repeated 2 AM alarms]
21. Telegram switched on but no bot configured → recorded as failed; no channels on → alert marked failed.
22. "Sensor offline" warnings are direct messages, not alerts, and fire once per outage.
23. Request logs record only the route pattern, never real URLs (login tokens and the webhook secret live in URLs).
24. If the job queue is down, the server sends the alert itself instead of dropping it.
25. The final fix wave also took 15 small improvements (e.g. "sent" only when someone was actually listening; alerts older than 10 min are dropped instead of arriving late).
26–27. No "offline" warnings right after the server itself was down; a home whose only sensor or internet died is still warned, after ~13 min instead of 3.
28. Ran one extra targeted fix round beyond the normal process, because two bugs found at the end would have misled users.

## Needs you
- **Household timezone:** alert messages show time in UTC for now. I plan to add a per-household timezone in the enhancements step; tell me if you'd rather default to IST.
- **Commit identity check:** commits are `sabhinav9889 <sabhinav9889@gmail.com>` as you asked; nothing has been pushed.
- **Telegram:** to get phone pushes you'll need to create a bot with BotFather (free) — steps in `README.md`. I didn't create any accounts.
