"""Rule engine stub; Task 8 replaces the body."""

from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts.jobs import JobRunner
from app.events.models import Event


@dataclass
class RuleOutcome:
    merged: bool
    alert_ids: list[int]


async def process_event(
    session: AsyncSession, redis: Redis, jobs: JobRunner, event: Event, now: float
) -> RuleOutcome:
    return RuleOutcome(False, [])
