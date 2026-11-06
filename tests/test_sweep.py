import asyncio
from datetime import UTC, datetime, timedelta

from prometheus_client import REGISTRY
from sqlalchemy import select

import app.alerts.celery_app as celery_app
import app.alerts.sweep as sweep
import app.main as app_main
from app.alerts.models import Alert, Delivery
from app.alerts.sweep import sweep_stale_alerts

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


async def _alert(session, member, *, age_min, status="pending", dstatus="pending"):
    a = Alert(
        household_id=member.household_id, member_id=member.id, event_id=None, sound_key="test",
        status=status, created_at=NOW - timedelta(minutes=age_min),
    )  # fmt: skip
    session.add(a)
    await session.flush()
    d = Delivery(alert_id=a.id, channel="ntfy", status=dstatus)
    sent = Delivery(alert_id=a.id, channel="telegram", status="sent")
    session.add_all([d, sent])
    await session.flush()
    return a, d, sent


async def _reload(session, *objs):
    for o in objs:
        await session.refresh(o)


async def test_sweep_fails_stale_pending_and_delivering(signed_up, session):
    member, _ = await signed_up()
    p, pd, ps = await _alert(session, member, age_min=16)
    g, gd, _ = await _alert(session, member, age_min=16, status="delivering")
    assert await sweep_stale_alerts(session, NOW) == 2
    await _reload(session, p, pd, ps, g, gd)
    assert p.status == g.status == "failed"
    assert pd.status == gd.status == "failed" and pd.last_error == "stale"
    assert ps.status == "sent" and ps.last_error is None  # only pending deliveries change


async def test_sweep_leaves_young_and_finished_alerts(signed_up, session):
    member, _ = await signed_up()
    young, yd, _ = await _alert(session, member, age_min=14)
    sent, sd, _ = await _alert(session, member, age_min=60, status="sent", dstatus="sent")
    part, _, _ = await _alert(session, member, age_min=60, status="partial")
    assert await sweep_stale_alerts(session, NOW) == 0
    await _reload(session, young, yd, sent, sd, part)
    assert (young.status, yd.status) == ("pending", "pending")
    assert (sent.status, sd.status, part.status) == ("sent", "sent", "partial")
    rows = await session.scalars(select(Delivery).where(Delivery.last_error == "stale"))
    assert rows.all() == []


async def test_sweep_counts_metric_and_is_idempotent(signed_up, session):
    member, _ = await signed_up()
    await _alert(session, member, age_min=20)
    before = REGISTRY.get_sample_value("ha_alerts_stale_total") or 0
    assert await sweep_stale_alerts(session, NOW) == 1
    assert REGISTRY.get_sample_value("ha_alerts_stale_total") == before + 1
    assert await sweep_stale_alerts(session, NOW) == 0


def test_beat_schedules_sweep_every_five_minutes():
    entry = celery_app.celery.conf.beat_schedule["sweep-stale-alerts"]
    assert entry["task"] == "sweep_stale_alerts" and entry["schedule"] == 300.0
    assert celery_app.celery.conf.beat_schedule["check-offline-devices"]["schedule"] == 60.0


async def test_sweep_loop_survives_errors_and_cancels(monkeypatch):
    calls = []

    class _Ctx:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    async def flaky(session, now):
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return 0

    monkeypatch.setattr(sweep, "sweep_stale_alerts", flaky)
    task = asyncio.create_task(sweep.sweep_loop(_Ctx, interval_s=0.01))
    for _ in range(200):
        if len(calls) >= 3:
            break
        await asyncio.sleep(0.01)
    assert len(calls) >= 3
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("loop should end by cancellation")


async def test_lifespan_starts_sweep_loop_only_when_inprocess_and_opted_in(redis, monkeypatch):
    from types import SimpleNamespace

    started = []

    async def fake_sweep(factory, **kw):
        started.append(True)
        await asyncio.sleep(3600)

    async def fake_offline(factory, r, **kw):
        await asyncio.sleep(3600)

    async def fake_redis():
        return redis

    monkeypatch.setattr(app_main, "sweep_loop", fake_sweep)
    monkeypatch.setattr(app_main, "offline_loop", fake_offline)
    monkeypatch.setattr(app_main, "get_redis", fake_redis)
    monkeypatch.setattr(app_main, "_sessionmaker", lambda: None)
    for runner, opted, expect in (
        ("inprocess", True, True),
        ("celery", True, False),
        ("inprocess", False, False),
    ):
        started.clear()
        monkeypatch.setattr(
            app_main, "get_settings", lambda r=runner: SimpleNamespace(job_runner=r)
        )
        app = app_main.create_app(run_background_jobs=opted)
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.01)
            assert bool(started) is expect
