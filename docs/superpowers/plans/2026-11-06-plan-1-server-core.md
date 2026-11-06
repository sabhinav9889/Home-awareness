# Plan 1: Server Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A multi-household FastAPI backend that pairs sensors, ingests sound events and heartbeats, applies each member's rules, and delivers alerts by Telegram, ntfy, and WebSocket, runnable with `docker compose up` and exercised end to end by a fake sensor script.

**Architecture:** One FastAPI app split into feature packages (`auth`, `households`, `devices`, `events`, `rules`, `alerts`, `live`). PostgreSQL holds all durable state; every household-owned row carries `household_id` and is reached only through one tenancy helper. Redis holds rate-limit counters, merge windows, repeat counters, cooldowns, and live pub/sub. Alert delivery runs behind a `JobRunner` interface with a Celery implementation (Oracle) and an in-process one (Render fallback).

**Tech Stack:** Python 3.12, uv, FastAPI, Pydantic v2 + pydantic-settings, SQLAlchemy 2 async + asyncpg, Alembic, redis-py asyncio, Celery 5 (Redis broker) with beat, PyJWT, pwdlib[argon2], httpx, structlog, prometheus-client, pytest + pytest-asyncio + fakeredis + respx, ruff, Docker Compose, GitHub Actions.

**Spec:** `docs/design.md` (read sections 3, 5, 6, 7, 9, 10, 13 before starting).

**Later plans (not this one):** Plan 2 laptop edge client and alert agent (YAMNet, mic selection, accuracy table); Plan 3 dashboard and phone sensor page; Plan 4 deployment (Oracle, Caddy, DuckDNS, backups, Render/Neon/Upstash fallback).

## Global Constraints

- $0: only free, open-source dependencies; no paid APIs.
- Raw audio never reaches the server. The event payload is only `type`, `confidence`, `occurred_at` (device comes from the key). Reject any extra fields (`extra="forbid"`).
- Every household-owned table has a non-null `household_id` FK with `ON DELETE CASCADE`; every read and write of such a table goes through `app/tenancy.py`.
- All config from environment variables via `app/config.py`; no host-specific code paths except the `JOB_RUNNER` switch (`celery` | `inprocess`).
- Redis is used for cooldowns, merge windows, repeat counters, rate limits, and pub/sub only — never touched by the heartbeat path except its rate limit (one `INCR`).
- Default-on sounds: `scream`, `glass_breaking`, `smoke_alarm`. All others default off.
- Defaults: repeat 1 within 60 s, cooldown 120 s; `dog_barking` repeat 5 within 60 s. Merge window 5 s. Heartbeat interval 60 s; device offline after 180 s without one.
- Rate limits: signup/login/pair/password-reset 10 per minute per IP; events 60 per minute per device; heartbeats 4 per minute per device. Over limit → HTTP 429.
- Emails lowercased and stripped before storage and lookup.
- Every user-facing API description and the README carry: "Home-awareness tool. Not a medical device, security alarm, or emergency service."
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Device clock skew:** an event with `occurred_at` more than 10 minutes from server time → 422, not stored; within that window it is stored, and all rule timing uses server receive time. (Task 7)
2. **Revoked device keeps sending:** a deleted device's key gets 401 on its very next request, events and heartbeats alike. (Task 6)
3. **Removed member's open WebSocket:** removing a member or deleting the household closes their live connections within one message; they receive nothing afterward. (Task 10)
4. **Telegram down or bot blocked:** delivery retries 3 times with backoff, then the delivery is recorded `failed` with the error; the member's other channels still deliver. (Task 9)
5. **Same email twice, different case:** `A@x.com` then `a@x.com ` → 409 on the second signup and login works with either spelling. (Task 3)

---

## File Structure

```
pyproject.toml                      deps, ruff, pytest config
Dockerfile                          one image: api, worker, beat
docker-compose.yml                  db, redis, api, worker, beat
.env.example
alembic.ini
migrations/                         env.py (async), versions/
.github/workflows/ci.yml
scripts/fake_sensor.py              pairs, heartbeats, sends events (manual e2e)
app/
  main.py          create_app(), lifespan, routers, /health, /metrics
  config.py        Settings
  db.py            Base, engine, get_session
  redis.py         get_redis
  tenancy.py       Tenant (scoped select/get/add/delete)
  ratelimit.py     rate_limit dependency factory
  security.py      password hashing, JWT, token generation/hashing
  logging.py       structlog setup
  metrics.py       prometheus counters
  email.py         EmailSender protocol, ConsoleEmailSender, SmtpEmailSender, get_email_sender
  auth/            models.py (Member, EmailToken), schemas.py, router.py, deps.py
  households/      models.py (Household, Invite), schemas.py, router.py
  catalog.py       SOUNDS, get_sound
  rules/           models.py (SoundSetting, AlertPreference), schemas.py, router.py, effective.py, engine.py
  devices/         models.py (Device, PairingCode), schemas.py, router.py, deps.py, offline.py
  events/          models.py (Event), schemas.py, router.py
  alerts/          models.py (Alert, Delivery), jobs.py, delivery.py, telegram.py, ntfy.py, celery_app.py, router.py
  live/            hub.py, router.py
tests/
  conftest.py      app, client, db session (rollback), fakeredis, capturing email, recording jobs, factories
  test_<feature>.py
```

