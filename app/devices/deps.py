from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.devices.models import Device
from app.security import hash_token
from app.tenancy import Tenant


async def current_device(
    session: Annotated[AsyncSession, Depends(get_session)],
    x_device_key: Annotated[str | None, Header(alias="X-Device-Key")] = None,
) -> Device:
    """Looks the device up on every request (no caching), so revocation is immediate."""
    if not x_device_key:
        raise HTTPException(status_code=401, detail="invalid credentials")
    device = await session.scalar(select(Device).where(Device.key_hash == hash_token(x_device_key)))
    if device is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    return device


async def get_device_tenant(
    device: Annotated[Device, Depends(current_device)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Tenant:
    return Tenant.for_household(device.household_id, session)
