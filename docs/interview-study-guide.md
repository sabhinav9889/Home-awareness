# Home Awareness: Interview Study Guide

How to use this: spend about 20 minutes a day on each block below, **out loud**. Reading is not enough. If you can't say a block in your own words without looking, open the file it points to and read it again. The companion file [interview-prep.md](interview-prep.md) has the resume bullets, the 60-second pitch and model answers.

## 1. A three-day plan

| Day | Learn | Practise |
| --- | --- | --- |
| 1 | The map (section 2), then the server: `tenancy.py`, `rules/engine.py`, `alerts/delivery.py` | Draw the 5-box diagram from memory. Explain one alert from doorbell to screen. |
| 2 | Security and live feed: `security.py`, `ratelimit.py`, `live/router.py`; then the sensor: `detector.py`, `labels.py` | Answer questions 1 to 12 below out loud, then check the answers. |
| 3 | Cameras and deployment: `rules/incidents.py`, `snapshots/store.py`, `camera/pose_rules.py`, `deploy/deploy.sh`, `Caddyfile` | Answer questions 13 to 25. Then do a mock interview with the assistant. |

## 2. The map in one page

```
Sensor (laptop / Pi)                    Server (FastAPI)                         People
 mic -> YAMNet on device                 POST /device/events (device key)         Dashboard (WebSocket popup)
 camera -> pose/object models   ---->    validate, store                  ---->   Desktop agent (headphone alarm)
 only a tiny event (+1 JPEG)             rule engine (Redis): merge across         Telegram / ntfy
                                          sensors, per-member rules, cooldowns
                                         commit alerts to Postgres, THEN queue
                                         Celery worker: claim -> send per channel
```

One alert, step by step:
1. Sensor hears something, the **on-device** model scores it, and a per-sound threshold plus peak hold and debounce decide whether to report.
2. `POST /device/events` with the device key. The server validates the event (no extra fields, timestamp within 10 minutes) and stores it.
3. The **rule engine** merges duplicate reports from other sensors (a 5-second Redis window), then for each member: is the sound enabled, is the confidence above their threshold, are the repeat count and cooldown satisfied.
4. Alert rows are **committed first**, then queued.
5. A **Celery worker** atomically claims each alert (`pending` to `delivering`), sends it on each channel, and records each result.
6. The dashboard shows a full-screen alert. "I'm on it" acknowledges it for the whole household.

## 3. Reading order, and what each file teaches

Read each file once for the shape, then again looking for the **one idea** listed.

| File | Lines | The idea to take away |
| --- | --- | --- |
| `app/tenancy.py` | 107 | **Tenant isolation by design.** Every read and write of household data goes through one `Tenant` class that adds the `household_id` filter, so a route can't forget it. Another household's row gives the same 404 as a missing row (no leak that it exists). |
| `tests/test_isolation.py` | 176 | The **safety net**: a harness that tries every household-scoped route as household B against A's data. Mention it in the same breath as `tenancy.py`. |
| `app/rules/engine.py` | 102 | The Redis **merge window** (a Lua script, so check-and-set is atomic) and the per-member checks: enabled, threshold, repeat, cooldown. |
| `app/alerts/delivery.py` | 319 | **At-most-once delivery:** the atomic claim, per-channel Delivery rows, results committed one by one, plain results returned from send tasks (to avoid a race on shared ORM rows). |
| `app/alerts/sweep.py` | 91 | The sweep that marks stuck alerts `failed` after 15 minutes, so a crash is visible. It also removes expired photos. |
| `app/security.py` | 79 | JWT with a `tv` (token version) claim: a password reset bumps the version and kills every old session. Hashed device keys. |
| `app/ratelimit.py` | 63 | Rate limits in Redis. Note `by_ip`: behind a proxy it needs trusted `X-Forwarded-For` (a real bug found in review). |
| `app/live/router.py` | 104 | The WebSocket feed over Redis pub/sub, one shared connection per tab. |
| `edge/src/ha_edge/detector.py` | 136 | Per-sound thresholds, a **+0.20 boost while sound plays on the speakers**, one-window peak hold, 2 s debounce. |
| `edge/src/ha_edge/labels.py` | 124 | Label rules (`min_score`, `unless_stronger`) that came from **live testing**: they fixed the missed doorbell and cut glass false alarms. |
| `app/rules/incidents.py` | 172 | Combining sensors in a room: a 30 s Redis window per room, `SET NX EX 60` so a rule fires once, incidents are follow-ups that never delay normal alerts. |
| `app/snapshots/store.py` | 243 | Photos encrypted with Fernet, 300 KB and 640 px limits, retention (1/7/30 days), a 200 MB cap, files deleted only after the database commit. |
| `edge/src/ha_edge/camera/pose_rules.py` | 183 | Fall = upright to horizontal with the hips dropping inside about a second; lying still = horizontal and motionless for two minutes. It refuses to judge when the body isn't visible. |
| `edge/src/ha_edge/camera/runner.py` | 419 | The live loop: only run the object model while guarding, client cooldowns, recovery when the camera is busy, shutdown that waits for in-flight inference. |
| `deploy/deploy.sh` | 274 | Deploy with a lock, fast-forward only, migrate as a separate step, health check, **rollback that skips migrate**. |
| `deploy/Caddyfile` | 49 | HTTPS reverse proxy, `/metrics` hidden, secrets redacted from error logs. |

