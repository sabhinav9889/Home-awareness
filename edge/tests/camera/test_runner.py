import asyncio
import json
import logging
import shutil
import subprocess
from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
import pytest
import respx

from ha_edge.api import ServerClient
from ha_edge.camera import runner
from ha_edge.camera.capture import CameraBusy, Frame
from ha_edge.camera.detectors import Detection
from ha_edge.camera.pose_rules import Landmarks
from ha_edge.sensor import EXIT_OK, EXIT_REVOKED, REVOKED_MESSAGE

BASE = "http://srv"
T0 = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
ALL = ["fall", "lying_still", "person_away", "weapon_seen"]


def cfg_json(guarding=False, snapshots=True, enabled=ALL, thresholds=None, hb=3600):
    return {
        "enabled_sounds": enabled,
        "heartbeat_interval_s": hb,
        "thresholds": thresholds or {},
        "guarding": guarding,
        "send_snapshots": snapshots,
    }


class FakeTime:
    """Discrete-event clock: sleepers wake in order as the driver advances time."""

    def __init__(self):
        self.t = 0.0
        self.sleepers: list[tuple[float, asyncio.Future]] = []

    def clock(self):
        return self.t

    def now(self):
        return T0 + timedelta(seconds=self.t)

    async def sleep(self, d):
        fut = asyncio.get_running_loop().create_future()
        self.sleepers.append((self.t + d, fut))
        await fut

    async def drive(self, until, stop, expected=4):
        idle = 0
        while self.t < until and not stop.is_set():
            await asyncio.sleep(0.0005)
            if len(self.sleepers) >= expected or (self.sleepers and idle > 40):
                idle = 0
                self.sleepers.sort(key=lambda s: s[0])
                at, fut = self.sleepers.pop(0)
                self.t = max(self.t, at)
                fut.set_result(None)
            else:
                idle += 1
        stop.set()


class FakeSource:
    def __init__(self, ft, fail_opens=0):
        self.ft = ft
        self.closed = 0
        self.reads = 0
        self.none = False

    def read(self):
        self.reads += 1
        if self.none:
            return None
        return Frame(np.full((48, 64, 3), 128, np.uint8), self.ft.t)

    def close(self):
        self.closed += 1


class FakePose:
    def __init__(self, script=None):
        self.script = script or (lambda i: None)
        self.calls = 0
        self.closed = 0

    def torso(self, frame):
        i = self.calls
        self.calls += 1
        return self.script(i)

    def close(self):
        self.closed += 1


class FakeObjects:
    def __init__(self, dets=()):
        self.dets = list(dets)
        self.calls = 0
        self.closed = 0

    def detect(self, frame):
        self.calls += 1
        return list(self.dets)

    def close(self):
        self.closed += 1


def lm(kind):
    """Upright torso (shoulders above hips) or lying (side by side, hips low)."""
    v = 0.9
    if kind == "upright":
        return Landmarks((0.45, 0.3, v), (0.55, 0.3, v), (0.45, 0.7, v), (0.55, 0.7, v))
    return Landmarks((0.2, 0.88, v), (0.2, 0.9, v), (0.6, 0.88, v), (0.6, 0.9, v))


async def run(ft, mock, source, pose, objects, until, **kw):
    stop = asyncio.Event()
    client = ServerClient(BASE, "key")
    drv = asyncio.create_task(ft.drive(until, stop))
    try:
        code = await runner.run_camera(
            client,
            source,
            pose,
            objects,
            clock=ft.clock,
            now=ft.now,
            sleep=ft.sleep,
            stop=stop,
            **kw,
        )
    finally:
        await client.aclose()
        await drv
    return code


def events(mock):
    return [c.request for c in mock.calls if c.request.url.path == "/device/events"]


def body(req):
    if req.headers["content-type"].startswith("application/json"):
        return json.loads(req.content)
    return None


def setup_server(mock, hb_cfg, cfg_seq=None):
    mock.post("/device/heartbeat").respond(200, json=hb_cfg)
    seq = list(cfg_seq or [hb_cfg])
    mock.get("/device/config").mock(
        side_effect=lambda r: httpx.Response(200, json=seq.pop(0) if len(seq) > 1 else seq[0])
    )
    ev = mock.post("/device/events").respond(
        202, json={"event_id": 1, "merged": False, "alerts": 0}
    )
    return ev


@pytest.fixture
def mock():
    with respx.mock(base_url=BASE, assert_all_called=False) as m:
        yield m


async def test_guarding_off_never_runs_object_model(mock):
    ft = FakeTime()
    off = cfg_json(guarding=False, enabled=["person_away"])
    setup_server(mock, off)
    obj = FakeObjects([Detection("person", 0.9)])
    code = await run(ft, mock, FakeSource(ft), FakePose(), obj, until=50)
    assert code == EXIT_OK
    assert obj.calls == 0 and events(mock) == []


