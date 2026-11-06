#!/usr/bin/env python3
"""Measure per-sound accuracy of the edge detector on ESC-50 (CC BY-NC, download it yourself).

Usage (from edge/): uv run python ../scripts/eval_accuracy.py --dataset-dir ESC-50-master
"""

import argparse
import csv
import sys
from pathlib import Path

from ha_edge.detector import Detector
from ha_edge.evaluation import (
    clip_scores,
    compute_metrics,
    detections_from_scores,
    format_markdown,
    read_wav_16k,
)
from ha_edge.labels import build_index
from ha_edge.model import YamnetClassifier, fetch_model

THRESHOLDS = (0.30, 0.50)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset-dir", type=Path, required=True)
    ap.add_argument("--limit", type=int, default=None, help="only the first N clips")
    ap.add_argument("--output", type=Path, default=None, help="write the Markdown table here")
    args = ap.parse_args()

    meta = args.dataset_dir / "meta" / "esc50.csv"
    rows = list(csv.DictReader(meta.read_text("utf-8").splitlines()))
    if args.limit:
        rows = rows[: args.limit]
    clf = YamnetClassifier(fetch_model())
    index = build_index(clf.class_names)
    preds: dict[float, list] = {t: [] for t in THRESHOLDS}
    for n, row in enumerate(rows, 1):
        scores = clip_scores(read_wav_16k(args.dataset_dir / "audio" / row["filename"]), clf)
        for t in THRESHOLDS:
            det = Detector(index, threshold=t, num_classes=len(clf.class_names))
            preds[t].append((row["category"], detections_from_scores(scores, det)))
        if n % 100 == 0:
            print(f"{n}/{len(rows)}", file=sys.stderr)
    results = {t: compute_metrics(p) for t, p in preds.items()}
    table = format_markdown(results)
    print(table)
    if args.output:
        args.output.write_text(table, "utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
