import asyncio
import logging

import numpy as np
import pytest

from ha_edge import notify
from ha_edge.notify import AlarmPlayer, DesktopNotifier, synth_alarm

# --- alarm tone --------------------------------------------------------------------------


def _freq(x: np.ndarray, rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(x.astype(np.float64)))
    return float(np.fft.rfftfreq(len(x), 1 / rate)[int(np.argmax(spectrum))])


def test_synth_alarm_two_tone_half_amplitude_s16_mono():
    rate = notify.ALARM_RATE
    pcm = synth_alarm(1.0)
    samples = np.frombuffer(pcm, dtype="<i2")
    assert len(samples) == rate  # 1 s mono 16-bit
    assert abs(int(np.abs(samples).max()) - int(0.5 * 32767)) < 200
    quarter = rate // 4
    assert _freq(samples[:quarter], rate) == pytest.approx(880, abs=8)
    assert _freq(samples[quarter : 2 * quarter], rate) == pytest.approx(660, abs=8)


# --- AlarmPlayer --------------------------------------------------------------------------


class FakeStdin:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, b):
        self.data += b

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self.closed = True


class FakeProc:
    def __init__(self, exits_after_s=None):
        self.stdin = FakeStdin()
        self.returncode = None
        self.terminated = False
        self._done = asyncio.Event()
        if exits_after_s is not None:
            asyncio.get_running_loop().call_later(exits_after_s, self._exit, 0)

    def _exit(self, code):
        if self.returncode is None:
            self.returncode = code
            self._done.set()

    def terminate(self):
        self.terminated = True
        self._exit(-15)

    def kill(self):
        self._exit(-9)

    async def wait(self):
        await self._done.wait()
        return self.returncode


class Spawner:
    def __init__(self, **proc_kw):
        self.calls = []
        self.procs = []
        self.proc_kw = proc_kw

    async def __call__(self, *argv):
        self.calls.append(argv)
        p = FakeProc(**self.proc_kw)
        self.procs.append(p)
        return p


async def _until(cond):
    async with asyncio.timeout(2):
        while not cond():
            await asyncio.sleep(0)


async def test_play_spawns_pw_play_on_default_sink_and_feeds_pcm():
    spawn = Spawner()
    player = AlarmPlayer(spawn=spawn, max_s=1.0)
    await player.play()
    assert player.playing
    argv = spawn.calls[0]
    assert argv[0] == "pw-play"
    assert "--target" not in argv  # default output = the headphones when they're in use
    assert argv[-1] == "-"
    assert "--rate" in argv and str(notify.ALARM_RATE) in argv
    proc = spawn.procs[0]
    await _until(lambda: len(proc.stdin.data) == notify.ALARM_RATE * 2)
    player.stop()
    assert proc.terminated
    await _until(lambda: not player.playing)


async def test_stop_terminates_and_play_again_works():
    spawn = Spawner()
    player = AlarmPlayer(spawn=spawn, max_s=30.0)
    await player.play()
    player.stop()
    await player.wait()
    assert spawn.procs[0].terminated
    assert not player.playing
    await player.play()
    assert len(spawn.calls) == 2
    player.stop()
    await player.wait()


async def test_never_two_alarms_at_once():
    spawn = Spawner()
    player = AlarmPlayer(spawn=spawn)
    await player.play()
    await player.play()
    assert len(spawn.calls) == 1
    player.stop()
    await player.wait()


async def test_alarm_stops_itself_after_max_s():
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    spawn = Spawner()  # process would play forever
    player = AlarmPlayer(spawn=spawn, sleep=fake_sleep)
    await player.play()
    async with asyncio.timeout(2):
        await player.wait()
    assert slept == [30.0]
    assert spawn.procs[0].terminated
    assert not player.playing


async def test_natural_end_of_clip_ends_playing():
    spawn = Spawner(exits_after_s=0.01)
    player = AlarmPlayer(spawn=spawn, max_s=3.0)
    await player.play()
    async with asyncio.timeout(2):
        await player.wait()
    assert not player.playing


async def test_missing_pw_play_does_not_crash(caplog):
    async def spawn(*argv):
        raise FileNotFoundError("pw-play")

    player = AlarmPlayer(spawn=spawn)
    await player.play()
    await player.play()
    assert not player.playing
    assert player.unavailable
    assert sum("pw-play" in r.getMessage() for r in caplog.records) == 1  # logged once


# --- DesktopNotifier --------------------------------------------------------------------


class FakeLib:
    """Stands in for desktop_notifier.DesktopNotifier."""

    def __init__(self, caps=("buttons",), fail=None, press=None):
        self.caps = caps
        self.fail = fail
        self.press = press
        self.sent = []

    async def get_capabilities(self):
        return frozenset(notify._Capability[c.upper()] for c in self.caps)

    async def send(self, title, message, **kw):
        if self.fail:
            raise self.fail
        self.sent.append((title, message, kw))
        if self.press is not None:
            buttons = kw.get("buttons", ())
            hit = [b for b in buttons if b.title == self.press]
            asyncio.get_running_loop().call_soon(hit[0].on_pressed)
        return "id"


