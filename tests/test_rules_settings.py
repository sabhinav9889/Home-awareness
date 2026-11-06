import pytest

from app.catalog import SOUNDS, get_sound
from app.rules.effective import effective_settings, household_enabled_sounds, members_enabled_for


def test_catalog_has_17_sounds_and_three_default_on():
    assert len(SOUNDS) == 17
    assert {k for k, s in SOUNDS.items() if s.default_enabled} == {
        "scream",
        "glass_breaking",
        "smoke_alarm",
    }
    with pytest.raises(KeyError):
        get_sound("nope")


def test_catalog_severity_urgent_list_is_exact():
    assert {k for k, s in SOUNDS.items() if s.severity == "urgent"} == {
        "scream",
        "glass_breaking",
        "smoke_alarm",
        "siren",
        "explosion",
        "thud_fall",
        "gasping",
    }
    assert {s.severity for s in SOUNDS.values()} == {"urgent", "normal"}


async def test_sounds_endpoint_exposes_severity(client):
    by_key = {s["key"]: s["severity"] for s in (await client.get("/sounds")).json()}
    assert by_key["scream"] == "urgent" and by_key["doorbell"] == "normal"


def test_dog_barking_defaults_to_5_in_60s():
    s = get_sound("dog_barking")
    assert (s.default_repeat, s.default_window_s, s.default_cooldown_s) == (5, 60, 120)


async def _put(client, headers, key, **kw):
    body = {"enabled": True, "repeat_count": 1, "window_s": 60, "cooldown_s": 120} | kw
    return await client.put(f"/me/sound-settings/{key}", json=body, headers=headers)


async def test_effective_settings_merge_override_with_defaults(client, session, signed_up):
    member, h = await signed_up()
    assert (await client.get("/sounds")).json()[0]["key"] == "scream"
    assert (await _put(client, h, "doorbell", repeat_count=3, window_s=30)).status_code == 200
    assert (await _put(client, h, "scream", enabled=False)).status_code == 200
    eff = await effective_settings(session, member.id)
    assert len(eff) == 17
    assert (eff["doorbell"].enabled, eff["doorbell"].repeat_count, eff["doorbell"].window_s) == (
        True,
        3,
        30,
    )
    assert eff["scream"].enabled is False
    assert eff["dog_barking"].repeat_count == 5 and eff["dog_barking"].enabled is False
    assert eff["glass_breaking"].enabled is True
    listed = {s["sound_key"]: s for s in (await client.get("/me/sound-settings", headers=h)).json()}
    assert len(listed) == 17 and listed["doorbell"]["repeat_count"] == 3
    # second PUT updates in place
    assert (await _put(client, h, "doorbell", repeat_count=4)).status_code == 200
    assert (await effective_settings(session, member.id))["doorbell"].repeat_count == 4


async def test_household_enabled_sounds_is_union_of_members(client, session, signed_up):
    a, ha = await signed_up(email="a@example.com")
    r = await client.post("/households/me/invites", headers=ha)
    token = r.json()["invite_url"].split("invite=")[1]
    r = await client.post(
        "/auth/signup",
        json={"email": "b@example.com", "password": "correct-horse-1", "invite_token": token},
    )
    assert r.status_code == 201, r.text
    b_id = r.json()["member_id"]
    await _put(client, ha, "dog_barking")
    from app.auth.models import Member

    b = await session.get(Member, b_id)
    assert b.household_id == a.household_id
    from app.rules.models import SoundSetting

    session.add(
        SoundSetting(
            household_id=a.household_id,
            member_id=b_id,
            sound_key="scream",
            enabled=False,
            repeat_count=1,
            window_s=60,
            cooldown_s=120,
        )
    )
    await session.flush()
    union = await household_enabled_sounds(session, a.household_id)
    assert {"scream", "dog_barking"} <= union
    who = await members_enabled_for(session, a.household_id, "scream")
    assert [m for m, _ in who] == [a.id]
    assert {m for m, _ in await members_enabled_for(session, a.household_id, "dog_barking")} == {
        a.id
    }
    assert await members_enabled_for(session, a.household_id, "doorbell") == []


async def test_put_unknown_sound_404_and_bad_range_422(client, signed_up):
    _, h = await signed_up()
    assert (await _put(client, h, "nope")).status_code == 404
    for bad in (
        {"repeat_count": 0},
        {"repeat_count": 21},
        {"window_s": 4},
        {"cooldown_s": -1},
        {"cooldown_s": 86401},
    ):
        assert (await _put(client, h, "scream", **bad)).status_code == 422


async def test_telegram_pref_requires_linked_chat(client, signed_up):
    _, h = await signed_up()
    got = (await client.get("/me/alert-preferences", headers=h)).json()
    assert (got["popup"], got["alarm"], got["telegram"]) == (True, False, False)
    r = await client.put(
        "/me/alert-preferences",
        json={"popup": True, "alarm": True, "telegram": True, "ntfy_topic": None},
        headers=h,
    )
    assert r.status_code == 400 and r.json()["detail"] == "link Telegram first"
    body = {"popup": False, "alarm": True, "telegram": False, "ntfy_topic": None}
    r = await client.put("/me/alert-preferences", json=body, headers=h)
    assert r.status_code == 200 and r.json()["alarm"] is True


async def test_short_ntfy_topic_rejected_and_generated_topic_valid(client, signed_up):
    _, h = await signed_up()
    r = await client.put(
        "/me/alert-preferences",
        json={"popup": True, "alarm": False, "telegram": False, "ntfy_topic": "short"},
        headers=h,
    )
    assert r.status_code == 422
    r = await client.post("/me/alert-preferences/ntfy-topic", headers=h)
    assert r.status_code == 200
    topic = r.json()["ntfy_topic"]
    import re

    assert re.fullmatch(r"[A-Za-z0-9_-]{20,64}", topic)
    assert (await client.get("/me/alert-preferences", headers=h)).json()["ntfy_topic"] == topic


async def test_omitting_ntfy_topic_is_422_not_silent_clear(client, signed_up):
    _, h = await signed_up()
    topic = (await client.post("/me/alert-preferences/ntfy-topic", headers=h)).json()["ntfy_topic"]
    body = {"popup": True, "alarm": False, "telegram": False}
    assert (await client.put("/me/alert-preferences", json=body, headers=h)).status_code == 422
    assert (await client.get("/me/alert-preferences", headers=h)).json()["ntfy_topic"] == topic
    body["ntfy_topic"] = None  # clearing is explicit
    r = await client.put("/me/alert-preferences", json=body, headers=h)
    assert r.status_code == 200 and r.json()["ntfy_topic"] is None
