import json
import logging
import subprocess
from pathlib import Path

import pytest

from ha_edge import playback
from ha_edge.playback import NullProbe, PipeWireProbe

DATA = Path(__file__).parent / "data"


def load(name):
    return json.loads((DATA / f"pw-dump-playback-{name}.json").read_text())


class FakeRun:
    def __init__(self, payload=None, exc=None, returncode=0):
        self.payload = payload
        self.exc = exc
        self.returncode = returncode
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.exc:
            raise self.exc
        out = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return subprocess.CompletedProcess(cmd, self.returncode, stdout=out, stderr="")


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def probe(payload, **kw):
    return PipeWireProbe(run=FakeRun(payload, **kw), clock=Clock())


def node(dump, oid):
    return next(o for o in dump if o["id"] == oid)


def test_speakers_running_is_playing():
    assert probe(load("speakers")).playing() is True


def test_silent_is_not_playing():
    assert probe(load("silent")).playing() is False


def test_headphones_running_is_not_playing():
    assert probe(load("headphones")).playing() is False


@pytest.mark.parametrize("state", ["idle", "suspended", "paused", "creating"])
def test_non_running_stream_is_not_playing(state):
    d = load("speakers")
    node(d, 83)["info"]["state"] = state
    assert probe(d).playing() is False


def test_input_only_stream_is_not_playing():
    d = load("speakers")
    node(d, 83)["info"]["props"]["media.class"] = "Stream/Input/Audio"
    assert probe(d).playing() is False


def test_unknown_default_sink_assumes_speakers():
    d = [o for o in load("speakers") if o["id"] != 36]
    assert probe(d).playing() is True


def test_default_sink_not_found_in_graph_assumes_speakers():
    d = load("speakers")
    node(d, 36)["metadata"][0]["value"] = {"name": "gone"}
    assert probe(d).playing() is True


def test_form_factor_headset_on_device():
    d = load("speakers")
    node(d, 48)["info"]["props"]["device.form-factor"] = "headset"
    assert probe(d).playing() is False


def test_form_factor_headphone_on_node():
    d = load("speakers")
    node(d, 51)["info"]["props"]["device.form-factor"] = "headphone"
    assert probe(d).playing() is False


def test_headset_word_in_node_description():
    d = load("speakers")
    node(d, 51)["info"]["props"]["node.description"] = "Acme HeadSet Stereo"
    assert probe(d).playing() is False


def test_headphone_route_for_other_device_node_is_ignored():
    d = load("headphones")
    node(d, 51)["info"]["props"]["card.profile.device"] = 99
    # route belongs to a different PCM device; fall back to speakers
    assert probe(d).playing() is True


def test_missing_binary_false_and_logged_once(caplog):
    clock = Clock()
    p = PipeWireProbe(run=FakeRun(exc=FileNotFoundError("pw-dump")), clock=clock)
    with caplog.at_level(logging.WARNING, logger="ha_edge.playback"):
        for _ in range(3):
            clock.t += 10
            assert p.playing() is False
    assert len(caplog.records) == 1


def test_failure_kinds_each_logged_once(caplog):
    clock = Clock()
    runs = [
        FakeRun(exc=FileNotFoundError()),
        FakeRun(exc=subprocess.TimeoutExpired("pw-dump", 1)),
        FakeRun(payload="not json"),
        FakeRun(payload=[], returncode=1),
    ]
    with caplog.at_level(logging.WARNING, logger="ha_edge.playback"):
        for r in runs:
            p = PipeWireProbe(run=r, clock=clock)
            assert p.playing() is False
        assert len(caplog.records) == 4
        # repeating a kind on a fresh probe logs again (state is per-probe)
        pr = PipeWireProbe(run=runs[0], clock=clock)
        for _ in range(3):
            clock.t += 10
            pr.playing()
        assert len(caplog.records) == 5


def test_non_list_json_is_failure():
    assert probe({"a": 1}).playing() is False


def test_timeout_is_one_second_and_cmd():
    r = FakeRun(load("silent"))
    PipeWireProbe(run=r, clock=Clock()).playing()
    cmd, kw = r.calls[0]
    assert cmd == ["pw-dump"]
    assert kw["timeout"] == 1.0


def test_cache_respected_and_expires():
    r = FakeRun(load("speakers"))
    clock = Clock()
    p = PipeWireProbe(run=r, clock=clock, cache_s=2.0)
    assert p.playing() is True
    clock.t += 1.9
    assert p.playing() is True
    assert len(r.calls) == 1
    clock.t += 0.2
    p.playing()
    assert len(r.calls) == 2


def test_recovers_after_failure():
    r = FakeRun(exc=FileNotFoundError())
    clock = Clock()
    p = PipeWireProbe(run=r, clock=clock)
    assert p.playing() is False
    r.exc = None
    r.payload = load("speakers")
    clock.t += 5
    assert p.playing() is True


def test_null_probe():
    assert NullProbe().playing() is False


def test_default_probe(monkeypatch):
    monkeypatch.setattr(playback.shutil, "which", lambda n: "/usr/bin/pw-dump")
    assert isinstance(playback.default_probe(), PipeWireProbe)
    monkeypatch.setattr(playback.shutil, "which", lambda n: None)
    assert isinstance(playback.default_probe(), NullProbe)
