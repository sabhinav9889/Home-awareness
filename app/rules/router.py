import secrets
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog import SOUNDS
from app.db import get_session
from app.disclaimer import DISCLAIMER
from app.rules.effective import effective_settings
from app.rules.models import AlertPreference, SoundSetting
from app.rules.schemas import (
    AlertPreferencesIn,
    AlertPreferencesOut,
    NtfyTopicOut,
    SoundOut,
    SoundSettingIn,
    SoundSettingOut,
)
from app.tenancy import Tenant, get_tenant

router = APIRouter(tags=["rules"])


@router.get("/sounds", response_model=list[SoundOut], description=f"Sound catalog. {DISCLAIMER}")
async def list_sounds() -> list[SoundOut]:
    return [SoundOut(**asdict(s)) for s in SOUNDS.values()]


@router.get(
    "/me/sound-settings",
    response_model=list[SoundSettingOut],
    description=f"Your effective settings for every sound. {DISCLAIMER}",
)
async def get_sound_settings(
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[SoundSettingOut]:
    eff = await effective_settings(session, tenant.member.id)
    return [SoundSettingOut(**asdict(e)) for e in eff.values()]


@router.put(
    "/me/sound-settings/{key}",
    response_model=SoundSettingOut,
    description=f"Override a sound's settings for you. {DISCLAIMER}",
)
async def put_sound_setting(
    key: str,
    body: SoundSettingIn,
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SoundSettingOut:
    if key not in SOUNDS:
        raise HTTPException(status_code=404, detail="unknown sound")
    row = await session.scalar(
        tenant.select(SoundSetting).where(
            SoundSetting.member_id == tenant.member.id, SoundSetting.sound_key == key
        )
    )
    # exclude_unset: an omitted min_confidence keeps the stored override; explicit null clears.
    fields = body.model_dump(exclude_unset=True)
    if row is None:
        row = tenant.add(SoundSetting(member_id=tenant.member.id, sound_key=key, **fields))
    else:
        for field, value in fields.items():
            setattr(row, field, value)
    await session.commit()
    return SoundSettingOut(
        sound_key=key,
        enabled=row.enabled,
        repeat_count=row.repeat_count,
        window_s=row.window_s,
        cooldown_s=row.cooldown_s,
        min_confidence=(
            SOUNDS[key].default_min_confidence if row.min_confidence is None else row.min_confidence
        ),
    )


async def _prefs(tenant: Tenant, session: AsyncSession) -> AlertPreference:
    pref = await session.scalar(
        tenant.select(AlertPreference).where(AlertPreference.member_id == tenant.member.id)
    )
    if pref is None:  # defensive: members created before preferences existed
        pref = tenant.add(AlertPreference(member_id=tenant.member.id))
        await session.flush()
    return pref


def _out(p: AlertPreference) -> AlertPreferencesOut:
    return AlertPreferencesOut(
        popup=p.popup,
        alarm=p.alarm,
        telegram=p.telegram,
        telegram_linked=p.telegram_chat_id is not None,
        ntfy_topic=p.ntfy_topic,
    )


@router.get(
    "/me/alert-preferences",
    response_model=AlertPreferencesOut,
    description=f"Your alert channels. {DISCLAIMER}",
)
async def get_alert_preferences(
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AlertPreferencesOut:
    pref = await _prefs(tenant, session)
    await session.commit()
    return _out(pref)


@router.put(
    "/me/alert-preferences",
    response_model=AlertPreferencesOut,
    description=f"Set your alert channels. {DISCLAIMER}",
)
async def put_alert_preferences(
    body: AlertPreferencesIn,
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AlertPreferencesOut:
    pref = await _prefs(tenant, session)
    if body.telegram and pref.telegram_chat_id is None:
        raise HTTPException(status_code=400, detail="link Telegram first")
    pref.popup, pref.alarm, pref.telegram = body.popup, body.alarm, body.telegram
    pref.ntfy_topic = body.ntfy_topic
    await session.commit()
    return _out(pref)


@router.delete(
    "/me/telegram",
    status_code=204,
    description=f"Unlink your Telegram chat and turn Telegram alerts off. {DISCLAIMER}",
)
async def unlink_telegram(
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    pref = await _prefs(tenant, session)
    pref.telegram_chat_id, pref.telegram = None, False
    await session.commit()


@router.post(
    "/me/alert-preferences/ntfy-topic",
    response_model=NtfyTopicOut,
    description=f"Generate and store a random secret ntfy topic. {DISCLAIMER}",
)
async def generate_ntfy_topic(
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> NtfyTopicOut:
    pref = await _prefs(tenant, session)
    # token_urlsafe(24) is 32 chars of [A-Za-z0-9_-].
    pref.ntfy_topic = secrets.token_urlsafe(24)
    await session.commit()
    return NtfyTopicOut(ntfy_topic=pref.ntfy_topic)