## 4. Ideas in plain words

- **JWT:** a signed login token. The server doesn't store sessions; it checks the signature. Ours carries a `tv` number so we can revoke everything by bumping it.
- **Device key:** a random secret a sensor gets once when it pairs with a single-use 8-character code. Only its SHA-256 hash is stored.
- **Celery worker vs beat:** the worker runs jobs (deliver alerts). Beat is the scheduler that queues periodic jobs (offline check every 60 s, sweeps every 5 min). Exactly one beat; many workers are safe because of the atomic claim.
- **Atomic claim:** `UPDATE ... SET status='delivering' WHERE status='pending'`. The database lets only one caller win, so two workers can't send the same alert.
- **Commit before enqueue:** otherwise a worker could look for an alert row that isn't visible yet.
- **Redis pub/sub:** the server publishes a message; every connected WebSocket for that household or member receives it. It is why live fan-out works with more than one API process.
- **Lua script in Redis:** runs as one atomic step on the Redis server, so "check if a recent report exists and update the loudest score" can't race.
- **Cooldown vs delivery:** a cooldown limits how often a person is alerted. Delivery semantics is about what happens to one alert if something crashes. Don't mix them up.
- **Fernet:** symmetric encryption (AES with a built-in integrity check). One key from the environment. Lose the key, lose the photos.
- **CSP:** a header that tells the browser what scripts and images it may load. Ours forbids inline scripts, which is why the dashboard needs no framework tricks.
- **Reverse proxy (Caddy):** the only thing exposed to the internet. It gets the HTTPS certificate, hides internal ports, and passes the real client address in `X-Forwarded-For`.
- **Forward-only migrations:** a database change isn't undone on rollback, so schema changes must be additive; a rollback skips the migrate step.

## 5. Twenty-five self-test questions (answer out loud, then check)

