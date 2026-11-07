# Plan 5a: Camera Support — Server Foundations

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The server and dashboard side of camera sensors: rooms, camera devices with consent, Away mode and night hours, camera event types, encrypted snapshots with retention, combined incidents, and snapshots on every alert channel.

**Architecture:** Extends the existing FastAPI app. Cameras are devices (kind `camera` or capability `video`) that send the same events, optionally with a JPEG snapshot (multipart). Snapshots are Fernet-encrypted on a volume and swept by retention. A correlation step after `process_event` turns related events in the same room into a follow-up incident alert.

**Tech Stack:** As the server (FastAPI, SQLAlchemy async, Alembic, Redis, Celery); `cryptography` (Fernet); `python-multipart`.

**Spec:** `docs/superpowers/specs/2026-11-08-camera-sensors-design.md` (§2 privacy, §3 consent, §4 detections, §5 incidents, §6 fit, §12 settled calls).

**Later plans:** 5b — `ha-camera` client (laptop/USB/Pi). 5c — local HTTPS (Caddy) + Android sensor mode (microphone, camera).

## Global Constraints
- Video never reaches the server; only one JPEG per alerting event, longest side ≤ 640 px, ≤ 300 KB (larger → image dropped, event kept).
- Snapshots: Fernet-encrypted with env `SNAPSHOT_KEY` (urlsafe base64 32-byte key; startup error if set but invalid; if unset, the server accepts events but drops images and logs once). Stored under `SNAPSHOT_DIR` (default `/data/snapshots`, compose volume). Never logged; never in metrics labels.
- Retention per household: 1, 7 (default) or 30 days; household cap 200 MB, evict oldest on overflow.
- Only household members can fetch a snapshot; fetch URL needs auth (Bearer JWT or agent token) — the dashboard fetches it as a blob; Telegram/ntfy get the bytes uploaded, never a URL.
- Every new household-scoped by-id route is added to the isolation harness in the same commit (`tests/test_isolation.py`).
- Existing JSON `POST /device/events` stays unchanged for current sensors.
- Ingredient events always alert immediately through the existing pipeline; incidents are follow-up alerts, never a delay.
- Commits end with the Co-Authored-By line the implementer's harness gives it. Never push.

## Review Focus
1. **Corrupt or non-JPEG upload** → event stored, image dropped, 202 (Task 3).
2. **Snapshot of household A fetched by household B or by a removed member** → 404 (Task 3).
3. **Retention reduced from 30 → 1 day** → older snapshots deleted on the next sweep, alert rows keep working without image (Task 3).
4. **Glass + person 16 s apart, or in different rooms** → no incident (Task 4).
5. **Away toggled while a camera is mid-interval** → next heartbeat returns the new `guarding`; night hours evaluated in the household timezone across midnight (e.g. 23:00–06:00) (Task 2).

---

### Task 1: Rooms, camera devices, capabilities, consent
**Files:** `app/rooms/{__init__,models,schemas,router}.py` (new), `app/devices/{models,schemas,router}.py`, `app/main.py`, migration `0015_rooms_cameras.py`, tests `tests/test_rooms.py`, `tests/test_isolation.py`.
**Interfaces — Produces:**
- `Room(id, household_id FK cascade, name String(60), kind Literal["common","entrance","bedroom","bathroom","outdoor","other"], created_at)`; unique (household_id, name).
- `Device` gains `room_id FK rooms ON DELETE SET NULL nullable`, `capabilities: str` ("audio", "video" or "audio,video"; default "audio"), `send_snapshots: bool` (default true; forced default false when assigned to a bedroom/bathroom room at camera creation), `consent_confirmed_at`, `consent_confirmed_by FK members SET NULL`. Kind check adds `'camera'`.
- Routes: `GET/POST /rooms`, `PATCH/DELETE /rooms/{id}` (owner for create/rename/delete), `PATCH /devices/{id}` `{name?, room_id?, send_snapshots?(owner), consent_confirmed?(true only)}`; `POST /devices/pairing-codes` accepts kind `camera` and `capabilities`; a `video` device cannot be paired unless the pairing code was created with `consent_confirmed: true` (422 "confirm everyone on camera knows").
- `GET /devices` adds `room_id`, `room_name`, `capabilities`, `send_snapshots`, `consent_confirmed_at`.
- [ ] Tests: create/list rooms; member can't create (403); duplicate name 409; delete room → devices' room_id null; camera pairing requires consent; bedroom camera defaults send_snapshots false; member can't flip send_snapshots (403); isolation entries for `/rooms/{a_room_id}` (PATCH, DELETE) and `/devices/{a_device_id}` PATCH; listing `/rooms`.
- [ ] RED → implement → GREEN (`uv run pytest -q && uv run ruff check .`; migration up/down/up + `alembic check`) → commit `feat: rooms, camera devices and consent`.

