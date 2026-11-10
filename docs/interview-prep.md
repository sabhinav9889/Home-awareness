# Home Awareness — Interview Prep

Everything here is true of the code in this repo. If you can't explain a line below in your own words, open the file it points to before the interview.

---

## 1. Resume bullets (pick 3–4)

- Built **Home Awareness**, a privacy-first, multi-household sound-alert platform: on-device ML (YAMNet) detects screams, glass breaking, smoke alarms and doorbells; only small events reach a **FastAPI** backend that alerts household members by **WebSocket, Telegram and ntfy**.
- Designed **tenant isolation** for a multi-household SaaS (one scoping layer + an isolation test harness that checks every household-scoped route), with JWT auth, revocable scoped agent tokens and password-reset session revocation.
- Implemented an **at-most-once alert pipeline** (atomic claim in Postgres, per-channel delivery records, Celery workers with an in-process fallback) and a **Redis rule engine** (repeat counts, cooldowns, multi-sensor incident merging via an atomic Lua script).
- Built a Linux **edge client** (PipeWire capture, TFLite inference at ~3.4 ms/window, playback-aware thresholds, clipping detection) and a **no-framework web dashboard** with a live alert screen; **~600 automated tests** including real-browser Playwright tests; Docker Compose + GitHub Actions CI.
- **Measured** detection on 2,000 ESC-50 clips and used live testing to tune label rules — cut glass-breaking false alarms from 6 to 2 (precision 0.85 → 0.94).

- Extended it with **camera sensors**: on-device pose and object detection (MediaPipe) raises falls, "lying still", and "person while the house is guarding" alerts; **video never leaves the device** — only one small JPEG per alert, **Fernet-encrypted at rest**, retention-limited (1/7/30 days) and visible only to household members. A Redis-backed **correlation step** combines sensors in the same room into follow-up incidents ("possible break-in" = glass + person within 15 s).
- Wrote the **production deployment tooling** for a $0 Oracle Always Free VM: Docker Compose + Caddy automatic HTTPS, a deploy script with health-checked **rollback**, GitHub Actions auto-deploy gated on CI and a pinned SSH host key, nightly `pg_dump` backups with a tested restore, and a VM bootstrap script. Verified end to end locally, and now **running on a real Oracle VM** (a real laptop sensor is paired to it).

Numbers you can quote (measured 9 Nov 2026): ~5,200 lines of server Python, ~4,600 edge Python, ~3,100 JS, ~1,100 shell; 613 server + 429 edge test functions (about 1,000 tests, including real-browser Playwright tests and a real-Postgres backup/restore drill); 47 HTTP/WebSocket routes; 17 migrations; 192 commits.

---

## 2. The 60-second pitch

> "My mother had a nightmare and screamed while I was working with headphones on — I didn't hear her. So I built Home Awareness. A spare phone or laptop in a room listens locally; a small ML model on the device recognises sounds like a scream, glass breaking or a smoke alarm. The audio never leaves the device — only a tiny event like 'scream, 0.9, 2:14 AM' goes to my FastAPI server. The server applies each family member's rules — which sounds they care about, repeat counts, cooldowns — and alerts them the way they chose: a popup, an alarm through their headphones, or a phone push. It's multi-household, so other families can sign up, and each household's data is isolated."

Then **stop** and let them pick what to dig into.

---

## 3. Draw this on the whiteboard (5 boxes, 2 minutes)

```
[Sensors: laptop / Pi]  --HTTPS events+heartbeats-->  [FastAPI]  --> [Celery worker] --> Telegram / ntfy
 (YAMNet on device)                                    |      |
                                                  [Postgres] [Redis]
                                                       |
                                           --WebSocket--> [Dashboard / laptop alert agent]
```

Walk one alert end to end:
1. Sensor hears a doorbell → YAMNet scores 0.80 → detector checks per-sound threshold, holds 0.5 s for the peak, debounces.
2. `POST /device/events` with the device key → validated (no extra fields, timestamp within 10 min) → stored.
3. Rule engine: is it a duplicate of another sensor's report (merge window)? For each member who enabled it: confidence ≥ their threshold? repeats met? not in cooldown?
4. Alert rows committed **then** queued (commit-before-enqueue).
5. Worker atomically claims the alert, sends each channel, records each result.
6. Dashboard shows a full-screen alert; "I'm on it" acknowledges it for the whole household.

