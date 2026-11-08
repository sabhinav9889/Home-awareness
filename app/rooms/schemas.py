from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RoomKind = Literal["common", "entrance", "bedroom", "bathroom", "outdoor", "other"]


class RoomIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=60)
    kind: RoomKind


class RoomPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=60)
    kind: RoomKind | None = None


class RoomOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    kind: str
    created_at: datetime
