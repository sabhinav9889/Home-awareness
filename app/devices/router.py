import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog import INCIDENT_KEYS
from app.db import get_session
from app.devices.deps import current_device
from app.devices.models import (
    PAIRING_CODE_ALPHABET,
    PAIRING_CODE_LENGTH,
    PAIRING_CODE_TTL_MINUTES,
    Device,
    PairingCode,
)
from app.devices.offline import household_recipients, notify_back_online
from app.devices.schemas import (
    DeviceConfigOut,
    DeviceOut,
    DevicePatch,
    HeartbeatIn,
    PairIn,
    PairingCodeIn,
    PairingCodeOut,
    PairOut,
    WhoamiOut,
)
from app.disclaimer import DISCLAIMER
from app.households.guarding import is_guarding
from app.households.models import Household
from app.live.hub import publish_to_household
from app.ratelimit import by_device_key, by_ip, rate_limit
from app.redis import get_redis
from app.rooms.models import PRIVATE_ROOM_KINDS, Room
from app.rules.effective import household_enabled_sounds, household_thresholds
from app.security import hash_token, new_token
from app.tenancy import Tenant, get_tenant

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
    if body.room_id is not None:
        await tenant.get(Room, body.room_id)  # 404 for a room that is not this household's
    code = "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))
    expires_at = datetime.now(UTC) + timedelta(minutes=PAIRING_CODE_TTL_MINUTES)
    tenant.add(
        PairingCode(
            name=body.name,
            kind=body.kind,
            code_hash=hash_token(code),
            expires_at=expires_at,
            room_id=body.room_id,
            capabilities=body.capabilities,
            consent_confirmed=body.consent_confirmed,
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
            .returning(
                PairingCode.household_id,
                PairingCode.name,
                PairingCode.kind,
                PairingCode.room_id,
                PairingCode.capabilities,
                PairingCode.consent_confirmed,
            )
        )
    ).first()
    if claimed is None:
        raise HTTPException(status_code=400, detail="invalid or expired pairing code")
    key = new_token("ha_dev_")
    tenant = Tenant.for_household(claimed.household_id, session)
    room_kind = None
    room_id = None
    if claimed.room_id is not None:
        # The room may have been deleted since the code was made.
        room = await session.scalar(tenant.select(Room).where(Room.id == claimed.room_id))
        if room is not None:
            room_id, room_kind = room.id, room.kind
    device = tenant.add(
        Device(
            name=claimed.name,
            kind=claimed.kind,
            key_hash=hash_token(key),
            room_id=room_id,
            capabilities=claimed.capabilities,
            # Cameras in a bedroom or bathroom start with snapshots off.
            send_snapshots=room_kind not in PRIVATE_ROOM_KINDS,
            consent_confirmed_at=now if claimed.consent_confirmed else None,
        )
    )
    await session.flush()
    await session.commit()
    return PairOut(device_id=device.id, device_key=key)


def _device_out(device: Device, room_name: str | None) -> DeviceOut:
    out = DeviceOut.model_validate(device, from_attributes=True)
    out.room_name = room_name
    return out


@router.get(
    "/devices",
    response_model=list[DeviceOut],
    description=f"List this household's devices. {DISCLAIMER}",
)
async def list_devices(
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[DeviceOut]:
    rows = await session.execute(
        tenant.select(Device)
        .add_columns(Room.name)
        .outerjoin(Room, Room.id == Device.room_id)
        .order_by(Device.id)
    )
    return [_device_out(device, room_name) for device, room_name in rows.all()]


@router.patch(
    "/devices/{device_id}",
    response_model=DeviceOut,
    description=(
        "Rename a device, assign its room, or confirm camera consent (any member); "
        f"only the owner changes send_snapshots. {DISCLAIMER}"
    ),
)
async def patch_device(
    device_id: int,
    body: DevicePatch,
    tenant: Annotated[Tenant, Depends(get_tenant)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DeviceOut:
    device = await tenant.get(Device, device_id)
    if body.send_snapshots is not None and tenant.member.role != "owner":
        raise HTTPException(status_code=403, detail="owner only")
    if body.name is not None:
        device.name = body.name
    if "room_id" in body.model_fields_set:
        if body.room_id is not None:
            room = await tenant.get(Room, body.room_id)
            # Ruling 5a-R10: a camera moved into a bedroom or bathroom stops sending
            # snapshots; the owner can turn it back on.
            if room.kind in PRIVATE_ROOM_KINDS and "video" in device.capabilities:
                device.send_snapshots = False
        device.room_id = body.room_id
    if body.send_snapshots is not None:
        device.send_snapshots = body.send_snapshots
    if body.consent_confirmed and device.consent_confirmed_at is None:
        device.consent_confirmed_at = datetime.now(UTC)
        device.consent_confirmed_by = tenant.member.id
    await session.commit()
    room_name = (
        await session.scalar(select(Room.name).where(Room.id == device.room_id))
        if device.room_id is not None
        else None
    )
    return _device_out(device, room_name)


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
    sounds = await household_enabled_sounds(session, device.household_id) - INCIDENT_KEYS
    household = await session.get(Household, device.household_id)
    thresholds = await household_thresholds(session, device.household_id, sounds)
    return DeviceConfigOut(
        enabled_sounds=sorted(sounds),
        heartbeat_interval_s=HEARTBEAT_INTERVAL_S,
        thresholds=dict(sorted(thresholds.items())),
        guarding=is_guarding(household, datetime.now(UTC)),
        send_snapshots=device.send_snapshots,
    )


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
    background: BackgroundTasks,
    body: HeartbeatIn | None = None,
) -> DeviceConfigOut:
    was_offline = device.status == "offline"
    device.last_seen_at = datetime.now(UTC)
    device.status = "online"
    device.mic_clipping = body.mic_clipping if body is not None else False
    config = await _config(session, device)
    recipients = await household_recipients(session, device.household_id) if was_offline else []
    await session.commit()
    if was_offline:
        await publish_to_household(
            redis,
            device.household_id,
            {"kind": "device_online", "device_id": device.id, "name": device.name},
        )
        # Same channels as the offline warning, best effort, after the response is sent.
        background.add_task(notify_back_online, recipients, device.name)
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
