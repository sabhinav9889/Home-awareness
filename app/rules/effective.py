from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Member
from app.catalog import SOUNDS, Sound
from app.rules.models import SoundSetting


@dataclass
class EffectiveSetting:
    sound_key: str
    enabled: bool
    repeat_count: int
    window_s: int
    cooldown_s: int


def _default(s: Sound) -> EffectiveSetting:
    return EffectiveSetting(
        s.key, s.default_enabled, s.default_repeat, s.default_window_s, s.default_cooldown_s
    )


def _merge(overrides: dict[str, SoundSetting]) -> dict[str, EffectiveSetting]:
    out = {}
    for key, sound in SOUNDS.items():
        o = overrides.get(key)
        out[key] = (
            EffectiveSetting(key, o.enabled, o.repeat_count, o.window_s, o.cooldown_s)
            if o
            else _default(sound)
        )
    return out


async def effective_settings(session: AsyncSession, member_id: int) -> dict[str, EffectiveSetting]:
    rows = (
        await session.scalars(select(SoundSetting).where(SoundSetting.member_id == member_id))
    ).all()
    return _merge({r.sound_key: r for r in rows})


async def _household_effective(
    session: AsyncSession, household_id: int, *, confirmed_only: bool = False
) -> dict[int, dict[str, EffectiveSetting]]:
    query = select(Member.id).where(Member.household_id == household_id).order_by(Member.id)
    if confirmed_only:
        query = query.where(Member.email_confirmed.is_(True))
    member_ids = (await session.scalars(query)).all()
    rows = (
        await session.scalars(select(SoundSetting).where(SoundSetting.household_id == household_id))
    ).all()
    by_member: dict[int, dict[str, SoundSetting]] = {m: {} for m in member_ids}
    for r in rows:
        if r.member_id in by_member:
            by_member[r.member_id][r.sound_key] = r
    return {m: _merge(o) for m, o in by_member.items()}


async def household_enabled_sounds(session: AsyncSession, household_id: int) -> set[str]:
    per_member = await _household_effective(session, household_id)
    return {k for eff in per_member.values() for k, s in eff.items() if s.enabled}


async def members_enabled_for(
    session: AsyncSession, household_id: int, sound_key: str
) -> list[tuple[int, EffectiveSetting]]:
    # Unconfirmed members can't sign in to see or acknowledge alerts: never alert them.
    per_member = await _household_effective(session, household_id, confirmed_only=True)
    return [(m, eff[sound_key]) for m, eff in per_member.items() if eff[sound_key].enabled]
