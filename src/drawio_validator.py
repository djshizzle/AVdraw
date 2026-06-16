#!/usr/bin/env python3
"""
drawio_validator.py — Post-generation assertion checks on .drawio files
========================================================================
Run AFTER bom_to_drawio.py to catch generation bugs before they reach the
client. Asserts the structural invariants that every AVdraw schematic must
satisfy.

Checks performed:
  E-DUP    No duplicate mxCell ids (would corrupt draw.io rendering)
  E-XML    File parses as well-formed XML
  E-ROOT   Has a <root> element with at least one swimlane device
  E-BOM    Every BOM row name appears as a swimlane (when --bom is passed)
  W-DANGLE Every output port (Out/Tx) has at least one outgoing edge
  W-ORPHAN Every swimlane device has at least one connection
  W-STYLE  Every edge has a recognised signal color in strokeColor

Severity:
  ERROR    structural problem — abort the pipeline
  WARNING  quality issue — print and continue

Usage as CLI:
    python3 src/drawio_validator.py --input output/room.drawio
    python3 src/drawio_validator.py --input output/room.drawio --bom my_room.csv
    python3 src/drawio_validator.py --input output/room.drawio --strict

Usage as module:
    from drawio_validator import validate_drawio
    result = validate_drawio("output/room.drawio", bom_path="my_room.csv")
    if not result.ok: sys.exit(1)
"""

from __future__ import annotations

import argparse
import csv
import html
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Signal color → type map (mirror of docs/signal_colors.md)
# Used to validate edges have a recognised stroke colour.
# ---------------------------------------------------------------------------

SIGNAL_COLORS = {
    "#d6b656":  "hdmi",
    "#6d8764":  "sdi",
    "#7030a0":  "dante",
    "#006eaf":  "ethernet",
    "#0070c0":  "usb",        # also displayport — shared colour
    "#ff0000":  "speaker-level",
    "#ff6600":  "analog-audio",
    "#e36c09":  "ndi",
    "#833c00":  "avb",
    "#00b0f0":  "fiber",
    "#70ad47":  "hdbaset",
    "#ffc000":  "rs422",      # also gpio
    "#808080":  "rf",
}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ValidationIssue:
    code:     str      # E-DUP, W-DANGLE, etc.
    severity: str      # "ERROR" | "WARNING"
    message:  str
    location: str = ""

    def format(self) -> str:
        loc = f" @ {self.location}" if self.location else ""
        return f"[{self.severity} {self.code}]{loc} {self.message}"


@dataclass
class ValidationResult:
    ok:           bool                  = True
    errors:       list[ValidationIssue] = field(default_factory=list)
    warnings:     list[ValidationIssue] = field(default_factory=list)
    cell_count:   int                   = 0
    device_count: int                   = 0
    edge_count:   int                   = 0

    def err(self, code: str, msg: str, loc: str = "") -> None:
        self.errors.append(ValidationIssue(code, "ERROR", msg, loc))
        self.ok = False

    def warn(self, code: str, msg: str, loc: str = "") -> None:
        self.warnings.append(ValidationIssue(code, "WARNING", msg, loc))


# ---------------------------------------------------------------------------
# draw.io parsing helpers
# ---------------------------------------------------------------------------

def _parse_drawio(path: str) -> ET.Element | None:
    """Return the <root> element, or None on parse failure."""
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return None
    root = tree.getroot()
    if root.tag == "root":
        return root
    return root.find(".//root")


