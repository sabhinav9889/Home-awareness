import asyncio
import logging
from datetime import UTC, datetime

import numpy as np
import pytest

from ha_edge.api import DeviceConfig, DeviceRevoked, RateLimited, ServerUnavailable
from ha_edge.audio import MicLost
from ha_edge.config import SensorConfig
from ha_edge.model import load_class_names
from ha_edge.sensor import EXIT_MIC_LOST, EXIT_OK, EXIT_REVOKED, run_sensor

KEY = "secret-device-key-123"
CFG = SensorConfig(base_url="http://server", device_id=7, device_key=KEY, mic_name="mic")
NAMES = load_class_names()
DOORBELL = NAMES.index("Doorbell")
COUGH = NAMES.index("Cough")
CHUNK = np.zeros(15600, dtype=np.float32)


class FakeMic:
    """Yields `chunks` chunks, then either blocks forever or raises `then`."""

    def __init__(self, chunks=1000, then=None, gap=0.0):
        self.chunks = chunks
        self.then = then
        self.gap = gap
        self.entered = False
        self.closed = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for _ in range(self.chunks):
            await asyncio.sleep(self.gap)
            yield CHUNK
        if self.then is not None:
            raise self.then
        await asyncio.Event().wait()


class FakeClassifier:
    def __init__(self, hot=(DOORBELL,), value=0.9):
        self.class_names = NAMES
        self.vec = np.zeros(len(NAMES), dtype=np.float32)
        for i in hot:
            self.vec[i] = value
        self.calls = 0

    def scores(self, window):
        self.calls += 1
        return self.vec


class FakeProbe:
    def __init__(self, playing=False):
        self._playing = playing

    def playing(self):
        return self._playing


class FakeClient:
    def __init__(self, configs=None, send_errors=None):
        # configs: list of DeviceConfig or Exception, consumed per heartbeat; last repeats
        self.configs = list(configs or [DeviceConfig(frozenset({"doorbell"}), 60)])
        self.send_errors = list(send_errors or [])
        self.heartbeats = 0
        self.sent = []

    async def heartbeat(self):
        self.heartbeats += 1
        item = self.configs.pop(0) if len(self.configs) > 1 else self.configs[0]
        if isinstance(item, BaseException):
            raise item
        return item

    async def send_event(self, sound, confidence, occurred_at):
        if self.send_errors:
            raise self.send_errors.pop(0)
        self.sent.append((sound, confidence, occurred_at))


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        self.t += 0.5
        return self.t


def fast_sleep(log=None):
    async def sleep(s):
        if log is not None:
            log.append(s)
        await asyncio.sleep(0.001)

    return sleep


async def until(pred, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.001)


def start(client, mic=None, classifier=None, probe=None, sleep=None, stop=None):
    stop = stop or asyncio.Event()
    task = asyncio.create_task(
        run_sensor(
            CFG,
            client=client,
            classifier=classifier or FakeClassifier(),
            mic=mic or FakeMic(gap=0.002),
            probe=probe or FakeProbe(),
            stop=stop,
            monotonic=Clock(),
            sleep=sleep or fast_sleep(),
        )
    )
    return task, stop


async def finish(task, stop):
    stop.set()
    async with asyncio.timeout(2):
        return await task


async def test_nothing_sent_before_first_heartbeat():
    gate = asyncio.Event()

    class SlowClient(FakeClient):
        async def heartbeat(self):
            await gate.wait()
            return await super().heartbeat()

    client = SlowClient()
    clf = FakeClassifier()
    task, stop = start(client, classifier=clf)
    await until(lambda: clf.calls >= 10)
    assert client.sent == []
    gate.set()
    await until(lambda: client.sent)
    assert await finish(task, stop) == EXIT_OK


async def test_first_heartbeat_retried_every_5s_until_success():
    slept = []
    client = FakeClient(
        [ServerUnavailable("x"), ServerUnavailable("y"), DeviceConfig(frozenset({"doorbell"}), 60)]
    )
    task, stop = start(client, sleep=fast_sleep(slept))
    await until(lambda: client.sent)
    assert client.heartbeats >= 3
    assert slept.count(5.0) >= 2
    assert await finish(task, stop) == EXIT_OK


async def test_detection_flows_to_send_event():
    client = FakeClient()
    mic = FakeMic(gap=0.002)
    task, stop = start(client, mic=mic)
    await until(lambda: client.sent)
    assert await finish(task, stop) == EXIT_OK
    sound, conf, occurred_at = client.sent[0]
    assert sound == "doorbell"
    assert conf == pytest.approx(0.9)
    assert occurred_at.tzinfo is not None
    assert abs((datetime.now(UTC) - occurred_at).total_seconds()) < 5
    assert mic.entered and mic.closed


