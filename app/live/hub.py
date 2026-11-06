"""Live fan-out stubs; implemented in Task 10."""

from redis.asyncio import Redis


async def publish_to_member(redis: Redis, member_id: int, msg: dict) -> None:
    return None


async def publish_to_household(redis: Redis, household_id: int, msg: dict) -> None:
    return None


async def publish_disconnect(redis: Redis, member_ids: list[int]) -> None:
    return None