def _extract_style_value(style: str, key: str) -> str:
    """Pull a value out of a draw.io style string ('a=b;c=d;...')."""
    if not style:
        return ""
    for kv in style.split(";"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            if k.strip() == key:
                return v.strip()
    return ""


def _load_bom_names(bom_path: str) -> set[str]:
    """Load BOM device names for cross-checking. Skips comments."""
    p = Path(bom_path)
    if not p.exists():
        return set()
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = [ln for ln in text.splitlines(keepends=True)
             if not ln.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    name_col = None
    for c in (reader.fieldnames or []):
        if c and c.strip().lower() in ("name", "device_name", "device"):
            name_col = c
            break
    if not name_col:
        return set()
    names = set()
    for row in reader:
        n = (row.get(name_col, "") or "").strip()
        if n:
            names.add(n.lower())
    return names


# ---------------------------------------------------------------------------
# Validation core
# ---------------------------------------------------------------------------

def validate_drawio(
    path: str,
    bom_path: str = "",
    strict: bool = False,
) -> ValidationResult:
    """
    Run all post-generation checks against a .drawio file.

    bom_path: optional — when provided, cross-checks BOM names against
              swimlane labels (E-BOM check).
    strict:   when True, warnings are promoted to errors.
    """
    result = ValidationResult()

    p = Path(path)
    if not p.exists():
        result.err("E-XML", f"file not found: {path}")
        return result

    root = _parse_drawio(path)
    if root is None:
        result.err("E-XML", f"not well-formed XML: {path}")
        return result

    cells = root.findall("mxCell")
    result.cell_count = len(cells)

    if not cells:
        result.err("E-ROOT", "no <mxCell> elements found — file is empty")
        return result

    # ── E-DUP: duplicate cell IDs ──────────────────────────────────────────
    ids = [c.get("id", "") for c in cells if c.get("id")]
    dup_counts = Counter(ids)
    dups = [cid for cid, n in dup_counts.items() if n > 1]
    for cid in dups:
        result.err("E-DUP", f"duplicate mxCell id '{cid}' ({dup_counts[cid]}x)",
                   loc=f"id={cid}")

    # ── Index all cells ────────────────────────────────────────────────────
    cells_by_id: dict[str, ET.Element] = {}
    for c in cells:
        cid = c.get("id", "")
        if cid and cid not in cells_by_id:
            cells_by_id[cid] = c

    # ── Identify swimlane devices (top-level, parent=1 or 0) ───────────────
    devices: list[dict] = []
    for c in cells:
        style  = c.get("style", "") or ""
        parent = c.get("parent", "") or ""
        value  = (c.get("value", "") or "").strip()
        if "swimlane" not in style:
            continue
        if parent not in ("1", "0"):
            continue
        # Skip section-level swimlanes (Input/Output headers inside devices)
        if value in ("Input", "Output"):
            continue
        if not value:
            continue
        devices.append({
            "id":     c.get("id", ""),
            "label":  html.unescape(value),
            "style":  style,
        })

    result.device_count = len(devices)

    if result.device_count == 0:
        result.err("E-ROOT", "no top-level device swimlanes found")

    # ── Build edge index ───────────────────────────────────────────────────
    # In draw.io, edges are mxCells with edge="1", source=..., target=...
    edges: list[dict] = []
    for c in cells:
        if c.get("edge") != "1":
            continue
        src = c.get("source", "")
        tgt = c.get("target", "")
        if not src and not tgt:
            continue  # dangling label / decoration
        style = c.get("style", "") or ""
        color = _extract_style_value(style, "strokeColor").lower()
        edges.append({
            "id":     c.get("id", ""),
            "source": src,
            "target": tgt,
            "color":  color,
            "style":  style,
        })

    result.edge_count = len(edges)

    # Build outgoing / incoming maps keyed by root-device-id
    # An edge's source/target might be a port cell inside a device — walk up
    # to find the top-level device that owns it.
    def _root_device_of(cell_id: str) -> str:
        """Walk parent chain until we hit a top-level swimlane."""
        seen = set()
        cur = cell_id
        while cur and cur not in seen:
            seen.add(cur)
            cell = cells_by_id.get(cur)
            if cell is None:
                return cur
            style  = cell.get("style", "") or ""
            parent = cell.get("parent", "") or ""
            if "swimlane" in style and parent in ("1", "0"):
                return cur
            cur = parent
        return cur

    device_ids: set[str] = {d["id"] for d in devices}
    outgoing: defaultdict[str, list[dict]] = defaultdict(list)
    incoming: defaultdict[str, list[dict]] = defaultdict(list)
    edge_owners: list[tuple[str, str, dict]] = []  # (src_dev, tgt_dev, edge)

    for e in edges:
        src_dev = _root_device_of(e["source"]) if e["source"] else ""
        tgt_dev = _root_device_of(e["target"]) if e["target"] else ""
        if src_dev in device_ids:
            outgoing[src_dev].append(e)
        if tgt_dev in device_ids:
            incoming[tgt_dev].append(e)
        edge_owners.append((src_dev, tgt_dev, e))

    # ── W-ORPHAN: device with no connections at all ────────────────────────
    for d in devices:
        if not outgoing[d["id"]] and not incoming[d["id"]]:
            result.warn(
                "W-ORPHAN",
                f"device '{d['label']}' has no connections",
                loc=f"id={d['id']}",
            )

    # ── W-DANGLE: device has Output ports but no outgoing edges ────────────
    # Heuristic: if any child cell of the device sits in an "Output" section
    # swimlane, the device has outputs that should be wired up.
    for d in devices:
        # Find Output section within this device
        has_outputs = False
        for c in cells:
            parent = c.get("parent", "")
            value  = (c.get("value", "") or "").strip()
            if parent == d["id"] and "swimlane" in (c.get("style", "") or "") \
                    and value == "Output":
                has_outputs = True
                break
        if has_outputs and not outgoing[d["id"]]:
            result.warn(
                "W-DANGLE",
                f"device '{d['label']}' has output ports but no outgoing edges",
                loc=f"id={d['id']}",
            )

    # ── W-STYLE: edges with unrecognised stroke colour ─────────────────────
    for e in edges:
        if not e["color"]:
            continue  # default colour — fine, treated as generic
        if e["color"].lower() not in SIGNAL_COLORS:
            result.warn(
                "W-STYLE",
                f"edge has unrecognised stroke colour '{e['color']}' "
                f"(not in signal_colors.md)",
                loc=f"id={e['id']}",
            )

    # ── E-BOM: BOM ↔ schematic cross-check ─────────────────────────────────
    if bom_path:
        bom_names = _load_bom_names(bom_path)
        if not bom_names:
            result.warn("E-BOM", f"BOM file '{bom_path}' had no usable names")
        else:
            drawio_labels = [d["label"].lower() for d in devices]
            # bom_to_drawio.py expands Quantity > 1 into multiple devices with
            # suffix labels like " Cam 1", " Mic 2". So match by prefix /
            # substring rather than exact equality.
            missing_in_drawio = []
            for n in bom_names:
                found = any(n in label or label.startswith(n)
                            for label in drawio_labels)
                if not found:
                    missing_in_drawio.append(n)
            for n in sorted(missing_in_drawio):
                result.err(
                    "E-BOM",
                    f"BOM device '{n}' not found as swimlane in drawio",
                    loc="bom-row",
                )
            # Extra devices in drawio (not in BOM) are fine — could be
            # codec-auto-added peripherals from xStatus or manual additions

    # ── Strict mode promotion ──────────────────────────────────────────────
    if strict and result.warnings:
        for w in result.warnings:
            result.errors.append(
                ValidationIssue(w.code, "ERROR",
                                f"(strict) {w.message}", w.location)
            )
        result.ok = False
        result.warnings = []

    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_report(result: ValidationResult, path: str = "") -> None:
    width = 72
    print("=" * width)
    print(f"  draw.io Validation Report  {path}")
    print("=" * width)
    print(f"  Total cells    : {result.cell_count}")
    print(f"  Device count   : {result.device_count}")
    print(f"  Edge count     : {result.edge_count}")
    print(f"  Errors         : {len(result.errors)}")
    print(f"  Warnings       : {len(result.warnings)}")
    print("-" * width)

    if result.errors:
        print("\nERRORS:")
        for e in result.errors:
            print(f"  {e.format()}", file=sys.stderr)

    if result.warnings:
        print("\nWARNINGS:")
        for w in result.warnings:
            print(f"  {w.format()}", file=sys.stderr)

    print()
    if result.ok:
        print(f"✓ draw.io file is valid "
              f"({result.device_count} devices, {result.edge_count} edges)")
    else:
        print(f"✗ draw.io has {len(result.errors)} error(s) — aborting",
              file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate an AVdraw-generated .drawio schematic"
    )
    parser.add_argument("--input", "-i", required=True,
                        help="Path to .drawio file")
    parser.add_argument("--bom", "-b",
                        help="Optional BOM CSV for cross-check (E-BOM)")
    parser.add_argument("--strict", action="store_true",
                        help="Treat warnings as errors")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Print only issues, suppress success banner")
    args = parser.parse_args()

    result = validate_drawio(args.input,
                             bom_path=args.bom or "",
                             strict=args.strict)

    if args.quiet:
        for e in result.errors:
            print(e.format(), file=sys.stderr)
        for w in result.warnings:
            print(w.format(), file=sys.stderr)
    else:
        print_report(result, args.input)

    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
