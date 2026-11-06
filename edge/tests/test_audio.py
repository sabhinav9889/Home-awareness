import asyncio
import copy
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from ha_edge.audio import (
    InputDevice,
    MicLost,
    MicNotFound,
    MicStream,
    Windower,
    list_inputs,
    resample_linear,
    resolve_device,
)

FIXTURE = Path(__file__).parent / "data" / "pw-dump-sources.json"
BUILTIN = "alsa_input.pci-0000_00_1f.3.analog-stereo"


def fake_run_for(data):
    def run(cmd, **kwargs):
        assert cmd[0] == "pw-dump"
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(data), stderr="")

    return run


def fixture_data():
    return json.loads(FIXTURE.read_text())


def dev(index, name, description="", is_default=False):
    return InputDevice(index, name, 48000.0, is_default, description)


def test_list_inputs_parses_pw_dump_sources_only_and_marks_default():
    devices = list_inputs(run=fake_run_for(fixture_data()))
    assert [d.name for d in devices] == [BUILTIN]  # the Audio/Sink is excluded
    d = devices[0]
    assert d.index == 52
    assert d.description == "Built-in Audio Analog Stereo"
    assert d.default_samplerate == 48000.0
    assert d.is_default is True


def test_list_inputs_non_default_and_audio_rate_and_nick_fallback():
    data = fixture_data()
    src = copy.deepcopy(next(o for o in data if o["id"] == 52))
    src["id"] = 99
    props = src["info"]["props"]
    props["node.name"] = "alsa_input.usb-Headset"
    del props["node.description"]
    props["node.nick"] = "USB Headset"
    props["audio.rate"] = 44100
    data.append(src)
    devices = {d.name: d for d in list_inputs(run=fake_run_for(data))}
    h = devices["alsa_input.usb-Headset"]
    assert (h.index, h.description, h.default_samplerate, h.is_default) == (
        99,
        "USB Headset",
        44100.0,
        False,
    )
    assert devices[BUILTIN].is_default


def test_list_inputs_without_default_metadata():
    data = [o for o in fixture_data() if o["type"] != "PipeWire:Interface:Metadata"]
    assert [d.is_default for d in list_inputs(run=fake_run_for(data))] == [False]


def test_list_inputs_pw_dump_failure_raises_mic_lost_free_error():
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no daemon")

    with pytest.raises(RuntimeError, match="no daemon"):
        list_inputs(run=run)


def test_resolve_exact_then_substring():
    devs = [dev(1, "alsa_input.builtin", "Built-in Audio"), dev(2, "bluez_input.headset", "Pods")]
    assert resolve_device("alsa_input.builtin", devs).index == 1
    assert resolve_device("HEADSET", devs).index == 2  # case-insensitive name substring
    assert resolve_device("built-in", devs).index == 1  # description substring
    # exact name beats a substring that also matches another device
    devs2 = [dev(1, "mic", ""), dev(2, "mic-2", "")]
    assert resolve_device("mic", devs2).index == 1


def test_resolve_none_uses_system_default():
    devs = [dev(1, "a"), dev(2, "b", is_default=True)]
    assert resolve_device(None, devs).index == 2
    with pytest.raises(MicNotFound):
        resolve_device(None, [dev(1, "a")])


def test_resolve_ambiguous_or_missing_raises_with_available_list():
    devs = [dev(1, "usb_mic_a"), dev(2, "usb_mic_b")]
    for query in ("usb_mic", "nope"):
        with pytest.raises(MicNotFound) as ei:
            resolve_device(query, devs)
        assert ei.value.name == query
        assert ei.value.available == ["usb_mic_a", "usb_mic_b"]


def test_windower_emits_overlapping_windows():
    w = Windower(window=8, hop=4)
    x = np.arange(20, dtype=np.float32)
    assert w.push(x[:5]) == []
    out = w.push(x[5:11])  # 11 samples buffered -> windows at 0 and 4
    assert [int(o[0]) for o in out] == [0]
    out2 = w.push(x[11:20])
    all_windows = out + out2
    assert all(len(o) == 8 for o in all_windows)
    assert [int(o[0]) for o in all_windows] == [0, 4, 8, 12]
    for o in all_windows:
        assert o.tolist() == x[int(o[0]) : int(o[0]) + 8].tolist()


def test_resample_48k_to_16k_length():
    x = np.sin(np.linspace(0, 20, 48000)).astype(np.float32)
    y = resample_linear(x, 48000, 16000)
    assert y.dtype == np.float32
    assert len(y) == 16000
    assert resample_linear(x, 16000, 16000) is x


class FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, n):
        if self._chunks:
            c = self._chunks.pop(0)
            if isinstance(c, Exception):
                raise c
            return c
        return b""


class FakeProc:
    def __init__(self, chunks, stderr=b"", returncode=None):
        self.stdout = FakeStream(chunks)
        self.stderr = FakeStream([stderr] if stderr else [])
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def spawner(proc, calls):
    async def spawn(*argv, **kwargs):
        calls.append(argv)
        return proc

    return spawn


async def test_stream_yields_float32_and_handles_partial_frames():
    samples = np.array([0.0, 0.5, -0.25, 1.0, 0.125], dtype="<f4")
    raw = samples.tobytes()
    chunks = [raw[:3], raw[3:10], raw[10:11], raw[11:]]  # splits mid-frame
    proc = FakeProc(chunks, stderr=b"pw died")
    calls = []
    got = []
    with pytest.raises(MicLost, match="pw died"):
        async with MicStream(dev(5, BUILTIN), spawn=spawner(proc, calls)) as ms:
            async for chunk in ms:
                assert chunk.dtype == np.float32
                got.append(chunk)
    assert np.concatenate(got).tolist() == samples.tolist()
    assert calls == [
        (
            "pw-record",
            "--target",
            BUILTIN,
            "--rate",
            "16000",
            "--channels",
            "1",
            "--format",
            "f32",
            "-",
        )
    ]
    assert proc.terminated or proc.returncode is not None


async def test_stream_error_raises_mic_lost_not_fallback():
    proc = FakeProc([np.zeros(4, "<f4").tobytes(), OSError("boom")])
    calls = []
    ms = MicStream(dev(5, BUILTIN), spawn=spawner(proc, calls))
    await ms.__aenter__()
    first = await ms.__anext__()
    assert len(first) == 4
    with pytest.raises(MicLost):
        await ms.__anext__()
    await ms.aclose()
    assert len(calls) == 1  # never respawned, never another device
    assert calls[0][2] == BUILTIN


async def test_stream_terminates_subprocess_on_cancel_and_close():
    class Hang(FakeStream):
        async def read(self, n):
            await asyncio.sleep(3600)

    proc = FakeProc([])
    proc.stdout = Hang([])
    ms = MicStream(dev(5, BUILTIN), spawn=spawner(proc, []))
    await ms.__aenter__()
    task = asyncio.create_task(ms.__anext__())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await ms.aclose()
    assert proc.terminated
    await ms.aclose()  # idempotent
