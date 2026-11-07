from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate_timezone(v: str) -> str:
    """Return v if it is an IANA zone key ZoneInfo knows; else raise ValueError."""
    try:
        if not v or v.startswith(("+", "-")):
            raise ValueError
        ZoneInfo(v)
    except (ValueError, ZoneInfoNotFoundError, OSError):
        raise ValueError("unknown timezone; use an IANA name like Asia/Kolkata") from None
    return v


def tz_label(at: datetime, tz_name: str | None) -> tuple[str, str]:
    """('HH:MM', abbreviation) of the instant `at` in the named zone. A missing or unknown
    zone (e.g. removed from tzdata after it was saved) falls back to UTC: a label must never
    fail an alert."""
    try:
        if not tz_name or tz_name.startswith(("+", "-")):
            raise ValueError
        zone = ZoneInfo(tz_name)
    except (ValueError, ZoneInfoNotFoundError, OSError):
        zone = ZoneInfo("UTC")
    local = at.astimezone(zone)
    abbr = local.strftime("%Z")
    if abbr[:1] in ("+", "-"):  # numeric-looking: render GMT+5:30 style
        minutes = int(local.utcoffset().total_seconds() // 60)
        sign = "+" if minutes >= 0 else "-"
        h, m = divmod(abs(minutes), 60)
        abbr = f"GMT{sign}{h}" + (f":{m:02d}" if m else "")
    return local.strftime("%H:%M"), abbr
