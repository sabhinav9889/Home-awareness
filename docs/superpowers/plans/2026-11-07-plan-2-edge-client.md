# Plan 2: Laptop Edge Client and Alert Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python package that turns a laptop into (a) a sensor — mic → on-device YAMNet → small events to the server — and (b) an alert agent — live WebSocket → desktop popup and an alarm through whatever output is playing (the headphones when plugged in).

**Architecture:** A separate package `edge/` with its own `pyproject.toml` (heavy audio/ML deps never enter the server image). Pure logic (label mapping, detection, debounce, sensitivity) is separated from I/O (mic, model, HTTP, WebSocket, notifications, playback) behind small protocols so every decision is unit-tested with fakes. Two console scripts: `ha-sensor` and `ha-agent`.

**Tech Stack:** Python 3.12, uv, numpy, sounddevice (PortAudio), ai-edge-litert (TFLite runtime) for YAMNet, httpx, httpx-ws (WebSocket client), desktop-notifier, platformdirs, pytest + respx, ruff.

**Spec:** `docs/design.md` (§4 architecture, §5 catalog, §7 devices, §8 privacy, §13 settled calls). Server API: Plan 1 (`docs/superpowers/plans/2026-11-06-plan-1-server-core.md`, Tasks 5–7, 10).

## Global Constraints

- Raw audio never leaves the process and is never written to disk. Only `{type, confidence, occurred_at}` is sent (server rejects extra fields).
- Only sounds in the server's `enabled_sounds` (from heartbeat/config) are ever sent; everything else is dropped on device.
- Catalog keys are exactly the server's 17 keys (`app/catalog.py`).
- Audio: 16 kHz mono float32; YAMNet window 15 600 samples (0.975 s), hop 7 800 samples (0.4875 s).
- Default detection threshold 0.30 per sound; while the machine is playing audio, thresholds rise by +0.20 (capped at 0.95). Debounce: the same sound is emitted at most once per 2.0 s.
- Heartbeat every `heartbeat_interval_s` from the server (60 s). Events older than 10 minutes are dropped, never sent (server would 422).
- Device key stored only in the user config dir (`platformdirs.user_config_dir("home-awareness")/sensor.json`), file mode 0600. Never printed after pairing, never logged.
- Microphone is pinned by name; if the pinned device disappears, the sensor stops sending and logs/notify an error rather than silently switching to another input (e.g. a headset mic).
- Alarm plays on the default OUTPUT device (headphones when plugged in), at most 30 s, stoppable from the popup.
- Copy: "Home-awareness tool. Not a medical device, security alarm, or emergency service." in both CLIs' `--help`.
- No paid services; model fetched from Google's public URL with a pinned SHA-256.
- Commits end with the Co-Authored-By line the implementer's harness gives it.

## Review Focus

1. **Pinned mic unplugged mid-run** → sensor reports the error and sends nothing; does not fall back to another mic. (Task 5)
2. **Server unreachable / 5xx** → events retried with backoff for up to 10 min of age, then dropped; heartbeat failures don't crash the loop. (Task 2)
3. **Device key revoked (401)** → sensor stops and tells the user to re-pair; no retry storm. (Task 2)
4. **Media playing through speakers** → thresholds raised; a movie scream at 0.4 confidence is not sent while playback is active. (Tasks 3, 6)
5. **Alert agent JWT expired / socket closed 4401/4403** → agent shows "log in again" notification and stops reconnect-looping. (Task 7)

---

## File Structure

```
edge/
  pyproject.toml          package "home-awareness-edge", scripts ha-sensor / ha-agent
  README.md
  src/ha_edge/
    __init__.py
    config.py             SensorConfig, AgentConfig load/save (0600)
    api.py                ServerClient: pair, heartbeat, send_event (+ errors)
    labels.py             CATALOG_KEYS, YAMNET_LABELS: dict[key, tuple[class names]], class-index map
    detector.py           Detector: scores -> Detection list (thresholds, debounce, enabled filter, boost)
    model.py              Classifier protocol, YamnetClassifier (LiteRT), fetch_model()
    audio.py              list_inputs(), MicStream (pinned device, windowing)
    playback.py           PlaybackProbe protocol, PipeWireProbe (pw-dump), NullProbe
    sensor.py             run_sensor() loop wiring everything; queue + retry
    agent.py              run_agent(): WS client, reconnect policy
    notify.py             Notifier protocol, DesktopNotifier, AlarmPlayer (synthesized tone)
    cli.py                ha-sensor (pair | devices | run), ha-agent (login | run)
  tests/                  one test file per module
scripts/eval_accuracy.py  accuracy harness (ESC-50 subset) -> docs/accuracy.md
.github/workflows/ci.yml  add an `edge` job (uv sync in edge/, ruff, pytest)
```