async def test_guarding_flip_on_starts_person_events_within_15s(mock):
    ft = FakeTime()
    off = cfg_json(guarding=False, enabled=["person_away"])
    on = cfg_json(guarding=True, enabled=["person_away"])
    setup_server(mock, off, [on])  # heartbeat says off; the t=15 refresh says on
    obj = FakeObjects([Detection("person", 0.9)])
    await run(ft, mock, FakeSource(ft), FakePose(), obj, until=20)
    (req,) = events(mock)
    at = datetime.fromisoformat(
        json.loads(req.content.split(b"\r\n\r\n")[1].split(b"\r\n")[0])["occurred_at"]
    )
    assert timedelta(seconds=15) <= at - T0 <= timedelta(seconds=17)


async def test_guarding_flips_off_stops_object_model(mock):
    ft = FakeTime()
    on = cfg_json(guarding=True, enabled=["person_away"])
    off = cfg_json(guarding=False, enabled=["person_away"])
    setup_server(mock, on, [off])
    obj = FakeObjects([Detection("person", 0.9)])
    await run(ft, mock, FakeSource(ft), FakePose(), obj, until=60)
    calls_at_end = obj.calls
    # ran only until the first refresh (t=15) at 4 fps
    assert 0 < calls_at_end <= 15 * 4 + 2


async def test_snapshot_from_triggering_frame_and_occurred_at(mock):
    ft = FakeTime()
    setup_server(mock, cfg_json(enabled=["weapon_seen"]))
    obj = FakeObjects([Detection("knife", 0.8)])
    await run(ft, mock, FakeSource(ft), FakePose(), obj, until=3)
    (req,) = events(mock)
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert b"\xff\xd8" in req.content  # a JPEG
    assert b'"type":"weapon_seen"' in req.content
    assert b'"confidence":0.8' in req.content
    # first frame is read at t=0: occurred_at is that frame's time, not older
    assert b"2026-11-08T12:00:00" in req.content


async def test_send_snapshots_false_posts_json_and_never_encodes(mock, monkeypatch):
    ft = FakeTime()
    setup_server(mock, cfg_json(enabled=["weapon_seen"], snapshots=False))

    def boom(*a, **k):
        raise AssertionError("encode_snapshot must not be called")

    monkeypatch.setattr(runner, "encode_snapshot", boom)
    obj = FakeObjects([Detection("knife", 0.8)])
    await run(ft, mock, FakeSource(ft), FakePose(), obj, until=3)
    (req,) = events(mock)
    assert req.headers["content-type"] == "application/json"
    assert body(req)["type"] == "weapon_seen"


async def test_person_for_ten_minutes_gives_two_events(mock):
    ft = FakeTime()
    setup_server(mock, cfg_json(guarding=True, enabled=["person_away"]))
    obj = FakeObjects([Detection("person", 0.9)])
    await run(ft, mock, FakeSource(ft), FakePose(), obj, until=600, fps=1)
    assert len(events(mock)) == 2


async def test_object_threshold_gates_events(mock):
    ft = FakeTime()
    setup_server(mock, cfg_json(enabled=["weapon_seen"], thresholds={"weapon_seen": 0.7}))
    await run(ft, mock, FakeSource(ft), FakePose(), FakeObjects([Detection("knife", 0.6)]), until=5)
    assert events(mock) == []


async def test_not_enabled_event_types_never_sent_and_models_skipped(mock):
    ft = FakeTime()
    setup_server(mock, cfg_json(enabled=["fall"], guarding=True))
    pose = FakePose()
    obj = FakeObjects([Detection("knife", 0.9), Detection("person", 0.9)])
    await run(ft, mock, FakeSource(ft), pose, obj, until=5)
    assert obj.calls == 0 and pose.calls > 0 and events(mock) == []


async def test_pose_skipped_unless_fall_or_lying_enabled(mock):
    ft = FakeTime()
    setup_server(mock, cfg_json(enabled=["weapon_seen"]))
    pose = FakePose()
    await run(ft, mock, FakeSource(ft), pose, FakeObjects(), until=3)
    assert pose.calls == 0


async def test_fall_twice_sends_one_within_cooldown(mock):
    ft = FakeTime()
    setup_server(mock, cfg_json(enabled=["fall"], snapshots=False))
    # frames: upright x4, lying (fall), upright x4, lying (second fall)
    seq = ["upright"] * 4 + ["down"] + ["upright"] * 4 + ["down"] + ["upright"] * 4
    pose = FakePose(lambda i: lm(seq[i] if i < len(seq) else "upright"))
    await run(ft, mock, FakeSource(ft), pose, FakeObjects(), until=6)
    assert pose.calls >= 10
    (req,) = events(mock)
    assert body(req) == {
        "type": "fall",
        "confidence": 0.9,
        "occurred_at": body(req)["occurred_at"],
    }


