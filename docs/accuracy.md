# Detection accuracy

How well the on-device YAMNet detector recognises each catalog sound, measured on real labelled clips. The numbers back the reliability labels (`good`, `fair`, `experimental`) shown in the app.

## Method

- Dataset: [ESC-50](https://github.com/karolpiczak/ESC-50), 2000 five-second environmental clips, 50 classes, 40 clips each. Licence CC BY-NC. We do not redistribute it or commit any audio; run the script on your own download.
- Each clip is read (44.1 kHz mono WAV), resampled to 16 kHz, cut into the sensor's windows (0.975 s, hop 0.4875 s) and scored by the same `YamnetClassifier` and `Detector` the sensor uses. All sounds are enabled and the machine is treated as not playing audio. A sound counts as detected for a clip if it fires in any window.
- For a given sound, clips of its mapped ESC-50 class are positives; every other clip (all 49 other classes, including `door_wood_creaks` and `clock_alarm`) is a negative.
- Run at thresholds 0.30 (the default) and 0.50.
- Reproduce (from `edge/`): `uv run python -I ../scripts/eval_accuracy.py --dataset-dir ESC-50-master --output table.md`. It prints the metrics table and a per-class breakdown of where `doorbell` and `glass_breaking` fire among the confusable ESC-50 classes.

## ESC-50 to catalog mapping

| ESC-50 class | Catalog key |
|---|---|
| crying_baby | crying |
| dog | dog_barking |
| glass_breaking | glass_breaking |
| door_wood_knock | knocking |
| siren | siren |
| coughing | coughing |

## Results (ESC-50, 40 positive clips and 1960 negatives per sound)

With the label rules of Plan 2b (7 Nov 2026); the glass_breaking rows changed, every other row is unchanged. See "Label rules" below for before/after.

| Sound | Threshold | Clips | Precision | Recall | F1 | FP / 100 negatives |
|---|---|---|---|---|---|---|
| crying | 0.30 | 40 | 0.70 | 0.95 | 0.81 | 0.8 |
| crying | 0.50 | 40 | 0.80 | 0.88 | 0.83 | 0.5 |
| dog_barking | 0.30 | 40 | 0.78 | 1.00 | 0.88 | 0.6 |
| dog_barking | 0.50 | 40 | 0.93 | 0.95 | 0.94 | 0.2 |
| glass_breaking | 0.30 | 40 | 0.94 | 0.75 | 0.83 | 0.1 |
| glass_breaking | 0.50 | 40 | 0.93 | 0.70 | 0.80 | 0.1 |
| knocking | 0.30 | 40 | 0.78 | 0.70 | 0.74 | 0.4 |
| knocking | 0.50 | 40 | 0.87 | 0.65 | 0.74 | 0.2 |
| siren | 0.30 | 40 | 0.68 | 1.00 | 0.81 | 1.0 |
| siren | 0.50 | 40 | 0.87 | 0.97 | 0.92 | 0.3 |
| coughing | 0.30 | 40 | 0.53 | 0.88 | 0.66 | 1.6 |
| coughing | 0.50 | 40 | 0.72 | 0.85 | 0.78 | 0.7 |

Unmeasured (no ESC-50 class): scream, shouting, moaning, gasping, door_slam, car_alarm, smoke_alarm, explosion, running_water, thud_fall, doorbell. Their labels are not backed by measurement here.

## What this means

Recommended labels from F1 (good >= 0.8, fair 0.5-0.8, experimental < 0.5), at the default 0.30 threshold, compared with `app/catalog.py` today:

| Sound | F1 at 0.30 | F1 at 0.50 | Recommended | Current | Better threshold |
|---|---|---|---|---|---|
| crying | 0.81 | 0.83 | good | good | 0.50 slightly (fewer false positives, recall 0.88) |
| dog_barking | 0.88 | 0.94 | good | good | 0.50 |
| glass_breaking | 0.83 | 0.80 | good | good | 0.30 (recall 0.75 vs 0.70) |
| knocking | 0.74 | 0.74 | fair | fair | either; 0.50 has fewer false positives |
| siren | 0.81 | 0.92 | good | good | 0.50 |
| coughing | 0.66 | 0.78 | fair | fair | 0.50 |

No label changes are recommended: every measured sound already carries the label its F1 supports. crying and siren at 0.30 sit only just above the good cut-off; siren and coughing gain clearly from a 0.50 threshold, which the controller may want to consider as a per-sound default.

These "better threshold" values are now applied as per-sound defaults (`default_min_confidence` in `app/catalog.py`: 0.50 for crying, dog_barking, siren and coughing; 0.30 for glass_breaking, knocking and every unmeasured sound). Members can override them per sound; the sensor uses the lowest threshold any member wants and the rule engine applies each member's own.

Caveats: ESC-50 clips are short, clean and foreground-dominant, so real homes (background noise, distance, overlapping sounds) will score lower. `knocking` and `coughing` map to ESC-50 classes that are single events, whereas the catalog describes repeated knocking and a long coughing fit. Sound-level false-positive rates are per five-second clip, not per hour of audio. Treat these as upper bounds.

## Label rules (Plan 2b, 7 Nov 2026)

A catalog sound used to fire on the highest score of any of its YAMNet labels. Some labels need conditions (`edge/src/ha_edge/labels.py`, `LabelRule`):

| Sound | Labels before | Labels now |
|---|---|---|
| doorbell | Doorbell, Ding-dong | Doorbell, Ding-dong, Chime (score >= 0.50 and Wind chime not strictly higher), Ding (score >= 0.50) |
| glass_breaking | Glass, Shatter | Shatter, Glass (score >= 0.60) |

A rule that fails its condition contributes 0; the sound's per-member threshold, playback boost, peak hold and debounce then apply to the best qualifying score as before.

### Before and after on ESC-50

Same scores for every clip (one YAMNet pass), evaluated with the old and the new labels.

glass_breaking (40 positives, 1960 negatives):

| Labels | Threshold | Precision | Recall | F1 | False positives (clips) |
|---|---|---|---|---|---|
| before | 0.30 | 0.85 | 0.82 | 0.84 | 6 (pouring_water 4, can_opening 1, clock_alarm 1) |
| after | 0.30 | 0.94 | 0.75 | 0.83 | 2 (pouring_water 1, clock_alarm 1) |
| before | 0.50 | 0.91 | 0.72 | 0.81 | 3 (pouring_water 2, clock_alarm 1) |
| after | 0.50 | 0.93 | 0.70 | 0.80 | 2 (pouring_water 1, clock_alarm 1) |

Requiring 0.60 for plain "Glass" removes two thirds of the false alarms at 0.30 and costs three positive clips (33 -> 30 of 40); F1 is essentially unchanged and the label stays `good`.

Detections per confusable ESC-50 class (clips fired / 40):

| Sound | Labels | Threshold | church_bells | clock_alarm | glass_breaking | can_opening | door_wood_knock |
|---|---|---|---|---|---|---|---|
| doorbell | before | 0.30 | 1 | 1 | 0 | 0 | 0 |
| doorbell | after | 0.30 | 1 | 2 | 0 | 0 | 0 |
| doorbell | before | 0.50 | 1 | 0 | 0 | 0 | 0 |
| doorbell | after | 0.50 | 1 | 1 | 0 | 0 | 0 |
| glass_breaking | before | 0.30 | 0 | 1 | 33 | 1 | 0 |
| glass_breaking | after | 0.30 | 0 | 1 | 30 | 0 | 0 |
| glass_breaking | before | 0.50 | 0 | 1 | 29 | 0 | 0 |
| glass_breaking | after | 0.50 | 0 | 1 | 28 | 0 | 0 |

doorbell has no ESC-50 class, so only its false positives are measured: across all 2000 clips it fired on 2 before and 3 after at 0.30 (1 and 2 at 0.50), all church bells or clock alarms. The new Chime/Ding rules add one clock-alarm clip; church bells are mostly vetoed or below 0.50. Its recall on real chime doorbells is not measured by ESC-50; it is backed only by the live test below and a synthetic two-tone chime test (`edge/tests/test_model.py`, run with the real model: the chime emits `doorbell` and nothing else; a pure 1 kHz tone emits nothing).

## Live test, 7 Nov 2026

First test on the user's laptop with its built-in microphone, sounds played from a phone or made in the room. YAMNet scores (per 0.975 s window):

- A chime-style doorbell clip scored **Chime 0.67, Wind chime 0.67, Bell 0.33**; "Doorbell" and "Ding-dong" stayed below 0.05, so no doorbell event. Now detected: Chime 0.67 is at least 0.50 and Wind chime is not strictly higher.
- A synthetic two-tone chime scored **Ding 0.80, Clang 0.50, Ding-dong 0.33**. Under the old labels only Ding-dong counted: 0.33, just over the default 0.30 and missed at any higher threshold. Now its confidence is 0.80 via the Ding rule.
- A "ding dong" clip produced a **false `glass_breaking` event at 0.41** from the plain "Glass" label. Now ignored: Glass needs 0.60.
- A shouted scream at microphone volume 0.49 **clipped** (peaks 1.18-1.22) and was classified as Snoring, Cough, Pig and Speech; "Screaming" stayed below 0.05. Labels cannot fix distorted input: the sensor now warns when the microphone overloads (log line and an amber note on the dashboard), and `ha-sensor check` shows level, peak and clipping live so the user can set the input level.
