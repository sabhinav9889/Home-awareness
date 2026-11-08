# Plan 5b: `ha-camera` — Live Camera Sensor (laptop / USB / Pi)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `ha-camera` program that watches a real camera on this computer, detects falls, lying still, a person while the house is guarding, and (optionally) weapons, and sends each alert to the server with one small snapshot. Video never leaves the computer.

**Architecture:** A new `ha_edge.camera` package in the existing `edge/` project, sharing `ha_edge.api` (server client, event queue), `ha_edge.config` and the CLI style. A camera is its own paired device (own key file), so `ha-sensor` keeps running untouched. Frames come from a threaded latest-frame grabber behind a `FrameSource` protocol (a video file source makes everything testable without a camera). Pose and object models sit behind small protocols; the fall and lying-still logic are pure functions over torso samples.

**Tech Stack:** Python 3.12, OpenCV (headless), MediaPipe Tasks (Pose Landmarker lite, EfficientDet-Lite0 object detector), numpy, httpx. MediaPipe and OpenCV live in an optional `camera` extra; imports are lazy.

**Spec:** `docs/superpowers/specs/2026-11-08-camera-sensors-design.md` (§2 privacy, §4 detections, §6 client, §12 calls 3, 5, 8). Server side: Plan 5a (merged).

## Global Constraints
- **Never write frames, images or video to disk.** Snapshots are encoded in memory, uploaded, and dropped. Retries keep images in memory only (max 5 queued images; an image is dropped after 3 failed sends or 60 s, the event is kept).
- Snapshot: JPEG, longest side ≤ 640 px, ≤ 300 KB, taken from a frame ≤ 1 s old. If it cannot be made small enough, send the event without it.
- Obey the heartbeat config: `guarding` false → person detection off (the object model is not run); `send_snapshots` false → no image is encoded or sent; `weapon_seen` not in `enabled_sounds` → the object model is not used for weapons; event types not in `enabled_sounds` are never sent.
- Client cooldowns ≥ the server's: `fall` 120 s, `lying_still` 600 s, `person_away` 300 s, `weapon_seen` 300 s.
- Config refresh while running: `GET /device/config` every 15 s (no side effects), heartbeat every `heartbeat_interval_s`. This keeps the Away arming lag to about 15 s.
- The camera has its own key file `camera.json` (never `sensor.json`). Nothing in this plan may touch the running `ha-sensor`, its config, or the Docker stack.
- Models: downloaded by `ha-camera fetch-model` into the same model dir as YAMNet, with pinned SHA-256 (values recorded in Task 1), never committed. Apache-2.0 models only; no face recognition, no identifying people.
- The default `uv sync` install, CI and the existing 311 edge tests stay unaffected (lazy imports; tests that need the extra are marked `camera`).
- Commits end with the Co-Authored-By line the implementer's harness gives it. Never push. Never open `/dev/video*` in automated tests; real-camera checks are run by the user (`ha-camera check`).

## Review Focus
1. **Camera busy or unplugged** (a browser or another app holds it): clear message, retry every 5 s, heartbeats continue, no crash (Task 5).
2. **Stale frames** (V4L2 buffering): the frame used for a snapshot is ≤ 1 s old (Task 2).
3. **Guarding flips mid-run**: Away on/off takes effect within ~15 s; guarding off stops person events and stops running the object model (Task 5).
4. **`send_snapshots` false**: no image is encoded or uploaded, the event still goes (Task 3, 5).
5. **Someone stays in view or on the floor**: one alert per cooldown, not a stream; lying still reports once per cooldown (Task 4, 5).
6. **Landmarks not visible** (laptop webcam sees only head and shoulders): no fall or lying-still verdict is made (Task 4).

---

