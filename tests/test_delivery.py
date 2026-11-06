import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import respx
from sqlalchemy import select

import app.alerts.delivery as delivery
from app.alerts import jobs
from app.alerts.delivery import deliver_alert
from app.alerts.models import Alert, Delivery
from app.devices.models import Device
from app.events.models import Event
from app.rules.models import AlertPreference

TOKEN = "123:SECRET-TOKEN"
TG_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
TEST_TEXT = "🔔 Test alert from Home Awareness — your alerts work."


pytestmark = pytest.mark.usefixtures("frozen_ratelimit_clock")


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    s = SimpleNamespace(
        telegram_bot_token=TOKEN, ntfy_base_url="https://ntfy.test", job_runner="inprocess"
    )
    monkeypatch.setattr(delivery, "get_settings", lambda: s)
    monkeypatch.setattr(jobs, "get_settings", lambda: s)
    return s


class _Ctx:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def factory(session):
    return lambda: _Ctx(session)


@pytest.fixture
def published(monkeypatch):
    async def fake(redis, member_id, msg):
        calls.append((member_id, msg))
        return calls.listeners

    calls = _Published()
    monkeypatch.setattr(delivery, "publish_to_member", fake)
    return calls


class _Published(list):
    listeners = 1  # one open dashboard tab by default; set to 0 for "nobody listening"


async def _no_sleep(_s):
    return None


@pytest.fixture
def sleeps():
    rec = []

    async def sleep(s):
        rec.append(s)

    sleep.rec = rec
    return sleep


async def _prefs(session, member, **kw):
    prefs = await session.get(AlertPreference, member.id)
    if prefs is None:
        prefs = AlertPreference(member_id=member.id, household_id=member.household_id)
        session.add(prefs)
    for k, v in kw.items():
        setattr(prefs, k, v)
    await session.flush()


async def _alert(session, member, *, device_name="Hall", sound="scream"):
    device = Device(
        household_id=member.household_id,
        name=device_name,
        kind="pi",
        key_hash=f"h{member.id}{device_name}",
    )
    session.add(device)
    await session.flush()
    now = datetime(2026, 11, 7, 14, 5, 30, tzinfo=UTC)
    ev = Event(
        household_id=member.household_id,
        device_id=device.id,
        sound_key=sound,
        confidence=0.9,
        occurred_at=now,
        received_at=now,
    )
    session.add(ev)
    await session.flush()
    alert = Alert(
        household_id=member.household_id,
        member_id=member.id,
        event_id=ev.id,
        sound_key=sound,
        created_at=now,
    )
    session.add(alert)
    await session.flush()
    return alert


async def _deliveries(session, alert_id):
    rows = (await session.execute(select(Delivery).where(Delivery.alert_id == alert_id))).scalars()
    return {d.channel: d for d in rows}


async def _run(alert, factory, redis, sleep=_no_sleep, *, age=timedelta(seconds=30)):
    """Deliver as if `age` has passed since the alert was created (fresh by default)."""
    created = alert.created_at
    async with httpx.AsyncClient() as http:
        await deliver_alert(
            alert.id, session_factory=factory, redis=redis, http=http, sleep=sleep,
            clock=lambda: created + age,
        )  # fmt: skip