---

### Task 1: Skeleton, test harness, Compose, CI

**Files:**
- Create: `pyproject.toml`, `.env.example`, `.gitignore`, `docker-compose.yml` (db + redis only for now), `alembic.ini`, `migrations/env.py`, `app/__init__.py`, `app/main.py`, `app/config.py`, `app/db.py`, `app/redis.py`, `app/logging.py`, `tests/conftest.py`, `tests/test_health.py`, `.github/workflows/ci.yml`

**Interfaces:**
- Produces:
  - `Settings` fields: `database_url: str`, `redis_url: str`, `jwt_secret: str`, `jwt_ttl_minutes: int = 60*24`, `public_base_url: str = "http://localhost:8000"`, `job_runner: Literal["celery","inprocess"] = "celery"`, `email_backend: Literal["console","smtp"] = "console"`, `smtp_host/smtp_port/smtp_user/smtp_password/smtp_from: str | None`, `telegram_bot_token: str | None`, `telegram_bot_username: str | None`, `telegram_webhook_secret: str | None`, `ntfy_base_url: str = "https://ntfy.sh"`. `get_settings() -> Settings` (lru_cache).
  - `app.db`: `class Base(DeclarativeBase)`, `async def get_session() -> AsyncIterator[AsyncSession]`.
  - `app.redis`: `async def get_redis() -> Redis`.
  - `app.main.create_app() -> FastAPI`.
  - `tests/conftest.py` fixtures: `session` (AsyncSession inside an outer transaction rolled back after each test; uses `TEST_DATABASE_URL`, creates tables once per run via `Base.metadata.create_all`), `redis` (`fakeredis.aioredis.FakeRedis`), `client` (`httpx.AsyncClient` over `ASGITransport`, with `get_session` and `get_redis` overridden). Later tasks add fixtures here.

- [ ] **Step 1: Write the failing test** `tests/test_health.py`

```python
async def test_health_reports_db_and_redis(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok", "redis": "ok"}
```

- [ ] **Step 2: Run it** — `docker compose up -d db redis && uv run pytest tests/test_health.py -v` → FAIL (no app).
- [ ] **Step 3: Implement** the files above. `/health` runs `SELECT 1` and `PING`. pytest config: `asyncio_mode = "auto"`. Compose: `postgres:16-alpine` (db `home`, plus `home_test` created by an init SQL file), `redis:7-alpine`, both with healthchecks. CI: ubuntu, Python 3.12 via `astral-sh/setup-uv`, postgres and redis services, `uv run ruff check . && uv run pytest -q`.
- [ ] **Step 4: Run** `uv run pytest -q && uv run ruff check .` → PASS.
- [ ] **Step 5: Commit** `chore: project skeleton, health endpoint, test harness, CI`.

---

### Task 2: Households, members, signup, email confirmation, login

**Files:**
- Create: `app/security.py`, `app/email.py`, `app/households/models.py`, `app/auth/models.py`, `app/auth/schemas.py`, `app/auth/router.py`, `app/auth/deps.py`, `app/ratelimit.py`, `migrations/versions/0001_households_members.py`, `tests/test_auth.py`

