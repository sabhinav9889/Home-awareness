from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

import app.events.router as events_router
from app.events.models import Event
from tests.test_devices import _pair


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr("app.ratelimit.time.time", lambda: 1_800_000_000.0)


async def _device(client, headers, **kw):
    paired = await _pair(client, headers, **kw)
    return paired["device_id"], {"X-Device-Key": paired["device_key"]}


def _body(type_="scream", confidence=0.9, occurred_at=None, **extra):
    when = occurred_at or datetime.now(UTC)
    return {"type": type_, "confidence": confidence, "occurred_at": when.isoformat(), **extra}


async def test_event_stored_with_server_receive_time(client, signed_up, session, monkeypatch):
    member, h = await signed_up()
    device_id, dk = await _device(client, h, name="Hall")
    published = []

    async def fake_publish(redis, household_id, msg):
        published.append((household_id, msg))

    monkeypatch.setattr(events_router, "publish_to_household", fake_publish)
    occurred = datetime.now(UTC) - timedelta(minutes=5)
    before = datetime.now(UTC)
    r = await client.post("/device/events", json=_body(occurred_at=occurred), headers=dk)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["merged"] is False and body["alerts"] == 1  # scream is on by default
    event = await session.get(Event, body["event_id"])
    assert event.household_id == member.household_id and event.device_id == device_id
    assert event.sound_key == "scream" and event.confidence == 0.9
    assert event.received_at >= before  # server clock, not the device's
    assert abs(event.occurred_at - occurred) < timedelta(seconds=1)
    assert len(published) == 1 and published[0][0] == member.household_id
    msg = published[0][1]
    assert msg["kind"] == "event" and msg["event_id"] == event.id
    assert msg["label"] == "Scream" and msg["device_name"] == "Hall" and msg["merged"] is False
    assert (await client.post("/device/events", json=_body())).status_code == 401


async def test_unknown_sound_type_422(client, signed_up):
    _, h = await signed_up()
    _, dk = await _device(client, h)
    r = await client.post("/device/events", json=_body(type_="not_a_sound"), headers=dk)
    assert r.status_code == 422


async def test_extra_fields_rejected_422(client, signed_up):
    _, h = await signed_up()
    _, dk = await _device(client, h)
    r = await client.post("/device/events", json=_body(audio="UklGRg=="), headers=dk)
    assert r.status_code == 422


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
async def test_confidence_out_of_range_422(client, signed_up, confidence):
    _, h = await signed_up()
    _, dk = await _device(client, h)
    r = await client.post("/device/events", json=_body(confidence=confidence), headers=dk)
    assert r.status_code == 422


async def test_naive_occurred_at_422(client, signed_up):
    _, h = await signed_up()
    _, dk = await _device(client, h)
    body = _body()
    body["occurred_at"] = datetime.now(UTC).replace(tzinfo=None).isoformat()
    assert (await client.post("/device/events", json=body, headers=dk)).status_code == 422


async def test_clock_skew_over_10_minutes_422_and_not_stored(client, signed_up, session):
    _, h = await signed_up()
    _, dk = await _device(client, h)
    now = datetime.now(UTC)
    for skew in (timedelta(minutes=11), -timedelta(minutes=11)):
        r = await client.post("/device/events", json=_body(occurred_at=now + skew), headers=dk)
        assert r.status_code == 422, r.text
    assert await session.scalar(select(func.count()).select_from(Event)) == 0
    # Inside the window (either direction) is accepted.
    for skew in (timedelta(minutes=9), -timedelta(minutes=9)):
        r = await client.post("/device/events", json=_body(occurred_at=now + skew), headers=dk)
        assert r.status_code == 202, r.text


async def test_clock_skew_boundary_is_inclusive(client, signed_up, monkeypatch):
    _, h = await signed_up()
    _, dk = await _device(client, h)
    fixed = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr("app.events.schemas.datetime", FrozenDatetime)
    monkeypatch.setattr("app.events.router.datetime", FrozenDatetime)
    for skew in (timedelta(minutes=10), -timedelta(minutes=10)):
        r = await client.post("/device/events", json=_body(occurred_at=fixed + skew), headers=dk)
        assert r.status_code == 202, r.text
    r = await client.post(
        "/device/events",
        json=_body(occurred_at=fixed + timedelta(minutes=10, seconds=1)),
        headers=dk,
    )
    assert r.status_code == 422


