# Plan 3: Web Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A responsive web dashboard (phone + laptop) served by the FastAPI app at `/app/`, implementing the approved mockup (`docs/dashboard-mockup/*.dc.html`): overview, live events, sounds & rules, sensors & pairing, alert channels, household, and an in-browser alert screen with "I'm on it".

**Architecture:** No build step. Static files under `app/web/static/` (one `index.html`, ES modules under `js/`, one `styles.css`) mounted with `StaticFiles` at `/app/`. A tiny hash router renders views into `<main>`. All data via the existing JSON API and `WS /ws`. The JWT lives in `sessionStorage` (cleared when the tab closes). Server-side, only the mount + a redirect `/` → `/app/` are added.

**Tech Stack:** Vanilla JavaScript (ES2022 modules), HTML, CSS (custom properties for light/dark), Inter/Space Grotesk via Google Fonts with system fallbacks. Tests: pytest + Playwright for Python using the installed Google Chrome (`channel="chrome"`, headless), against the real app with the test DB and fakeredis.

**Spec:** `docs/design.md` §4 (dashboard), §6, §7, §9; mockup in `docs/dashboard-mockup/`.

## Global Constraints

- No framework, no bundler, no CDN JavaScript (privacy + $0 + no supply chain). Fonts from Google Fonts only, with system fallbacks.
- Same-origin only: the dashboard calls the API on the same host; no CORS changes.
- The JWT is stored in `sessionStorage` only; never in `localStorage`, cookies or URLs (except the `/ws?token=` the server requires). Never logged to the console.
- Every page works at 360 px width (phone) and 1280 px (laptop); no horizontal page scroll.
- Accessibility: real `<button>`/`<a>`/`<label>`; 4.5:1 text contrast; focus visible; `aria-live="assertive"` on the alert screen.
- Copy: the footer of every view carries "Home-awareness tool. Not a medical device, security alarm, or emergency service."
- Colours from the mockup: teal `#0F766E` (normal), orange `#C2410C` (alert), amber `#B45309` (offline), dark alert screen `#0B1220`.
- Playwright tests must be deterministic: no sleeps > 2 s; wait for selectors/network.
- Commits end with the Co-Authored-By line the implementer's harness gives it.

## Review Focus

1. **Expired/invalid JWT mid-session** → any 401 sends the user to the login view with a message; the WS closing 4401/4403 does the same.
2. **WebSocket drops** → reconnect with backoff (1→30 s) and a visible "Reconnecting…" pill; no duplicate event rows after reconnect.
3. **XSS** → every server-provided string (device names, household name, emails, labels) is inserted with `textContent`, never `innerHTML`.
4. **Two tabs open** → an "I'm on it" in one tab updates the other via the `alert_ack` message.
5. **Phone width** → the events table scrolls inside its own box; nothing overflows the viewport.

---

### Task 1: Static mount, shell, auth views

**Files:** Create `app/web/__init__.py` (mount helper), `app/web/static/index.html`, `app/web/static/styles.css`, `app/web/static/js/{app.js,api.js,router.js,auth.js,ui.js}`; Modify `app/main.py`; Tests `tests/web/conftest.py` (live server fixture: uvicorn in a thread on a free port using the test DB + fakeredis via dependency overrides, console email capture), `tests/web/test_auth_ui.py`; add `playwright` to dev deps.
**Interfaces — Produces:** `api.js`: `request(method, path, body?) -> Promise<json>` (adds Bearer, throws `ApiError{status, detail}`; 401 → `auth.logout("Your session ended — please log in again.")`). `auth.js`: `login(email, password)`, `logout(msg?)`, `token()`, `signup(...)`, `confirm(token)`, `resend(email)`. `router.js`: `route(hash, renderFn)`, `navigate(hash)`. Views: `#/login`, `#/signup` (household name + timezone select defaulting to the browser's `Intl.DateTimeFormat().resolvedOptions().timeZone`), `#/confirm?token=` (the server's confirm email links here — update the email link path to `/app/#/confirm?token=` in the server in this task), `#/reset`.
- [ ] Tests: signup → confirm via the captured email link → login → lands on `#/overview` (placeholder); wrong password shows the server's message; reload keeps the session (sessionStorage) and a new browser context does not; 401 from any call returns to login with the message; `GET /` redirects to `/app/`.
- [ ] Commit `feat(web): dashboard shell, auth views, static mount`.

### Task 2: Overview + live events (WebSocket)