**Interfaces:**
- Produces:
  - Models: `Household(id: int, name: str, created_at)`. `Member(id, household_id FK cascade, email unique, password_hash, role: "owner"|"member", email_confirmed: bool=False, created_at)`. `EmailToken(id, member_id FK cascade, purpose: "confirm"|"reset", token_hash unique, expires_at, used_at nullable)`.
  - `app.security`: `hash_password(p: str) -> str`, `verify_password(p: str, h: str) -> bool`, `create_access_token(member_id: int, household_id: int) -> str`, `decode_access_token(t: str) -> TokenClaims` (raises `InvalidToken`), `new_token(prefix: str) -> str` (`prefix + secrets.token_urlsafe(32)`), `hash_token(t: str) -> str` (sha256 hex).
  - `app.email`: `class EmailSender(Protocol): async def send(self, to: str, subject: str, body: str) -> None`; `ConsoleEmailSender` (logs), `SmtpEmailSender` (aiosmtplib), `get_email_sender() -> EmailSender`. Test fixture `outbox` overrides it with a list-capturing sender.
  - `app.ratelimit.rate_limit(scope: str, limit: int, per_seconds: int, key: Callable[[Request], str]) -> Depends` — fixed window `INCR rl:{scope}:{key}:{window}` + `EXPIRE`; raises 429. `by_ip(request) -> str`.
  - `app.auth.deps.current_member(...) -> Member` (Bearer JWT; 401 if invalid, member gone, or unconfirmed).
  - Endpoints: `POST /auth/signup {email, password(min 10), household_name}` → 201 `{member_id, household_id}`; sends confirm email containing `{public_base_url}/confirm?token=...`. `POST /auth/confirm {token}` → 204. `POST /auth/login {email, password}` → 200 `{access_token, token_type:"bearer"}`; 401 bad creds; 403 `{"detail":"email not confirmed"}`. `GET /auth/me` → `{id, email, role, household_id}`. Confirm tokens expire after 24 h, single use.
  - Test helper fixture `signed_up(email, household_name) -> (member, headers)` that signs up, confirms through the outbox token, logs in.

- [ ] **Step 1: Write failing tests** in `tests/test_auth.py`:
  - `test_signup_creates_owner_and_household` — 201; member role `"owner"`; household name stored.
  - `test_login_requires_confirmed_email` — login before confirm → 403 `"email not confirmed"`; after confirming via the token in `outbox[0].body` → 200 with `access_token`.
  - `test_email_is_case_and_space_insensitive` — signup `"A@x.com"`, second signup `"a@x.com "` → 409; login with `"A@X.COM"` → 200.
  - `test_confirm_token_single_use_and_expiry` — second use → 400; token with `expires_at` in the past → 400.
  - `test_wrong_password_401_and_rate_limited` — 10 bad logins → 401 each, 11th → 429.
  - `test_me_requires_valid_token` — no header → 401; tampered token → 401.
- [ ] **Step 2: Run** `uv run pytest tests/test_auth.py -v` → FAIL.
- [ ] **Step 3: Implement** the interfaces. Migration via `alembic revision --autogenerate`, then review it.
- [ ] **Step 4: Run** `uv run pytest -q` → PASS.
- [ ] **Step 5: Commit** `feat(auth): households, signup, email confirmation, login`.

---

### Task 3: Password reset, invites, members, tenancy, isolation harness

**Files:**
- Create: `app/tenancy.py`, `app/households/schemas.py`, `app/households/router.py`, `migrations/versions/0002_invites.py`, `tests/test_households.py`, `tests/test_isolation.py`
- Modify: `app/households/models.py` (add `Invite`), `app/auth/router.py` (reset, invite signup)

**Interfaces:**
- Consumes: Task 2 models, `current_member`, `new_token`, `hash_token`, `EmailSender`, `rate_limit`.
- Produces:
  - `Invite(id, household_id FK cascade, token_hash unique, created_by FK member, expires_at, used_at)`; 7-day expiry.
  - `class Tenant` in `app/tenancy.py`, built by dependency `get_tenant(member=Depends(current_member), session=...) -> Tenant`:
    - `select(model) -> Select` (adds `model.household_id == hid`)
    - `async get(model, id: int) -> Model` (404 if missing **or** in another household — never 403, never leaks existence)
    - `add(obj) -> obj` (sets `obj.household_id`)
    - `async delete(model, id: int) -> None`
    - properties `household_id`, `member`.
    A second dependency `get_device_tenant` (Task 6) builds the same object from a device.
  - Endpoints: `POST /auth/password-reset/request {email}` → 202 always (sends only if exists; 1 h expiry). `POST /auth/password-reset/confirm {token, new_password}` → 204. `POST /auth/signup` also accepts `invite_token` instead of `household_name` → joins as `"member"`. `GET /households/me` → `{id, name, members:[{id,email,role}]}`. `POST /households/me/invites` (owner) → 201 `{invite_url, expires_at}`. `DELETE /households/me/members/{id}` (owner, not self) → 204. `POST /households/me/leave` (member only; owner → 400) → 204. `DELETE /households/me` (owner) → 204, cascades everything.
  - Hook for Task 10: after a member is removed/leaves or the household is deleted, call `await publish_disconnect(redis, member_ids: list[int])` from `app.live.hub` (create a no-op stub now; Task 10 implements).