1. **Where does the ML model run, and why?** On the device. Privacy (audio never leaves), bandwidth, cost, latency. Trade-off: weaker hardware and updating models means updating clients.
2. **Why FastAPI, Postgres, Redis and Celery?** Async I/O and WebSockets; relational transactions for claims; Redis for short-lived counters, locks and pub/sub; Celery so delivery happens off the request path with retries.
3. **How do you stop one household reading another's data?** One `Tenant` class adds the household filter to every query, other households' rows return the same 404 as missing ones, and an isolation test harness tries every route as household B.
4. **How do devices authenticate?** Single-use, 10-minute pairing code gives a random device key. Only its hash is stored. Deleting the device revokes the key.
5. **What does a password reset do to existing sessions?** Bumps `token_version`, so every JWT dies, and revokes agent tokens.
6. **At most once or at least once delivery? Why?** At most once. Atomic claim; a crash leaves `delivering`; a sweep marks it `failed` at 15 minutes. A duplicate 2 am alarm is worse than a rare visible loss.
7. **What if the job queue is down?** Fall back to delivering in-process. Safe because the atomic claim prevents a double send.
8. **One scream, three sensors. How many alerts?** One. A 5-second Redis merge window keeps the loudest report, and a stricter member can still be alerted if a closer sensor hears it louder.
9. **How do repeat and cooldown rules work?** Per member and per sound: "5 barks in 60 seconds", then a cooldown before the next alert.
10. **How do you know a sensor died?** Heartbeats every 60 s. Offline after 180 s. A guard avoids reporting everything offline just because the server restarted.
11. **How does the live dashboard work?** One shared WebSocket, Redis pub/sub, reconnect with backoff, events deduplicated by id, catch-up of missed alerts after a reconnect.
12. **Why no frontend framework?** Small surface, strict CSP without inline scripts, no build step. All server text is inserted as text, never HTML.
13. **Which sound model, and how good is it?** YAMNet (AudioSet, TFLite, about 3.4 ms per window). On ESC-50, F1 about 0.88 for dog and 0.83 for glass. Scream, smoke alarm and doorbell are unmeasured. Say so.
14. **Tell me about a time testing changed your design.** Live test: a chime doorbell was missed ("Chime") and a ding-dong falsely scored glass. Label rules fixed it: glass precision 0.85 to 0.94, with recall down from 0.82 to 0.75, a trade-off you accepted.
15. **What if the laptop plays a movie with a scream in it?** A probe detects sound on the speakers and raises thresholds by 0.20. Imperfect: it doesn't see headphones.
16. **How is a camera privacy-safe?** Video is analysed on the device and never leaves it. One JPEG per alert (640 px, at most 300 KB), Fernet-encrypted, kept 1, 7 or 30 days, 200 MB cap, members only, per-camera switch, consent tick, bedrooms default off.
17. **How do incidents work?** Sensors belong to rooms. A per-room Redis window; glass plus a person within 15 s makes "Possible break-in". `SET NX EX 60` stops double firing. Incidents are follow-ups that never delay the normal alerts.
18. **How does fall detection work and how good is it?** Pose torso angle and hip drop in about a second. Tested only on synthetic positions. A laptop webcam sees too little of the body. Experimental.
19. **Why is weapon detection off by default?** In your own test it fired on a phone and a hand. It needs a "held by a hand" check, sustained detection and measured precision.
20. **How do you deploy and roll back?** `deploy.sh`: lock, fast-forward, build, migrate, up, health check. On failure it restarts the old containers without migrating. Migrations are forward-only, so they must be additive.
21. **What could go wrong with a reverse proxy and rate limits?** Every client looks like the proxy unless real client addresses are passed; Caddy sets `X-Forwarded-For` itself, and you verified forged headers can't pick the key.
22. **What did the security reviews find in the deployment?** Caddy's error log recorded tokens and device keys; a public `/metrics`; a restore that could empty the live database; a secrets generator that wiped settings.
23. **How do backups work?** Nightly `pg_dump`, private files, retention, a timeout, and a restore into a scratch database that you actually tested. Photos are excluded; the key must be stored separately.
24. **What did you not finish, and what would you do next?** Phone sensors (needs HTTPS, now available), a "real / false alarm" button to measure accuracy, more measurement for scream, falls and weapons.
25. **Did you build this yourself?** Be honest: you set the direction and constraints and used AI agents to implement under tests, independent reviews and live testing. Say what you personally decided and checked.

## 6. The honest limits sheet (say these before you're asked)

- Scream detection is **unmeasured**; weapon, fall and lying-still are **experimental** and untested on real people.
- Phone sensors are **designed, not built**.
- It runs on **one free VM**, with no redundancy; the no-card fallback is untested.
- Migrations are **forward-only**; rollbacks cover the application containers only.
- Some parts were built with AI assistance, and you can explain the files listed in section 3.

## 7. Outside resources (official docs, by name)

- **FastAPI:** the tutorial and "Advanced User Guide" on `fastapi.tiangolo.com` (dependencies, security, WebSockets, background tasks).
- **SQLAlchemy 2.0 async:** `docs.sqlalchemy.org` (the asyncio extension and the unified tutorial).
- **Celery:** `docs.celeryq.dev` ("First steps", "Periodic tasks", "Task retries").
- **Redis:** `redis.io/docs` (Lua scripting, pub/sub, `SET` with `NX` and `EX`).
- **Postgres:** `postgresql.org/docs` (transactions, `UPDATE ... RETURNING`, `pg_dump`).
- **OWASP:** the "Authorization", "Multi-Tenant Application Security" and "Logging" cheat sheets on `cheatsheetseries.owasp.org`.
- **Caddy:** `caddyserver.com/docs` (reverse proxy, automatic HTTPS).
- **Book chapters** that match your answers: *Designing Data-Intensive Applications* by Martin Kleppmann: delivery guarantees and idempotence, transactions.

## 8. Practice routine

1. Say the 60-second pitch out loud until it sounds natural.
2. Draw the map (section 2) from memory and walk one alert through it.
3. Pick five questions from section 5, answer them **without looking**, then compare.
4. Run a mock interview with the assistant: "Mock-interview me on Home Awareness", one question at a time, with feedback.
