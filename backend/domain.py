"""Domain model for the AV Schematic Builder.

Stdlib-only dataclasses (no pydantic dependency) so the model and the core
loop run anywhere. The hierarchy mirrors the wireframes:

    Project ──▶ Room ──▶ Device ──▶ Port
                  └────▶ Connection (cable run)

Each dataclass round-trips through plain dicts (``to_dict`` / ``from_dict``)
for JSON persistence and for handing straight to the HTTP layer.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Optional

# ── Status vocab (from the wireframes: "Draft / In design / Ready") ─────────
ROOM_STATUSES = ("draft", "in-design", "ready")
PROJECT_STATUSES = ("draft", "in-design", "ready")

# Build input modes — the four "doors" in the New-build screen.
BUILD_MODES = ("bom", "brief", "catalog", "duplicate")


def stable_id(prefix: str, *parts: str) -> str:
    """Deterministic id from semantic parts (idempotent — no random UUIDs).

    Mirrors the repo's ``cell_id`` convention so re-running a build doesn't
    churn ids.
    """
    raw = ":".join([prefix, *parts])
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}-{h}"


@dataclass
class Port:
    """A single connector on a device."""

    id: str
    label: str
    direction: str = "bidirectional"  # input | output | bidirectional
    signalType: str = "ethernet"
    connectorType: str = "rj45"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Port":
        return cls(
            id=d["id"],
            label=d.get("label", ""),
            direction=d.get("direction", "bidirectional"),
            signalType=d.get("signalType", "ethernet"),
            connectorType=d.get("connectorType", "rj45"),
        )


@dataclass
class Device:
    """A piece of AV equipment within a room (a BOM row + its ports)."""

    id: str
    name: str                      # label shown on the schematic
    type: str = ""                 # codec, switcher, display, mic, amp, ...
    model: str = ""
    quantity: int = 1
    serial: str = ""
    notes: str = ""
    confidence: str = "confirmed"  # confirmed | high | unknown  (AI proposal)
    ports: list[Port] = field(default_factory=list)
    # Free-form per-signal port counts straight from the BOM (hdmi_in, etc.).
    attrs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ports"] = [p.to_dict() for p in self.ports]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Device":
        return cls(
            id=d["id"],
            name=d.get("name", ""),
            type=d.get("type", ""),
            model=d.get("model", ""),
            quantity=int(d.get("quantity", 1) or 1),
            serial=d.get("serial", ""),
            notes=d.get("notes", ""),
            confidence=d.get("confidence", "confirmed"),
            ports=[Port.from_dict(p) for p in d.get("ports", [])],
            attrs=dict(d.get("attrs", {})),
        )


@dataclass
class CableRun:
    """One row of the cable schedule — derived from a schematic edge."""

    id: str
    fromRef: str    # "Device·Port"
    toRef: str      # "Device·Port"
    signalType: str
    length: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CableRun":
        return cls(
            id=d["id"],
            fromRef=d.get("fromRef", ""),
            toRef=d.get("toRef", ""),
            signalType=d.get("signalType", ""),
            length=d.get("length", ""),
        )


@dataclass
class TitleBlock:
    """Drawing title block (Export screen)."""

    jobNo: str = ""
    client: str = ""
    drawnBy: str = ""
    revision: str = "A"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TitleBlock":
        return cls(
            jobNo=d.get("jobNo", ""),
            client=d.get("client", ""),
            drawnBy=d.get("drawnBy", ""),
            revision=d.get("revision", "A"),
        )


@dataclass
class Room:
    """A single room — the unit the core loop builds a schematic for."""

    id: str
    name: str
    building: str = ""             # optional grouping (building-map view)
    status: str = "draft"
    devices: list[Device] = field(default_factory=list)
    # Last build result (populated by the core loop), kept as opaque dicts.
    schematic: Optional[dict[str, Any]] = None      # EasySchematic JSON
    cableSchedule: list[CableRun] = field(default_factory=list)
    drawioPath: str = ""
    titleBlock: TitleBlock = field(default_factory=TitleBlock)

    @property
    def device_count(self) -> int:
        return sum(max(1, d.quantity) for d in self.devices)

    @property
    def cable_count(self) -> int:
        return len(self.cableSchedule)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "building": self.building,
            "status": self.status,
            "devices": [d.to_dict() for d in self.devices],
            "schematic": self.schematic,
            "cableSchedule": [c.to_dict() for c in self.cableSchedule],
            "drawioPath": self.drawioPath,
            "titleBlock": self.titleBlock.to_dict(),
            "deviceCount": self.device_count,
            "cableCount": self.cable_count,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Room":
        return cls(
            id=d["id"],
            name=d.get("name", ""),
            building=d.get("building", ""),
            status=d.get("status", "draft"),
            devices=[Device.from_dict(x) for x in d.get("devices", [])],
            schematic=d.get("schematic"),
            cableSchedule=[CableRun.from_dict(x) for x in d.get("cableSchedule", [])],
            drawioPath=d.get("drawioPath", ""),
            titleBlock=TitleBlock.from_dict(d.get("titleBlock", {})),
        )


@dataclass
class Project:
    """A build/project grouping one or more rooms (optionally by building)."""

    id: str
    name: str
    status: str = "draft"
    client: str = ""
    created: str = field(default_factory=lambda: date.today().isoformat())
    rooms: list[Room] = field(default_factory=list)

    @property
    def room_count(self) -> int:
        return len(self.rooms)

    @property
    def device_count(self) -> int:
        return sum(r.device_count for r in self.rooms)

    def to_dict(self, *, include_rooms: bool = True) -> dict[str, Any]:
        d = {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "client": self.client,
            "created": self.created,
            "roomCount": self.room_count,
            "deviceCount": self.device_count,
        }
        if include_rooms:
            d["rooms"] = [r.to_dict() for r in self.rooms]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Project":
        return cls(
            id=d["id"],
            name=d.get("name", ""),
            status=d.get("status", "draft"),
            client=d.get("client", ""),
            created=d.get("created", date.today().isoformat()),
            rooms=[Room.from_dict(x) for x in d.get("rooms", [])],
        )