### Task 2: Camera event types, Away mode, night hours, guarding
**Files:** `app/catalog.py`, `app/households/{models,schemas,router}.py`, `app/devices/{router,schemas}.py`, `app/rules/effective.py`, migration `0016_guarding.py`, tests `tests/test_guarding.py`.
**Interfaces — Produces:**
- Catalog entries (exact): `fall` "Fall" group "Health" default on, reliability "experimental", severity urgent, cooldown 120; `lying_still` "Lying still (possible collapse)" Health, on, experimental, urgent, cooldown 600; `person_away` "Person while guarding" Security, on, good, urgent, cooldown 300; `weapon_seen` "Weapon seen" Security, **off**, experimental, urgent, cooldown 300; incidents (group "Incidents", on, severity urgent, cooldown 300, reliability "experimental"): `incident_break_in` "Possible break-in", `incident_fall_confirmed` "Fall confirmed", `incident_property_damage` "Possible property damage". Incident keys are never accepted from devices (422 on `POST /device/events`).
- `Household` gains `away: bool` (default false), `night_start: time|None`, `night_end: time|None`, `snapshot_retention_days: int` (1|7|30, default 7).
- `def is_guarding(household, now_utc: datetime) -> bool` in `app/households/guarding.py`: away OR now (in household timezone) within [night_start, night_end) with wrap past midnight.
- Routes: `PATCH /households/me {away}` any member; `PATCH /households/me {night_start, night_end, snapshot_retention_days}` owner (combined with existing timezone PATCH rules); away changes publish `{"kind":"guarding","away":bool,"by_email"}` to the household live channel.
- `DeviceConfigOut` adds `guarding: bool`.
- [ ] Tests: catalog exact keys/groups/defaults; device posting `incident_break_in` → 422; member toggles away (200) and owner-only fields 403 for member; `is_guarding` parametrized incl. 23:00–06:00 at 05:59 / 06:00 / 22:59 / 23:00 in Asia/Kolkata and a DST zone; heartbeat returns guarding; live message published.
- [ ] RED → implement → GREEN → commit `feat: camera event types, Away mode and night hours`.

