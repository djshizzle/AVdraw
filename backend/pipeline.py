"""Single-room core loop.

This is the heart of the backend: given one room's devices (or a raw BOM),
produce the full set of engineering artifacts the wireframes revolve around —
a draw.io schematic, an EasySchematic JSON document, and a cable schedule —
with validation at each gate.

    devices/BOM ─▶ [BOM CSV] ─▶ validate_bom ─▶ build_drawio ─▶ .drawio
                                                       │
                              validate_drawio ◀────────┤
                                                       ▼
                              parse_drawio ─▶ build_schematic ─▶ EasySchematic
                                                       │
                                                       ▼
                                           derive cable schedule

It reuses the existing ``src/`` modules unchanged — no logic is reimplemented
here — and writes all artifacts to ``output/`` (never into ``src/``).

Stdlib-only: this module imports no web framework, so the loop is runnable and
testable without FastAPI installed (``python3 -m backend.pipeline ...``).
"""

from __future__ import annotations

import csv
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import config
from .domain import CableRun, Device, Port, Room, stable_id

logger = logging.getLogger("avdraw.pipeline")

# ── Make the existing pipeline modules importable ───────────────────────────
if str(config.SRC_DIR) not in sys.path:
    sys.path.insert(0, str(config.SRC_DIR))

import bom_to_drawio          # noqa: E402  (path injected above)
import drawio_to_easyschematic  # noqa: E402
import bom_validator          # noqa: E402
import drawio_validator       # noqa: E402

# Columns we emit when serialising Devices back to a BOM CSV. Names match the
# aliases load_bom()/validate_bom() understand.
_BOM_BASE_COLUMNS = ["name", "type", "model", "serial", "notes", "quantity"]


# ── Result types ────────────────────────────────────────────────────────────
@dataclass
class ValidationReport:
    """A normalised view of either validator's result."""

    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings}

    @classmethod
    def from_result(cls, result: Any) -> "ValidationReport":
        return cls(
            ok=bool(getattr(result, "ok", True)),
            errors=[i.format() for i in getattr(result, "errors", [])],
            warnings=[i.format() for i in getattr(result, "warnings", [])],
        )


@dataclass
class BuildResult:
    """Everything the single-room core loop produces."""

    name: str
    drawio_path: str
    drawio_xml: str
    schematic: dict[str, Any]
    cable_schedule: list[CableRun]
    devices: list[Device]
    bom_validation: ValidationReport
    drawio_validation: ValidationReport

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "drawioPath": self.drawio_path,
            "schematic": self.schematic,
            "cableSchedule": [c.to_dict() for c in self.cable_schedule],
            "devices": [d.to_dict() for d in self.devices],
            "validation": {
                "bom": self.bom_validation.to_dict(),
                "drawio": self.drawio_validation.to_dict(),
            },
            "counts": {
                "devices": len(self.devices),
                "nodes": len(self.schematic.get("nodes", [])),
                "edges": len(self.schematic.get("edges", [])),
                "cables": len(self.cable_schedule),
            },
        }


class BuildError(RuntimeError):
    """Raised when a validation gate fails in strict mode (fail loud)."""

    def __init__(self, message: str, report: Optional[ValidationReport] = None):
        super().__init__(message)
        self.report = report


# ── Helpers ─────────────────────────────────────────────────────────────────
def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", name.strip()).strip("_").lower()
    return s or "room"


def devices_to_bom_rows(devices: list[Device]) -> list[dict[str, str]]:
    """Serialise Device objects to flat CSV-row dicts (one per device)."""
    rows: list[dict[str, str]] = []
    extra_cols: list[str] = []
    for d in devices:
        for k in d.attrs:
            if k not in extra_cols:
                extra_cols.append(k)
    for d in devices:
        row = {
            "name": d.name,
            "type": d.type,
            "model": d.model,
            "serial": d.serial,
            "notes": d.notes,
            "quantity": str(max(1, d.quantity)),
        }
        for k in extra_cols:
            row[k] = str(d.attrs.get(k, ""))
        rows.append(row)
    return rows


