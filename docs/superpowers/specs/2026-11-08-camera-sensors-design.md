# Camera Sensors: Design

Date: 2026-11-08 · Status: **approved by the author 2026-11-08 01:2x IST** (retention 7 days; all four detection groups incl. experimental weapons and combined incidents; common areas with bedroom/bathroom opt-in; laptop/USB/Pi cameras first; then (author, 01:3x) **Android phones as full sensors — microphone and camera — in v1**)
Builds on: `docs/design.md` (the system design) and everything merged on `main`.

Legend: **(author)** = the author said it; **(my call)** = a default I chose that the author should check.

## 1. Intent and why
Sound sensors catch screams, glass and alarms, but some emergencies are silent: a parent who faints, someone lying on the floor, a stranger in the house at night. **(author)** The author wants cameras — any device camera or a spare device's camera — to raise alerts for:
- **health/awareness:** someone falls; someone gets dizzy, collapses or is unconscious;
- **security:** someone stealing; someone smashing property; someone carrying a weapon.

**(author)** A **snapshot photo is sent with each camera alert**, so the family can see at once whether it's real. **(author)** The server watches every sensor in the household at the same time (sound and camera), which lets it combine them into stronger incidents.

## 2. Privacy model (changes one promise of the original design)
Original promise: "raw audio or video never leaves a device." New rule **(author: snapshot; my call: the limits)**:
- Video is analysed **on the device**. Video is **never streamed or recorded**.
- When a camera raises an alert, **one still image** (JPEG, longest side 640 px) taken at that moment goes with the event. Nothing else leaves the device.
- Snapshots are **encrypted at rest** on the server, visible **only to members of that household**, and **deleted automatically after 7 days** **(author)** (per-household setting: 1, 7 or 30 days).
- Each camera has a **"Send snapshots" switch** (on by default); with it off, camera alerts are events-only like sound.
- **No face recognition, no identifying people, no tracking across cameras** — anti-goals.
- Sound sensors are unchanged (still audio-free).
- The disclaimer gains: "Cameras send a single snapshot only when they raise an alert."

