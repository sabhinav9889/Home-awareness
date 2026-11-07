"""Tenant: the only sanctioned way to read or write household-owned rows.

Sanctioned bypasses. Each is safe because the household is fixed by something the caller
proved (a secret token, a code, a device key, a JWT) or by a row already scoped elsewhere,
never by client-chosen ids:

- Pair claim (devices/router.pair_device): the single-use code hash finds the PairingCode;
  the household comes from that row, and the new Device is then added through a Tenant.
- Engine Alert insert (rules/engine.process_event): household_id is copied from the event,
  which was created through the device's Tenant; member ids come from that same household.
- Delivery and deliveries via alert (alerts/delivery, alerts/router.list_alerts): the worker
  claims an alert by id (ids come only from committed rows the server created) and then
  reads its prefs/event/device through Tenant.for_household(alert.household_id); Delivery
  rows have no household_id and are reached only through alerts already scoped.
- Offline sweep (devices/offline.check_offline_devices): a deliberate cross-household
  UPDATE of stale devices plus max(last_seen_at); each household is then notified through
  its own Tenant.
- Telegram relink clear (alerts/router.telegram_webhook): clears the chat id wherever it is
  linked, keyed on the chat that just proved a fresh one-time code; only unlinks, never reads.
- effective.py household reads (rules/effective): filter on an explicit household_id or
  member_id that the caller already scoped (member from a JWT, household from a device).
- Signup AlertPreference insert (auth/router.signup): the household was just created, or
  just claimed through a single-use invite, in the same transaction as the member.
- Invite claim (auth/router.signup): an atomic UPDATE on the invite token hash; the token
  itself carries the household.
- Auth lookups (auth/router, auth/deps, devices/deps, live/router): members by email, id
  from a verified JWT, agent tokens and devices by key hash, i.e. identifying who is
  calling at all. An agent token's member must still be in the token's household.
"""

from typing import Annotated, Any

from fastapi import Depends, HTTPException
from sqlalchemy import Select, Update, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_member, current_member_or_agent
from app.auth.models import Member
from app.db import get_session


class Tenant:
    """The only sanctioned way to read or write household-owned rows (see module doc)."""

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


async def get_agent_tenant(
    member: Annotated[Member, Depends(current_member_or_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Tenant:
    """Like get_tenant, but an `ha-agent` token is accepted too. Only for POST /alerts/{id}/ack
    and GET /snapshots/{event_id}."""
    return Tenant(member, session)