---

### Task 1: Package skeleton and config storage

**Files:** Create `edge/pyproject.toml`, `edge/src/ha_edge/__init__.py`, `edge/src/ha_edge/config.py`, `edge/tests/test_config.py`, `edge/README.md`; Modify `.github/workflows/ci.yml` (add `edge` job; install `libportaudio2` apt package).

**Interfaces — Produces:**
- `@dataclass SensorConfig(base_url: str, device_id: int, device_key: str, mic_name: str | None = None)`; `@dataclass AgentConfig(base_url: str, email: str, access_token: str)`.
- `config_dir() -> Path` (platformdirs; overridable by env `HA_EDGE_CONFIG_DIR` for tests); `load_sensor() -> SensorConfig | None`; `save_sensor(cfg) -> Path` (writes JSON atomically, chmod 0600); same for agent (`agent.json`).

- [ ] **Step 1: Failing tests** `test_save_then_load_roundtrip`, `test_saved_file_mode_is_0600`, `test_missing_file_returns_none`, `test_corrupt_file_raises_clear_error` (`ConfigError` naming the path).
- [ ] **Step 2–4:** run (FAIL) → implement → `cd edge && uv run pytest -q && uv run ruff check .` PASS.
- [ ] **Step 5: Commit** `chore(edge): package skeleton and config storage`.

### Task 2: Server API client

**Files:** Create `edge/src/ha_edge/api.py`, `edge/tests/test_api.py`.

**Interfaces — Produces:**
- `class ServerClient(base_url: str, device_key: str | None = None, http: httpx.AsyncClient | None = None)`.
  - `async pair(code: str) -> tuple[int, str]` — `POST /device/pair {code}` → `(device_id, device_key)` (name and kind were set when the code was created in the dashboard).
  - `async heartbeat() -> DeviceConfig` (`@dataclass DeviceConfig(enabled_sounds: frozenset[str], heartbeat_interval_s: int)`).
  - `async send_event(sound: str, confidence: float, occurred_at: datetime) -> EventResult` (`@dataclass EventResult(event_id: int, merged: bool, alerts: int)`).
- Errors: `DeviceRevoked` (401), `RateLimited(retry_after_s: float)` (429), `ServerUnavailable` (network error / 5xx), `Rejected(detail)` (422/400; not retried).
- `class EventQueue(max_age_s=600)`: `put(sound, conf, occurred_at)`, `async drain(client, now) -> int` — sends oldest first, stops at first `ServerUnavailable`/`RateLimited` (keeps the rest), drops items older than `max_age_s`, drops `Rejected` items, re-raises `DeviceRevoked`.