async def test_lying_still_passes_t_with_none_samples():
    seen = []

    class Spy(runner.LyingStillDetector):
        def update(self, sample, t=None):
            seen.append((sample, t))
            return False

    a = runner.Analyzer(FakePose(), FakeObjects())
    a.lying = Spy()
    from ha_edge.api import DeviceConfig

    cfg = DeviceConfig(frozenset({"lying_still"}), 60)
    a.analyze(Frame(np.zeros((4, 4, 3), np.uint8), 0.0), cfg, 7.5)
    assert seen == [(None, 7.5)]


async def test_camera_busy_at_start_retries_and_heartbeats(mock, caplog):
    ft = FakeTime()
    setup_server(mock, cfg_json(enabled=["weapon_seen"], hb=4))
    attempts = []

    def factory():
        attempts.append(ft.t)
        if len(attempts) < 3:
            raise CameraBusy("busy")
        return FakeSource(ft)

    obj = FakeObjects([Detection("knife", 0.9)])
    with caplog.at_level(logging.INFO, logger="ha_edge.camera.runner"):
        await run(ft, mock, factory, FakePose(), obj, until=30)
    assert attempts[:3] == [0.0, 5.0, 10.0]
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("camera busy or unplugged" in m for m in msgs) == 1
    assert sum("camera is back" in m for m in msgs) == 1
    beats = [c for c in mock.calls if c.request.url.path == "/device/heartbeat"]
    assert len(beats) >= 7  # every 4 s throughout
    assert len(events(mock)) == 1


async def test_frames_stop_for_3s_logs_once_and_reopens(mock, caplog):
    ft = FakeTime()
    setup_server(mock, cfg_json(enabled=["weapon_seen"]))
    sources = []

    def factory():
        s = FakeSource(ft)
        sources.append(s)
        return s

    async def unplug():
        await asyncio.sleep(0.05)
        sources[0].none = True

    asyncio.create_task(unplug())
    obj = FakeObjects()
    with caplog.at_level(logging.INFO, logger="ha_edge.camera.runner"):
        await run(ft, mock, factory, FakePose(), obj, until=40)
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("camera busy or unplugged" in m for m in msgs) <= 1
    assert sources[0].closed >= 1


async def test_revoked_exits_with_message(mock, caplog):
    ft = FakeTime()
    mock.post("/device/heartbeat").respond(401)
    mock.get("/device/config").respond(401)
    src, pose, obj = FakeSource(ft), FakePose(), FakeObjects()
    with caplog.at_level(logging.ERROR, logger="ha_edge.camera.runner"):
        code = await run(ft, mock, src, pose, obj, until=60)
    assert code == EXIT_REVOKED != 0
    assert REVOKED_MESSAGE in caplog.text
    assert src.closed == 1 and pose.closed == 1 and obj.closed == 1


async def test_stop_closes_source_and_models(mock):
    ft = FakeTime()
    setup_server(mock, cfg_json())
    src, pose, obj = FakeSource(ft), FakePose(), FakeObjects()
    code = await run(ft, mock, src, pose, obj, until=5)
    assert code == EXIT_OK
    assert (src.closed, pose.closed, obj.closed) == (1, 1, 1)


async def test_server_down_events_queue_and_loop_continues(mock):
    ft = FakeTime()
    mock.post("/device/heartbeat").respond(200, json=cfg_json(enabled=["weapon_seen"]))
    mock.get("/device/config").respond(200, json=cfg_json(enabled=["weapon_seen"]))
    calls = []

    def ev(request):
        calls.append(ft.t)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(202, json={"event_id": 1, "merged": False, "alerts": 0})

    mock.post("/device/events").mock(side_effect=ev)
    src = FakeSource(ft)
    await run(ft, mock, src, FakePose(), FakeObjects([Detection("knife", 0.9)]), until=30)
    assert len(calls) >= 3 and src.reads > 20


def _clip(tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    pytest.importorskip("cv2")
    path = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=8",
            "-t",
            "2",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )
    return path


async def test_file_source_real_snapshot_and_real_clock(mock, tmp_path):
    from ha_edge.camera.capture import FileSource

    setup_server(mock, cfg_json(enabled=["weapon_seen"]))
    src = FileSource(_clip(tmp_path), loop=True, fps=8)
    stop = asyncio.Event()
    client = ServerClient(BASE, "key")
    task = asyncio.create_task(
        runner.run_camera(
            client,
            src,
            FakePose(),
            FakeObjects([Detection("knife", 0.9)]),
            fps=8,
            stop=stop,
        )
    )
    await asyncio.sleep(2.5)
    stop.set()
    assert await task == EXIT_OK
    await client.aclose()
    assert events(mock) and b"\xff\xd8" in events(mock)[0].content
