"""Camera event types, Away mode, night hours and the device `guarding` flag."""

from datetime import UTC, datetime, time
from types import SimpleNamespace

import pytest

import app.households.router as households_router
from app.catalog import SOUNDS
from app.events.schemas import INCIDENT_KEYS
from app.households.guarding import is_guarding
from tests.test_heartbeat import _device

pytestmark = pytest.mark.usefixtures("frozen_ratelimit_clock")

INCIDENTS = {
    "incident_break_in": "Possible break-in",
    "incident_fall_confirmed": "Fall confirmed",
    "incident_property_damage": "Possible property damage",
}


def test_camera_catalog_entries_are_exact():
    expected = {
        # key: (label, group, default_enabled, reliability, cooldown)
        "fall": ("Fall", "Health", True, "experimental", 120),
        "lying_still": ("Lying still (possible collapse)", "Health", True, "experimental", 600),
        "person_away": ("Person while guarding", "Security", True, "good", 300),
        "weapon_seen": ("Weapon seen", "Security", False, "experimental", 300),
    } | {k: (label, "Incidents", True, "experimental", 300) for k, label in INCIDENTS.items()}
    for key, (label, group, on, reliability, cooldown) in expected.items():
        s = SOUNDS[key]
        assert (s.label, s.group, s.default_enabled, s.reliability, s.default_cooldown_s) == (
            label,
            group,
            on,
            reliability,
            cooldown,
        ), key
        assert s.severity == "urgent", key
    assert set(INCIDENT_KEYS) == set(INCIDENTS)
    assert len(SOUNDS) == 24


async def test_device_cannot_post_incident_keys(client, signed_up):
    _, h = await signed_up()
    _, dk = await _device(client, h)
    now = datetime.now(UTC).isoformat()
    for key in INCIDENTS:
        r = await client.post(
            "/device/events",
            json={"type": key, "confidence": 0.9, "occurred_at": now},
            headers=dk,
        )
        assert r.status_code == 422, key
    r = await client.post(
        "/device/events", json={"type": "fall", "confidence": 0.9, "occurred_at": now}, headers=dk
    )
    assert r.status_code == 202, r.text


async def test_device_config_excludes_incident_keys(client, signed_up):
    _, h = await signed_up()
    _, dk = await _device(client, h)
    sounds = (await client.post("/device/heartbeat", headers=dk)).json()["enabled_sounds"]
    assert "fall" in sounds and "person_away" in sounds
    assert not set(sounds) & set(INCIDENTS)


async def test_member_toggles_away_and_heartbeat_reports_guarding(
    client, signed_up, member_headers, monkeypatch
):
    _, oh = await signed_up()
    mh = await member_headers(oh)
    _, dk = await _device(client, oh)
    assert (await client.post("/device/heartbeat", headers=dk)).json()["guarding"] is False

    published = []

    async def fake_publish(redis, household_id, msg):
        published.append((household_id, msg))
        return 0

    monkeypatch.setattr(households_router, "publish_to_household", fake_publish)
    r = await client.patch("/households/me", json={"away": True}, headers=mh)
    assert r.status_code == 200, r.text
    assert r.json()["away"] is True
    assert published == [
        (published[0][0], {"kind": "guarding", "away": True, "by_email": "member@example.com"})
    ]
    assert (await client.post("/device/heartbeat", headers=dk)).json()["guarding"] is True
    assert (await client.get("/device/config", headers=dk)).json()["guarding"] is True

    r = await client.patch("/households/me", json={"away": False}, headers=oh)
    assert r.json()["away"] is False
    assert (await client.post("/device/heartbeat", headers=dk)).json()["guarding"] is False


async def test_owner_only_fields_forbidden_for_member(client, signed_up, member_headers):
    _, oh = await signed_up()
    mh = await member_headers(oh)
    for body in (
        {"night_start": "23:00:00", "night_end": "06:00:00"},
        {"snapshot_retention_days": 30},
        {"timezone": "Asia/Kolkata"},
        {"away": True, "snapshot_retention_days": 30},
    ):
        r = await client.patch("/households/me", json=body, headers=mh)
        assert r.status_code == 403, body
    assert (await client.get("/households/me", headers=oh)).json()["away"] is False