async def test_event_rate_limit_61st_429(client, signed_up):
    _, h = await signed_up()
    _, dk = await _device(client, h)
    _, dk2 = await _device(client, h, name="Other")
    for _ in range(60):
        assert (await client.post("/device/events", json=_body(), headers=dk)).status_code == 202
    assert (await client.post("/device/events", json=_body(), headers=dk)).status_code == 429
    assert (await client.post("/device/events", json=_body(), headers=dk2)).status_code == 202


async def test_route_reports_rule_outcome(client, signed_up, session, monkeypatch):
    _, h = await signed_up()
    _, dk = await _device(client, h)

    async def fake_process(session, redis, event, now):
        return events_router.RuleOutcome(merged=True, alert_ids=[1, 2])

    monkeypatch.setattr(events_router, "process_event", fake_process)
    r = await client.post("/device/events", json=_body(), headers=dk)
    assert r.status_code == 202
    assert r.json()["merged"] is True and r.json()["alerts"] == 2
    event = await session.get(Event, r.json()["event_id"])
    await session.refresh(event)
    assert event.merged is True


async def test_history_newest_first_paginated_and_scoped(client, signed_up, two_households):
    headers_a, headers_b = two_households
    _, dka = await _device(client, headers_a, name="Kitchen")
    _, dkb = await _device(client, headers_b, name="Other")
    ids = []
    for t in ("scream", "glass_breaking", "smoke_alarm"):
        r = await client.post("/device/events", json=_body(type_=t), headers=dka)
        ids.append(r.json()["event_id"])
    rb = await client.post("/device/events", json=_body(), headers=dkb)

    r = await client.get("/events", headers=headers_a)
    assert r.status_code == 200
    rows = r.json()
    assert [e["id"] for e in rows] == ids[::-1]
    assert rb.json()["event_id"] not in {e["id"] for e in rows}
    assert rows[0]["sound_key"] == "smoke_alarm" and rows[0]["device_name"] == "Kitchen"
    assert set(rows[0]) == {
        "id",
        "sound_key",
        "label",
        "device_name",
        "confidence",
        "received_at",
        "merged",
    }

    page1 = (await client.get("/events?limit=2", headers=headers_a)).json()
    assert [e["id"] for e in page1] == ids[::-1][:2]
    page2 = (
        await client.get(f"/events?limit=2&before_id={page1[-1]['id']}", headers=headers_a)
    ).json()
    assert [e["id"] for e in page2] == [ids[0]]
    assert (await client.get("/events?limit=201", headers=headers_a)).status_code == 422
    assert (await client.get("/events?limit=0", headers=headers_a)).status_code == 422
    assert (await client.get("/events")).status_code == 401


async def test_history_keeps_events_after_device_deleted(client, signed_up):
    _, h = await signed_up()
    device_id, dk = await _device(client, h)
    await client.post("/device/events", json=_body(), headers=dk)
    assert (await client.delete(f"/devices/{device_id}", headers=h)).status_code == 204
    rows = (await client.get("/events", headers=h)).json()
    assert len(rows) == 1 and rows[0]["device_name"] is None


async def test_device_tenant_cannot_touch_other_household(
    client, signed_up, session, two_households
):
    headers_a, headers_b = two_households
    device_a, dka = await _device(client, headers_a)
    await _device(client, headers_b)
    r = await client.post("/device/events", json=_body(), headers=dka)
    assert r.status_code == 202
    event = await session.get(Event, r.json()["event_id"])
    a_household = (await client.get("/households/me", headers=headers_a)).json()["id"]
    b_household = (await client.get("/households/me", headers=headers_b)).json()["id"]
    assert event.household_id == a_household != b_household and event.device_id == device_a
    assert (await client.get("/events", headers=headers_b)).json() == []
    total = await session.scalar(
        select(func.count()).select_from(Event).where(Event.household_id == b_household)
    )
    assert total == 0