- [ ] **Step 1: Failing tests (respx):** `test_pair_returns_id_and_key`; `test_heartbeat_parses_config`; `test_send_event_posts_exact_fields_only` (assert JSON keys == {"type","confidence","occurred_at"} and occurred_at is ISO-8601 with offset); `test_401_raises_device_revoked`; `test_5xx_and_connect_error_raise_server_unavailable`; `test_queue_keeps_items_on_unavailable_and_drops_after_10_min`; `test_queue_drops_rejected_and_continues`; `test_queue_reraises_revoked`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(edge): server API client with event queue`.

### Task 3: Label map and detector (pure logic)

**Files:** Create `edge/src/ha_edge/labels.py`, `edge/src/ha_edge/detector.py`, `edge/tests/test_labels.py`, `edge/tests/test_detector.py`.

**Interfaces — Produces:**
- `CATALOG_KEYS: frozenset[str]` (the 17 server keys). `YAMNET_LABELS: dict[str, tuple[str, ...]]` — AudioSet display names per key:
  scream→("Screaming",), shouting→("Shout","Yell","Bellow"), crying→("Crying, sobbing","Baby cry, infant cry","Whimper"), moaning→("Groan","Grunt"), gasping→("Gasp","Wheeze","Breathing"), coughing→("Cough",), glass_breaking→("Glass","Shatter","Breaking"), door_slam→("Slam","Door"), knocking→("Knock",), car_alarm→("Car alarm",), dog_barking→("Bark","Dog","Bow-wow"), smoke_alarm→("Smoke detector, smoke alarm","Fire alarm"), siren→("Siren","Emergency vehicle","Police car (siren)","Ambulance (siren)","Fire engine, fire truck (siren)","Civil defense siren"), explosion→("Explosion","Bang"), running_water→("Water tap, faucet","Sink (filling or washing)","Bathtub (filling or washing)"), thud_fall→("Thump, thud",), doorbell→("Doorbell","Ding-dong").
- `def build_index(class_names: Sequence[str]) -> dict[str, list[int]]` — raises `ValueError` listing any label not found in the model's class map.
- `@dataclass Detection(sound: str, confidence: float, at: float)`.
- `class Detector(index: dict[str, list[int]], threshold: float = 0.30, boost: float = 0.20, debounce_s: float = 2.0)`:
  - `def process(scores: np.ndarray, at: float, enabled: frozenset[str], playing: bool) -> list[Detection]` — per sound: confidence = max of its class scores; emit if sound ∈ enabled and confidence ≥ min(threshold + (boost if playing else 0), 0.95) and not emitted for that sound within `debounce_s`.

- [ ] **Step 1: Failing tests:** `test_catalog_keys_match_server` (import the list as a literal of the 17 keys — the server's `app/catalog.py` keys, copied verbatim); `test_build_index_reports_missing_labels`; `test_build_index_with_real_class_map` (uses `edge/tests/data/yamnet_class_map.csv` — download it in this task from the public TF models URL in Task 4 notes and commit it; 521 rows); `test_disabled_sound_never_emitted`; `test_threshold_boundary_inclusive`; `test_playback_raises_threshold` (0.40 not emitted while playing, emitted when not); `test_debounce_2s_per_sound` (t=0 emit, t=1.9 no, t=2.0 yes; another sound unaffected); `test_confidence_is_max_over_mapped_classes`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(edge): label map and detector`.

### Task 4: YAMNet classifier

**Files:** Create `edge/src/ha_edge/model.py`, `edge/tests/test_model.py`.

**Interfaces — Produces:**
- `MODEL_URL = "https://storage.googleapis.com/download.tensorflow.org/models/tflite/task_library/audio_classification/android/lite-model_yamnet_classification_tflite_1.tflite"`; `MODEL_SHA256` = the hash computed when the implementer first downloads it (record it in the report). `CLASS_MAP_URL = "https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv"`.
- `def fetch_model(dest_dir: Path, *, http: httpx.Client | None = None) -> Path` — downloads to a temp file, verifies SHA-256 (mismatch → `ModelIntegrityError`, temp deleted), renames into place; no-op if present and valid. Default dest: `platformdirs.user_cache_dir("home-awareness")`.
- `class Classifier(Protocol): class_names: list[str]; def scores(self, window: np.ndarray) -> np.ndarray` (len 521).
- `class YamnetClassifier(model_path: Path)` via `ai_edge_litert.interpreter.Interpreter`; input float32 shape (15600,); class names read from the CSV next to the model or the bundled test copy. If the tflite model exposes class labels in its metadata, prefer the CSV anyway (deterministic).
- [ ] **Step 1: Failing tests:** `test_fetch_verifies_sha_and_rejects_mismatch` (respx-mocked bytes); `test_fetch_is_noop_when_present`; `test_yamnet_scores_shape_and_range` and `test_yamnet_detects_synthetic_siren_or_tone_class` — marked `@pytest.mark.model` and skipped unless the model file exists (CI downloads it in a cached step; locally run `uv run ha-sensor fetch-model` first). For the synthetic test: a 1 kHz sine at 0.5 amplitude should score highest in a tone-like class ("Sine wave"/"Beep, bleep"); assert the top class name is in an allowed set.
- [ ] **Step 2–4:** FAIL → implement → PASS (including the model-marked tests locally after fetching).
- [ ] **Step 5: Commit** `feat(edge): YAMNet classifier with verified model download`.

