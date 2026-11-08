# Camera client (Plan 5b): build report, 9 Nov 2026

Branch `camera-client` in `~/home-awareness-5b`, built on `main` (which already has the camera server and dashboard from Plan 5a). Not merged, not pushed. Edge-only: no server, database or Docker change.

## What it is
`ha-camera` watches a real camera on this computer and sends alerts to the server, with one small snapshot per alert. Video is analysed on the computer and never sent or saved.
- Detects: fall, lying still, person while the house is guarding (Away or night hours), weapon seen (off by default, experimental).
- Commands: `ha-camera pair | cameras | fetch-model | check | run`.
- Reads the server config every ~15 s, so Away and the "Send snapshots" switch take effect within ~15 s.
- Own key file (`camera.json`), separate from `ha-sensor`.

## Tests
475 edge tests pass (311 existing + 164 new), including the real MediaPipe models. ruff clean. Full details per task in `.superpowers/sdd/2026-11-08-plan-5b/`.
The final review ran the real program (real models, a generated test video, a stand-in server): it starts, runs at about 4 frames/s, and stops cleanly on Ctrl-C and on a stop signal. It exits with the right code if the camera is removed. The server's own parser accepted its photo uploads.

## Bugs the reviews caught (all fixed)
- `run` and `check` crashed on startup (model loaders imported from the wrong module; the tests had mocked it).
- Stopping while analysing a frame segfaulted the real model (reproduced 3 of 3); now waits for the analysis.
- Photos in a pending-alert record would have printed if logged; now hidden from reprs.
- The body angle was wrong on widescreen video (45° read as 29° at 16:9).
- A second "lying still" within 10 minutes was never reported; a missed frame could start a silent 5-minute cooldown; a partial `--camera` name broke later runs; the revoked-camera message named the wrong command.

## Not measured / known limits
- Accuracy on real people has NOT been measured. The logic is tested on synthetic body positions only.
- A laptop webcam usually sees head and shoulders only, so fall and lying-still need a wider view of the floor. When the body isn't visible the program stays silent by design.
- Stealing and smashing can't be detected from video; they appear only as combined incidents (e.g. "Possible break-in").
- A TV or poster showing people can trigger person detection. `weapon_seen` fires on any knife or bat in view.
- Background items are in `.superpowers/backlog-plan5b-minors.md`.

## Calls made for you (please check)
- Rule-based events (fall, lying still) are sent with confidence 1.0, so any member's threshold up to 0.95 still gets them.
- Lying still: one odd reading doesn't restart the 2-minute clock; it restarts when 3 of the last 5 readings are off.
- Arming lag for Away is about 15 s (config refresh), not up to a minute, on the client side.
- The history on this branch has one tangled commit (`c927b7d` holds a step-3 fix plus a one-line step-4 fix; the commit before it, `e3ee051`, has a syntax error that `c927b7d` fixes). The final code is correct.

## To try it
Follow `docs/camera-live-test.md` (nine steps, with exact commands). Short version, from `~/home-awareness-5b/edge`:
`uv sync --extra camera`, `uv run ha-camera fetch-model`, add a camera in the dashboard (Sensors → Add a camera) and pair it, then `uv run ha-camera check` standing in view. Paste me the output.
