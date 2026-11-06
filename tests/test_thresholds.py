"""Per-sound confidence thresholds: catalog defaults, member overrides, device config, engine."""

import pytest
from sqlalchemy import select

from app.alerts.models import Alert
from app.auth.models import Member
from app.catalog import SOUNDS, get_sound
from app.rules.effective import effective_settings
from app.rules.models import SoundSetting
from tests.test_heartbeat import _device
from tests.test_rule_engine import _fire, _set, _setup

pytestmark = pytest.mark.usefixtures("frozen_ratelimit_clock")

MEASURED = {
    "siren": 0.50,
    "coughing": 0.50,
    "dog_barking": 0.50,
    "crying": 0.50,
    "glass_breaking": 0.30,
    "knocking": 0.30,
}


def test_catalog_defaults_follow_measured_accuracy():
    for key, sound in SOUNDS.items():
        assert sound.default_min_confidence == MEASURED.get(key, 0.30), key


async def test_sounds_endpoint_exposes_default_min_confidence(client):
    by_key = {s["key"]: s for s in (await client.get("/sounds")).json()}
    assert by_key["siren"]["default_min_confidence"] == 0.50
    assert by_key["scream"]["default_min_confidence"] == 0.30


async def _put(client, headers, key, **kw):
    body = {"enabled": True, "repeat_count": 1, "window_s": 60, "cooldown_s": 120} | kw
    return await client.put(f"/me/sound-settings/{key}", json=body, headers=headers)


async def test_effective_min_confidence_defaults_and_override(client, session, signed_up):
    member, h = await signed_up()
    eff = await effective_settings(session, member.id)
    assert eff["siren"].min_confidence == 0.50 and eff["scream"].min_confidence == 0.30

    r = await _put(client, h, "siren", min_confidence=0.7)
    assert r.status_code == 200, r.text
    assert r.json()["min_confidence"] == 0.7
    # an override row without min_confidence keeps the catalog default
    r = await _put(client, h, "doorbell")
    assert r.json()["min_confidence"] == 0.30
    listed = {s["sound_key"]: s for s in (await client.get("/me/sound-settings", headers=h)).json()}
    assert listed["siren"]["min_confidence"] == 0.7
    assert listed["dog_barking"]["min_confidence"] == 0.50


async def test_omitted_min_confidence_keeps_override_and_null_clears_it(client, signed_up):
    _, h = await signed_up()
    await _put(client, h, "siren", min_confidence=0.8)
    r = await _put(client, h, "siren", repeat_count=2)  # omitted: keep the override
    assert r.json()["min_confidence"] == 0.8 and r.json()["repeat_count"] == 2
    r = await _put(client, h, "siren", min_confidence=None)  # explicit null: back to default
    assert r.json()["min_confidence"] == 0.50


@pytest.mark.parametrize("bad", [0.0, 0.04, 0.96, 1.0, -0.1])
async def test_min_confidence_out_of_range_is_422(client, signed_up, bad):
    _, h = await signed_up()
    assert (await _put(client, h, "siren", min_confidence=bad)).status_code == 422


@pytest.mark.parametrize("ok", [0.05, 0.95])
async def test_min_confidence_bounds_accepted(client, signed_up, ok):
    _, h = await signed_up()
    assert (await _put(client, h, "siren", min_confidence=ok)).status_code == 200


async def _second_member(session, owner, email="m2@example.com", confirmed=True):
    other = Member(
        household_id=owner.household_id,
        email=email,
        password_hash="x",
        role="member",
        email_confirmed=confirmed,
    )
    session.add(other)
    await session.flush()
    return other


async def _set_conf(session, member, sound, min_confidence, enabled=True):
    session.add(
        SoundSetting(
            household_id=member.household_id,
            member_id=member.id,
            sound_key=sound,
            enabled=enabled,
            repeat_count=1,
            window_s=60,
            cooldown_s=120,
            min_confidence=min_confidence,
        )
    )
    await session.flush()