@respx.mock
async def test_delivers_to_all_enabled_channels(signed_up, session, factory, redis, published):
    member, _ = await signed_up()
    await _prefs(
        session, member, popup=True, alarm=True, telegram=True, telegram_chat_id="42",
        ntfy_topic="my-topic",
    )  # fmt: skip
    alert = await _alert(session, member)
    tg = respx.post(TG_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    nt = respx.post("https://ntfy.test/my-topic").mock(return_value=httpx.Response(200))
    await _run(alert, factory, redis)
    d = await _deliveries(session, alert.id)
    assert set(d) == {"websocket", "telegram", "ntfy"}
    assert all(x.status == "sent" for x in d.values())
    assert tg.call_count == 1 and nt.call_count == 1
    assert nt.calls[0].request.headers["Priority"] == "high"
    await session.refresh(alert)
    assert alert.status == "sent"
    ((member_id, msg),) = published
    assert member_id == member.id
    assert msg == {
        "kind": "alert", "alert_id": alert.id, "sound_key": "scream", "label": "Scream",
        "device_name": "Hall", "at": "2026-11-07T14:05:30+00:00", "popup": True, "alarm": True,
    }  # fmt: skip


@respx.mock
async def test_telegram_retries_3_times_then_failed_other_channels_still_sent(
    signed_up, session, factory, redis, published, sleeps
):
    member, _ = await signed_up()
    await _prefs(session, member, telegram=True, telegram_chat_id="42", ntfy_topic="t")
    alert = await _alert(session, member)
    tg = respx.post(TG_URL).mock(
        return_value=httpx.Response(403, json={"ok": False, "description": "bot was blocked"})
    )
    respx.post("https://ntfy.test/t").mock(return_value=httpx.Response(200))
    await _run(alert, factory, redis, sleeps)
    d = await _deliveries(session, alert.id)
    assert tg.call_count == 3 and sleeps.rec == [1, 4]
    assert d["telegram"].status == "failed" and d["telegram"].attempts == 3
    assert "bot was blocked" in d["telegram"].last_error
    assert TOKEN not in d["telegram"].last_error
    assert d["ntfy"].status == "sent" and d["ntfy"].attempts == 1
    assert d["websocket"].status == "sent"
    await session.refresh(alert)
    assert alert.status == "partial"


@respx.mock
async def test_transport_error_never_stores_token(signed_up, session, factory, redis, published):
    member, _ = await signed_up()
    await _prefs(session, member, popup=False, telegram=True, telegram_chat_id="42")
    alert = await _alert(session, member)
    respx.post(TG_URL).mock(side_effect=httpx.ConnectError(f"boom {TG_URL}"))
    await _run(alert, factory, redis)
    d = await _deliveries(session, alert.id)
    assert d["telegram"].status == "failed" and TOKEN not in d["telegram"].last_error
    await session.refresh(alert)
    assert alert.status == "failed"


@respx.mock
async def test_message_text_exact(signed_up, session, factory, redis, published):
    member, _ = await signed_up()
    await _prefs(session, member, telegram=True, telegram_chat_id="42", ntfy_topic="t")
    alert = await _alert(session, member, device_name="Hall")
    tg = respx.post(TG_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    nt = respx.post("https://ntfy.test/t").mock(return_value=httpx.Response(200))
    await _run(alert, factory, redis)
    expected = "⚠️ Scream detected by Hall at 14:05 UTC — Home Awareness"
    import json

    assert json.loads(tg.calls[0].request.content) == {"chat_id": "42", "text": expected}
    assert nt.calls[0].request.content.decode() == expected


@respx.mock
async def test_removed_device_and_missing_prefs_defaults(
    signed_up, session, factory, redis, published
):
    member, _ = await signed_up()
    alert = await _alert(session, member)
    ev = await session.get(Event, alert.event_id)
    ev.device_id = None
    await session.flush()
    await _run(alert, factory, redis)
    d = await _deliveries(session, alert.id)
    assert set(d) == {"websocket"}  # defaults: popup only
    assert published[0][1]["device_name"] == "a removed sensor"
    assert published[0][1]["popup"] is True and published[0][1]["alarm"] is False


@respx.mock
async def test_not_pending_alert_is_skipped(signed_up, session, factory, redis, published):
    member, _ = await signed_up()
    alert = await _alert(session, member)
    alert.status = "sent"
    await session.flush()
    await _run(alert, factory, redis)
    assert await _deliveries(session, alert.id) == {} and published == []


async def test_inprocess_runner_runs_delivery(monkeypatch):
    seen = []

    async def fake(alert_id, **kw):
        seen.append(alert_id)

    monkeypatch.setattr(jobs, "deliver_alert", fake)
    runner = jobs.InProcessJobRunner()
    await runner.enqueue_alert(7)
    await asyncio.gather(*runner._tasks)
    assert seen == [7]


def test_job_runner_selected_by_setting(settings):
    settings.job_runner = "inprocess"
    assert isinstance(jobs.get_job_runner(), jobs.InProcessJobRunner)
    settings.job_runner = "celery"
    assert isinstance(jobs.get_job_runner(), jobs.CeleryJobRunner)


async def test_celery_runner_publishes_fast_and_off_loop(monkeypatch, settings):
    import app.alerts.celery_app as celery_app

    calls = []
    settings.job_runner = "celery"
    monkeypatch.setattr(
        celery_app.deliver_alert_task,
        "apply_async",
        lambda args, **kw: calls.append((args, kw)),
    )
    await jobs.get_job_runner().enqueue_alert(3)
    assert calls == [((3,), {"retry": False, "expires": 600})]


async def test_inprocess_runner_logs_task_exceptions(monkeypatch, caplog):
    async def boom(alert_id, **kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(jobs, "deliver_alert", boom)
    runner = jobs.InProcessJobRunner()
    with caplog.at_level("ERROR", logger="app.alerts.jobs"):
        await runner.enqueue_alert(9)
        await asyncio.gather(*runner._tasks, return_exceptions=True)
        await asyncio.sleep(0)
    assert any("alert 9" in r.getMessage() and r.exc_info for r in caplog.records)
    assert not runner._tasks


@respx.mock
async def test_second_delivery_of_same_alert_sends_nothing(
    signed_up, session, factory, redis, published
):
    # The shared savepoint session can't run two truly concurrent transactions, so the race
    # is simulated: the second call runs after the first has claimed the alert (the claim is
    # one atomic UPDATE ... WHERE status='pending', which is what serialises real races).
    member, _ = await signed_up()
    await _prefs(session, member, telegram=True, telegram_chat_id="42", ntfy_topic="t")
    alert = await _alert(session, member)
    tg = respx.post(TG_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    nt = respx.post("https://ntfy.test/t").mock(return_value=httpx.Response(200))
    await _run(alert, factory, redis)
    await _run(alert, factory, redis)
    assert tg.call_count == 1 and nt.call_count == 1 and len(published) == 1
    assert len(await _deliveries(session, alert.id)) == 3


@respx.mock
async def test_crash_after_first_channel_keeps_sent_row_and_never_resends(
    signed_up, session, factory, redis, published
):
    member, _ = await signed_up()
    await _prefs(session, member, telegram=True, telegram_chat_id="42")
    alert = await _alert(session, member)
    respx.post(TG_URL).mock(return_value=httpx.Response(500))

    async def crashing_sleep(_s):
        raise RuntimeError("worker died")

    with pytest.raises(RuntimeError):
        await _run(alert, factory, redis, crashing_sleep)
    d = await _deliveries(session, alert.id)
    assert d["websocket"].status == "sent"
    assert d["telegram"].status == "pending"
    await session.refresh(alert)
    assert alert.status == "delivering"
    await _run(alert, factory, redis)  # redelivery: claim fails, nothing re-sent
    assert len(published) == 1


async def test_alerts_list_only_own(client, signed_up, session):
    owner, h_owner = await signed_up(email="o@example.com", household_name="O")
    other, h_other = await signed_up(email="p@example.com", household_name="P")
    mine = await _alert(session, owner)
    theirs = await _alert(session, other)
    session.add(Delivery(alert_id=mine.id, channel="ntfy", status="sent", attempts=1))
    await session.flush()
    r = await client.get("/alerts", headers=h_owner)
    assert r.status_code == 200
    body = r.json()
    assert [a["id"] for a in body] == [mine.id]
    assert body[0]["deliveries"][0]["channel"] == "ntfy"
    assert body[0]["label"] == "Scream" and body[0]["device_name"] == "Hall"
    assert [a["id"] for a in (await client.get("/alerts", headers=h_other)).json()] == [theirs.id]


async def test_alerts_list_excludes_other_members_in_same_household(client, signed_up, session):
    owner, h = await signed_up()
    from app.auth.models import Member

    m2 = Member(
        household_id=owner.household_id, email="m2@example.com", password_hash="x",
        role="member", email_confirmed=True,
    )  # fmt: skip
    session.add(m2)
    await session.flush()
    mine = await _alert(session, owner)
    other = await _alert(session, m2)
    ids = {a["id"] for a in (await client.get("/alerts", headers=h)).json()}
    assert ids == {mine.id} and other.id not in ids


@respx.mock
async def test_test_alert_uses_all_enabled_channels_and_exact_text(
    client, signed_up, session, factory, redis, published, jobs
):
    member, h = await signed_up()
    await _prefs(session, member, telegram=True, telegram_chat_id="42", ntfy_topic="t")
    r = await client.post("/me/test-alert", headers=h)
    assert r.status_code == 202
    alert_id = r.json()["alert_id"]
    assert jobs.enqueued == [alert_id]  # exactly one enqueue, of the new alert
    alert = await session.get(Alert, alert_id)
    assert alert.event_id is None and alert.sound_key == "test" and alert.member_id == member.id
    tg = respx.post(TG_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    nt = respx.post("https://ntfy.test/t").mock(return_value=httpx.Response(200))
    await _run(alert, factory, redis)
    import json

    assert json.loads(tg.calls[0].request.content)["text"] == TEST_TEXT
    assert nt.calls[0].request.content.decode() == TEST_TEXT
    assert set(await _deliveries(session, alert_id)) == {"websocket", "telegram", "ntfy"}


async def test_test_alert_rate_limited_4th_429(client, signed_up, jobs):
    _, h = await signed_up()
    codes = [(await client.post("/me/test-alert", headers=h)).status_code for _ in range(4)]
    assert codes == [202, 202, 202, 429]
    _, h2 = await signed_up(email="z@example.com", household_name="Z")
    assert (await client.post("/me/test-alert", headers=h2)).status_code == 202


@respx.mock
async def test_websocket_without_live_listener_is_failed_not_sent(
    signed_up, session, factory, redis, published
):
    member, _ = await signed_up()
    await _prefs(session, member, ntfy_topic="t")
    alert = await _alert(session, member)
    respx.post("https://ntfy.test/t").mock(return_value=httpx.Response(200))
    published.listeners = 0
    await _run(alert, factory, redis)
    d = await _deliveries(session, alert.id)
    assert d["websocket"].status == "failed" and d["websocket"].last_error == "no live listener"
    assert d["ntfy"].status == "sent"
    await session.refresh(alert)
    assert alert.status == "partial"


async def test_websocket_only_and_nobody_listening_fails_alert(
    signed_up, session, factory, redis
):
    member, _ = await signed_up()  # defaults: popup only; real (fake)redis, no subscriber
    alert = await _alert(session, member)
    await _run(alert, factory, redis)
    d = await _deliveries(session, alert.id)
    assert d["websocket"].status == "failed" and d["websocket"].last_error == "no live listener"
    await session.refresh(alert)
    assert alert.status == "failed"


@respx.mock
async def test_alert_older_than_10_minutes_fails_without_sending(
    signed_up, session, factory, redis, published
):
    member, _ = await signed_up()
    await _prefs(session, member, telegram=True, telegram_chat_id="42", ntfy_topic="t")
    alert = await _alert(session, member)
    tg = respx.post(TG_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    nt = respx.post("https://ntfy.test/t").mock(return_value=httpx.Response(200))
    await _run(alert, factory, redis, age=timedelta(minutes=10, seconds=1))
    assert not tg.called and not nt.called and published == []
    assert await _deliveries(session, alert.id) == {}
    await session.refresh(alert)
    assert alert.status == "failed"


@respx.mock
async def test_alert_just_under_10_minutes_still_delivers(
    signed_up, session, factory, redis, published
):
    member, _ = await signed_up()
    alert = await _alert(session, member)
    await _run(alert, factory, redis, age=timedelta(minutes=9, seconds=59))
    await session.refresh(alert)
    assert alert.status == "sent" and len(published) == 1