def write_bom_csv(devices: list[Device], path: Path) -> Path:
    """Write devices to a BOM CSV that load_bom()/validate_bom() can read."""
    rows = devices_to_bom_rows(devices)
    columns = list(_BOM_BASE_COLUMNS)
    for r in rows:
        for k in r:
            if k not in columns:
                columns.append(k)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c, "") for c in columns})
    return path


def devices_from_bom_rows(rows: list[dict[str, Any]]) -> list[Device]:
    """Build Device objects from load_bom() rows (used by the proposal step).

    load_bom() expands quantity into one row per unit; we collapse them back so
    the review screen shows one Device with a quantity.
    """
    grouped: dict[str, Device] = {}
    order: list[str] = []
    # per-signal port-count columns carried as attrs
    port_cols = [
        "hdmi_in", "hdmi_out", "dante_in", "dante_out", "usb_in", "usb_out",
        "sdi_in", "sdi_out", "hdbaset_in", "hdbaset_out", "ethernet",
        "analog_audio_in", "analog_audio_out", "fiber_ports",
    ]
    for row in rows:
        name = row.get("name") or row.get("_label") or "Device"
        dtype = row.get("device_type") or row.get("type") or ""
        model = row.get("model", "")
        key = stable_id("dev", name, dtype, model)
        if key not in grouped:
            confidence = "confirmed" if dtype and dtype != "generic" else "unknown"
            attrs = {c: row[c] for c in port_cols if str(row.get(c, "")).strip() not in ("", "0")}
            grouped[key] = Device(
                id=key, name=name, type=dtype, model=model,
                serial=row.get("serial", ""), notes=row.get("notes", ""),
                quantity=0, confidence=confidence, attrs=attrs,
            )
            order.append(key)
        grouped[key].quantity += 1
    return [grouped[k] for k in order]