### Task 5: Microphone capture and device pinning

**Files:** Create `edge/src/ha_edge/audio.py`, `edge/tests/test_audio.py`.

**Interfaces — Produces:**
- `@dataclass InputDevice(index: int, name: str, default_samplerate: float, is_default: bool)`; `def list_inputs(sd=sounddevice) -> list[InputDevice]`.
- `def resolve_device(name: str | None, devices: list[InputDevice]) -> InputDevice` — exact name match, else unique case-insensitive substring; `name=None` → the system default input, but `run` then saves that device's name so later runs are pinned. Not found → `MicNotFound(name, available=[...])`.
- `class Windower(window=15600, hop=7800)`: `def push(samples: np.ndarray) -> list[np.ndarray]` — returns complete windows (pure, tested).
- `class MicStream(device: InputDevice, sd=sounddevice)` async iterator of float32 chunks at 16 kHz mono (resample from the device rate with numpy linear interpolation if the device can't open at 16 kHz); raises `MicLost` if the stream errors/closes (device unplugged). Never reopens a different device.
- [ ] **Step 1: Failing tests** (fake `sd` module): `test_resolve_exact_then_substring`; `test_resolve_ambiguous_or_missing_raises_with_available_list`; `test_windower_emits_overlapping_windows` (lengths, hop, continuity across pushes); `test_resample_48k_to_16k_length`; `test_stream_error_raises_mic_lost_not_fallback` (Review Focus 1).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(edge): pinned microphone capture and windowing`.

### Task 6: Playback probe

**Files:** Create `edge/src/ha_edge/playback.py`, `edge/tests/test_playback.py`, `edge/tests/data/pw-dump-*.json` (fixtures).

**Interfaces — Produces:** `class PlaybackProbe(Protocol): def playing(self) -> bool`. `PipeWireProbe(run=subprocess.run, cache_s=2.0)` — runs `pw-dump`, returns True if any node has `info.props["media.class"] == "Stream/Output/Audio"` and `info.state == "running"`; caches for `cache_s`; on any failure (binary missing, bad JSON, timeout 1 s) returns False and logs once. `NullProbe` (always False). `def default_probe() -> PlaybackProbe` (PipeWire on Linux if `pw-dump` exists, else Null).
- [ ] **Step 1: Failing tests** with captured fixture JSON: running output stream → True; corked/idle → False; input-only streams → False; missing binary → False, logged once; cache respected.
- [ ] **Step 2–4:** FAIL → implement → PASS. Capture one real `pw-dump` from this machine while audio plays and while silent; reduce to the relevant nodes for the fixtures (no personal data).
- [ ] **Step 5: Commit** `feat(edge): PipeWire playback probe`.

### Task 7: Sensor loop and `ha-sensor` CLI

**Files:** Create `edge/src/ha_edge/sensor.py`, `edge/src/ha_edge/cli.py`, `edge/tests/test_sensor.py`, `edge/tests/test_cli.py`.

**Interfaces — Produces:**
- `async def run_sensor(cfg: SensorConfig, *, client, classifier, mic, probe, clock=time.time, stop: asyncio.Event) -> None` — tasks: (1) audio: for each window → `classifier.scores` → `detector.process(..., enabled, probe.playing())` → `queue.put`; (2) heartbeat every interval → update `enabled`; (3) queue drain every 1 s. `DeviceRevoked` → log "This sensor was removed. Pair it again with `ha-sensor pair`." and stop. `MicLost` → log error, stop sending, exit non-zero. Before the first successful heartbeat, `enabled` is empty (nothing sent).
- CLI (`argparse`): `ha-sensor pair --server URL --code CODE`; `ha-sensor devices`; `ha-sensor fetch-model`; `ha-sensor run [--mic NAME]`. Exit codes: 0 ok, 2 usage, 3 not paired, 4 mic lost, 5 revoked.
- [ ] **Step 1: Failing tests** with fakes: `test_nothing_sent_before_first_heartbeat`; `test_detection_flows_to_send_event`; `test_disabled_sound_dropped`; `test_revoked_stops_loop_with_exit_5`; `test_mic_lost_exits_4_without_switching`; `test_heartbeat_updates_enabled_set`; `test_pair_saves_config_0600_and_never_prints_key`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(edge): sensor loop and ha-sensor CLI`.

### Task 8: Alert agent (`ha-agent`)

**Files:** Create `edge/src/ha_edge/agent.py`, `edge/src/ha_edge/notify.py`, `edge/tests/test_agent.py`, `edge/tests/test_notify.py`; Modify `edge/src/ha_edge/cli.py`.

**Interfaces — Produces:**
- `class Notifier(Protocol): async def popup(self, title: str, body: str, actions: list[str]) -> str | None` (returns chosen action); `DesktopNotifier` (desktop-notifier lib), `FakeNotifier` in tests.
- `class AlarmPlayer(sd=sounddevice, max_s=30.0)`: `async def play() -> None` (synthesized 880/660 Hz two-tone, 0.5 amplitude, on the default output device), `def stop() -> None`. Never two alarms at once.
- `async def run_agent(cfg: AgentConfig, *, connect, notifier, alarm, clock) -> int` — connects `WS {base}/ws?token=...`; on `{"kind":"alert"}`: popup title `"{label} — {device_name}"`, body `"{HH:MM} · Home Awareness"`, actions `["I'm on it", "Stop alarm"]`; if `alarm` true → start alarm; "Stop alarm" → stop; "I'm on it" → `POST /alerts/{id}/ack` with the bearer token, stop alarm. On `alert_ack` from someone else → popup "{by_email} is handling it" and stop alarm. `device_offline` → popup. Close 4401/4403 → popup "Please log in again: ha-agent login" and return 6 (no reconnect). Other disconnects → reconnect with backoff 1, 2, 4 … 60 s.
- CLI: `ha-agent login --server URL --email E` (password via getpass; stores only the access token, 0600); `ha-agent run`; `ha-agent test-alarm` (plays 3 s).
- [ ] **Step 1: Failing tests** with a fake WS connection: alert with alarm → popup + alarm started; "Stop alarm" stops it; "I'm on it" posts ack (respx) and stops alarm; ack from other member stops alarm and shows who; 4401 → returns 6 without reconnect; generic drop → reconnects with backoff (fake clock/sleep); alarm stops itself after 30 s; `login` stores token not password.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(edge): alert agent with popup and headphone alarm`.

### Task 9: Accuracy harness and docs

**Files:** Create `scripts/eval_accuracy.py`, `edge/tests/test_eval.py`, `docs/accuracy.md`; Modify `edge/README.md`, root `README.md` (link).

**Interfaces — Produces:** `scripts/eval_accuracy.py --dataset-dir DIR [--limit N]` — uses ESC-50 (`https://github.com/karolpiczak/ESC-50`, CC BY-NC; downloaded by the user, not committed) mapping ESC-50 categories to our keys where they exist: crying_baby→crying, dog→dog_barking, glass_breaking→glass_breaking, door_wood_knock→knocking, siren→siren, coughing→coughing, door_wood_creaks→(negative), clock_alarm→(negative), plus all other categories as negatives. Runs the real classifier + detector (threshold 0.30, no playback) over each 5 s clip (any detection in the clip counts), prints per-sound precision/recall and writes a Markdown table. `docs/accuracy.md` explains method, coverage gaps (scream, smoke alarm, doorbell, thud have no ESC-50 class → "unmeasured"), and how reliability labels in `app/catalog.py` should be updated from results.
- [ ] **Step 1: Failing test** `test_metrics_from_confusion` (pure function computing precision/recall from synthetic predictions) and `test_mapping_covers_only_catalog_keys`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: (Optional, if download ≤ 15 min)** run the harness on the full ESC-50 set and commit the resulting table into `docs/accuracy.md`; otherwise leave the table marked "not yet run".
- [ ] **Step 6: Commit** `feat(edge): accuracy harness and docs`.

---

## Self-review notes
- Spec coverage: on-device detection + events only (T3, T4, T7), mic pinning (T5), speaker sensitivity (T3, T6), heartbeat config (T2, T7), alert agent popup + headphone alarm (T8), "I'm on it" from the laptop (T8), reliability measurement (T9).
- Deferred: phone browser sensor page (Plan 3), long-lived agent tokens instead of 24 h JWT (enhancement plan), Windows/macOS playback probes (NullProbe fallback).
