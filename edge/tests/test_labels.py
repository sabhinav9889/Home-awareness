import csv
from pathlib import Path

import numpy as np
import pytest

from ha_edge.detector import Detector
from ha_edge.labels import (
    CATALOG_KEYS,
    YAMNET_LABELS,
    IndexRule,
    LabelRule,
    build_index,
    sound_confidence,
)

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


def _class_names() -> list[str]:
    path = Path(__file__).parent / "data" / "yamnet_class_map.csv"
    with path.open(newline="") as f:
        return [row["display_name"] for row in csv.DictReader(f)]


NAMES = _class_names()
INDEX = build_index(NAMES)


def test_build_index_with_real_class_map():
    assert len(NAMES) == 521
    assert set(INDEX) == CATALOG_KEYS
    for key, rules in INDEX.items():
        assert len(rules) == len(YAMNET_LABELS[key])
        for rule, label in zip(rules, YAMNET_LABELS[key], strict=True):
            assert NAMES[rule.index] == label.name
            assert rule.min_score == label.min_score
            assert [NAMES[i] for i in rule.unless_stronger] == list(label.unless_stronger)


def test_no_generic_labels():
    banned = {"Door", "Dog", "Breathing", "Breaking", "Bang", "Grunt"}
    used = {rule.name for rules in YAMNET_LABELS.values() for rule in rules}
    assert not banned & used


def test_every_catalog_value_is_a_rule_tuple():
    for rules in YAMNET_LABELS.values():
        assert isinstance(rules, tuple)
        assert all(isinstance(r, LabelRule) for r in rules)


def test_plain_labels_become_unconditional_rules():
    assert YAMNET_LABELS["scream"] == (LabelRule("Screaming"),)
    assert YAMNET_LABELS["shouting"] == (LabelRule("Shout"), LabelRule("Yell"), LabelRule("Bellow"))


def test_doorbell_and_glass_rules():
    assert YAMNET_LABELS["doorbell"] == (
        LabelRule("Doorbell"),
        LabelRule("Ding-dong"),
        LabelRule("Chime", min_score=0.50, unless_stronger=("Wind chime",)),
        LabelRule("Ding", min_score=0.50),
    )
    assert YAMNET_LABELS["glass_breaking"] == (
        LabelRule("Shatter"),
        LabelRule("Glass", min_score=0.60),
    )


def test_build_index_reports_missing_unless_stronger_label(monkeypatch):
    from ha_edge import labels

    monkeypatch.setitem(
        labels.YAMNET_LABELS, "doorbell", (LabelRule("Doorbell", unless_stronger=("Nope",)),)
    )
    with pytest.raises(ValueError) as exc:
        build_index(NAMES)
    assert "Nope" in str(exc.value)


def vec(**named: float) -> np.ndarray:
    """A YAMNet score vector with the given display names set (spaces etc. via dict)."""
    s = np.zeros(len(NAMES), dtype=np.float32)
    for name, v in named.items():
        s[NAMES.index(name)] = v
    return s


def conf(sound: str, **named: float) -> float:
    return sound_confidence(vec(**named), INDEX[sound])


# Observed live on 7 Nov (built-in laptop mic) and the rules' edges.
@pytest.mark.parametrize(
    ("sound", "scores", "expected"),
    [
        # chime doorbell: Chime 0.67 tied with Wind chime 0.67 -> a tie is not "stronger"
        ("doorbell", {"Chime": 0.67, "Wind chime": 0.67, "Bell": 0.33}, 0.67),
        ("doorbell", {"Wind chime": 0.80, "Chime": 0.60}, 0.0),
        ("doorbell", {"Chime": 0.45}, 0.0),  # below Chime's 0.50 minimum
        ("doorbell", {"Ding": 0.80, "Clang": 0.50, "Ding-dong": 0.33}, 0.80),
        ("doorbell", {"Ding": 0.40}, 0.0),
        ("doorbell", {"Ding": 0.40, "Ding-dong": 0.33}, 0.33),  # Ding-dong unconditional
        ("doorbell", {"Doorbell": 0.10}, 0.10),
        ("glass_breaking", {"Glass": 0.41}, 0.0),  # the live false alarm
        ("glass_breaking", {"Glass": 0.65}, 0.65),
        ("glass_breaking", {"Shatter": 0.35}, 0.35),
        ("glass_breaking", {"Breaking": 0.9}, 0.0),
        ("scream", {"Screaming": 0.42}, 0.42),
        ("siren", {"Siren": 0.2, "Civil defense siren": 0.7}, 0.7),
    ],
)
def test_sound_confidence(sound, scores, expected):
    assert conf(sound, **scores) == pytest.approx(expected)


def test_sound_confidence_with_raw_rules():
    s = np.array([0.6, 0.7, 0.2], dtype=np.float32)
    assert sound_confidence(s, (IndexRule(0, min_score=0.5, unless_stronger=(1,)),)) == 0.0
    assert sound_confidence(s, (IndexRule(0, min_score=0.5, unless_stronger=(2,)),)) == (
        pytest.approx(0.6)
    )
    assert sound_confidence(s, ()) == 0.0


def _emitted(scores: np.ndarray) -> set[str]:
    d = Detector(INDEX, num_classes=len(NAMES))
    out = d.process(scores, 0.0, CATALOG_KEYS, playing=False) + d.flush()
    return {x.sound for x in out}


def test_detector_emits_doorbell_for_live_chime_vector():
    assert "doorbell" in _emitted(vec(**{"Chime": 0.67, "Wind chime": 0.67, "Bell": 0.33}))
    assert "doorbell" in _emitted(vec(Ding=0.80, Clang=0.50, **{"Ding-dong": 0.33}))


def test_detector_ignores_wind_chime_and_plain_glass():
    assert _emitted(vec(**{"Wind chime": 0.80, "Chime": 0.60})) == set()
    assert _emitted(vec(Glass=0.41)) == set()
    assert _emitted(vec(Glass=0.65)) == {"glass_breaking"}
