"""Pydantic request models for the HTTP layer.

Responses are returned as plain dicts produced by the domain ``to_dict()`` /
``BuildResult.to_dict()`` methods, so there's no second copy of every field to
keep in sync here — only inbound payloads are modelled.

This module imports pydantic; it is only imported from the FastAPI app, which
guards the import so ``backend`` stays usable without web deps installed.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, description="Project / build name")
    client: str = ""
    status: str = "draft"


class CreateRoomRequest(BaseModel):
    name: str = Field(..., min_length=1, description="Room name")
    building: str = Field("", description="Optional building grouping")


class DeviceInput(BaseModel):
    name: str = Field(..., min_length=1)
    type: str = ""
    model: str = ""
    quantity: int = 1
    serial: str = ""
    notes: str = ""
    confidence: str = "confirmed"
    attrs: dict[str, Any] = Field(default_factory=dict)


class SetDevicesRequest(BaseModel):
    devices: list[DeviceInput] = Field(default_factory=list)


class ParseBomRequest(BaseModel):
    """BOM-paste / auto-map (proposal screen) — parse without persisting."""

    csv: str = Field(..., min_length=1, description="Raw BOM CSV text")
    name: str = "Room"


class RunBuildRequest(BaseModel):
    """One-shot core loop from raw CSV (no persistence)."""

    csv: str = Field(..., min_length=1, description="Raw BOM CSV text")
    name: str = "Room"
    strict: bool = False


class BuildRoomRequest(BaseModel):
    """Run the core loop on a stored room."""

    strict: bool = False
