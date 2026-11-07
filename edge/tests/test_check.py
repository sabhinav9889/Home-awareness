"""ha-sensor check: live microphone diagnostics (fake mic + fake classifier, no audio)."""

import numpy as np
import pytest

from ha_edge.audio import MicLost
from ha_edge.check import advice, run_check
from ha_edge.model import load_class_names

NAMES = load_class_names()
WINDOW = 15600
HOP = 7800


def vec(**named: float) -> np.ndarray:
    s = np.zeros(len(NAMES), dtype=np.float32)
    for name, v in named.items():
        s[NAMES.index(name)] = v
    return s


class FakeMic:
    def __init__(self, chunks, then=None):
        self.chunks = chunks
        self.then = then
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self.chunks:
            yield c
        if self.then is not None:
            raise self.then


class FakeClassifier:
    def __init__(self, vectors):
        self.class_names = NAMES
        self.vectors = list(vectors)
        self.windows = []

    def scores(self, window):
        self.windows.append(window)
        return self.vectors[min(len(self.windows) - 1, len(self.vectors) - 1)]


def chunk(amplitude: float, n: int = WINDOW, clipped: int = 0) -> np.ndarray:
    c = np.full(n, amplitude, dtype=np.float32)
    if clipped:
        c[-clipped:] = 1.0  # at the end, so the next (overlapping) window holds it too
    return c


async def _run(chunks, vectors, **kw):
    lines: list[str] = []
    kw.setdefault("seconds", 60)
    kw.setdefault("thresholds", {})
    summary = await run_check(FakeClassifier(vectors), FakeMic(chunks), out=lines.append, **kw)
    return summary, lines


async def test_prints_level_peak_top3_and_catalog_sounds_per_window():
    v = vec(**{"Chime": 0.67, "Wind chime": 0.60, "Bell": 0.33, "Glass": 0.41})
    summary, lines = await _run([chunk(0.2)], [v])
    assert summary.windows == 1
    [line] = [x for x in lines if x.startswith("level")]
    assert line.startswith("level 0.200 peak 0.20")
    assert "| Chime 0.67, Wind chime 0.60, Glass 0.41 |" in line
    # doorbell would emit (Chime rule, default 0.30); glass_breaking only scores 0 now
    assert line.endswith("| doorbell 0.67 ✓")


async def test_lists_catalog_sounds_from_005_and_marks_only_those_that_would_emit():
    v = vec(Cough=0.20, Screaming=0.04, Bark=0.55)
    _, lines = await _run([chunk(0.1)], [v], thresholds={"dog_barking": 0.5, "coughing": 0.5})
    [line] = [x for x in lines if x.startswith("level")]
    assert line.endswith("| dog_barking 0.55 ✓, coughing 0.20")
    assert "scream" not in line


async def test_sounds_nobody_enabled_are_marked_off():
    _, lines = await _run(
        [chunk(0.1)], [vec(Bark=0.9, Doorbell=0.9)], enabled=frozenset({"doorbell"})
    )
    [line] = [x for x in lines if x.startswith("level")]
    assert line.endswith("| dog_barking 0.90 (off), doorbell 0.90 ✓")  # ties: by name


async def test_nothing_from_the_catalog_prints_a_dash():
    _, lines = await _run([chunk(0.01)], [vec(Speech=0.9)])
    [line] = [x for x in lines if x.startswith("level")]
    assert line.endswith("| -")


async def test_clipping_marker_and_summary_advise_lowering_the_level():
    loud = chunk(0.3, clipped=1000)  # 6.4 % clipped
    summary, lines = await _run([loud, chunk(0.1, n=HOP)], [vec(Speech=0.5)])
    assert summary.windows == 2
    assert summary.clipping_windows == 2  # the second window still holds the clipped part
    assert "CLIPPING 6%" in lines[0]
    assert summary.loudest_peak == pytest.approx(1.0)
    assert "lower" in summary.advice.lower()
    assert "wpctl set-volume @DEFAULT_AUDIO_SOURCE@ 0.3" in summary.advice
    assert lines[-1] == f"Advice: {summary.advice}"


async def test_summary_reports_loudest_level_and_best_score_per_sound():
    vs = [vec(Doorbell=0.2), vec(Doorbell=0.7, Shatter=0.1), vec(Doorbell=0.1)]
    chunks = [chunk(0.05), chunk(0.4, n=HOP), chunk(0.1, n=HOP)]
    summary, lines = await _run(chunks, vs)
    assert summary.windows == 3
    # windows overlap by half: the third is half 0.4, half 0.1
    assert summary.loudest_level == pytest.approx(np.sqrt((0.4**2 + 0.1**2) / 2), abs=1e-6)
    assert summary.best["doorbell"] == pytest.approx(0.7)
    assert summary.best["glass_breaking"] == pytest.approx(0.1)
    assert summary.best["scream"] == 0.0
    text = "\n".join(lines)
    assert "Best scores: doorbell 0.70 ✓, glass_breaking 0.10" in text
    assert "Clipping: 0 of 3 windows" in text


async def test_stops_after_the_requested_seconds_of_audio():
    clf = FakeClassifier([vec()])
    mic = FakeMic([chunk(0.1, n=HOP)] * 100)
    summary = await run_check(clf, mic, seconds=2, thresholds={}, out=lambda s: None)
    # 2 s = 32000 samples; the chunk that crosses it is still analysed:
    # windows end at 15600, 23400, 31200 and 39000 samples
    assert summary.windows == 4
    assert mic.closed


async def test_mic_lost_propagates():
    with pytest.raises(MicLost):
        await run_check(
            FakeClassifier([vec()]),
            FakeMic([chunk(0.1)], then=MicLost("gone")),
            seconds=10,
            thresholds={},
            out=lambda s: None,
        )


def test_advice():
    assert "lower" in advice(loudest_peak=1.0, clipping_windows=1).lower()
    raise_ = advice(loudest_peak=0.05, clipping_windows=0)
    assert "raise" in raise_.lower() and "0.05" in raise_
    assert advice(loudest_peak=0.5, clipping_windows=0) == "The input level looks right."