---

### Add these two boxes if they ask about cameras or hosting

```
[Camera: ha-camera]--event + 1 JPEG (multipart)-->[FastAPI]-->[Redis: per-room window]--> follow-up "incident" alert
 (pose + object models on device)                     |
                                                 [encrypted snapshot on a volume, swept by retention]

[Internet]--HTTPS-->[Caddy]-->[FastAPI]   (only Caddy publishes ports; db/redis/api are internal)
                      |
              [backup service: nightly pg_dump, restore script]      [GitHub Actions --SSH--> deploy.sh: build, migrate, health check, rollback]
```

## 4. Deep-dive questions and good answers

### Architecture & design
**Why process audio on the device instead of the server?**
Privacy (audio never leaves the home — a stated anti-goal), bandwidth, cost ($0 budget), and latency. Trade-off: weaker devices, model updates need a client update.

**Why FastAPI + Postgres + Redis + Celery?**
FastAPI: async I/O for many sensors + WebSockets, Pydantic validation. Postgres: relational data with transactions (claims, cascades). Redis: short-lived counters (cooldowns, repeat windows, rate limits) and pub/sub for live updates. Celery: alert delivery off the request path with retries. I kept a job-runner interface so the same code runs with an in-process runner on a host without a worker (portability).

**How does the server stay hardware-agnostic?**
Every device sends the same event shape `{type, confidence, occurred_at}`. A future CCTV or water sensor just sends events — no server redesign.

### Multi-tenancy & security
**How do you stop one household seeing another's data?**
Every household-owned table has `household_id`. All reads/writes go through one `Tenant` class (`app/tenancy.py`) that adds the filter; another household's row returns the **same 404 as a missing row** (no existence leak). An isolation test harness (`tests/test_isolation.py`) tries every household-scoped route as household B against A's resources. Sanctioned exceptions are listed in the tenancy docstring.

**How do devices authenticate?**
Pairing: the dashboard creates an 8-character, single-use, 10-minute code; the device trades it for a random key. Only SHA-256 hashes are stored. Codes are claimed with an atomic `UPDATE … WHERE used_at IS NULL RETURNING` so they work exactly once. Deleting a device revokes its key on the next request.

**What happens on a password reset?**
A `token_version` is bumped atomically: every JWT carries `tv`, so all old sessions die. Agent tokens are revoked, pending invites from that account are cancelled. I found and closed a race where a stolen session could mint a new agent token at the exact moment of the reset (lock + re-check the version).

**Secrets in logs?**
Request logs record the **route template**, never the URL (the WebSocket token and Telegram webhook secret are in URLs). A log-record factory redacts bot tokens in every process, and uvicorn's own access line is masked — a reviewer caught that one.

### Reliability
**At-most-once or at-least-once delivery? Why?**
At-most-once per alert. The worker claims an alert with `UPDATE … SET status='delivering' WHERE status='pending'`; only one worker can win, and each channel's result is committed as it finishes. For a 2 AM alarm, a repeated alarm is worse than a rare lost one after a worker crash. A sweep marks anything stuck > 15 min as failed so it's visible, never silent.

**What if the job queue (broker) is down?**
Enqueue failures fall back to in-process delivery — safe because the atomic claim prevents double sends. Without that, the alert would be stuck and the cooldown would also block the sensor's retry.

**How do you avoid alert floods?**
Repeat rules ("5 barks in 60 s"), per-member cooldowns, and a 5-second merge window so one scream heard by three sensors is one incident. The merge is an atomic Redis Lua script that also tracks the loudest report, so a stricter member is still alerted when a closer sensor hears it louder.

**A sensor dies — how would anyone know?**
Heartbeats every 60 s; offline after 180 s → a direct message to members. Guarded so a server restart doesn't falsely report every sensor offline.

### ML / edge
**Which model and why?**
YAMNet (AudioSet, 521 classes), TFLite, ~4 MB, ~3.4 ms per 0.975 s window on my laptop. Pre-trained so the AI part stays small and the engineering carries the project.

**How did you measure accuracy?**
ESC-50 (2,000 clips): F1 dog 0.88, glass 0.83, crying 0.81, siren 0.81, knocking 0.74, coughing 0.66. These are clean-recording upper bounds; scream, smoke alarm and doorbell have no ESC-50 class, so they're marked unmeasured — honest labels in the catalog.

