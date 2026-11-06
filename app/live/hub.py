"""Live fan-out over Redis pub/sub. Publishes are best-effort: they never raise."""

import json
import logging

from redis.asyncio import Redis

log = logging.getLogger(__name__)


def member_channel(member_id: int) -> str:
    return f"live:member:{member_id}"


def household_channel(household_id: int) -> str:
    return f"live:household:{household_id}"


async def _publish(redis: Redis, channel: str, msg: dict) -> None:
    try:
        await redis.publish(channel, json.dumps(msg, default=str))
    except Exception:  # noqa: BLE001  a Redis hiccup must not fail an already-committed request
        log.warning("live publish to %s failed", channel, exc_info=True)


async def publish_to_member(redis: Redis, member_id: int, msg: dict) -> None:
    await _publish(redis, member_channel(member_id), msg)


async def publish_to_household(redis: Redis, household_id: int, msg: dict) -> None:
    await _publish(redis, household_channel(household_id), msg)


async def publish_disconnect(redis: Redis, member_ids: list[int]) -> None:
    for member_id in member_ids:
        await _publish(redis, member_channel(member_id), {"kind": "disconnect"})