async def test_debounce_uses_monotonic_clock():
    client = FakeClient()
    # Clock advances 0.5 s per window: at most one doorbell per 2 s => ~1 per 4 windows
    clf = FakeClassifier()
    task, stop = start(client, classifier=clf)
    await until(lambda: clf.calls >= 41 and len(client.sent) >= 1)
    before = clf.calls
    await asyncio.sleep(0.05)
    assert clf.calls > before  # windows kept flowing during the pause
    await finish(task, stop)
    assert len(client.sent) <= clf.calls // 4 + 1


async def test_disabled_sound_dropped():
    client = FakeClient([DeviceConfig(frozenset({"doorbell"}), 60)])
    clf = FakeClassifier(hot=(COUGH,))
    task, stop = start(client, classifier=clf)
    await until(lambda: clf.calls >= 20 and client.heartbeats >= 1)
    before = clf.calls
    await asyncio.sleep(0.05)
    assert clf.calls > before  # still classifying while we watched for sends
    assert await finish(task, stop) == EXIT_OK
    assert client.sent == []


async def test_heartbeat_updates_enabled_set():
    client = FakeClient(
        [DeviceConfig(frozenset({"doorbell"}), 60), DeviceConfig(frozenset({"coughing"}), 60)]
    )
    clf = FakeClassifier(hot=(COUGH,))
    task, stop = start(client, classifier=clf)
    await until(lambda: client.sent)
    assert client.heartbeats >= 2
    assert {s for s, _, _ in client.sent} == {"coughing"}
    assert await finish(task, stop) == EXIT_OK


async def test_heartbeat_failure_does_not_exit():
    slept = []
    client = FakeClient(
        [
            DeviceConfig(frozenset({"doorbell"}), 60),
            ServerUnavailable("down"),
            ServerUnavailable("down"),
            DeviceConfig(frozenset({"doorbell"}), 60),
        ]
    )
    task, stop = start(client, sleep=fast_sleep(slept))
    await until(lambda: client.heartbeats >= 4)
    assert not task.done()
    assert slept.count(60) >= 3  # failures after the first success wait a full interval
    assert await finish(task, stop) == EXIT_OK


async def test_revoked_stops_loop_with_exit_5(caplog):
    caplog.set_level(logging.INFO)
    client = FakeClient([DeviceRevoked()])
    mic = FakeMic(gap=0.002)
    task, _stop = start(client, mic=mic)
    async with asyncio.timeout(2):
        code = await task
    assert code == EXIT_REVOKED
    assert "Pair it again with `ha-sensor pair`" in caplog.text
    assert KEY not in caplog.text
    assert mic.closed


async def test_revoked_during_send_exits_5():
    client = FakeClient(send_errors=[DeviceRevoked()])
    task, _stop = start(client)
    async with asyncio.timeout(2):
        assert await task == EXIT_REVOKED


async def test_mic_lost_exits_4_without_switching(caplog):
    caplog.set_level(logging.INFO)
    client = FakeClient()
    mic = FakeMic(chunks=3, then=MicLost("unplugged"), gap=0.002)
    task, _stop = start(client, mic=mic)
    async with asyncio.timeout(2):
        code = await task
    assert code == EXIT_MIC_LOST
    assert mic.closed
    assert "unplugged" in caplog.text
    assert KEY not in caplog.text


async def test_drain_backs_off_on_server_unavailable_then_delivers():
    slept = []
    errs = [ServerUnavailable("x")] * 6
    client = FakeClient(send_errors=errs)
    task, stop = start(client, sleep=fast_sleep(slept))
    await until(lambda: client.sent)
    await finish(task, stop)
    backoffs = [s for s in slept if s not in (1.0, 60, 5.0)]
    assert [s for s in slept if s in (2.0, 4.0, 8.0, 16.0, 30.0)] == [2.0, 4.0, 8.0, 16.0, 30.0]
    assert backoffs == [2.0, 4.0, 8.0, 16.0, 30.0]


async def test_drain_sleeps_retry_after_on_rate_limit():
    slept = []
    client = FakeClient(send_errors=[RateLimited(7.5)])
    task, stop = start(client, sleep=fast_sleep(slept))
    await until(lambda: client.sent)
    await finish(task, stop)
    assert 7.5 in slept


async def test_stop_event_ends_cleanly_and_closes_mic():
    client = FakeClient()
    mic = FakeMic(gap=0.002)
    task, stop = start(client, mic=mic)
    await until(lambda: mic.entered)
    assert await finish(task, stop) == EXIT_OK
    assert mic.closed


async def test_mic_failing_to_open_exits_4():
    class BrokenMic(FakeMic):
        async def __aenter__(self):
            raise MicLost("cannot start pw-record")

    task, _stop = start(FakeClient(), mic=BrokenMic())
    async with asyncio.timeout(2):
        assert await task == EXIT_MIC_LOST


SIREN = NAMES.index("Siren")


