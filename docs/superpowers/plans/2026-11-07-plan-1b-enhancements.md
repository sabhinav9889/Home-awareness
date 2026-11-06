# Plan 1b: Enhancements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve the merged server core (Plan 1) and the laptop edge client (Plan 2) with the highest-value items from the competitor research (`docs/research/2026-11-07-competitors.md`), the measured accuracy (`docs/accuracy.md`) and the reviewers' deferred backlog.

**Architecture:** Small, independent changes to existing modules; each task adds a migration only where stated. Server changes keep the tenancy layer, at-most-once delivery and template-only logging intact.

**Tech Stack:** As Plans 1 and 2 (FastAPI, SQLAlchemy async, Alembic, Redis, Celery; edge: httpx, httpx-ws).

**Spec:** `docs/design.md`. Inputs: `docs/research/2026-11-07-competitors.md`, `docs/accuracy.md`.

## Global Constraints

- Everything in Plan 1's and Plan 2's Global Constraints still holds (privacy: events only; $0; tenancy; template-only logs; secrets never logged; at-most-once delivery).
- Server tests: `uv run pytest -q && uv run ruff check .` at repo root (services: `REDIS_HOST_PORT=6380 docker compose up -d db redis`, `.env` exists). Edge tests: `cd edge && uv run pytest -q && uv run ruff check .`.
- Every migration: autogenerate (or hand-write check constraints), review, verify upgrade/downgrade/upgrade and `alembic check`.
- New household-scoped by-id routes are added to the isolation harness in the same commit.
- Commits end with the Co-Authored-By line the implementer's harness gives it.

## Review Focus

1. **Invalid timezone strings** (`"Mars/Base"`, `""`, offsets) → 422; existing households keep working with `UTC`. (Task 1)
2. **A member lowers a sound's confidence below the floor or above 1** → 422; the sensor never receives a threshold outside [0.05, 0.95]. (Task 2)
3. **Agent token leaked** → revocable individually; revoked token closes the socket (4401) on next connect; agent tokens can't call any route except `/ws` and `POST /alerts/{id}/ack`. (Task 4)
4. **Password reset** → all existing JWTs and agent tokens for that member stop working. (Task 5)
5. **Stale sweep never touches an alert that's legitimately being delivered right now** (< 15 min old). (Task 6)

---

### Task 1: Household timezone in alert texts

**Files:** Modify `app/households/models.py`, `app/households/schemas.py`, `app/households/router.py`, `app/auth/schemas.py` + signup, `app/alerts/delivery.py`, `app/devices/offline.py` (message times if any); Create `migrations/versions/0009_household_timezone.py`; Tests `tests/test_timezone.py`.

**Interfaces — Produces:**
- `Household.timezone: str` (IANA name, NOT NULL, server default `'UTC'`), validated with `zoneinfo.ZoneInfo` (unknown → 422).
- Signup accepts optional `timezone` (default `"UTC"`). `GET /households/me` includes `timezone`. `PATCH /households/me {timezone}` (owner only; member → 403).
- Alert text time: `"{HH:MM} {TZ}"` where TZ is the zone's abbreviation at that instant (e.g. `IST`, `UTC`, `GMT+5:30`-style fallback when the abbreviation is numeric). Exact alert text becomes `"⚠️ {label} detected by {device_name} at {HH:MM} {TZ} — Home Awareness"`. Test alert text unchanged.
- [ ] Tests: `test_default_timezone_is_utc`; `test_signup_with_asia_kolkata`; `test_invalid_timezone_422` (parametrize "Mars/Base", "", "+05:30"); `test_only_owner_changes_timezone`; `test_alert_text_uses_household_local_time` (02:14 IST case: created_at 20:44 UTC → "02:14 IST").
- [ ] RED → implement → GREEN → commit `feat(households): per-household timezone for alert times`.

### Task 2: Per-sound confidence thresholds (from measured accuracy)

**Files:** Server: `app/catalog.py`, `app/rules/models.py` (+`min_confidence`), `app/rules/schemas.py`, `app/rules/effective.py`, `app/devices/router.py` (config), migration `0010_min_confidence.py`, tests `tests/test_thresholds.py`. Edge: `edge/src/ha_edge/api.py` (DeviceConfig), `edge/src/ha_edge/detector.py`, `edge/src/ha_edge/sensor.py`, tests.

**Interfaces — Produces:**
- `Sound.default_min_confidence: float` — defaults from `docs/accuracy.md` "better threshold": siren 0.50, coughing 0.50, dog_barking 0.50, crying 0.50, glass_breaking 0.30, knocking 0.30; every unmeasured sound 0.30.
- `SoundSetting.min_confidence: float | None` (CHECK 0.05–0.95); `EffectiveSetting.min_confidence`; `PUT /me/sound-settings/{key}` accepts optional `min_confidence` (0.05–0.95, else 422).
- Device config adds `thresholds: dict[str, float]` = for each enabled sound, the **minimum** effective min_confidence across members who enabled it (the sensor must report anything any member would want). Existing `enabled_sounds` stays.
- Server-side: the rule engine ignores an event for a member when `event.confidence < that member's effective min_confidence` (so a member with a stricter threshold isn't alerted by another member's looser one).
- Edge: `DeviceConfig.thresholds` (default `{}` for older servers); `Detector.process(..., thresholds: Mapping[str, float] | None = None)` — base threshold per sound = `thresholds.get(sound, self.threshold)`, then the playback boost and 0.95 cap as before. Sensor passes the latest thresholds.
- [ ] Server tests: defaults per catalog; override range 422; config `thresholds` = min across enabling members; engine skips a member below their threshold but alerts another member whose threshold is met. Edge tests: per-sound threshold respected; missing key falls back; boost still applied and capped; old-server config without `thresholds` works.
- [ ] RED → implement → GREEN (both suites) → commit `feat: per-sound confidence thresholds from measured accuracy`.

