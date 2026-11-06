import numpy as np
import pytest

from ha_edge.detector import Detection, Detector

INDEX = {"scream": [0], "siren": [1, 2], "doorbell": [3]}
ALL = frozenset(INDEX)


def sc(**kw):
    s = np.zeros(4, dtype=np.float32)
    for k, v in kw.items():
        s[int(k[1:])] = v
    return s


SILENT = sc()


def emit(d, scores, at, enabled=ALL, playing=False, **kw):
    """One window, then a silent one: whatever the first window held comes out."""
    return d.process(scores, at, enabled, playing, **kw) + d.process(
        SILENT, at + 0.25, enabled, playing, **kw
    )


def test_disabled_sound_never_emitted():
    d = Detector(INDEX)
    assert emit(d, sc(i0=0.9), 0.0, frozenset({"siren"})) == []


def test_threshold_boundary_inclusive():
    d = Detector(INDEX, threshold=0.5)
    assert emit(d, sc(i0=0.49), 0.0) == []
    out = emit(d, sc(i0=0.5), 1.0)
    assert out == [Detection("scream", pytest.approx(0.5), 1.0)]


def test_playback_raises_threshold():
    d = Detector(INDEX)
    assert emit(d, sc(i0=0.40), 0.0, playing=True) == []
    assert len(emit(d, sc(i0=0.40), 0.5)) == 1


def test_boosted_threshold_capped():
    d = Detector(INDEX, threshold=0.9, boost=0.2)
    assert len(emit(d, sc(i0=0.96), 0.0, playing=True)) == 1


def test_debounce_2s_from_emit_per_sound():
    d = Detector(INDEX)
    assert d.process(sc(i0=0.9), 0.0, ALL, False) == []  # held one window
    assert len(d.process(sc(i0=0.9), 0.5, ALL, False)) == 1  # emitted at 0.5
    assert d.process(sc(i0=0.9), 2.4, ALL, False) == []  # 1.9 s after the emit
    other = emit(d, sc(i0=0.9, i3=0.9), 2.4)
    assert [x.sound for x in other] == ["doorbell"]
    assert d.process(sc(i0=0.9), 2.5, ALL, False) == []  # 2.0 s after: held again
    assert [x.sound for x in d.process(SILENT, 3.0, ALL, False)] == ["scream"]


def test_confidence_is_max_over_mapped_classes():
    d = Detector(INDEX)
    out = emit(d, sc(i1=0.35, i2=0.8), 0.0)
    assert out[0].sound == "siren"
    assert out[0].confidence == pytest.approx(0.8)


def test_bad_shape_raises():
    d = Detector(INDEX)
    with pytest.raises(ValueError):
        d.process(np.zeros(3), 0.0, ALL, False)
    with pytest.raises(ValueError):
        d.process(np.zeros((2, 4)), 0.0, ALL, False)


def test_enabling_after_disabled_emits_immediately():
    d = Detector(INDEX)
    assert emit(d, sc(i0=0.9), 0.0, frozenset({"siren"})) == []
    assert [x.sound for x in emit(d, sc(i0=0.9), 0.5)] == ["scream"]


def test_playback_matters_only_between_threshold_and_boosted_threshold():
    d = Detector(INDEX)  # 0.30, +0.20
    assert not d.playback_matters(sc(), ALL)  # silence
    assert not d.playback_matters(sc(i0=0.29), ALL)
    assert d.playback_matters(sc(i0=0.30), ALL)
    assert d.playback_matters(sc(i2=0.49), ALL)
    assert not d.playback_matters(sc(i0=0.50), ALL)  # emitted either way
    assert not d.playback_matters(sc(i0=0.40), frozenset({"siren"}))  # not enabled


def test_playback_matters_uses_capped_boost():
    d = Detector(INDEX, threshold=0.9, boost=0.2)
    assert d.playback_matters(sc(i0=0.94), ALL)
    assert not d.playback_matters(sc(i0=0.96), ALL)