- [ ] **Step 1: Write failing tests**
  - `tests/test_households.py`: `test_reset_request_is_202_for_unknown_email_and_sends_nothing`; `test_reset_changes_password_and_token_single_use`; `test_invite_signup_joins_existing_household_as_member`; `test_expired_or_used_invite_rejected` (400); `test_only_owner_invites_and_removes` (member → 403); `test_owner_cannot_leave_or_remove_self` (400); `test_delete_household_removes_members_and_rows` (afterwards the old token → 401).
  - `tests/test_isolation.py`: fixture `two_households` → `(headers_a, headers_b)`. `test_other_household_resources_are_404` parametrized over a module-level list `HOUSEHOLD_SCOPED_ROUTES: list[tuple[method, path_template, body]]`; for each, a resource created by A is requested by B → 404, and A's listing never contains B's ids. Start the list with `("DELETE", "/households/me/members/{a_member_id}", None)`. **Every later task that adds a household-scoped route appends to this list in the same commit.**
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest -q` → PASS.
- [ ] **Step 5: Commit** `feat(households): invites, membership, password reset, tenant isolation`.

---

### Task 4: Sound catalog, per-member sound settings, alert preferences

**Files:**
- Create: `app/catalog.py`, `app/rules/models.py`, `app/rules/schemas.py`, `app/rules/router.py`, `app/rules/effective.py`, `migrations/versions/0003_rules.py`, `tests/test_rules_settings.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Sound: key, label, group, default_enabled: bool, reliability: Literal["good","fair","experimental"], default_repeat: int = 1, default_window_s: int = 60, default_cooldown_s: int = 120`. `SOUNDS: dict[str, Sound]` with exactly these keys, groups, defaults and reliabilities from design §5: `scream`, `shouting`, `crying`, `moaning`, `gasping`, `coughing`, `glass_breaking`, `door_slam`, `knocking`, `car_alarm`, `dog_barking` (repeat 5), `smoke_alarm`, `siren`, `explosion`, `running_water`, `thud_fall`, `doorbell`. `get_sound(key) -> Sound` (KeyError).
  - `SoundSetting(id, household_id, member_id FK cascade, sound_key, enabled, repeat_count 1..20, window_s 5..3600, cooldown_s 0..86400)`, unique `(member_id, sound_key)`. Row exists only when a member overrides the default.
  - `AlertPreference(member_id PK FK cascade, household_id, popup: bool=True, alarm: bool=False, telegram: bool=False, telegram_chat_id: str|None, ntfy_topic: str|None)`; created with defaults at signup (modify signup in this task).
  - `effective.py`: `@dataclass EffectiveSetting(sound_key, enabled, repeat_count, window_s, cooldown_s)`; `async def effective_settings(session, member_id: int) -> dict[str, EffectiveSetting]`; `async def household_enabled_sounds(session, household_id: int) -> set[str]` (union over members); `async def members_enabled_for(session, household_id: int, sound_key: str) -> list[tuple[int, EffectiveSetting]]`.
  - Endpoints: `GET /sounds` → catalog list (public). `GET /me/sound-settings` → all 17 effective settings. `PUT /me/sound-settings/{key}` body `{enabled, repeat_count, window_s, cooldown_s}` → 200 (unknown key → 404; out of range → 422). `GET /me/alert-preferences`, `PUT /me/alert-preferences {popup, alarm, telegram, ntfy_topic}` → 200; `ntfy_topic` when set must match `^[A-Za-z0-9_-]{20,64}$` (secret-length); `telegram: true` without a linked chat → 400 `"link Telegram first"`. `POST /me/alert-preferences/ntfy-topic` → generates and stores a random topic, returns it.

- [ ] **Step 1: Write failing tests**: `test_catalog_has_17_sounds_and_three_default_on` (exact set `{"scream","glass_breaking","smoke_alarm"}`); `test_dog_barking_defaults_to_5_in_60s`; `test_effective_settings_merge_override_with_defaults`; `test_household_enabled_sounds_is_union_of_members` (A enables `dog_barking`, B disables `scream` → union contains both `scream` and `dog_barking`); `test_put_unknown_sound_404_and_bad_range_422`; `test_telegram_pref_requires_linked_chat`; `test_short_ntfy_topic_rejected`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `uv run pytest -q` → PASS.
- [ ] **Step 5: Commit** `feat(rules): sound catalog, member sound settings, alert preferences`.

---

### Task 5: Devices — pairing, device keys, list, revoke

**Files:**
- Create: `app/devices/models.py`, `app/devices/schemas.py`, `app/devices/router.py`, `app/devices/deps.py`, `migrations/versions/0004_devices.py`, `tests/test_devices.py`
- Modify: `tests/test_isolation.py` (append routes)

**Interfaces:**
- Consumes: `Tenant`, `rate_limit`, `new_token`, `hash_token`.
- Produces:
  - `Device(id, household_id FK cascade, name, kind: "laptop"|"phone"|"pi"|"other", key_hash unique, last_seen_at nullable, status: "pending"|"online"|"offline" = "pending", created_at)`.
  - `PairingCode(id, household_id, name, kind, code_hash unique, expires_at, used_at)`. Code: 8 chars from `ABCDEFGHJKMNPQRSTUVWXYZ23456789`, 10-minute expiry, single use.
  - `app.devices.deps.current_device(x_device_key: str = Header(alias="X-Device-Key")) -> Device` (lookup by `hash_token`; 401 if missing/unknown). `get_device_tenant(device) -> Tenant`.
  - Endpoints: `POST /devices/pairing-codes {name, kind}` → 201 `{code, expires_at}`. `POST /device/pair {code}` (no auth; rate limit 10/min/IP; code case-insensitive) → 201 `{device_id, device_key}` where key = `new_token("ha_dev_")`, shown once. `GET /devices` → `[{id, name, kind, status, last_seen_at}]`. `DELETE /devices/{id}` → 204.

- [ ] **Step 1: Write failing tests**: `test_pair_with_code_returns_key_once`; `test_pairing_code_single_use_expiry_and_case_insensitive`; `test_wrong_code_rate_limited` (11th → 429); `test_device_key_authenticates_and_unknown_key_401`; `test_revoked_device_key_401_immediately` (Review Focus 2: delete, then the same key on a device endpoint → 401; use `GET /device/whoami` → `{device_id, household_id}`, a small endpoint added in this task); `test_list_devices_scoped`. Append `GET /devices` listing check and `("DELETE", "/devices/{a_device_id}", None)` to `HOUSEHOLD_SCOPED_ROUTES`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `uv run pytest -q` → PASS.
- [ ] **Step 5: Commit** `feat(devices): pairing codes, device keys, list and revoke`.

---

### Task 6: Heartbeats and device config

**Files:**
- Modify: `app/devices/router.py`, `app/devices/schemas.py`
- Test: `tests/test_heartbeat.py`

**Interfaces:**
- Consumes: `current_device`, `household_enabled_sounds`, `rate_limit`.
- Produces: `POST /device/heartbeat` → 200 `{enabled_sounds: list[str] (sorted), heartbeat_interval_s: 60}`; sets `last_seen_at = now`, `status = "online"`; if it was `"offline"`, publish a `device_online` live message via `app.live.hub.publish_to_household` (stub now, Task 10 implements). Rate limit 4/min per device. `GET /device/config` → same body without touching `last_seen_at`.

- [ ] **Step 1: Write failing tests**: `test_heartbeat_marks_online_and_returns_union_config`; `test_config_reflects_setting_change_on_next_heartbeat`; `test_heartbeat_rate_limit_5th_in_minute_429`; `test_heartbeat_uses_one_redis_command` (wrap fakeredis to count calls; exactly the rate-limit `INCR` + `EXPIRE` pipeline, nothing else).
- [ ] **Step 2–4:** run → FAIL, implement, run `uv run pytest -q` → PASS.
- [ ] **Step 5: Commit** `feat(devices): heartbeats return household sound config`.

---

### Task 7: Event ingest and history

**Files:**
- Create: `app/events/models.py`, `app/events/schemas.py`, `app/events/router.py`, `migrations/versions/0005_events.py`, `tests/test_events.py`
- Modify: `tests/test_isolation.py`

**Interfaces:**
- Consumes: `current_device`, `get_device_tenant`, `Tenant`, `get_sound`, `rate_limit`.
- Produces:
  - `Event(id, household_id FK cascade, device_id FK set null, sound_key, confidence: float, occurred_at, received_at, merged: bool=False)`, index `(household_id, received_at desc)`.
  - `EventIn` (`extra="forbid"`): `type: str` (must be a catalog key → 422), `confidence: float` in [0,1], `occurred_at: datetime` (aware; |occurred_at − now| ≤ 10 min else 422).
  - `POST /device/events` → 202 `{event_id, merged, alerts: int}`; rate limit 60/min per device; stores the event, then calls `await process_event(...)` from Task 8 (stub returning `RuleOutcome(merged=False, alert_ids=[])` until then) and publishes an `event` live message (stub).
  - `GET /events?limit=50&before_id=` → newest first, max limit 200, `[{id, sound_key, label, device_name, confidence, received_at, merged}]`.

- [ ] **Step 1: Write failing tests**: `test_event_stored_with_server_receive_time`; `test_unknown_sound_type_422`; `test_extra_fields_rejected_422` (e.g. `"audio": "..."` → 422; this enforces the privacy rule); `test_clock_skew_over_10_minutes_422_and_not_stored` (Review Focus 1); `test_event_rate_limit_61st_429`; `test_history_newest_first_paginated_and_scoped`. Append `GET /events` listing to the isolation list.
- [ ] **Step 2–4:** FAIL → implement → `uv run pytest -q` PASS.
- [ ] **Step 5: Commit** `feat(events): event ingest with validation and history`.

---

### Task 8: Rule engine — merge, repeat, cooldown

**Files:**
- Create: `app/rules/engine.py`, `app/alerts/models.py`, `migrations/versions/0006_alerts.py`, `tests/test_rule_engine.py`
- Modify: `app/events/router.py` (replace stub)

**Interfaces:**
- Consumes: `members_enabled_for`, `JobRunner` (from Task 9 — define `app/alerts/jobs.py` with only the Protocol and a `RecordingJobRunner` test double now; test fixture `jobs` overrides `get_job_runner`).
- Produces:
  - `Alert(id, household_id FK cascade, member_id FK cascade, event_id FK, sound_key, created_at, status: "pending"|"sent"|"partial"|"failed")`. `Delivery(id, alert_id FK cascade, channel: "telegram"|"ntfy"|"websocket", status: "pending"|"sent"|"failed", attempts: int, last_error: str|None, updated_at)`.
  - `@dataclass RuleOutcome(merged: bool, alert_ids: list[int])`.
  - `async def process_event(session, redis, jobs: JobRunner, event: Event, now: float) -> RuleOutcome` — `now` is server time in epoch seconds (injectable for tests).

  Algorithm (fixed; tests assert each branch):
  ```
  merge_key = f"merge:{hid}:{sound}"
  holder = GET merge_key
  if holder is not None and holder != str(device_id):
      mark event.merged = True; return RuleOutcome(True, [])
  SET merge_key device_id EX 5
  for member_id, s in members_enabled_for(hid, sound):      # enabled only
      rep = f"rep:{hid}:{member_id}:{sound}"
      ZADD rep {event_id: now}; ZREMRANGEBYSCORE rep -inf now - s.window_s; EXPIRE rep s.window_s
      if ZCARD rep < s.repeat_count: continue
      if not SET f"cd:{hid}:{member_id}:{sound}" 1 NX EX max(s.cooldown_s,1): continue   # in cooldown
      DEL rep
      create Alert(pending) → jobs.enqueue_alert(alert.id)
  return RuleOutcome(False, alert_ids)
  ```
  Use one pipeline per member to keep Upstash command count low.

- [ ] **Step 1: Write failing tests** (fakeredis, explicit `now`):
  - `test_single_scream_alerts_owner_with_default_settings` → 1 alert, `jobs.enqueued == [alert_id]`.
  - `test_disabled_sound_creates_no_alert`.
  - `test_dog_barking_needs_5_within_60s` — 4 events at t=0..40 → 0 alerts; 5th at t=50 → 1; 5 events spread over t=0..240 → 0.
  - `test_cooldown_suppresses_then_expires` — scream at t=0 alerts; t=30 no alert; after cooldown elapses (advance fakeredis time or set cooldown 1 s and sleep) → alerts again.
  - `test_same_scream_on_two_devices_merges` — device A then device B within 5 s → second `merged=True`, 1 alert total.
  - `test_same_device_repeats_are_not_merged` — device A twice within 5 s → both counted (matters for repeat rules).
  - `test_each_member_gets_own_alert_and_rules` — owner default scream, member with scream repeat 3 → first scream alerts only the owner.
- [ ] **Step 2–4:** FAIL → implement → `uv run pytest -q` PASS.
- [ ] **Step 5: Commit** `feat(rules): rule engine with merge window, repeat counts, cooldowns`.

---

### Task 9: Job runners and alert delivery (Telegram, ntfy, WebSocket)

**Files:**
- Create: `app/alerts/delivery.py`, `app/alerts/telegram.py`, `app/alerts/ntfy.py`, `app/alerts/celery_app.py`, `app/alerts/router.py`, `tests/test_delivery.py`
- Modify: `app/alerts/jobs.py`

**Interfaces:**
- Produces:
  - `class JobRunner(Protocol): async def enqueue_alert(self, alert_id: int) -> None`. `CeleryJobRunner` (`deliver_alert_task.delay(alert_id)`), `InProcessJobRunner` (`asyncio.create_task(deliver_alert(alert_id))`, keeps task refs), `get_job_runner() -> JobRunner` chosen by `settings.job_runner`.
  - `async def deliver_alert(alert_id: int, *, session_factory=..., redis=..., http: httpx.AsyncClient=...) -> None`: loads alert + member's `AlertPreference`; creates one `Delivery` per enabled channel (`websocket` if `popup` or `alarm`; `telegram` if `telegram` and chat linked; `ntfy` if topic set); sends each independently; retries each failing external channel up to 3 attempts with backoff 1 s, 4 s (injectable `sleep` for tests); final status per delivery; alert status `sent` (all ok), `partial`, or `failed`.
  - Message text (exact): `"⚠️ {label} detected by {device_name} at {HH:MM} — Home Awareness"`.
  - `telegram.send_message(http, token, chat_id, text) -> None` (POST `https://api.telegram.org/bot{token}/sendMessage`; raise on non-200 or `ok:false`). `ntfy.publish(http, base_url, topic, text) -> None` (POST `{base}/{topic}`, header `Priority: high`).
  - WebSocket channel calls `app.live.hub.publish_to_member(redis, member_id, {"kind":"alert", "alert_id", "sound_key", "label", "device_name", "at", "popup": bool, "alarm": bool})` (stub until Task 10; "sent" means published).
  - `celery_app.py`: `celery = Celery("home", broker=settings.redis_url)`; task `deliver_alert_task(alert_id)` runs `asyncio.run(deliver_alert(alert_id))`.
  - `GET /alerts?limit=50` → member's own alerts with deliveries (household-scoped and member-scoped).