### Task 3: Urgent priority for high-severity sounds (ntfy) and Telegram

**Files:** `app/catalog.py` (+`severity: Literal["urgent","normal"]`), `app/alerts/ntfy.py`, `app/alerts/delivery.py`, `app/alerts/telegram.py`, tests.

**Interfaces — Produces:**
- `Sound.severity`: urgent = scream, glass_breaking, smoke_alarm, siren, explosion, thud_fall, gasping; all others normal. Exposed in `GET /sounds`.
- ntfy publish takes `priority: Literal["urgent","high"]` → header `Priority: urgent` for urgent sounds (ntfy's max: phone vibrates/bypasses quiet per the user's ntfy app settings), `high` otherwise; also `Tags: rotating_light` for urgent, `bell` otherwise.
- Telegram: no change (the Bot API has no priority levels); document that in the README so users who need vibration-through-quiet-mode use ntfy for urgent sounds.
- [ ] Tests: urgent sound → ntfy `Priority: urgent` + tag; normal → `high`; test alert → `high`; catalog severity list exact.
- [ ] Commit `feat(alerts): urgent priority for high-severity sounds`.

### Task 4: Long-lived, revocable agent tokens for `ha-agent`

**Files:** Server: `app/auth/models.py` (+`AgentToken`), `app/auth/router.py` or new `app/auth/agent_tokens.py`, `app/auth/deps.py` (`member_from_token` accepts agent tokens for allowed routes), `app/live/router.py`, `app/alerts/router.py` (ack), migration `0011_agent_tokens.py`, tests `tests/test_agent_tokens.py`, isolation harness entry. Edge: `edge/src/ha_edge/cli.py` (`ha-agent login` creates an agent token), `edge/src/ha_edge/config.py` (AgentConfig stores agent token), tests.

**Interfaces — Produces:**
- `AgentToken(id, household_id FK cascade, member_id FK cascade, name, token_hash unique, created_at, last_used_at nullable, revoked_at nullable)`. Token format `ha_agent_` + token_urlsafe(32); stored hashed; returned once.
- `POST /me/agent-tokens {name}` (JWT only) → 201 `{id, token}`; `GET /me/agent-tokens` → list without tokens; `DELETE /me/agent-tokens/{id}` → 204 (Tenant 404 for others'). Limit 10 active per member (409 beyond).
- Agent tokens authenticate ONLY `WS /ws` and `POST /alerts/{id}/ack`; any other route with an agent token → 401. JWTs keep working everywhere.
- Revoked or member-removed → WS closes 4401 at connect; `last_used_at` updated at most once per minute.
- Edge `ha-agent login`: logs in with password (getpass) → creates an agent token named `"{hostname} agent"` → stores only the agent token. Old `access_token` configs still load (backwards compatible) but print a hint to re-login.
- [ ] Tests (server): create/list/revoke; token shown once; hashed at rest; agent token rejected on `/households/me` (401) and accepted on `/ws` + ack; revoked → 4401; isolation entry `("DELETE", "/me/agent-tokens/{a_agent_token_id}", None)` with creator. (edge): login stores agent token not password/JWT.
- [ ] Commit(s) `feat(auth): revocable agent tokens`, `feat(edge): ha-agent uses agent tokens`.

### Task 5: Password reset revokes sessions

**Files:** `app/auth/models.py` (+`Member.token_version: int default 0`), `app/security.py` (JWT claim `tv`), `app/auth/deps.py`, `app/auth/router.py` (reset confirm increments `token_version` and revokes all the member's agent tokens), migration `0012_token_version.py`, tests.
- [ ] Tests: JWT issued before reset → 401 after; agent token → revoked; new login works; JWT without `tv` claim (old) treated as version 0.
- [ ] Commit `feat(auth): password reset signs out every session`.

### Task 6: Stale-alert sweep and Telegram secret header

**Files:** `app/alerts/sweep.py` (new), `app/alerts/celery_app.py` (beat every 5 min), `app/main.py` (in-process loop alongside the offline loop), `app/alerts/router.py` (webhook header), README (setWebhook with `secret_token`), tests.
- `sweep_stale_alerts(session, now) -> int`: alerts with status `pending` or `delivering` and `created_at < now - 15 min` → `failed`; their `pending` deliveries → `failed` with `last_error = "stale"`. Never touches younger alerts. Atomic UPDATE…RETURNING.
- Telegram webhook additionally requires header `X-Telegram-Bot-Api-Secret-Token` == `telegram_webhook_secret` (constant-time) — README's setWebhook call passes `secret_token`. Missing/wrong header → 404 like a wrong path secret.
- [ ] Tests: 16-min-old pending/delivering → failed; 14-min-old untouched; sent untouched; webhook without header → 404, with header → 200.
- [ ] Commit(s) `feat(alerts): sweep stale alerts`, `feat(alerts): require Telegram secret-token header`.

---

## Self-review notes
- Research ranks covered: timezone (UX), per-sound thresholds (accuracy), urgent priority (deaf/HoH vibration via ntfy), agent tokens (laptop agent usability — 24 h JWT expiry would silently stop alarms).
- Backlog covered: stale sweep, Telegram header, reset revokes JWTs.
- Deliberately not here: quiet hours (design non-goal — conflicts with a safety tool unless urgent sounds bypass; needs your decision), HA/MQTT bridge (later plan), dashboard (Plan 3).