async def test_owner_sets_night_hours_and_retention(client, signed_up):
    _, oh = await signed_up()
    r = await client.patch(
        "/households/me",
        json={"night_start": "23:00:00", "night_end": "06:00:00", "snapshot_retention_days": 30},
        headers=oh,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["night_start"] == "23:00:00" and body["night_end"] == "06:00:00"
    assert body["snapshot_retention_days"] == 30 and body["away"] is False
    # clear night hours
    r = await client.patch(
        "/households/me", json={"night_start": None, "night_end": None}, headers=oh
    )
    assert r.json()["night_start"] is None and r.json()["night_end"] is None
    assert r.json()["snapshot_retention_days"] == 30


@pytest.mark.parametrize(
    "body",
    [
        {"snapshot_retention_days": 3},
        {"night_start": "23:00:00"},
        {"night_end": "06:00:00"},
        {"night_start": "23:00:00", "night_end": "23:00:00"},
    ],
)
async def test_invalid_guarding_patch_is_422(client, signed_up, body):
    _, oh = await signed_up()
    assert (await client.patch("/households/me", json=body, headers=oh)).status_code == 422


def _h(tz="UTC", away=False, start=time(23), end=time(6)):
    return SimpleNamespace(timezone=tz, away=away, night_start=start, night_end=end)


def _utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


@pytest.mark.parametrize(
    "tz,now,expected",
    [
        # Asia/Kolkata is UTC+5:30: 05:59 IST = 00:29 UTC, 22:59 IST = 17:29 UTC
        ("Asia/Kolkata", _utc(2026, 11, 8, 0, 29), True),  # 05:59
        ("Asia/Kolkata", _utc(2026, 11, 8, 0, 30), False),  # 06:00
        ("Asia/Kolkata", _utc(2026, 11, 8, 17, 29), False),  # 22:59
        ("Asia/Kolkata", _utc(2026, 11, 8, 17, 30), True),  # 23:00
        # America/New_York: EST (UTC-5) on 2026-11-08 after DST ends at 06:00 UTC
        ("America/New_York", _utc(2026, 11, 8, 10, 59), True),  # 05:59 EST
        ("America/New_York", _utc(2026, 11, 8, 11, 0), False),  # 06:00 EST
        ("America/New_York", _utc(2026, 11, 8, 2, 59), False),  # 22:59 EDT (before DST ends)
        ("America/New_York", _utc(2026, 11, 8, 4, 0), True),  # 00:00 EDT, inside the night
        # EDT (UTC-4) in summer
        ("America/New_York", _utc(2026, 7, 8, 9, 59), True),  # 05:59 EDT
        ("America/New_York", _utc(2026, 7, 8, 10, 0), False),  # 06:00 EDT
        ("America/New_York", _utc(2026, 7, 8, 2, 59), False),  # 22:59 EDT
        ("America/New_York", _utc(2026, 7, 8, 3, 0), True),  # 23:00 EDT
        ("UTC", _utc(2026, 11, 8, 12, 0), False),
    ],
)
def test_is_guarding_night_hours_wrap_midnight(tz, now, expected):
    assert is_guarding(_h(tz), now) is expected


def test_is_guarding_away_overrides_and_no_night_hours():
    noon = _utc(2026, 11, 8, 12, 0)
    assert is_guarding(_h(away=True), noon) is True
    assert is_guarding(_h(start=None, end=None), noon) is False
    assert is_guarding(_h(start=None, end=None), _utc(2026, 11, 8, 2, 0)) is False


def test_is_guarding_same_day_window_and_bad_timezone():
    h = _h(start=time(9), end=time(17))
    assert is_guarding(h, _utc(2026, 11, 8, 9, 0)) is True
    assert is_guarding(h, _utc(2026, 11, 8, 17, 0)) is False
    # an unknown zone falls back to UTC rather than failing
    assert is_guarding(_h("Mars/Base", start=time(9), end=time(17)), _utc(2026, 11, 8, 10, 0))