- [ ] **Step 1: Write failing tests** (respx for HTTP, fake `sleep`): `test_delivers_to_all_enabled_channels`; `test_telegram_retries_3_times_then_failed_other_channels_still_sent` (Review Focus 4: Telegram returns 403 `bot was blocked`; ntfy 200 → telegram delivery `failed` with error text, attempts 3; ntfy `sent`; alert `partial`); `test_message_text_exact`; `test_inprocess_runner_runs_delivery`; `test_job_runner_selected_by_setting`; `test_alerts_list_only_own`. Append `GET /alerts` to the isolation list.
- [ ] **Step 2–4:** FAIL → implement → `uv run pytest -q` PASS.
- [ ] **Step 5: Commit** `feat(alerts): job runners and multi-channel delivery with retries`.

---

### Task 10: Live hub (WebSocket + Redis pub/sub)

**Files:**
- Modify: `app/live/hub.py` (replace stubs), Create: `app/live/router.py`, `tests/test_live.py`

**Interfaces:**
- Produces:
  - `WS /ws?token=<JWT>` — rejects invalid token with close code 4401. On connect subscribes to Redis channels `live:member:{member_id}` and `live:household:{household_id}`; forwards JSON messages.
  - `async def publish_to_member(redis, member_id: int, msg: dict) -> None`; `async def publish_to_household(redis, household_id: int, msg: dict) -> None`; `async def publish_disconnect(redis, member_ids: list[int]) -> None` → publishes `{"kind":"disconnect"}` to each member channel; the socket handler closes with code 4403 on receiving it.
  - Message kinds: `event`, `alert`, `device_offline`, `device_online`, `disconnect`.
