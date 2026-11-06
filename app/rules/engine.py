"""Rule engine: merge window, per-member confidence thresholds, repeat counts, cooldowns.

Decides whether a detected sound becomes an alert per member. Creates pending
Alert rows (flushed, so they have ids); the caller enqueues them after commit.
"""

from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts.models import Alert
from app.events.models import Event
from app.rules.effective import members_enabled_for

MERGE_WINDOW_S = 5


@dataclass
class RuleOutcome:
    merged: bool
    alert_ids: list[int]


async def _is_merged(redis: Redis, event: Event) -> bool:
    """True if another device already reported this sound within the merge window."""
    key = f"merge:{event.household_id}:{event.sound_key}"
    device = str(event.device_id)
    if await redis.set(key, device, nx=True, ex=MERGE_WINDOW_S):
        return False
    holder = await redis.get(key)
    if holder is not None and holder != device:
        return True
    # Same device repeating (or key expired between calls): counts, refresh window.
    await redis.set(key, device, ex=MERGE_WINDOW_S)
    return False


async def process_event(
    session: AsyncSession, redis: Redis, event: Event, now: float
) -> RuleOutcome:
    hid, sound = event.household_id, event.sound_key
    if await _is_merged(redis, event):
        event.merged = True
        return RuleOutcome(True, [])

    alerts: list[Alert] = []
    for member_id, s in await members_enabled_for(session, hid, sound):
        if event.confidence < s.min_confidence:
            continue  # below this member's threshold: neither counted nor alerted
        rep = f"rep:{hid}:{member_id}:{sound}"
        pipe = redis.pipeline(transaction=False)
        pipe.zadd(rep, {str(event.id): now})
        pipe.zremrangebyscore(rep, "-inf", now - s.window_s)
        pipe.expire(rep, s.window_s)
        pipe.zcard(rep)
        count = (await pipe.execute())[-1]
        if count < s.repeat_count:
            continue
        if not await redis.set(
            f"cd:{hid}:{member_id}:{sound}", 1, nx=True, ex=max(s.cooldown_s, 1)
        ):
            continue  # in cooldown
        await redis.delete(rep)
        alert = Alert(household_id=hid, member_id=member_id, event_id=event.id, sound_key=sound)
        session.add(alert)
        alerts.append(alert)
    await session.flush()
    return RuleOutcome(False, [a.id for a in alerts])