**Files:** `js/views/overview.js`, `js/live.js`, tests `tests/web/test_overview.py`.
**Interfaces:** `live.js`: `connect(onMessage)` with backoff 1→30 s, status callback (`live`/`reconnecting`), closes and logs out on 4401/4403; dedupes events by `event_id`.
- Overview (mockup `Main.dc.html`): status card ("All quiet right now" / "N sensors offline"), counts (events today, alerts today from `/events` and `/alerts`), sensor cards (online/offline/pending, last heartbeat relative time), live events table (time in the household timezone, sound label, sensor, confidence, result chip: Alerted/Merged/Logged/Cooldown — derive "Alerted" from `/alerts` event ids, "Merged" from `merged`), alert-channel summary.
- [ ] Tests: an event posted by a paired fake device appears in the table without reload; a device going offline turns its card amber; after a forced WS close the pill shows "Reconnecting…" then "Live" and the event isn't duplicated; XSS: a device named `<img src=x onerror=alert(1)>` renders as text.
- [ ] Commit `feat(web): overview with live events`.

### Task 3: Alert screen + browser notifications + "I'm on it"

**Files:** `js/views/alert.js`, `js/notify.js`, tests `tests/web/test_alert_ui.py`.
- On `{"kind":"alert"}`: full-screen dark alert (mockup `PhoneAlert.dc.html`) with label, sensor, time, "I'm on it" / "Event details" / "Dismiss"; `aria-live="assertive"`; if the tab is hidden and Notification permission is granted, show a browser notification (ask permission from a button on Alert channels, never on load). Optional in-page beep via Web Audio when `alarm` is true and the page is visible (stops on any button).
- "I'm on it" → `POST /alerts/{id}/ack`; 409 shows "{by} is already handling it". `alert_ack` for the same event (from another tab/member) replaces the screen with "{email} is handling it" and silences the beep.
- [ ] Tests: test alert shows the screen; ack posts and closes it; a second tab sees the ack; 409 path message.
- [ ] Commit `feat(web): alert screen, browser notifications, I'm on it`.

### Task 4: Sounds & rules

**Files:** `js/views/sounds.js`, tests `tests/web/test_sounds_ui.py`.
- Mockup `Sounds.dc.html`: grouped list from `GET /sounds` + `GET /me/sound-settings`; per sound a switch, reliability badge, severity badge (urgent), and "Alert after [N]× in [T]s · quiet for [C] · min confidence [x]" inputs that `PUT /me/sound-settings/{key}` on change (debounced 400 ms) with inline error display for 422.
- [ ] Tests: toggling dog barking persists across reload; invalid repeat 0 shows the 422 message; reliability and urgent badges render.
- [ ] Commit `feat(web): sounds and rules`.

### Task 5: Sensors & pairing, alert channels, household

**Files:** `js/views/{sensors,channels,household}.js`, tests `tests/web/test_settings_ui.py`.
- Sensors (mockup `PhoneSensors.dc.html`): list with status, "Add a sensor" → name + kind → shows the 8-char code large with a 10-min countdown; remove sensor (confirm dialog) → DELETE.
- Alert channels (mockup `PhoneChannels.dc.html`): popup/alarm/telegram/ntfy toggles (`PUT /me/alert-preferences` with full body), "Link Telegram" (opens the deep link), "Generate ntfy topic", "Send me a test alert", "Enable browser notifications", agent tokens list with revoke.
- Household: name, timezone (owner can change), members list, invite link (owner), remove member / leave / delete household (typed confirmation).
- [ ] Tests: create pairing code shows 8 chars; remove sensor; test alert arrives on the overview; generate ntfy topic persists; owner changes timezone and event times re-render; member doesn't see owner-only controls.
- [ ] Commit `feat(web): sensors, channels, household`.

### Task 6: Polish, dark mode, docs

- Dark mode via `prefers-color-scheme`; 360 px audit on every view (Playwright screenshot at 360 and 1280 for each view saved to `docs/screenshots/` and linked from README); README "Dashboard" section; CI runs the web tests (install Chrome via `browser-actions/setup-chrome` or skip with a marker when unavailable).
- [ ] Tests: no horizontal overflow at 360 px for each view (`document.documentElement.scrollWidth <= innerWidth`).
- [ ] Commit `feat(web): dark mode, phone-width audit, docs`.

---

## Self-review notes
- Deferred: the phone **sensor** page (browser microphone + on-device model) — needs a browser build of YAMNet; separate plan. This plan is the dashboard only.