### Task 1: Package, config, pairing, `cameras`, `fetch-model`
**Files:** `edge/pyproject.toml` (extra `camera = ["mediapipe", "opencv-python-headless"]`, script `ha-camera = "ha_edge.camera.cli:main"`), `edge/src/ha_edge/camera/{__init__,cli,models,devices}.py`, `edge/src/ha_edge/config.py`, tests `edge/tests/camera/test_cli.py`.
**Interfaces — Produces:**
- `CameraConfig(base_url, device_id, device_key, camera_name: str | None = None, camera_index: int | None = None)`; `save_camera(cfg)` / `load_camera()` writing `camera.json` (0600, atomic, like `sensor.json`).
- `models.POSE_FILE = "pose_landmarker_lite.task"`, `OBJECT_FILE = "efficientdet_lite0.tflite"`; URLs `https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task` and `https://storage.googleapis.com/mediapipe-models/object_detector/efficientdet_lite0/float16/latest/efficientdet_lite0.tflite`. Pin `POSE_SHA256` / `OBJECT_SHA256` as constants: the implementer downloads both files once, runs `sha256sum`, and records the values in `models.py` (a spike on 2026-11-08 measured pose = 5,777,746 bytes and objects = 7,254,339 bytes; the implementer's fresh hashes must be re-checked against a second download). `fetch_models(dest_dir=None, http=None) -> Path` verifies, writes atomically, raises `ModelIntegrityError` on mismatch (reuse `ha_edge.model`).
- `devices.list_cameras() -> list[CameraInfo(index: int, name: str, node: str)]` from `/sys/class/video4linux/*/{name,index}`; nodes with `index != 0` (metadata nodes) are skipped. Never opens a device.
- CLI `ha-camera`: `pair --server URL --code CODE [--camera NAME]` (saves `camera.json`, prints the "camera is watching, snapshots only on alerts" notice), `cameras`, `fetch-model`, plus `check` and `run` registered by Tasks 5.
- [ ] Tests: `camera.json` round-trip and 0600; `list_cameras` with a fake sysfs tree; pair with respx saves config and never prints the key; `fetch_models` with respx: good hash ok, bad hash raises and leaves no file; `ha-sensor --help` and its tests unchanged.
- [ ] RED → implement → GREEN (`cd edge && uv run pytest -q && uv run ruff check .`) → commit `feat(camera): ha-camera package, pairing, camera listing, model fetch`.

### Task 2: Frame sources and snapshot encoding
**Files:** `edge/src/ha_edge/camera/{capture,snapshot}.py`, tests `edge/tests/camera/test_capture.py`, `test_snapshot.py`.
**Interfaces — Produces:**
- `@dataclass Frame: image: np.ndarray` (BGR, uint8) `, captured_at: float` (monotonic).
- `class FrameSource(Protocol): def read(self) -> Frame | None; def close(self) -> None`.
- `CameraSource(index: int)`: OpenCV `VideoCapture` (V4L2) in a daemon thread that grabs continuously and keeps only the newest frame (buffer size 1); `read()` returns the newest frame not yet returned, else `None` after 1 s without a new frame; `opened: bool`; closing joins the thread and releases the device. Raises `CameraBusy` if the device cannot be opened or gives no frame in 3 s.
- `FileSource(path, loop=True, fps=4)` for tests and dry runs (no device).
- `encode_snapshot(frame: Frame, now: float, *, max_age_s: float = 1.0, max_side: int = 640, max_bytes: int = 300_000) -> bytes | None`: `None` if the frame is older than `max_age_s` or cannot be made ≤ `max_bytes` (quality 85 → 40); output has JPEG magic and EOI; never touches disk.
- [ ] Tests (no real camera): an ffmpeg-generated clip played through `FileSource` yields frames at ~fps; `encode_snapshot` of a 1920×1080 frame has longest side 640 and ≤ 300 KB, is a valid JPEG; stale frame → `None`; a noise frame that cannot fit → `None`; a test that patches `open`/`Path.write_bytes`/`cv2.imwrite` to fail proves nothing is written; `CameraSource` with a fake `VideoCapture` returns only the newest frame and raises `CameraBusy` on open failure.
- [ ] RED → implement → GREEN → commit `feat(camera): frame sources and in-memory snapshots`.

### Task 3: Server client and queue carry snapshots, guarding and send_snapshots
**Files:** `edge/src/ha_edge/api.py`, tests `edge/tests/test_api.py` (extend) and `edge/tests/camera/test_api_snapshot.py`.
**Interfaces — Produces:**
- `DeviceConfig` gains `guarding: bool = False` and `send_snapshots: bool = True` (parsed leniently; absent keys use the defaults, so old servers and `ha-sensor` are unaffected).
- `ServerClient.send_event(sound, confidence, occurred_at, snapshot: bytes | None = None)`: with a snapshot it posts `multipart/form-data` with part `event` (the same JSON, `application/json`) and part `snapshot` (`image/jpeg`); without one it behaves exactly as today (JSON body).
- `EventQueue.put(sound, conf, occurred_at, snapshot: bytes | None = None)`; images are held in memory only; at most 5 queued images (the oldest lose their image first); an image is dropped after 3 failed sends or when the event is older than 60 s, and the event is then sent without it. `drain` passes the image through.
- [ ] Tests: JSON path byte-for-byte unchanged for existing callers; multipart request has both parts with the right content types; config parsing with and without the new keys; image cap of 5; image dropped after 3 failures with the event still delivered; an oversize or 4xx on the image part retries the event once without the image.
- [ ] RED → implement → GREEN (the 311 existing edge tests must still pass) → commit `feat(edge): events can carry a snapshot; config reads guarding and send_snapshots`.

### Task 4: Detection logic (pure) and model wrappers
**Files:** `edge/src/ha_edge/camera/{pose_rules,detectors}.py`, tests `edge/tests/camera/test_pose_rules.py`, `test_detectors.py`.
**Interfaces — Produces:**
- `@dataclass Landmarks: left_shoulder, right_shoulder, left_hip, right_hip` each `(x, y, visibility)` with x, y in 0–1 image coordinates.
- `torso_sample(lm: Landmarks, t: float, *, min_visibility: float = 0.5) -> TorsoSample | None` where `TorsoSample(t, angle_deg, hip_y, cx, cy)`; `angle_deg` is the torso's angle from vertical (0 = upright, 90 = horizontal); returns `None` when any of the four points is below `min_visibility` (no verdict on partial bodies).
- `FallDetector(window_s: float = 1.0, upright_deg: float = 35, down_deg: float = 60, min_drop: float = 0.15)`: `update(sample: TorsoSample | None) -> bool` is true once when, inside the window, the angle goes from upright to down AND `hip_y` increases by ≥ `min_drop`; a slow lie-down, sitting down, or a `None` gap does not fire; it re-arms only after the torso is upright again.
- `LyingStillDetector(min_still_s: float = 120, down_deg: float = 60, max_motion: float = 0.03)`: `update(sample) -> bool` true once when the torso stays ≥ `down_deg` with centre motion ≤ `max_motion` for `min_still_s`; `None` for longer than 5 s resets it; re-arms after the torso is upright.
- `PersonTracker(min_hits: int = 3, of: int = 5)`: `update(person_seen: bool) -> bool` true while at least `min_hits` of the last `of` inference frames saw a person.
- Protocols `PoseModel.torso(frame) -> Landmarks | None` and `ObjectModel.detect(frame) -> list[Detection(label: str, score: float)]`; real `MediapipePose` and `MediapipeObjects` (lazy mediapipe import, explicit `close()`, model paths from `models`, `score_threshold` passed in); `weapon_labels = {"knife", "baseball bat"}`; animals are never reported as a person.
- [ ] Tests (synthetic torso streams, no model): upright → horizontal in 0.6 s with a hip drop fires once; same change over 10 s does not; sitting (small angle change) does not; partial visibility gives `None` and no event; lying still fires after 120 s of horizontal and stillness, not when the person moves; `PersonTracker` ignores a single-frame blip and holds through one missed frame; a `model`-marked smoke test loads both real models and runs one blank frame (no person, no exception, `close()` clean).
- [ ] RED → implement → GREEN → commit `feat(camera): fall, lying-still and person logic with pose and object model wrappers`.

### Task 5: The run loop, `check`, and robustness
**Files:** `edge/src/ha_edge/camera/{runner,check}.py`, `edge/src/ha_edge/camera/cli.py` (register `check`, `run`), tests `edge/tests/camera/test_runner.py`, `test_check.py`.
**Interfaces — Consumes:** Tasks 1–4. **Produces:**
- `async run_camera(client: ServerClient, source: FrameSource, pose: PoseModel, objects: ObjectModel, *, clock=time.monotonic, fps: float = 4.0, config_refresh_s: float = 15, stop: asyncio.Event) -> int`:
  - ~4 fps; skips pose unless `fall` or `lying_still` is enabled; runs the object model only while (`guarding` and `person_away` enabled) or `weapon_seen` is enabled (and then only for the labels needed);
  - each event type has a client cooldown (Global Constraints) and the server's per-sound threshold from the config (`DeviceConfig.thresholds`, default 0.5 for objects, 1.0 for rule-based fall/lying events);
  - snapshot from the frame that triggered the event, via `encode_snapshot`, only if `send_snapshots`; events go through `EventQueue`;
  - heartbeat per `heartbeat_interval_s` and config refresh every 15 s; a revoked device exits non-zero with a clear message; server down → events queue (in memory) and the loop continues;
  - `CameraBusy` or a source that stops giving frames → log once "camera busy or unplugged", retry every 5 s, heartbeats continue; shutdown on `stop`/SIGINT/SIGTERM closes the source and both models.
- `ha-camera check [--seconds 15] [--camera NAME]`: opens the camera, prints per frame: torso angle (or "no body"), `person` score, whether guarding, and `would send: fall|lying_still|person_away|weapon_seen` — **nothing is sent**; ends with a summary and plain advice (too dark, no body visible, camera busy). `ha-camera run` prints at start: "Watching — video is analysed on this computer; only a snapshot goes with an alert."
- [ ] Tests with `FileSource` plus fake models and a respx server: guarding off → object model never called and no `person_away`; flip guarding on in the next config refresh → person events start within 15 s (fake clock); `send_snapshots` false → event posted as JSON without an image; person visible for 10 minutes → exactly 2 `person_away` events; fall then immediate second fall → one event within 120 s; camera busy at start → retries, heartbeat still posted; revoked key → exit 1 with message; stop event closes source and models; `check` sends nothing (respx asserts no `/device/events`).
- [ ] RED → implement → GREEN → commit `feat(camera): ha-camera run and check with guarding, snapshots and recovery`.

### Task 6: Docs and a live checklist
**Files:** `edge/README.md` (camera section), `README.md`, `docs/camera-live-test.md`, `docs/design.md` (privacy promise note), `.gitignore` (`.fake_camera_key`), `scripts/fake_camera.py` (committed from the working copy).
- Install (`uv sync --extra camera`), `fetch-model`, `pair` (camera pairing code from the dashboard Sensors page), `check`, `run`; what each detection needs (lighting, whole body in view for falls, a laptop webcam usually sees only head and shoulders so fall and lying-still need a wider view of the floor); privacy statement; what "suspicious" means (person while guarding, fall, lying still, weapon — stealing and smashing are only caught through combined incidents); the false-positive notes (TV or posters with people, kitchen knives).
- `docs/camera-live-test.md`: step-by-step for the user: pair, `ha-camera check` standing in view, turn Away on and wait ~15 s, watch the dashboard alert with the snapshot, cooldowns, camera busy test (open the browser camera), unplug test.
- [ ] Docs only; `uv run ruff check .`; commit `docs: camera client usage and live test checklist`.

---
## Self-review notes
Spec §6 `ha-camera pair|cameras|check|run` → T1, T5; §4 detections → T4–T5; §2 privacy (no frames on disk, snapshot only) → Global Constraints, T2, T3; §12.3 guarding via config → T3, T5; §12.8 separate paired device → T1 `camera.json`. Android phone cameras remain Plan 5c. Arming lag reduced to ~15 s by the config poll (spec §12.3 said "heartbeat"; this is a tightening).
