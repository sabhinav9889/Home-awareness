from typing import Annotated, Any

from fastapi import Depends, HTTPException
from sqlalchemy import Select, Update, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_member
from app.auth.models import Member
from app.db import get_session


class Tenant:
    """The only sanctioned way to read or write household-owned rows."""

    def __init__(
        self, member: Member | None, session: AsyncSession, household_id: int | None = None
    ) -> None:
        if member is None and household_id is None:
            raise ValueError("a Tenant needs a member or a household_id")
        self._member = member
        self._session = session
        self._household_id = member.household_id if member is not None else household_id

    @classmethod
    def for_household(cls, household_id: int, session: AsyncSession) -> "Tenant":
        """A member-less tenant, for device-authenticated code."""
        return cls(None, session, household_id)

    @property
    def household_id(self) -> int:
        assert self._household_id is not None
        return self._household_id

    @property
    def member(self) -> Member:
        if self._member is None:
            raise RuntimeError("device tenants have no member")
        return self._member

    def select(self, model: Any) -> Select:
        return select(model).where(model.household_id == self.household_id)

    def update(self, model: Any) -> Update:
        return update(model).where(model.household_id == self.household_id)

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