async def test_config_thresholds_are_min_across_enabling_members(client, session, signed_up):
    owner, h = await signed_up()
    _, dk = await _device(client, h)
    other = await _second_member(session, owner)
    pending = await _second_member(session, owner, "pending@example.com", confirmed=False)
    await _set_conf(session, owner, "siren", 0.7)
    await _set_conf(session, other, "siren", 0.6)
    await _set_conf(session, pending, "siren", 0.1)  # unconfirmed: never alerted, ignored
    await _set_conf(session, owner, "dog_barking", 0.9)
    await _set_conf(session, other, "dog_barking", 0.1, enabled=False)  # disabled: ignored
    await session.commit()

    r = await client.get("/device/config", headers=dk)
    assert r.status_code == 200, r.text
    cfg = r.json()
    assert cfg["thresholds"]["siren"] == 0.6
    assert cfg["thresholds"]["dog_barking"] == 0.9
    assert cfg["thresholds"]["scream"] == 0.30  # catalog default, nobody overrode it
    assert set(cfg["thresholds"]) == set(cfg["enabled_sounds"])
    hb = await client.post("/device/heartbeat", headers=dk)
    assert hb.json()["thresholds"] == cfg["thresholds"]


async def test_engine_skips_member_below_their_threshold(signed_up, session, redis):
    owner, (dev,) = await _setup(signed_up, session)
    other = await _second_member(session, owner)
    await _set_conf(session, owner, "scream", 0.95)  # stricter than the 0.9 event
    await _set_conf(session, other, "scream", 0.5)
    _, out = await _fire(session, redis, owner, dev, "scream", 1000.0)
    alerts = (await session.scalars(select(Alert).where(Alert.id.in_(out.alert_ids)))).all()
    assert [a.member_id for a in alerts] == [other.id]
    # the skipped event did not count toward the strict member's repeat window
    assert await redis.zcard(f"rep:{owner.household_id}:{owner.id}:scream") == 0


async def test_engine_uses_catalog_default_threshold(signed_up, session, redis):
    owner, (dev,) = await _setup(signed_up, session)
    await _set(session, owner, "siren")  # override row with no min_confidence -> 0.50
    assert get_sound("siren").default_min_confidence == 0.50
    _, out = await _fire(session, redis, owner, dev, "siren", 1000.0, confidence=0.4)
    assert out.alert_ids == []
    _, out = await _fire(session, redis, owner, dev, "siren", 1001.0, confidence=0.5)
    assert len(out.alert_ids) == 1


async def _alerted(session, out):
    rows = (await session.scalars(select(Alert).where(Alert.id.in_(out.alert_ids)))).all()
    return [a.member_id for a in rows]


async def _two_devices_x_loose_y_strict(signed_up, session):
    owner, (a, b) = await _setup(signed_up, session, devices=2)
    y = await _second_member(session, owner)
    await _set_conf(session, owner, "scream", 0.30)  # X
    await _set_conf(session, y, "scream", 0.50)  # Y
    return owner, y, a, b


async def test_merged_higher_report_alerts_stricter_member_once(signed_up, session, redis):
    x, y, a, b = await _two_devices_x_loose_y_strict(signed_up, session)
    _, o1 = await _fire(session, redis, x, a, "scream", 1000.0, confidence=0.35)
    assert o1.merged is False and await _alerted(session, o1) == [x.id]
    e2, o2 = await _fire(session, redis, x, b, "scream", 1001.0, confidence=0.80)
    assert o2.merged is True and e2.merged is True
    assert await _alerted(session, o2) == [y.id]  # X was already satisfied by A's report
    _, o3 = await _fire(session, redis, x, b, "scream", 1002.0, confidence=0.90)
    _, o4 = await _fire(session, redis, x, a, "scream", 1003.0, confidence=0.95)
    assert o3.alert_ids == [] and o4.alert_ids == []
    total = (await session.scalars(select(Alert.member_id).order_by(Alert.id))).all()
    assert total == [x.id, y.id]


async def test_merged_report_below_every_threshold_alerts_nobody(signed_up, session, redis):
    x, y, a, b = await _two_devices_x_loose_y_strict(signed_up, session)
    await _fire(session, redis, x, a, "scream", 1000.0, confidence=0.35)
    _, out = await _fire(session, redis, x, b, "scream", 1001.0, confidence=0.45)
    assert out.merged is True and out.alert_ids == []


async def test_repeated_reports_from_holder_do_not_block_other_device(signed_up, session, redis):
    x, y, a, b = await _two_devices_x_loose_y_strict(signed_up, session)
    for i in range(3):
        await _fire(session, redis, x, a, "scream", 1000.0 + i, confidence=0.35)
    _, out = await _fire(session, redis, x, b, "scream", 1004.0, confidence=0.80)
    assert out.merged is True and await _alerted(session, out) == [y.id]
