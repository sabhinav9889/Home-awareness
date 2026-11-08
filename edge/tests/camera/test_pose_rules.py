import math

from ha_edge.camera.pose_rules import (
    FallDetector,
    Landmarks,
    LyingStillDetector,
    PersonTracker,
    TorsoSample,
    torso_sample,
)


def landmarks(angle_deg=0.0, cx=0.5, cy=0.5, length=0.3, vis=0.9):
    """Torso centred on (cx, cy); angle from vertical (0 upright, 90 horizontal)."""
    a = math.radians(angle_deg)
    dx, dy = math.sin(a) * length / 2, math.cos(a) * length / 2
    return Landmarks(
        left_shoulder=(cx - dx - 0.05, cy - dy, vis),
        right_shoulder=(cx - dx + 0.05, cy - dy, vis),
        left_hip=(cx + dx - 0.05, cy + dy, vis),
        right_hip=(cx + dx + 0.05, cy + dy, vis),
    )


def sample(t, angle, hip_y=0.6, cx=0.5, cy=0.5):
    return TorsoSample(t=t, angle_deg=angle, hip_y=hip_y, cx=cx, cy=cy)


def stream(det, points):
    return [det.update(s) for s in points]


def ramp(t0, dur, a0, a1, y0, y1, fps=10):
    n = int(dur * fps)
    out = []
    for i in range(n + 1):
        f = i / n
        out.append(sample(t0 + dur * f, a0 + (a1 - a0) * f, y0 + (y1 - y0) * f))
    return out


def test_torso_sample_upright_and_horizontal():
    up = torso_sample(landmarks(0), 1.0)
    assert up is not None and abs(up.angle_deg) < 1e-6
    assert abs(up.hip_y - 0.65) < 1e-6 and up.t == 1.0
    flat = torso_sample(landmarks(90), 2.0)
    assert abs(flat.angle_deg - 90) < 1e-6
    # upside-down torso still reads as 0..90 from vertical
    inv = Landmarks((0.5, 0.8, 1), (0.5, 0.8, 1), (0.5, 0.5, 1), (0.5, 0.5, 1))
    assert abs(torso_sample(inv, 0).angle_deg) < 1e-6


def test_torso_sample_partial_visibility_is_none():
    lm = landmarks(0)
    lm.left_hip = (lm.left_hip[0], lm.left_hip[1], 0.2)
    assert torso_sample(lm, 0.0) is None
    assert torso_sample(landmarks(0, vis=0.4), 0.0, min_visibility=0.3) is not None


def test_fall_fires_once_on_fast_drop():
    det = FallDetector()
    pts = [sample(i * 0.1, 5, 0.5) for i in range(5)]
    pts += ramp(0.5, 0.6, 5, 85, 0.5, 0.8)
    pts += [sample(1.1 + i * 0.1, 85, 0.8) for i in range(10)]
    assert stream(det, pts).count(True) == 1


def test_slow_lie_down_does_not_fire():
    det = FallDetector()
    pts = ramp(0.0, 10.0, 5, 85, 0.5, 0.8)
    assert not any(stream(det, pts))


def test_sitting_does_not_fire():
    det = FallDetector()
    pts = [sample(i * 0.1, 5, 0.5) for i in range(5)] + ramp(0.5, 0.5, 5, 25, 0.5, 0.7)
    assert not any(stream(det, pts))


def test_angle_change_without_hip_drop_does_not_fire():
    det = FallDetector()
    pts = ramp(0.0, 0.6, 5, 85, 0.5, 0.52)
    assert not any(stream(det, pts))


def test_none_gap_does_not_fire_and_none_never_fires():
    det = FallDetector()
    assert det.update(None) is False
    assert det.update(sample(0.0, 5, 0.5)) is False
    # upright, then unseen for 3 s, then already down and lower: window expired
    assert det.update(None) is False
    assert det.update(sample(3.0, 85, 0.8)) is False


def test_fall_rearms_only_after_upright():
    det = FallDetector()
    first = [sample(0.0, 5, 0.5)] + ramp(0.1, 0.5, 5, 85, 0.5, 0.8)
    assert stream(det, first).count(True) == 1
    # stays down, then a second fast "drop" without being upright: no fire
    again = [sample(2.0, 5.0 + 40, 0.5), sample(2.1, 85, 0.8)]
    assert not any(stream(det, again))
    # up again, then fall again
    up = [sample(3.0 + i * 0.1, 5, 0.5) for i in range(5)]
    assert not any(stream(det, up))
    second = ramp(3.6, 0.5, 5, 85, 0.5, 0.8)
    assert stream(det, second).count(True) == 1


def lying(t0, dur, cx=0.5, cy=0.8, step=1.0, jitter=0.0):
    out = []
    n = int(dur / step)
    for i in range(n + 1):
        sign = 1 if i % 2 else -1
        out.append(sample(t0 + i * step, 85, 0.8, cx + sign * jitter, cy))
    return out


def test_lying_still_fires_once_after_120s():
    det = LyingStillDetector()
    res = stream(det, lying(0, 130))
    assert res.count(True) == 1
    assert res.index(True) == 120  # t == 120 s


def test_lying_still_not_before_min_time():
    assert not any(stream(LyingStillDetector(), lying(0, 119)))


def test_lying_with_motion_does_not_fire():
    det = LyingStillDetector()
    assert not any(stream(det, lying(0, 200, jitter=0.1)))


def test_lying_none_gap_over_5s_resets():
    det = LyingStillDetector()
    pts = lying(0, 100)
    assert not any(stream(det, pts))
    assert det.update(None, t=101.0) is False
    assert det.update(None, t=110.0) is False  # gap > 5 s: reset
    after = lying(111, 60)
    assert not any(stream(det, after))  # only 60 s since reset
    assert any(stream(det, lying(171, 70)))  # 120 s since 111


def test_lying_short_none_gap_does_not_reset():
    det = LyingStillDetector()
    assert not any(stream(det, lying(0, 60)))
    assert det.update(None, t=62.0) is False
    assert any(stream(det, lying(63, 70)))


def test_lying_none_never_counts_as_evidence():
    det = LyingStillDetector()
    assert not any(det.update(None, t=float(t)) for t in range(300))


def test_lying_rearms_after_upright():
    det = LyingStillDetector()
    assert stream(det, lying(0, 130)).count(True) == 1
    assert not any(stream(det, lying(131, 200)))  # still the same episode
    up = [sample(340, 5, 0.5)]
    assert not any(stream(det, up))
    assert stream(det, lying(341, 130)).count(True) == 1


def test_person_tracker_blip_and_hold():
    tr = PersonTracker()
    assert [tr.update(x) for x in [False, True, False, False, False]] == [False] * 5
    tr = PersonTracker()
    seq = [True, True, True, False, True, False, False, False]
    assert [tr.update(x) for x in seq] == [False, False, True, True, True, True, False, False]


def test_lying_rearm_fires_again_on_next_frame():
    det = LyingStillDetector()
    assert stream(det, lying(0, 130)).count(True) == 1
    assert not det.update(sample(131, 85, 0.8, 0.5, 0.8))
    det.rearm()
    assert det.update(sample(132, 85, 0.8, 0.5, 0.8)) is True
    assert det.update(sample(133, 85, 0.8, 0.5, 0.8)) is False