async def test_queued_event_for_sound_disabled_by_heartbeat_is_never_sent():
    class OutageClient(FakeClient):
        """Server is down for sends until the second heartbeat (which drops siren)."""

        def __init__(self):
            super().__init__(
                [DeviceConfig(frozenset({"siren"}), 60), DeviceConfig(frozenset({"doorbell"}), 60)]
            )
            self.second_done = False
            self.failed_sends = 0

        async def heartbeat(self):
            if self.heartbeats >= 1:
                while self.failed_sends < 3:  # let siren events pile up in the queue
                    await asyncio.sleep(0.001)
            cfg = await super().heartbeat()
            if self.heartbeats >= 2:
                self.second_done = True
            return cfg

        async def send_event(self, sound, confidence, occurred_at):
            if not self.second_done:
                self.failed_sends += 1
                raise ServerUnavailable("down")
            self.sent.append((sound, confidence, occurred_at))

    client = OutageClient()
    clf = FakeClassifier(hot=(SIREN, DOORBELL))
    task, stop = start(client, classifier=clf)
    await until(lambda: client.sent)
    sent, calls = len(client.sent), clf.calls
    await asyncio.sleep(0.05)
    assert clf.calls > calls and len(client.sent) > sent  # detecting and draining went on
    assert await finish(task, stop) == EXIT_OK
    assert {s for s, _, _ in client.sent} == {"doorbell"}


async def test_heartbeat_interval_clamped_to_at_least_1s():
    slept = []
    client = FakeClient([DeviceConfig(frozenset({"doorbell"}), 0)])
    task, stop = start(client, sleep=fast_sleep(slept))
    await until(lambda: client.heartbeats >= 3)
    await finish(task, stop)
    assert 0 not in slept and slept.count(1) >= 2


async def test_heartbeat_404_points_at_the_server_url(caplog):
    from ha_edge.api import Rejected

    caplog.set_level(logging.INFO)
    client = FakeClient([Rejected("Not Found", 404)])
    task, stop = start(client)
    await until(lambda: client.heartbeats >= 3)
    assert await finish(task, stop) == EXIT_OK
    errors = [r.getMessage() for r in caplog.records if "404" in r.getMessage()]
    assert len(errors) == 1  # said once, not every retry
    assert "http://server" in errors[0] and "server URL" in errors[0]


class CountingProbe(FakeProbe):
    def __init__(self, playing=False):
        super().__init__(playing)
        self.calls = 0

    def playing(self):
        self.calls += 1
        return self._playing


async def test_playback_probe_skipped_when_it_cannot_change_the_outcome():
    client = FakeClient()
    clf = FakeClassifier(value=0.0)  # silent windows
    probe = CountingProbe()
    task, stop = start(client, classifier=clf, probe=probe)
    await until(lambda: clf.calls >= 10 and client.heartbeats >= 1)
    await finish(task, stop)
    assert probe.calls == 0


async def test_playback_probe_consulted_for_borderline_scores():
    client = FakeClient()
    clf = FakeClassifier(value=0.4)  # above 0.30, below 0.50: playback decides
    probe = CountingProbe(playing=True)
    task, stop = start(client, classifier=clf, probe=probe)
    await until(lambda: clf.calls >= 10 and client.heartbeats >= 1 and probe.calls >= 1)
    before = clf.calls
    await asyncio.sleep(0.02)
    assert clf.calls > before and probe.calls > 1
    assert await finish(task, stop) == EXIT_OK
    assert client.sent == []  # boosted threshold suppressed it


async def test_heartbeat_thresholds_applied_to_detection():
    strict = DeviceConfig(frozenset({"doorbell"}), 60, {"doorbell": 0.5})
    client = FakeClient([strict])
    clf = FakeClassifier(value=0.4)  # above the global 0.30, below doorbell's 0.50
    task, stop = start(client, classifier=clf)
    await until(lambda: clf.calls >= 20 and client.heartbeats >= 1)
    before = clf.calls
    await asyncio.sleep(0.05)
    assert clf.calls > before  # still classifying while we watched for sends
    assert await finish(task, stop) == EXIT_OK
    assert client.sent == []

    loose = DeviceConfig(frozenset({"doorbell"}), 60, {"doorbell": 0.35})
    client = FakeClient([loose])
    task, stop = start(client, classifier=FakeClassifier(value=0.4))
    await until(lambda: client.sent)
    assert await finish(task, stop) == EXIT_OK
    assert {s for s, _, _ in client.sent} == {"doorbell"}


async def test_playback_probe_skipped_below_per_sound_threshold():
    client = FakeClient([DeviceConfig(frozenset({"doorbell"}), 60, {"doorbell": 0.6})])
    clf = FakeClassifier(value=0.4)  # borderline for 0.30, but dropped anyway at 0.60
    probe = CountingProbe()
    task, stop = start(client, classifier=clf, probe=probe)
    await until(lambda: clf.calls >= 10 and client.heartbeats >= 1)
    before = clf.calls
    await asyncio.sleep(0.02)
    assert clf.calls > before
    assert await finish(task, stop) == EXIT_OK
    assert probe.calls == 0
