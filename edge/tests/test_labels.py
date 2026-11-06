import csv
from pathlib import Path

import pytest

from ha_edge.labels import CATALOG_KEYS, YAMNET_LABELS, build_index

SERVER_KEYS = [
    "scream",
    "shouting",
    "crying",
    "moaning",
    "gasping",
    "coughing",
    "glass_breaking",
    "door_slam",
    "knocking",
    "car_alarm",
    "dog_barking",
    "smoke_alarm",
    "siren",
    "explosion",
    "running_water",
    "thud_fall",
    "doorbell",
]


def test_catalog_keys_match_server():
    assert len(SERVER_KEYS) == 17
    assert CATALOG_KEYS == frozenset(SERVER_KEYS)
    assert set(YAMNET_LABELS) == set(SERVER_KEYS)


def test_build_index_reports_missing_labels():
    with pytest.raises(ValueError) as exc:
        build_index(["Screaming", "Cough"])
    assert "Shout" in str(exc.value)


def test_build_index_with_real_class_map():
    path = Path(__file__).parent / "data" / "yamnet_class_map.csv"
    with path.open(newline="") as f:
        names = [row["display_name"] for row in csv.DictReader(f)]
    assert len(names) == 521
    index = build_index(names)
    assert set(index) == CATALOG_KEYS
    for key, idxs in index.items():
        assert len(idxs) == len(YAMNET_LABELS[key])
        assert [names[i] for i in idxs] == list(YAMNET_LABELS[key])
