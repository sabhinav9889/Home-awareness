from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.households.models import Household


def _zone(name: str | None) -> ZoneInfo:
    try:
        if not name or name.startswith(("+", "-")):
            raise ValueError
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError, OSError):
        return ZoneInfo("UTC")  # a bad stored zone must never break a heartbeat


def is_guarding(household: Household, now_utc: datetime) -> bool:
    """True when the household is Away, or its local time is inside the night window
    [night_start, night_end), which may wrap past midnight."""
    if household.away:
        return True
    start, end = household.night_start, household.night_end
    if start is None or end is None or start == end:
        return False
    local = now_utc.astimezone(_zone(household.timezone)).time().replace(tzinfo=None)
    if start < end:
        return start <= local < end
    return local >= start or local < end
