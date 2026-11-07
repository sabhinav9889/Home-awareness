"""Combined incidents: two sensors in one room that together mean more than either alone.

Each ingredient event of a device with a room is remembered for 30 s in a per-room Redis
sorted set (score = epoch seconds). When an arriving event completes a rule, a synthetic
incident Event is created and run through the normal rule engine (per-member enablement,
thresholds and cooldowns apply). The caller commits, then enqueues the returned alert ids.

Each (room, rule) fires at most once per 60 s: SET NX EX 60 decides, so two events that
complete the same rule concurrently cannot both fire it.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import Device
from app.events.models import Event
from app.households.guarding import is_guarding
from app.households.models import Household
from app.rules.engine import process_event
from app.snapshots import store
from app.snapshots.models import Snapshot

KEEP_S = 30
FIRE_ONCE_S = 60
NEW_FILES = "incident_snapshot_files"
NX_KEYS = "incident_nx_keys"  # session.info: keys this call set, undone on failure
FIRED = "incident_fired"  # session.info flag: an incident row is pending a commit


@dataclass(frozen=True)
class Rule:
    key: str
    first: frozenset[str]
    second: frozenset[str]
    within_s: int
    needs_guarding: bool = False


# Order matters: break-in is checked first and suppresses property damage (ruling 5a-R4).
RULES: tuple[Rule, ...] = (
    Rule("incident_break_in", frozenset({"glass_breaking"}), frozenset({"person_away"}), 15),
    Rule(
        "incident_fall_confirmed",
        frozenset({"fall"}),
        frozenset({"thud_fall", "scream"}),
        10,
    ),
    Rule(
        "incident_property_damage",
        frozenset({"explosion", "door_slam", "glass_breaking"}),
        frozenset({"person_away"}),
        15,
        needs_guarding=True,
    ),
)
INGREDIENT_KEYS = frozenset(k for r in RULES for k in r.first | r.second)


async def _remember(redis: Redis, room_key: str, event: Event, has_snapshot: bool, now: float):
    member = json.dumps([event.sound_key, event.id, event.confidence, has_snapshot])
    pipe = redis.pipeline(transaction=False)
    pipe.zadd(room_key, {member: now})
    pipe.zremrangebyscore(room_key, "-inf", now - KEEP_S)
    pipe.expire(room_key, KEEP_S * 2)
    pipe.zrange(room_key, 0, -1, withscores=True)
    rows = (await pipe.execute())[-1]
    return [(json.loads(m), score) for m, score in rows]


def _partners(rows, rule: Rule, event: Event, now: float) -> list[list] | None:
    """Ingredients (as stored) that complete `rule` together with `event`, or None."""
    key = event.sound_key
    if key in rule.first:
        other = rule.second
    elif key in rule.second:
        other = rule.first
    else:
        return None
    found = [m for m, score in rows if m[0] in other and now - score <= rule.within_s]
    return found or None


async def check_incidents(
    session: AsyncSession, redis: Redis, event: Event, now: float
) -> list[int]:
    """Record `event` as an ingredient and raise any incident it completes. Returns the
    alert ids to enqueue after the caller commits (empty when nothing fired)."""
    if event.sound_key not in INGREDIENT_KEYS or event.device_id is None:
        return []
    device = await session.get(Device, event.device_id)
    if device is None or device.room_id is None:
        return []  # devices without a room never participate
    hid = event.household_id
    room_key = f"incwin:{hid}:{device.room_id}"
    has_snapshot = await _has_snapshot(session, event.id)
    rows = await _remember(redis, room_key, event, has_snapshot, now)

    household = await session.get(Household, hid)
    guarding = household is not None and is_guarding(household, datetime.fromtimestamp(now, UTC))
    alert_ids: list[int] = []
    break_in_completed = False
    for rule in RULES:
        if rule.key == "incident_property_damage" and break_in_completed:
            continue  # one set of ingredients, one incident
        partners = _partners(rows, rule, event, now)
        if partners is None:
            continue
        if rule.key == "incident_break_in":
            break_in_completed = True
        if rule.needs_guarding and not guarding:
            continue
        nx_key = f"incfired:{hid}:{device.room_id}:{rule.key}"
        if not await redis.set(nx_key, 1, nx=True, ex=FIRE_ONCE_S):
            continue
        session.info.setdefault(NX_KEYS, []).append(nx_key)
        ingredients = [[event.sound_key, event.id, event.confidence, has_snapshot], *partners]
        alert_ids += await _fire(session, redis, rule, event, ingredients, now)
    return alert_ids


async def _has_snapshot(session: AsyncSession, event_id: int) -> bool:
    return (
        await session.scalar(select(Snapshot.id).where(Snapshot.event_id == event_id))
    ) is not None


async def _fire(
    session: AsyncSession, redis: Redis, rule: Rule, trigger: Event, ingredients: list, now: float
) -> list[int]:
    at = datetime.fromtimestamp(now, UTC)
    incident = Event(
        household_id=trigger.household_id,
        device_id=trigger.device_id,
        sound_key=rule.key,
        confidence=max(i[2] for i in ingredients),
        occurred_at=at,
        received_at=at,
        merged=False,
    )
    session.add(incident)
    session.info[FIRED] = True
    await session.flush()
    with_image = [i for i in ingredients if i[3]]
    if with_image:
        best = max(with_image, key=lambda i: i[2])
        source = await session.scalar(select(Snapshot).where(Snapshot.event_id == best[1]))
        jpeg = await store.try_load(source)
        if jpeg is not None:
            # A copy under its own row and file: retention, cap eviction and the orphan
            # sweep treat it like any other snapshot.
            copy = await store.save(session, trigger.household_id, incident.id, jpeg)
            if copy is not None:  # so a failed commit can remove the new file
                session.info.setdefault(NEW_FILES, []).append(copy.path)
    outcome = await process_event(session, redis, incident, now, merge=False)
    return outcome.alert_ids