**Tell me about a time testing changed your design.** *(great story — see §6)*
Live test: a chime doorbell was missed (YAMNet said "Chime"), and a ding-dong caused a false glass-breaking alert ("Glass" 0.41). I added label rules with minimum scores and vetoes ("Chime" counts unless "Wind chime" is stronger; "Glass" needs ≥ 0.60), re-measured: glass false alarms 6 → 2, precision 0.85 → 0.94, recall 0.82 → 0.75 — I accepted that trade.

**What if the laptop plays a movie with a scream in it?**
A PipeWire probe detects audio playing through speakers and raises thresholds by 0.20; not through headphones (the mic can't hear those). It's imperfect and documented as such.

### Frontend / realtime
**How does the live dashboard work?**
One shared WebSocket per logged-in tab with pub/sub; reconnect with exponential backoff (reset only after 10 s stable, to avoid storms); events deduplicated by id; on reconnect it catches up via the API, including alerts missed while offline.

**Why no React?**
Small surface, strict Content-Security-Policy (no inline scripts, no eval, no CDN JS), and no build step. Server strings are always inserted as text, never HTML.

### Testing & delivery
**How did you test?**
TDD; ~600 test functions; isolation harness; Playwright tests in real Chrome; fakeredis; tests that prove ordering (commit before enqueue); a real Docker end-to-end run; CI runs migrations + `alembic check` before tests.

**Hardest bug?**
Pick one you can explain fully:
- Concurrent channel sends mutating ORM rows while another coroutine committed → a "sent" result silently not saved. Fix: send tasks return plain results; only the main task writes rows.
- Flaky browser tests → two test runs sharing one database; fixed with a file lock.

---

### Cameras, privacy and incidents
**Isn't a camera a big privacy step? How did you limit it?**
Analysis happens on the device; video is never streamed, recorded or stored. When something alerts, **one** JPEG (longest side 640 px, ≤ 300 KB, from a frame at most 1 s old) goes with the event. The server encrypts it with Fernet before writing it to a volume, keeps it 1, 7 or 30 days (owner's choice, 200 MB cap per household), and only household members can fetch it. Each camera has a "Send snapshots" switch, and adding a camera requires a consent tick; cameras in bedrooms or bathrooms default to photos off. No face recognition, by design. Honest limit: ntfy and Telegram copies leave my control.

**How do you combine microphone and camera?**
Devices belong to rooms. After the normal alert path commits, a correlation step keeps a 30 s Redis sorted set per room and household; when glass breaking and a person event are both in the window (15 s), it creates a *follow-up* incident alert. Incidents never delay the individual alerts, and a `SET NX EX 60` key stops a rule firing twice. Failures there are caught and can't affect the original event's response — a review found my first version could still turn a rolled-back session into a 500 on the ingredient response, so the response is built before the incident step.

**How does fall detection work, and how good is it?**
Pose landmarks give a torso line; a fall is "upright to horizontal with the hips dropping within about a second", lying-still is "horizontal and nearly motionless for two minutes". It refuses to judge when shoulders or hips aren't visible, which on a laptop webcam is most of the time. It's tested on synthetic landmark streams, **not measured on real falls**. A review also found I had ignored the video's aspect ratio when computing angles (a true 45° read as 29° at 16:9); fixing that meant scaling x by width/height.

**Why is weapon detection off by default?**
Because it's wrong: in my own live test it raised six alerts at 0.42–0.48 confidence, and the two snapshots I opened showed a phone held near the lens and a hand — no weapon. I would only trust it after adding a "held by a hand" check, a sustained-detection rule and measured precision.

### Deployment and operations
**How do you deploy, and what happens if a deploy fails?**
`deploy.sh` on the VM: validate the git ref, take a lock, fast-forward only, build, run migrations as a separate step, `up --wait`, then a health check against the local Caddy. On failure it rolls back api/worker/beat to the last healthy commit *without* re-running migrations (an old image can't run a newer schema's `alembic upgrade`). Migrations are forward-only by policy, so they must be additive. The GitHub workflow only deploys a commit CI passed on `main`, over SSH with a pinned host key, detached from the SSH session so a dropped connection can't kill it mid-switch.

**What security problems did you find in the deployment?**
Reviews found: Caddy's error log recorded `?token=` URLs and device keys on every 502; a public `/metrics` page; rate limits that would have seen every client as the proxy (fixed with explicit `X-Forwarded-For` handling and verified with forged headers); a restore script that could empty the live database on a bad dump (now one transaction plus pre-validation); and a secrets generator whose `--force` wiped settings.

**Backups?**
Nightly `pg_dump` (plain SQL, restores anywhere including Neon), 14 daily plus weekly retention, 0600 files, atomic writes, a timeout, and a **tested** restore into a scratch database. Photos are deliberately excluded (short-lived, encrypted); the `SNAPSHOT_KEY` must be stored separately or old photos are unreadable.

**Is it running in production?**
Yes: it runs on an Oracle Always Free Arm VM behind Caddy with automatic HTTPS. Be ready for honest details: Oracle had no free Arm capacity at first, so I wrote a retry script; the deployment guide was written from doing it. Auto-deploy from GitHub is built and tested but only enabled once the five repository secrets are added.

### What went wrong (good stories)
- **A mocked test hid a startup crash.** `ha-camera run` crashed on launch because the CLI looked up a class in the wrong module, and every CLI test replaced that function with a fake. An end-to-end review that ran the real path found it. Lesson: mock at the edges, and run the real path once.
- **Real model, real crash.** Closing the MediaPipe model while a worker thread was still inside `detect` exited with SIGSEGV 3 times out of 3. Fixed by waiting for the in-flight inference before closing.
- **A build agent migrated my live database.** A test command broke halfway and an agent's `alembic` ran against the live database because the worktree's `.env` pointed at it. Additive tables only, nothing lost, but it's why subagent prompts now forbid database tools and the worktrees have no `.env`.

## 5. Trade-offs and "what would you change?"

- **Scale:** single VM; next steps would be partitioning events by time, a managed queue, horizontal API workers behind the proxy (the API is stateless; live fan-out already goes through Redis pub/sub).
- **Model:** scream detection is weak for shouted/close-mic screams; I'd fine-tune or add a second model on scream-specific data.
- **At-most-once** loses an alert after a worker crash mid-send; a stuck-alert sweep makes it visible. I'd add an idempotent outbox with per-channel dedup keys for at-least-once if channels supported it.
- **Phone as sensor** (browser mic + camera with in-browser models) is designed but not built; it needs HTTPS, which the cloud deployment provides.
- **Cameras:** fall, lying-still and weapon detection are not measured on real people; a laptop webcam can't see a whole body. Accuracy work needs a feedback loop ("real / false alarm" on each alert) and a public fall dataset.
- **Deployment:** runs on one free VM with no redundancy; the no-card fallback is untested; migrations are forward-only, so rollbacks only cover the application containers.

---

## 6. Behavioural story (STAR)

- **Situation:** my mother screamed in a nightmare; I didn't hear because of headphones.
- **Task:** build something that gets sound to a person even when they can't hear it — privately, at $0.
- **Action:** designed events-only architecture; built backend, edge client, dashboard; reviewed every piece; **tested it live** and found real misdetections; measured and tuned.
- **Result:** working end to end on real hardware; doorbell detection fixed and measured; glass false alarms cut by two-thirds; clear honest limits (scream is experimental).

---

## 7. How it was built — be honest about this

You built this with AI coding agents: you set the product direction and constraints, approved the design, and every change went through test-first development, independent code review and fix rounds. Interviewers increasingly ask about this. Say it plainly, then show you own it:

- "I used AI agents to implement under a strict process: written design, step-by-step plans, tests first, an independent reviewer per task, and a whole-branch review before every merge. My job was the product decisions, the trade-offs, and verifying — like the live testing that found the doorbell and glass problems."
- **The risk:** if you can't explain a part, it shows. Before interviews, read and be able to explain at least: `app/tenancy.py`, `app/rules/engine.py`, `app/alerts/delivery.py`, `app/live/router.py`, `edge/src/ha_edge/detector.py`, `edge/src/ha_edge/labels.py`, and for the newer parts `app/snapshots/store.py`, `app/rules/incidents.py`, `edge/src/ha_edge/camera/pose_rules.py`, `edge/src/ha_edge/camera/runner.py`, `deploy/deploy.sh` and `deploy/Caddyfile`.

---

## 8. Practice

- Say the 60-second pitch out loud until it's natural.
- Draw the 5-box diagram from memory and walk one alert through it.
- Ask Claude: *"Mock-interview me on Home Awareness"* — one question at a time, with feedback.
