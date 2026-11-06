# Detection accuracy

How well the on-device YAMNet detector recognises each catalog sound, measured on real labelled clips. The numbers back the reliability labels (`good`, `fair`, `experimental`) shown in the app.

## Method

- Dataset: [ESC-50](https://github.com/karolpiczak/ESC-50), 2000 five-second environmental clips, 50 classes, 40 clips each. Licence CC BY-NC. We do not redistribute it or commit any audio; run the script on your own download.
- Each clip is read (44.1 kHz mono WAV), resampled to 16 kHz, cut into the sensor's windows (0.975 s, hop 0.4875 s) and scored by the same `YamnetClassifier` and `Detector` the sensor uses. All sounds are enabled and the machine is treated as not playing audio. A sound counts as detected for a clip if it fires in any window.
- For a given sound, clips of its mapped ESC-50 class are positives; every other clip (all 49 other classes, including `door_wood_creaks` and `clock_alarm`) is a negative.
- Run at thresholds 0.30 (the default) and 0.50.
- Reproduce (from `edge/`): `uv run python ../scripts/eval_accuracy.py --dataset-dir ESC-50-master --output table.md`

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

| Sound | Threshold | Clips | Precision | Recall | F1 | FP / 100 negatives |
|---|---|---|---|---|---|---|
| crying | 0.30 | 40 | 0.70 | 0.95 | 0.81 | 0.8 |
| crying | 0.50 | 40 | 0.80 | 0.88 | 0.83 | 0.5 |
| dog_barking | 0.30 | 40 | 0.78 | 1.00 | 0.88 | 0.6 |
| dog_barking | 0.50 | 40 | 0.93 | 0.95 | 0.94 | 0.2 |
| glass_breaking | 0.30 | 40 | 0.85 | 0.82 | 0.84 | 0.3 |
| glass_breaking | 0.50 | 40 | 0.91 | 0.72 | 0.81 | 0.2 |
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
| glass_breaking | 0.84 | 0.81 | good | good | 0.30 (recall 0.82 vs 0.72) |
| knocking | 0.74 | 0.74 | fair | fair | either; 0.50 has fewer false positives |
| siren | 0.81 | 0.92 | good | good | 0.50 |
| coughing | 0.66 | 0.78 | fair | fair | 0.50 |

No label changes are recommended: every measured sound already carries the label its F1 supports. crying and siren at 0.30 sit only just above the good cut-off; siren and coughing gain clearly from a 0.50 threshold, which the controller may want to consider as a per-sound default.

Caveats: ESC-50 clips are short, clean and foreground-dominant, so real homes (background noise, distance, overlapping sounds) will score lower. `knocking` and `coughing` map to ESC-50 classes that are single events, whereas the catalog describes repeated knocking and a long coughing fit. Sound-level false-positive rates are per five-second clip, not per hour of audio. Treat these as upper bounds.