- [ ] **Step 1: Write failing tests** (Starlette `TestClient` websocket with fakeredis shared): `test_ws_rejects_bad_token_4401`; `test_ws_receives_event_published_to_household`; `test_ws_receives_alert_only_for_own_member`; `test_removed_member_socket_closed_4403` (Review Focus 3: owner removes member → member's open socket closes, then a household event is not received); `test_other_household_receives_nothing`.
- [ ] **Step 2–4:** FAIL → implement → `uv run pytest -q` PASS.
- [ ] **Step 5: Commit** `feat(live): WebSocket hub over Redis pub/sub`.

---

### Task 11: Offline sensor detection

**Files:**
- Create: `app/devices/offline.py`, `tests/test_offline.py`
- Modify: `app/alerts/celery_app.py` (beat schedule every 60 s), `app/main.py` (lifespan loop every 60 s when `job_runner == "inprocess"`)

**Interfaces:**
- Produces: `async def check_offline_devices(session, redis, now: datetime) -> list[int]` — devices with `status == "online"` and `last_seen_at < now - 180 s` → set `"offline"`, publish `{"kind":"device_offline","device_id","name"}` to the household, and send each member a Telegram/ntfy message (exact text `"📴 Sensor {name} is offline — alerts from it have stopped."`) through the same channel senders as Task 9 (best effort, failures logged). Returns ids marked offline. `"pending"` devices (never sent a heartbeat) are ignored.
- [ ] **Step 1: Write failing tests**: `test_device_marked_offline_after_180s_once` (second run returns `[]`); `test_pending_device_ignored`; `test_offline_notifies_household_live_and_channels`; `test_heartbeat_after_offline_marks_online_and_publishes_device_online`.
- [ ] **Step 2–4:** FAIL → implement → `uv run pytest -q` PASS.
- [ ] **Step 5: Commit** `feat(devices): offline sensor detection and warnings`.

---

### Task 12: Telegram linking

**Files:**
- Modify: `app/alerts/router.py`, `app/rules/router.py`, Test: `tests/test_telegram_link.py`

**Interfaces:**
- Produces: `POST /me/telegram/link` → `{deep_link: "https://t.me/{bot_username}?start={code}"}` (code = `new_token("")[:24]`, stored hashed in Redis `tglink:{hash}` → member_id, EX 600). `POST /telegram/webhook/{secret}` — 404 if `secret != settings.telegram_webhook_secret`; on update text `/start <code>` stores `chat_id` in the member's `AlertPreference`, sets `telegram = True`, replies `"✅ Linked. You'll get Home Awareness alerts here."`; unknown/expired code → replies `"Link expired. Create a new one in the dashboard."`. Always returns 200 to Telegram for a valid secret. `DELETE /me/telegram` unlinks.
- [ ] **Step 1: Write failing tests** (respx): `test_link_flow_sets_chat_and_enables_telegram`; `test_wrong_webhook_secret_404`; `test_expired_code_replies_expired_and_changes_nothing`; `test_unlink_disables_telegram`.
- [ ] **Step 2–4:** FAIL → implement → `uv run pytest -q` PASS.
- [ ] **Step 5: Commit** `feat(alerts): Telegram account linking`.

---

### Task 13: Observability, full Compose stack, fake sensor, README

**Files:**
- Create: `app/metrics.py`, `Dockerfile`, `scripts/fake_sensor.py`, `README.md`, `tests/test_metrics.py`
- Modify: `app/main.py`, `app/logging.py`, `docker-compose.yml` (add `api`, `worker`, `beat`, `migrate` one-shot running `alembic upgrade head`), `.github/workflows/ci.yml` (add `docker build`)

**Interfaces:**
- Produces:
  - Counters: `ha_events_received_total{sound}`, `ha_events_merged_total`, `ha_alerts_created_total{sound}`, `ha_deliveries_total{channel,status}`, `ha_devices_offline_total`. `GET /metrics` (Prometheus text). Request logging middleware: JSON line with `method, path, status, duration_ms, request_id` (no bodies, no tokens).
  - `scripts/fake_sensor.py --base-url URL --code PAIRCODE [--sound scream] [--count 1] [--interval 1]` — pairs, heartbeats every 60 s, sends events; prints responses. Saves the device key to `.fake_sensor_key`.
  - README: what it is + disclaimer line, the architecture image `docs/architecture.png`, quick start (`cp .env.example .env && docker compose up --build`), how to create a Telegram bot with BotFather, running tests, and a "manual end-to-end" walkthrough below.
- [ ] **Step 1: Write failing test** `test_metrics_counts_events_and_alerts` (send one scream → `ha_events_received_total{sound="scream"} 1.0` and `ha_alerts_created_total{sound="scream"} 1.0` in `/metrics` text).
- [ ] **Step 2–4:** FAIL → implement → `uv run pytest -q && uv run ruff check .` PASS.
- [ ] **Step 5: Manual end-to-end check** — `docker compose up --build -d`; sign up via `/docs`, confirm via the token printed in `docker compose logs api` (console email), log in, `POST /devices/pairing-codes`, run `uv run python scripts/fake_sensor.py --base-url http://localhost:8000 --code <CODE> --sound scream`; expected: event 202 with `alerts: 1`, `GET /alerts` shows it `sent` (websocket channel), `/metrics` shows counts. Stop the fake sensor, wait 4 minutes, `GET /devices` shows `offline`.
- [ ] **Step 6: Commit** `feat: metrics, structured logs, full compose stack, fake sensor, README`.

---

## Self-review notes

- Spec coverage for this plan's scope: sign-up/households/invites/reset (T2–3), isolation (T3 + appended per task), catalog and defaults (T4), pairing and keys (T5), heartbeats and config union (T6), events-only privacy and validation (T7), repeat/cooldown/merge (T8), channels and retries and job-runner portability (T9), live push (T10), offline warning (T11), Telegram (T12), health/metrics/logs/Compose/CI (T1, T13). Account deletion (T3). Abuse limits (T2, T5–7).
- Deferred to later plans by design: edge client, YAMNet mapping, mic handling, accuracy table (Plan 2); dashboard UI, phone sensor page, browser notifications (Plan 3); Caddy, DuckDNS, Oracle setup guide, backups, Render/Neon/Upstash deployment and restore test (Plan 4).