def test_per_sound_threshold_respected():
    d = Detector(INDEX)  # global 0.30
    th = {"scream": 0.6}
    assert emit(d, sc(i0=0.59), 0.0, thresholds=th) == []
    assert [x.sound for x in emit(d, sc(i0=0.6), 1.0, thresholds=th)] == ["scream"]


def test_per_sound_threshold_missing_key_falls_back_to_global():
    d = Detector(INDEX)
    out = emit(d, sc(i3=0.35), 0.0, thresholds={"scream": 0.6})
    assert [x.sound for x in out] == ["doorbell"]


def test_per_sound_threshold_boosted_and_capped_while_playing():
    d = Detector(INDEX)
    th = {"scream": 0.5, "doorbell": 0.9}
    # 0.5 + 0.2 boost = 0.7
    assert emit(d, sc(i0=0.69), 0.0, playing=True, thresholds=th) == []
    out = emit(d, sc(i0=0.71), 3.0, playing=True, thresholds=th)  # float32: 0.7 is 0.6999...
    assert [x.sound for x in out] == ["scream"]
    # 0.9 + 0.2 capped at 0.95
    out = emit(d, sc(i3=0.96), 6.0, playing=True, thresholds=th)
    assert [x.sound for x in out] == ["doorbell"]


def test_received_thresholds_clamped_and_non_finite_ignored():
    d = Detector(INDEX)
    # 0.0 is clamped up to 0.05; 2.0 is clamped down to 0.95
    assert emit(d, sc(i0=0.04), 0.0, thresholds={"scream": 0.0}) == []
    assert len(emit(d, sc(i0=0.05), 3.0, thresholds={"scream": 0.0})) == 1
    assert len(emit(d, sc(i3=0.96), 6.0, thresholds={"doorbell": 2.0})) == 1
    # NaN would make every comparison false (always emit): fall back to the global 0.30
    assert emit(d, sc(i0=0.2), 9.0, thresholds={"scream": float("nan")}) == []


def test_playback_matters_uses_per_sound_threshold():
    d = Detector(INDEX)
    th = {"scream": 0.6}
    assert not d.playback_matters(sc(i0=0.4), ALL, thresholds=th)  # below 0.6: dropped anyway
    assert d.playback_matters(sc(i0=0.7), ALL, thresholds=th)  # in [0.6, 0.8)
    assert not d.playback_matters(sc(i0=0.8), ALL, thresholds=th)  # emitted either way
    assert d.playback_matters(sc(i3=0.4), ALL, thresholds=th)  # doorbell: global 0.30


def test_partial_then_peak_emits_once_with_the_peak():
    d = Detector(INDEX)
    assert d.process(sc(i0=0.35), 0.0, ALL, False) == []  # crosses 0.30: held
    out = d.process(sc(i0=0.9), 0.5, ALL, False)
    assert out == [Detection("scream", pytest.approx(0.9), 0.0)]
    assert d.process(sc(i0=0.9), 1.0, ALL, False) == []  # debounced from the emit
    assert d.flush() == []


def test_lower_next_window_emits_the_held_confidence():
    d = Detector(INDEX)
    d.process(sc(i0=0.8), 0.0, ALL, False)
    out = d.process(sc(i0=0.4), 0.5, ALL, False)
    assert [(x.sound, x.confidence) for x in out] == [("scream", pytest.approx(0.8))]


def test_single_window_spike_emits():
    d = Detector(INDEX)
    assert d.process(sc(i3=0.9), 0.0, ALL, False) == []
    assert [x.sound for x in d.process(SILENT, 0.5, ALL, False)] == ["doorbell"]


def test_flush_emits_held_candidate_once():
    d = Detector(INDEX)
    assert d.process(sc(i0=0.6), 0.0, ALL, False) == []
    assert d.flush() == [Detection("scream", pytest.approx(0.6), 0.0)]
    assert d.flush() == []


def test_held_candidate_dropped_if_sound_disabled_meanwhile():
    d = Detector(INDEX)
    d.process(sc(i0=0.9), 0.0, ALL, False)
    assert d.process(sc(i0=0.9), 0.5, frozenset({"siren"}), False) == []
    assert d.flush() == []
