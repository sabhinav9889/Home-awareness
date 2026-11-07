# Plan 2b: Detection Tuning (from live testing)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Fix the misdetections found in the first live test on the user's laptop, and give users tools to see and fix their microphone setup.

**Evidence (7 Nov, live mic, built-in laptop microphone):**
- A chime-style doorbell clip scored **Chime 0.67, Wind chime 0.67, Bell 0.33**; "Doorbell"/"Ding-dong" < 0.05 → no doorbell event.
- A synthetic two-tone chime scored **Ding 0.80, Clang 0.50, Ding-dong 0.33**.
- A "ding dong" clip produced a **false `glass_breaking` event at 0.41** (plain "Glass" label).
- A shouted scream at mic volume 0.49 **clipped** (peaks 1.18–1.22) and was classified as Snoring/Cough/Pig/Speech; "Screaming" < 0.05.

**Spec:** `docs/design.md` §5 (catalog, reliability), §7 (devices). Plan 2 (edge) and Plan 1b (thresholds) are merged.

## Global Constraints
- Privacy: no audio is ever written to disk or sent; fixtures and logs hold **YAMNet score vectors / numbers only**.
- Edge tests: `cd edge && uv run pytest -q && uv run ruff check .`; server tests: `uv run pytest -q && uv run ruff check .` at the root (a file lock serialises runs).
- Commits end with the Co-Authored-By line the implementer's harness gives it. Never push.

## Task 1: Label rules (doorbell, glass) — edge
**Files:** `edge/src/ha_edge/labels.py`, `edge/src/ha_edge/detector.py`, tests.
- Replace the plain label tuples with rules: `@dataclass(frozen=True) class LabelRule(name: str, min_score: float = 0.0, unless_stronger: tuple[str, ...] = ())`. `YAMNET_LABELS: dict[str, tuple[LabelRule, ...]]` (plain strings still accepted and converted, so other sounds don't change).
- A sound's confidence for a window = max over its rules of `score[name]` where `score >= rule.min_score` and no label in `unless_stronger` scores strictly higher; 0 if none qualifies. The detector's per-sound threshold, playback boost, peak hold and debounce apply to that confidence exactly as now.
- **doorbell:** `Doorbell`, `Ding-dong`, `Chime (min 0.50, unless_stronger=("Wind chime",))`, `Ding (min 0.50)`.
- **glass_breaking:** `Shatter`, `Glass (min 0.60)`. (`Breaking` stays removed.)
- `build_index` validates every rule name and `unless_stronger` name exists in the class map.
- Tests (score-vector fixtures, no audio): the observed vectors above → doorbell from Chime 0.67 + Wind chime 0.67 ✔ (tie is not "strictly higher"), Wind chime 0.80 + Chime 0.60 ✘, Ding 0.80 ✔, Ding 0.40 ✘; Glass 0.41 ✘ (the false alarm), Glass 0.65 ✔, Shatter 0.35 ✔; unrelated sounds unchanged.

## Task 2: Measured regression check — edge + docs
**Files:** `edge/src/ha_edge/evaluation.py` (if needed), `scripts/eval_accuracy.py`, `docs/accuracy.md`, `edge/tests/test_model.py` (model-marked).
- Model-marked tests with deterministic synthetic signals through the REAL classifier + detector: two-tone ding-dong chime (as used in the live debug) → emits `doorbell`; a pure 1 kHz tone → emits nothing.
- Re-run the ESC-50 evaluation with the new rules (download as before into the scratchpad, delete afterwards) and update `docs/accuracy.md`: glass_breaking precision/recall before vs after, plus false positives from ESC-50 `church_bells`, `clock_alarm`, `glass_breaking`, `can_opening`, `door_wood_knock` categories for doorbell and glass. Add a "Live test, 7 Nov" section with the evidence above.

## Task 3: Clipping warning — edge + server + dashboard
**Files:** edge `audio.py`/`sensor.py`/`api.py`; server `app/devices/{models,router,schemas}.py` + migration; dashboard `app/web/static/js/views/sensors.js` (+ overview sensor card); tests in all three.
- Edge: per window, `clip_ratio = mean(|x| >= 0.99)`. "Mic overloading" when ≥ 1 % clipped samples in ≥ 3 of the last 10 windows. Log one warning per 5 minutes: `Microphone is overloading — lower its input level (e.g. wpctl set-volume @DEFAULT_AUDIO_SOURCE@ 0.3)`. Heartbeat body adds optional `{"mic_clipping": bool}` (state over the last minute).
- Server: `POST /device/heartbeat` accepts optional `mic_clipping` (default false, backward compatible); `Device.mic_clipping: bool` (migration, default false); exposed in `GET /devices`.
- Dashboard: sensor card / sensors list shows an amber note "Microphone is overloading — lower its input level" when true.

## Task 4: `ha-sensor check`
**Files:** `edge/src/ha_edge/cli.py`, new `edge/src/ha_edge/check.py`, tests; remove `scripts/mic_check.py` (superseded).
- `ha-sensor check [--seconds 15] [--mic NAME]`: uses the pinned mic (or default), prints per window: level, peak, CLIPPING marker, top-3 YAMNet classes, and catalog sounds scoring ≥ 0.05 with `✓` when they would emit under the current thresholds (from the server's config if paired and reachable, else defaults). Summary at the end: loudest level, clipping windows, best score per catalog sound, and a one-line advice (lower/raise input level).
- Tests with fake mic + fake classifier; README section "Check your microphone".