async def test_popup_returns_pressed_action():
    lib = FakeLib(press="Stop alarm")
    n = DesktopNotifier(lib=lib)
    got = await n.popup("Scream — Kitchen", "12:00 · Home Awareness", ["I'm on it", "Stop alarm"])
    assert got == "Stop alarm"
    title, _body, kw = lib.sent[0]
    assert title == "Scream — Kitchen"
    assert [b.title for b in kw["buttons"]] == ["I'm on it", "Stop alarm"]


async def test_popup_without_actions_returns_none_immediately():
    lib = FakeLib()
    n = DesktopNotifier(lib=lib)
    async with asyncio.timeout(1):
        assert await n.popup("bob is handling it", "Home Awareness", []) is None
    assert lib.sent


async def test_popup_dismissed_returns_none():
    lib = FakeLib()
    n = DesktopNotifier(lib=lib)
    task = asyncio.create_task(n.popup("t", "b", ["I'm on it"]))
    await _until(lambda: lib.sent)
    lib.sent[0][2]["on_dismissed"]()
    async with asyncio.timeout(1):
        assert await task is None


async def test_server_without_buttons_returns_none_and_hints(capsys):
    lib = FakeLib(caps=())
    n = DesktopNotifier(lib=lib)
    async with asyncio.timeout(1):
        assert await n.popup("t", "b", ["I'm on it", "Stop alarm"]) is None
    assert "Ctrl-C" in capsys.readouterr().err


async def test_no_dbus_falls_back_to_terminal_and_logs_once(capsys, caplog):
    caplog.set_level(logging.INFO)
    n = DesktopNotifier(lib=FakeLib(fail=RuntimeError("no session bus")))
    assert await n.popup("Scream — Kitchen", "12:00 · Home Awareness", ["Stop alarm"]) is None
    assert await n.popup("Second", "x", []) is None
    err = capsys.readouterr().err
    assert "Scream — Kitchen" in err and "Second" in err
    assert sum("notifications unavailable" in r.getMessage() for r in caplog.records) == 1


async def test_lib_factory_failure_falls_back(capsys):
    def boom():
        raise RuntimeError("no dbus")

    n = DesktopNotifier(lib_factory=boom)
    assert await n.popup("Hello", "b", []) is None
    assert "Hello" in capsys.readouterr().err


async def test_play_right_after_stop_starts_a_new_alarm():
    spawn = Spawner()
    player = AlarmPlayer(spawn=spawn)
    await player.play()
    player.stop()
    await player.play()  # old task still reaping the terminated process
    assert len(spawn.calls) == 2
    assert spawn.procs[0].terminated and not spawn.procs[1].terminated
    assert player.playing
    player.stop()
    await player.wait()
    assert not player.playing


async def test_concurrent_plays_after_stop_start_only_one_alarm():
    spawn = Spawner()
    player = AlarmPlayer(spawn=spawn)
    await player.play()
    player.stop()
    await asyncio.gather(player.play(), player.play())
    assert len(spawn.calls) == 2
    player.stop()
    await player.wait()


class SlowDyingProc(FakeProc):
    """terminate() takes effect a little later, like a real process being reaped."""

    def terminate(self):
        self.terminated = True
        asyncio.get_running_loop().call_later(0.05, self._exit, -15)


async def test_stop_while_play_waits_for_old_alarm_to_die_starts_nothing():
    spawn = Spawner()
    player = AlarmPlayer(spawn=spawn)

    async def slow_spawn(*argv):
        spawn.calls.append(argv)
        p = SlowDyingProc()
        spawn.procs.append(p)
        return p

    player._spawn = slow_spawn
    await player.play()
    player.stop()
    restart = asyncio.create_task(player.play())  # waits for the old process to die
    await asyncio.sleep(0.01)
    assert not restart.done()
    player.stop()  # the user pressed Stop while the restart was waiting
    await restart
    assert len(spawn.calls) == 1
    assert not player.playing
    await player.wait()


async def test_stop_while_pw_play_is_starting_terminates_the_new_process():
    release = asyncio.Event()
    procs = []

    async def gated_spawn(*argv):
        await release.wait()
        p = FakeProc()
        procs.append(p)
        return p

    player = AlarmPlayer(spawn=gated_spawn)
    starting = asyncio.create_task(player.play())
    await asyncio.sleep(0.01)
    player.stop()  # pressed while pw-play was still being spawned
    release.set()
    await starting
    assert len(procs) == 1 and procs[0].terminated
    assert not player.playing
    await player.wait()


async def test_alarm_pcm_is_synthesized_once_per_length(monkeypatch):
    calls = []
    real = notify.synth_alarm

    def counting(seconds, rate=notify.ALARM_RATE):
        calls.append(seconds)
        return real(seconds, rate)

    monkeypatch.setattr(notify, "synth_alarm", counting)
    spawn = Spawner()
    player = AlarmPlayer(spawn=spawn, max_s=0.123)
    for _ in range(3):
        await player.play()
        await _until(lambda: spawn.procs[-1].stdin.closed)
        player.stop()
        await player.wait()
    assert calls == [0.123]
