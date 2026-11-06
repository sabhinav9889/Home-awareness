from typing import Annotated, Any

from fastapi import Depends, HTTPException
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_member
from app.auth.models import Member
from app.db import get_session


class Tenant:
    """The only sanctioned way to read or write household-owned rows."""

    def __init__(self, member: Member, session: AsyncSession) -> None:
        self._member = member
        self._session = session

    @property
    def household_id(self) -> int:
        return self._member.household_id

    @property
    def member(self) -> Member:
        return self._member

    def select(self, model: Any) -> Select:
        return select(model).where(model.household_id == self.household_id)

    async def get(self, model: Any, id: int) -> Any:
        # Missing and other-household rows are indistinguishable: both 404.
        obj = await self._session.scalar(self.select(model).where(model.id == id))
        if obj is None:
            raise HTTPException(status_code=404, detail="not found")
        return obj

    def add(self, obj: Any) -> Any:
        obj.household_id = self.household_id
        self._session.add(obj)
        return obj

    async def delete(self, model: Any, id: int) -> None:
        obj = await self.get(model, id)
        await self._session.delete(obj)
        await self._session.flush()


async def get_tenant(
    member: Annotated[Member, Depends(current_member)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Tenant:
    return Tenant(member, session)
