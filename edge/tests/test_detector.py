import numpy as np
import pytest

from ha_edge.detector import Detection, Detector

NAMES = ["a", "b", "c", "d"]
INDEX = {"scream": [0], "siren": [1, 2], "doorbell": [3]}
ALL = frozenset(INDEX)


def sc(**kw):
    s = np.zeros(4, dtype=np.float32)
    for k, v in kw.items():
        s[int(k[1:])] = v
    return s


def test_disabled_sound_never_emitted():
    d = Detector(INDEX)
    assert d.process(sc(i0=0.9), 0.0, frozenset({"siren"}), False) == []


def test_threshold_boundary_inclusive():
    d = Detector(INDEX, threshold=0.5)
    assert d.process(sc(i0=0.49), 0.0, ALL, False) == []
    out = d.process(sc(i0=0.5), 1.0, ALL, False)
    assert out == [Detection("scream", pytest.approx(0.5), 1.0)]


def test_playback_raises_threshold():
    d = Detector(INDEX)
    assert d.process(sc(i0=0.40), 0.0, ALL, True) == []
    assert len(d.process(sc(i0=0.40), 0.5, ALL, False)) == 1


def test_boosted_threshold_capped():
    d = Detector(INDEX, threshold=0.9, boost=0.2)
    assert len(d.process(sc(i0=0.96), 0.0, ALL, True)) == 1


def test_debounce_2s_per_sound():
    d = Detector(INDEX)
    assert len(d.process(sc(i0=0.9), 0.0, ALL, False)) == 1
    assert d.process(sc(i0=0.9), 1.9, ALL, False) == []
    other = d.process(sc(i0=0.9, i3=0.9), 1.9, ALL, False)
    assert [x.sound for x in other] == ["doorbell"]
    assert [x.sound for x in d.process(sc(i0=0.9), 2.0, ALL, False)] == ["scream"]


def test_confidence_is_max_over_mapped_classes():
    d = Detector(INDEX)
    out = d.process(sc(i1=0.35, i2=0.8), 0.0, ALL, False)
    assert out[0].sound == "siren"
    assert out[0].confidence == pytest.approx(0.8)


def test_bad_shape_raises():
    d = Detector(INDEX)
    with pytest.raises(ValueError):
        d.process(np.zeros(3), 0.0, ALL, False)
    with pytest.raises(ValueError):
        d.process(np.zeros((2, 4)), 0.0, ALL, False)
