# Home Awareness — Interview Prep

Everything here is true of the code in this repo. If you can't explain a line below in your own words, open the file it points to before the interview.

---

## 1. Resume bullets (pick 3–4)

- Built **Home Awareness**, a privacy-first, multi-household sound-alert platform: on-device ML (YAMNet) detects screams, glass breaking, smoke alarms and doorbells; only small events reach a **FastAPI** backend that alerts household members by **WebSocket, Telegram and ntfy**.
- Designed **tenant isolation** for a multi-household SaaS (one scoping layer + an isolation test harness that checks every household-scoped route), with JWT auth, revocable scoped agent tokens and password-reset session revocation.
- Implemented an **at-most-once alert pipeline** (atomic claim in Postgres, per-channel delivery records, Celery workers with an in-process fallback) and a **Redis rule engine** (repeat counts, cooldowns, multi-sensor incident merging via an atomic Lua script).
- Built a Linux **edge client** (PipeWire capture, TFLite inference at ~3.4 ms/window, playback-aware thresholds, clipping detection) and a **no-framework web dashboard** with a live alert screen; **~600 automated tests** including real-browser Playwright tests; Docker Compose + GitHub Actions CI.
- **Measured** detection on 2,000 ESC-50 clips and used live testing to tune label rules — cut glass-breaking false alarms from 6 to 2 (precision 0.85 → 0.94).

Numbers you can quote: ~3,900 lines of server Python, ~3,000 edge Python, ~2,400 JS; 317 server + 277 edge test functions; 38 HTTP/WebSocket routes; 14 migrations; 148 commits.

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

## 5. Trade-offs and "what would you change?"

- **Scale:** single VM; next steps would be partitioning events by time, a managed queue, horizontal API workers behind the proxy (the API is stateless; live fan-out already goes through Redis pub/sub).
- **Model:** scream detection is weak for shouted/close-mic screams; I'd fine-tune or add a second model on scream-specific data.
- **At-most-once** loses an alert after a worker crash mid-send; a stuck-alert sweep makes it visible. I'd add an idempotent outbox with per-channel dedup keys for at-least-once if channels supported it.
- **Phone as sensor** (browser mic + in-browser model) is designed but not built; needs HTTPS.

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
- **The risk:** if you can't explain a part, it shows. Before interviews, read and be able to explain at least: `app/tenancy.py`, `app/rules/engine.py`, `app/alerts/delivery.py`, `app/live/router.py`, `edge/src/ha_edge/detector.py`, `edge/src/ha_edge/labels.py`.

---

## 8. Practice

- Say the 60-second pitch out loud until it's natural.
- Draw the 5-box diagram from memory and walk one alert through it.
- Ask Claude: *"Mock-interview me on Home Awareness"* — one question at a time, with feedback.