def _derive_cable_schedule(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> list[CableRun]:
    """Turn EasySchematic edges into cable-schedule rows (Cable schedule screen)."""
    node_label = {n["id"]: n.get("data", {}).get("label", "?") for n in nodes}
    port_label: dict[str, str] = {}
    for n in nodes:
        for p in n.get("data", {}).get("ports", []):
            port_label[p["id"]] = p.get("label", "")

    def ref(node_id: str, handle: Optional[str]) -> str:
        label = node_label.get(node_id, "?")
        port = port_label.get(handle or "", "")
        return f"{label}·{port}" if port else label

    runs: list[CableRun] = []
    for e in edges:
        signal = e.get("data", {}).get("signalType", "")
        cid = stable_id("cbl", e["source"], e.get("sourceHandle", ""),
                        e["target"], e.get("targetHandle", ""))
        runs.append(CableRun(
            id=cid,
            fromRef=ref(e["source"], e.get("sourceHandle")),
            toRef=ref(e["target"], e.get("targetHandle")),
            signalType=signal,
            length=e.get("data", {}).get("length", ""),
        ))
    return runs


# ── The core loop ────────────────────────────────────────────────────────────
def run_build_from_csv(
    csv_path: str,
    name: str,
    *,
    strict: bool = False,
    output_dir: Optional[Path] = None,
) -> BuildResult:
    """Run the full single-room loop from a BOM CSV file.

    Gates (each is fail-loud in ``strict`` mode, warn-and-continue otherwise):
      1. validate_bom   — pre-flight BOM check
      2. build_drawio   — generate the schematic
      3. validate_drawio— post-generation check (with BOM cross-check)
      4. parse_drawio + build_schematic — EasySchematic JSON
      5. derive cable schedule
    """
    config.ensure_dirs()
    out = output_dir or config.OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    slug = _slug(name)

    # 1. BOM validation -------------------------------------------------------
    bom_result = bom_validator.validate_bom(csv_path, strict=strict)
    bom_report = ValidationReport.from_result(bom_result)
    if strict and not bom_report.ok:
        raise BuildError("BOM validation failed", bom_report)
    for w in bom_report.warnings:
        logger.warning("BOM: %s", w)

    # 2. Generate draw.io -----------------------------------------------------
    rows = bom_to_drawio.load_bom(csv_path)
    if not rows:
        raise BuildError("no usable devices found in BOM", bom_report)
    drawio_xml = bom_to_drawio.build_drawio(rows, None, name)
    drawio_path = out / f"{slug}.drawio"
    drawio_path.write_text(drawio_xml, encoding="utf-8")
    logger.info("wrote %s (%d devices)", drawio_path, len(rows))

    # 3. draw.io validation (cross-checked against the BOM) --------------------
    drawio_result = drawio_validator.validate_drawio(
        str(drawio_path), bom_path=csv_path, strict=strict
    )
    drawio_report = ValidationReport.from_result(drawio_result)
    if strict and not drawio_report.ok:
        raise BuildError("draw.io validation failed", drawio_report)
    for w in drawio_report.warnings:
        logger.warning("draw.io: %s", w)

    # 4. EasySchematic JSON ---------------------------------------------------
    nodes, edges = drawio_to_easyschematic.parse_drawio(str(drawio_path))
    schematic = drawio_to_easyschematic.build_schematic(nodes, edges, name)

    # 5. Cable schedule -------------------------------------------------------
    cable_schedule = _derive_cable_schedule(nodes, edges)

    devices = devices_from_bom_rows(rows)

    return BuildResult(
        name=name,
        drawio_path=str(drawio_path),
        drawio_xml=drawio_xml,
        schematic=schematic,
        cable_schedule=cable_schedule,
        devices=devices,
        bom_validation=bom_report,
        drawio_validation=drawio_report,
    )


def run_room_build(
    room: Room,
    *,
    project_name: str = "",
    strict: bool = False,
    output_dir: Optional[Path] = None,
) -> BuildResult:
    """Run the core loop for a stored Room (devices → artifacts).

    The room's devices are serialised to a BOM CSV under ``output/`` and fed
    through :func:`run_build_from_csv`, so the room path and the raw-CSV path
    share identical behaviour.
    """
    if not room.devices:
        raise BuildError(f"room {room.name!r} has no devices to build")
    out = output_dir or config.OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    name = room.name or project_name or "Room"
    csv_path = out / f"{_slug(name)}.bom.csv"
    write_bom_csv(room.devices, csv_path)
    return run_build_from_csv(
        str(csv_path), name, strict=strict, output_dir=out
    )


# ── CLI entry point (lets the loop be exercised without the web layer) ───────
def _main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Run the single-room core loop (BOM → draw.io → "
                    "EasySchematic + cable schedule)."
    )
    parser.add_argument("--bom", required=True, help="BOM CSV file")
    parser.add_argument("--name", default="Room", help="Room/schematic name")
    parser.add_argument("--strict", action="store_true",
                        help="Promote validation warnings to errors")
    parser.add_argument("--output-dir", default=None,
                        help="Where to write artifacts (default: output/)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    out = Path(args.output_dir) if args.output_dir else None
    try:
        result = run_build_from_csv(
            args.bom, args.name, strict=args.strict, output_dir=out
        )
    except BuildError as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        if exc.report:
            for e in exc.report.errors:
                print(f"  {e}", file=sys.stderr)
        return 1

    c = result.to_dict()["counts"]
    print(f"OK  {result.name}")
    print(f"    drawio : {result.drawio_path}")
    print(f"    devices: {c['devices']}  nodes: {c['nodes']}  "
          f"edges: {c['edges']}  cables: {c['cables']}")
    print(f"    bom    : ok={result.bom_validation.ok} "
          f"warn={len(result.bom_validation.warnings)}")
    print(f"    drawio : ok={result.drawio_validation.ok} "
          f"warn={len(result.drawio_validation.warnings)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