### Task 3: Encrypted snapshots, retention, delivery with photos
**Files:** `app/snapshots/{__init__,store,router}.py` (new), `app/events/{router,models}.py`, `app/alerts/{delivery,telegram,ntfy,sweep}.py`, `app/config.py`, `docker-compose.yml` (volume + `SNAPSHOT_KEY`), `.env.example`, README, migration `0017_snapshots.py`, tests `tests/test_snapshots.py`.
**Interfaces — Produces:**
- `Snapshot(id, household_id FK cascade, event_id FK events cascade unique, size_bytes, created_at, path)`; `Event.snapshot_id` relationship via Snapshot.event_id.
- `store.save(household_id, event_id, jpeg: bytes) -> Snapshot | None` (validates JPEG magic + Pillow-free size check via header parse or `PIL` if added — implementer's choice, keep dependency-light; downscale not done server-side: reject > 300 KB); `store.load(snapshot) -> bytes`; `store.delete(snapshot)`; `store.enforce_cap(household_id)`.
- `POST /device/events` accepts `multipart/form-data` with part `event` (same JSON schema) and optional `snapshot` (image/jpeg); only devices with capability `video` and `send_snapshots` true keep the image.
- `GET /snapshots/{event_id}` → `image/jpeg` for members (Tenant-scoped 404 otherwise), `Cache-Control: private, no-store`.
- Live `event` and `alert` messages add `has_snapshot: bool` (+ `event_id` already present). Telegram: `sendPhoto` with caption = existing alert text when a snapshot exists (falls back to sendMessage on failure). ntfy: attachment upload (PUT with `Filename: snapshot.jpg`, `Message` header = text) when a snapshot exists.
- Sweep (existing 5-min job) deletes snapshots older than each household's retention.
- [ ] Tests: multipart event with JPEG → stored encrypted (file bytes ≠ JPEG, decrypt equals); non-JPEG/oversize → event 202, no snapshot; audio-only device's image dropped; GET by member 200, other household 404 (isolation entry `GET /snapshots/{a_event_id}`), no auth 401; retention 1 day deletes older; cap eviction; Telegram sendPhoto called with caption (respx), ntfy attachment headers; missing SNAPSHOT_KEY → images dropped, warning once.
- [ ] RED → implement → GREEN → commit `feat: encrypted alert snapshots with retention`.

### Task 4: Combined incidents
**Files:** `app/rules/incidents.py` (new), `app/events/router.py` (call after commit+enqueue), tests `tests/test_incidents.py`.
**Interfaces — Produces:**
- `RULES` (exact):
  - `incident_break_in`: (`glass_breaking`) + (`person_away` or any camera `fall`/`person` event — use `person_away`) within 15 s, same room.
  - `incident_fall_confirmed`: `fall` + (`thud_fall` or `scream`) within 10 s, same room.
  - `incident_property_damage`: (`explosion` or `door_slam` or `glass_breaking`) + `person_away` within 15 s, same room, while guarding.
- `async def check_incidents(session, redis, event: Event, now: float) -> list[int]` — keeps a per-room Redis sorted set of recent event (key, id, confidence, has_snapshot) for 30 s; when a rule completes, creates a synthetic `Event` (sound_key = incident key, device_id = the camera event's device, confidence = max of ingredients, merged=false, snapshot = highest-confidence ingredient with a snapshot) and runs the normal `process_event` on it (so per-member enablement/cooldowns apply), returning alert ids to enqueue after commit. Each rule fires at most once per room per 60 s.
- Devices without a room never participate.
- [ ] Tests: glass then person_away 5 s later same room → one `incident_break_in` alert with the camera's snapshot; reversed order also works; 16 s apart → none; different rooms → none; no room → none; fall + scream → fall confirmed; repeat within 60 s → no second incident; members who disabled the incident key aren't alerted; ingredient alerts still delivered immediately.
- [ ] RED → implement → GREEN → commit `feat: combined incidents across sensors in a room`.

### Task 5: Dashboard — rooms, cameras, Away, snapshots
**Files:** `app/web/static/js/views/{sensors,household,overview,alert,rooms}.js`, `app/web/static/js/app.js` (nav), `styles.css`, tests `tests/web/test_camera_ui.py`.
- Rooms page (`#/rooms`): list/create/rename/delete (owner), devices per room, assign device to room.
- Sensors page: "Add a camera" (kind camera, room select, consent checkbox required, bedroom/bathroom notice), camera cards show room, snapshots switch (owner), consent line.
- Overview header: **Away** toggle (any member) with guarding state; household page: night hours + photo retention (owner).
- Alert screen and live events: show the snapshot (fetched with auth as a blob → object URL; revoked on close); "No photo" when absent.
- [ ] Playwright tests: create room + assign; add camera requires consent; Away toggle updates for a second tab via live message; alert with snapshot shows an `<img>` whose src is a `blob:` URL; member sees no owner controls; 360 px no overflow on new pages.
- [ ] RED → implement → GREEN → commit `feat(web): rooms, cameras, Away mode and alert photos`.

---
## Self-review notes
Spec §2 → T3; §3 → T1+T5; §4 catalog → T2 (detection itself is Plan 5b/5c); §5 → T4; §6 rooms/away/alerts/storage → T1–T5; §12 calls 1–9 → T1–T4. Phones/HTTPS → Plan 5c.
