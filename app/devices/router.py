import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.devices.deps import current_device
from app.devices.models import (
    PAIRING_CODE_ALPHABET,
    PAIRING_CODE_LENGTH,
    PAIRING_CODE_TTL_MINUTES,
    Device,
    PairingCode,
)
from app.devices.schemas import (
    DeviceConfigOut,
    DeviceOut,
    PairIn,
    PairingCodeIn,
    PairingCodeOut,
    PairOut,
    WhoamiOut,
)
from app.live.hub import publish_to_household
from app.ratelimit import by_device_key, by_ip, rate_limit
from app.redis import get_redis
from app.rules.effective import household_enabled_sounds
from app.security import hash_token, new_token
from app.tenancy import Tenant, get_tenant

DISCLAIMER = "Home-awareness tool. Not a medical device, security alarm, or emergency service."

HEARTBEAT_INTERVAL_S = 60

router = APIRouter(tags=["devices"])


@router.post(
    "/devices/pairing-codes",
    status_code=201,
    response_model=PairingCodeOut,
    description=f"Create a single-use pairing code valid for 10 minutes. {DISCLAIMER}",
)
async def create_pairing_code(
    body: PairingCodeIn,
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PairingCodeOut:
    code = "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))
    expires_at = datetime.now(UTC) + timedelta(minutes=PAIRING_CODE_TTL_MINUTES)
    tenant.add(
        PairingCode(
            name=body.name, kind=body.kind, code_hash=hash_token(code), expires_at=expires_at
        )
    )
    await session.commit()
    return PairingCodeOut(code=code, expires_at=expires_at)


@router.post(
    "/device/pair",
    status_code=201,
    response_model=PairOut,
    dependencies=[Depends(rate_limit("pair", 10, 60, by_ip))],
    description=f"Exchange a pairing code for a device key (shown once). {DISCLAIMER}",
)
async def pair_device(
    body: PairIn, session: Annotated[AsyncSession, Depends(get_session)]
) -> PairOut:
    now = datetime.now(UTC)
    # Atomic claim: the row is marked used in the same statement that finds it, so a code
    # can be redeemed exactly once even under concurrent requests. The caller is
    # unauthenticated; the code itself carries the household.
    claimed = (
        await session.execute(
            update(PairingCode)
            .where(
                PairingCode.code_hash == hash_token(body.code.strip().upper()),
                PairingCode.used_at.is_(None),
                PairingCode.expires_at > now,
            )
            .values(used_at=now)
            .returning(PairingCode.household_id, PairingCode.name, PairingCode.kind)
        )
    ).first()
    if claimed is None:
        raise HTTPException(status_code=400, detail="invalid or expired pairing code")
    key = new_token("ha_dev_")
    device = Tenant.for_household(claimed.household_id, session).add(
        Device(name=claimed.name, kind=claimed.kind, key_hash=hash_token(key))
    )
    await session.flush()
    await session.commit()
    return PairOut(device_id=device.id, device_key=key)


@router.get(
    "/devices",
    response_model=list[DeviceOut],
    description=f"List this household's devices. {DISCLAIMER}",
)
async def list_devices(
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[Device]:
    return list((await session.scalars(tenant.select(Device).order_by(Device.id))).all())


@router.delete(
    "/devices/{device_id}",
    status_code=204,
    description=f"Revoke a device; its key stops working immediately. {DISCLAIMER}",
)
async def delete_device(
    device_id: int,
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    await tenant.delete(Device, device_id)
    await session.commit()
    return Response(status_code=204)


@router.get(
    "/device/whoami",
    response_model=WhoamiOut,
    description=f"Identify the device behind an X-Device-Key. {DISCLAIMER}",
)
async def whoami(device: Annotated[Device, Depends(current_device)]) -> WhoamiOut:
    return WhoamiOut(device_id=device.id, household_id=device.household_id)


async def _config(session: AsyncSession, device: Device) -> DeviceConfigOut:
    sounds = await household_enabled_sounds(session, device.household_id)
    return DeviceConfigOut(enabled_sounds=sorted(sounds), heartbeat_interval_s=HEARTBEAT_INTERVAL_S)


@router.post(
    "/device/heartbeat",
    response_model=DeviceConfigOut,
    dependencies=[Depends(rate_limit("heartbeat", 4, 60, by_device_key))],
    description=f"Record a heartbeat and return the device's sound config. {DISCLAIMER}",
)
async def heartbeat(
    device: Annotated[Device, Depends(current_device)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> DeviceConfigOut:
    was_offline = device.status == "offline"
    device.last_seen_at = datetime.now(UTC)
    device.status = "online"
    config = await _config(session, device)
    await session.commit()
    if was_offline:
        await publish_to_household(
            redis,
            device.household_id,
            {"kind": "device_online", "device_id": device.id, "name": device.name},
        )
    return config


@router.get(
    "/device/config",
    response_model=DeviceConfigOut,
    description=f"Current sound config for the device, without a heartbeat. {DISCLAIMER}",
)
async def device_config(
    device: Annotated[Device, Depends(current_device)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DeviceConfigOut:
    return await _config(session, device)