## 3. Placement and consent
**(author: common areas suggested; bedrooms/bathrooms opt-in; details my call)**:
- The product suggests **common areas and entrances** (living room, hallway, front door, a parent's room only with their agreement).
- When adding a camera, the owner picks a **room** and ticks **"Everyone who may be on this camera knows about it."** Cameras marked as bedroom/bathroom show a stronger notice and default **"Send snapshots" to off**.
- A visible on-device indicator while analysing (the camera's own light where available; the sensor's terminal says so).

## 4. What gets detected
| Event | How (on device) | Reliability goal | Default |
|---|---|---|---|
| **Fall** | Body-pose model: hips/shoulders drop fast + torso turns horizontal within ~1 s | good with tuning | on |
| **Lying still** (possible collapse) | Torso horizontal, low on the frame, little movement for N minutes (default 2) | good | on |
| **Person while Away / at night** (theft, intruder) | Person detection when the household is in **Away mode** or inside **night hours** | good | on (needs Away/night) |
| **Weapon seen** | Object detector sees "knife" or "baseball bat" held by a person | **experimental** (kitchen knives!) | off |
| **Smashing** | Not from video alone — see combined incidents | — | — |

Pets: person detection only; animals never trigger camera events.

## 5. Combined incidents (server)
**(author: server watches all sensors at once; my call: the rules.)** Sensors are grouped into **rooms**. Within the same room and a short window, the server merges related events into one stronger incident:
| Incident | Ingredients | Window | Severity |
|---|---|---|---|
| **Possible break-in** | `glass_breaking` (sound) + person (camera) or `person_away` | 15 s | urgent |
| **Fall (confirmed)** | `fall` (camera) + `thud_fall` or `scream` (sound) | 10 s | urgent |
| **Possible collapse** | `lying_still` alone | — | urgent |
| **Property damage** | `explosion`/`door_slam`/`glass_breaking` + person while Away | 15 s | urgent |

An incident alert carries the camera's snapshot. Individual events still appear in the history.

## 6. How it fits the existing system
- **Camera sensor** = a new mode of the edge client: `ha-camera pair|cameras|check|run` (laptop webcam, USB camera, Raspberry Pi camera). Same pairing codes and device keys; device kind `camera`. Frames analysed at a low rate (my call: 4 frames/s) with free on-device models (**my call:** Google MediaPipe Pose Landmarker and Object Detector — Apache-2.0; avoid AGPL models).
- **Events**: same `POST /device/events`, plus an optional snapshot (multipart). New catalog entries: `fall`, `lying_still`, `person_away`, `weapon_seen`; groups Health / Security.
- **Rooms**: new household-scoped `Room` (name); devices get an optional room. Correlation is per room.
- **Away mode & night hours**: household setting (dashboard toggle + optional schedule in the household's timezone). Members can switch it from the dashboard; Away mode change is shown to everyone.
- **Alerts**: dashboard alert screen shows the snapshot; Telegram sends the photo (`sendPhoto`); ntfy sends it as an attachment. Snapshot URLs are short-lived and need login.
- **Storage ($0)**: snapshots on the server's disk/volume (encrypted with a key from the environment), swept by the existing sweep job; size-capped per household.
- **Android phones (author: in v1, microphone + camera)**: a **Sensor mode** page in the dashboard (`#/sensor-mode`) for Chrome on Android. Pairs with a code like any sensor; runs the models **in the browser** (my call: Google MediaPipe Tasks for Web — AudioClassifier with YAMNet, PoseLandmarker, ObjectDetector — vendored and served same-origin, no CDN; CSP adds `'wasm-unsafe-eval'` only for the sensor page if needed); applies the same detector rules (thresholds from the heartbeat, peak hold, debounce, label rules, playback-awareness not available in browsers → documented); sends events + snapshots with the device key; heartbeats; Screen Wake Lock; shows a clear "Listening / Watching" banner; stops when the tab is hidden and says so (browsers suspend background tabs — the page asks the user to keep it open on a charger).
- **HTTPS on the home network (my call)**: browsers allow mic/camera only on secure pages. Add a **Caddy** service to Docker Compose with `tls internal` serving `https://<laptop-ip>:8443` (API + dashboard + WebSocket); the user installs Caddy's local root certificate on the phone once (documented step-by-step). Fallback for quick tests: Chrome's "Insecure origins treated as secure" flag. This is also the first step toward the cloud deployment plan.
- **Build order (my call)**: (1) rooms, Away/night, catalog + snapshots on the server; (2) laptop/USB/Pi camera sensor; (3) combined incidents; (4) local HTTPS; (5) Android sensor mode — microphone; (6) Android sensor mode — camera.

## 7. Goals, non-goals, anti-goals
**Goals:** fall/collapse/intruder alerts from cheap cameras; snapshot with the alert; combined incidents; same pairing, rules, channels and "I'm on it".
**Non-goals (v1):** iPhone-specific support (Safari may work but isn't tested); recording/clips; live view; privacy masking zones; people counting.
**Anti-goals:** video leaving the device; face recognition or identity; keeping snapshots beyond the retention setting; camera alerts from pets; claiming medical-grade fall detection.

## 8. Constraints
$0 (free models, server disk); runs on a laptop CPU or a Pi 4; privacy rules above; every new household-scoped route in the isolation harness; template-only logging (never log snapshot URLs/tokens).

## 9. Measuring
- Fall/lying-still: evaluate on a public research fall dataset (e.g. UR Fall Detection) — precision/recall like `docs/accuracy.md`; plus a live home test.
- Person-while-Away: false alarms per day in a normal home.
- Weapons: experimental until measured.

## 10. Left to the builder
Exact pose heuristics and thresholds; model files and download/checksum; frame rate tuning; snapshot encoding; schema details; dashboard layout for snapshots and rooms.

## 11. Risks
False falls (sitting down fast, kids playing), lighting/night vision (most webcams are poor in the dark — note it), CPU/battery on spare devices, legal/consent norms for cameras at home, snapshot storage leaks (mitigated by encryption, retention, auth).

## 12. Settled after builder review (my calls — author to check)
1. **Incidents never delay alerts.** Ingredient events alert on their own immediately (existing pipeline, per-member rules and cooldowns unchanged). When the server sees the incident pattern within the window, it creates a **separate follow-up incident alert** ("Possible break-in", "Fall confirmed") with the best snapshot. Incident types are catalog entries in a new group "Incidents" (enabled by default, member-toggleable), keyed like any sound for cooldown (default 5 min) and repeat (1).
2. **Severity "urgent"** = the existing catalog `severity` (ntfy `Priority: urgent`). It never bypasses a member's disabled sound, cooldown or repeat rule.
3. **Away mode** is one household boolean (any member may toggle; change published live and logged in history). **Night hours** = optional household schedule in the household's timezone; the effective "guarding" state = Away OR inside night hours. Cameras learn it from the heartbeat config (`guarding: bool`) and **only run/report person detection while guarding** (privacy + event volume). Falls/lying-still run always.
4. **Permissions:** owner-only — photo retention, each camera's "Send snapshots" switch, rooms create/rename/delete, night-hours schedule. Any member — Away toggle, assigning their view; everyone in the household can see snapshots.
5. **Snapshot ingest:** `POST /device/events` keeps accepting JSON (unchanged for existing sensors); cameras may instead send `multipart/form-data` (`event` JSON part + optional `snapshot` JPEG ≤ 300 KB). Event is accepted even if the image is missing/invalid (image dropped, event kept). Household cap 200 MB; at the cap the **oldest snapshots are evicted**. An incident carries the snapshot of the highest-confidence camera event in the window.
6. **Encryption:** one Fernet key from env `SNAPSHOT_KEY` (required when cameras are used; startup error if missing/weak); no per-household keys in v1; key loss = old snapshots unreadable (documented); snapshots are **excluded** from the nightly DB backup (they're short-lived by design).
7. **Rooms:** household-scoped; many devices per room; devices without a room don't take part in correlation (dashboard nudges to assign one); existing devices start with no room.
8. **Device kind:** add `camera`; a device may have **capabilities** `audio` and/or `video` (an Android phone in sensor mode can be both; a laptop can run `ha-sensor` and `ha-camera` as two paired devices). Migration extends the kind check.
9. **Consent:** stored per camera (`consent_confirmed_at`, `confirmed_by`), shown on the camera's card to all members; bedroom/bathroom room types default snapshots off.
