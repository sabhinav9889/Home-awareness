"""Rule engine: merge window, per-member confidence thresholds, repeat counts, cooldowns.

A report merged into another device's incident still alerts members whose threshold it
newly crosses (the earlier reports were too weak for them).

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


# One round trip, atomic. KEYS[1] holds the device that opened the incident, KEYS[2] the
# highest confidence reported during it (same lifetime). Returns {merged, previous max}.
_MERGE_LUA = """
local ttl = tonumber(ARGV[3])
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ttl) then
  redis.call('SET', KEYS[2], ARGV[2], 'EX', ttl)
  return {0, ''}
end
local prev = redis.call('GET', KEYS[2])
local top = ARGV[2]
if prev and tonumber(prev) > tonumber(top) then top = prev end
if redis.call('GET', KEYS[1]) == ARGV[1] then
  -- the same device repeating counts as new reports and refreshes the window
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ttl)
  redis.call('SET', KEYS[2], top, 'EX', ttl)
  return {0, prev or ''}
end
local left = redis.call('PTTL', KEYS[1])
if left > 0 then redis.call('SET', KEYS[2], top, 'PX', left) end
return {1, prev or ''}
"""


async def _merge_state(redis: Redis, event: Event) -> tuple[bool, float]:
    """(merged, highest confidence reported earlier in this incident).

    Merged means another device already reported this sound within the merge window.
    """
    base = f"{event.household_id}:{event.sound_key}"
    script = redis.register_script(_MERGE_LUA)
    merged, prev = await script(
        keys=[f"merge:{base}", f"mergemax:{base}"],
        args=[str(event.device_id), repr(float(event.confidence)), MERGE_WINDOW_S],
    )
    return bool(int(merged)), float(prev) if prev else float("-inf")


async def process_event(
    session: AsyncSession, redis: Redis, event: Event, now: float, merge: bool = True
) -> RuleOutcome:
    """`merge=False` skips the cross-device merge window (server-raised incidents, which are
    already one event per room); thresholds, repeats and cooldowns still apply."""
    hid, sound = event.household_id, event.sound_key
    merged, prev_max = await _merge_state(redis, event) if merge else (False, float("-inf"))
    if merged:
        event.merged = True

    alerts: list[Alert] = []
    for member_id, s in await members_enabled_for(session, hid, sound):
        if event.confidence < s.min_confidence:
            continue  # below this member's threshold: neither counted nor alerted
        if merged and prev_max >= s.min_confidence:
            # A duplicate from another device: this member was already served by an
            # earlier, confident-enough report of the same incident. Only members whose
            # threshold this higher report newly crosses go on (cooldown still applies).
            continue
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
    return RuleOutcome(merged, [a.id for a in alerts])
