#!/usr/bin/env python3
"""
draw.io / BOM → DXF (AutoCAD) Converter
-----------------------------------------
Converts an AV schematic (from draw.io or direct BOM) into a DXF file
that AutoCAD, AutoCAD LT, BricsCAD, DraftSight, and LibreCAD can open.

DXF output features:
  - Each device is a BLOCK with a border rectangle + text rows for label/ports
  - Connections are LWPOLYLINE entities (orthogonal right-angle routing)
  - Layers per signal type (HDMI, DANTE, ETHERNET, etc.) — toggle in AutoCAD
  - Layer colors match the standard AV signal color convention
  - Title block on a TITLE layer
  - All geometry on a 1:1 scale at 1 unit = 1mm

Usage:
  # From draw.io file:
  python3 src/drawio_to_dxf.py --input output/Boardroom_Pro.drawio --output output/Boardroom_Pro.dxf

  # Direct from BOM (skips draw.io step):
  python3 src/drawio_to_dxf.py --bom my_room.csv --name "Boardroom A" --output output/Boardroom_A.dxf

  # Full pipeline shortcut (add --dxf flag to pipeline.sh):
  python3 src/drawio_to_dxf.py --input output/Boardroom_Pro.drawio --output output/Boardroom_Pro.dxf
"""

import argparse
import math
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import ezdxf
    from ezdxf import colors as dxf_colors
    from ezdxf.enums import TextEntityAlignment
except ImportError:
    sys.exit("ezdxf not installed. Run: pip3 install ezdxf --user")

# ─── Layer definitions ────────────────────────────────────────────────────────
# (layer_name, AutoCAD color index, description)
LAYERS = [
    ("DEVICES",        7,   "Device blocks"),
    ("DEVICE_TEXT",    7,   "Device labels and port text"),
    ("HDMI",          40,   "HDMI connections (amber)"),
    ("SDI",           94,   "SDI connections (olive green)"),
    ("DANTE",        191,   "Dante audio connections (purple)"),
    ("ETHERNET",      84,   "Ethernet / network connections (teal)"),
    ("USB",           84,   "USB connections (blue)"),
    ("DISPLAYPORT",   84,   "DisplayPort connections (blue)"),
    ("SPEAKER_LEVEL",  1,   "Speaker-level connections (red)"),
    ("ANALOG_AUDIO",  30,   "Analog audio connections (orange)"),
    ("NDI",           30,   "NDI connections (orange)"),
    ("HDBASET",       92,   "HDBaseT connections (green)"),
    ("FIBER",        130,   "Fiber connections (light blue)"),
    ("RS422",         50,   "RS-422 / serial connections (yellow)"),
    ("GPIO",          50,   "GPIO connections (yellow)"),
    ("RF",             8,   "RF connections (grey)"),
    ("TITLE",          7,   "Title block"),
    ("BORDER",         7,   "Sheet border"),
    ("NOTES",          8,   "Notes and annotations"),
]

SIGNAL_TO_LAYER = {
    "hdmi":          "HDMI",
    "sdi":           "SDI",
    "displayport":   "DISPLAYPORT",
    "usb":           "USB",
    "ethernet":      "ETHERNET",
    "dante":         "DANTE",
    "ndi":           "NDI",
    "avb":           "ETHERNET",
    "speaker-level": "SPEAKER_LEVEL",
    "analog-audio":  "ANALOG_AUDIO",
    "rf":            "RF",
    "fiber":         "FIBER",
    "hdbaset":       "HDBASET",
    "rs422":         "RS422",
    "gpio":          "GPIO",
}

# draw.io strokeColor → signal type
COLOR_TO_SIGNAL = {
    "#d6b656": "hdmi",
    "#6d8764": "sdi",
    "#006eaf": "ethernet",
    "#7030a0": "dante",
    "#e36c09": "ndi",
    "#ff0000": "speaker-level",
    "#ff6600": "analog-audio",
    "#808080": "rf",
    "#00b0f0": "fiber",
    "#70ad47": "hdbaset",
    "#ffc000": "rs422",
    "#0070c0": "usb",
}

LABEL_TO_SIGNAL = {
    "hdmi": "hdmi", "sdi": "sdi", "usb": "usb",
    "ethernet": "ethernet", "ethernet rj45": "ethernet", "rj45": "ethernet",
    "dante": "dante", "ndi": "ndi", "avb": "avb",
    "speaker": "speaker-level", "speaker-level": "speaker-level",
    "analog audio": "analog-audio", "analog-audio": "analog-audio",
    "rf": "rf", "fiber": "fiber", "hdbaset": "hdbaset",
    "displayport": "displayport", "dp": "displayport",
    "rs-422": "rs422", "rs422": "rs422", "gpio": "gpio",
    "control": "ethernet", "network": "ethernet",
}

# ─── Scale / layout constants ─────────────────────────────────────────────────
# draw.io pixels → mm  (draw.io uses 96dpi, 1px ≈ 0.2646mm)
PX_TO_MM   = 0.2646
DEVICE_W   = 160 * PX_TO_MM    # ~42mm
ROW_H      = 26  * PX_TO_MM    # ~6.9mm
HEADER_H   = 40  * PX_TO_MM    # ~10.6mm
SECTION_H  = 26  * PX_TO_MM    # ~6.9mm
TEXT_H     = 2.5                # mm, standard AutoCAD text height
SMALL_TEXT = 1.8
PAGE_W_MM  = 420                # A3 landscape
PAGE_H_MM  = 297
MARGIN_MM  = 10


def px(v) -> float:
    """Convert draw.io pixel value to mm."""
    return float(v or 0) * PX_TO_MM


def signal_from_edge(cell: ET.Element) -> str:
    label = (cell.get("value") or "").strip().lower()
    if label in LABEL_TO_SIGNAL:
        return LABEL_TO_SIGNAL[label]
    style = cell.get("style", "").lower()
    m = re.search(r"strokecolor=(#[0-9a-f]+)", style)
    if m:
        return COLOR_TO_SIGNAL.get(m.group(1).lower(), "ethernet")
    return "ethernet"


# ─── draw.io parser ───────────────────────────────────────────────────────────

def parse_drawio(path: str) -> tuple[list[dict], list[dict]]:
    """
    Returns:
      devices = [{id, label, x, y, w, h, ports:[{label,direction}], model, notes}]
      edges   = [{src_id, tgt_id, signal, label, src_x,src_y, tgt_x,tgt_y}]
    """
    tree = ET.parse(path)
    root = tree.getroot()
    if root.tag == "mxfile":
        root = root.find(".//mxGraphModel") or root

    cells = root.findall(".//mxCell")
    cell_by_id = {c.get("id", ""): c for c in cells}
    parent_of  = {c.get("id", ""): c.get("parent", "") for c in cells}

    # Device containers
    device_ids: set[str] = set()
    for c in cells:
        sty = c.get("style", "")
        if ("swimlane" in sty and "childLayout=stackLayout" in sty
                and c.get("parent") == "1" and c.get("vertex") == "1"):
            device_ids.add(c.get("id", ""))

    # Section swimlanes
    section_type: dict[str, str]   = {}
    section_parent: dict[str, str] = {}
    section_geo:  dict[str, tuple] = {}
    for c in cells:
        sty = c.get("style", "")
        cid = c.get("id", "")
        pid = c.get("parent", "")
        if pid in device_ids and "childLayout=stackLayout" in sty and "swimlane" in sty:
            lbl = (c.get("value") or "").strip().lower()
            section_type[cid]   = "input" if "input" in lbl else ("output" if "output" in lbl else "info")
            section_parent[cid] = pid
            geo = c.find("mxGeometry")
            sy  = float(geo.get("y", 0)) if geo is not None else 0
            sh  = float(geo.get("height", 0)) if geo is not None else 0
            section_geo[cid] = (sy, sh)

    # Build devices list
    devices: list[dict] = []
    dev_center: dict[str, tuple] = {}   # id → (cx, cy) in mm

    for did in device_ids:
        c = cell_by_id.get(did)
        if c is None:
            continue
        geo = c.find("mxGeometry")
        dx  = px(geo.get("x", 0)) if geo is not None else 0
        dy  = px(geo.get("y", 0)) if geo is not None else 0
        dw  = px(geo.get("width", 160)) if geo is not None else px(160)
        dh  = px(geo.get("height", 120)) if geo is not None else px(120)
        label = (c.get("value") or "Device").strip()

        # Collect port rows
        ports: list[dict] = []
        model = ""
        notes = ""

        for child in cells:
            cpid  = child.get("parent", "")
            csty  = child.get("style", "")
            clbl  = (child.get("value") or "").strip()
            cgeo  = child.find("mxGeometry")
            cy    = float(cgeo.get("y", 0)) if cgeo is not None else 0

            if "portConstraint=eastwest" not in csty or "swimlane" in csty:
                continue

            direction = "bidirectional"
            if cpid == did:
                # direct child = info row
                direction = "bidirectional"
                # first direct child after header is typically model
                if not model and clbl and cy <= px(70):
                    model = clbl
                    continue
                elif clbl and "s/n:" in clbl.lower():
                    notes = clbl
                    continue
            elif cpid in section_parent and section_parent[cpid] == did:
                stype = section_type.get(cpid, "info")
                direction = "input" if stype == "input" else ("output" if stype == "output" else "bidirectional")
            else:
                # grand-child
                gp = parent_of.get(cpid, "")
                if gp == did:
                    direction = "bidirectional"
                elif gp in section_parent and section_parent.get(gp) == did:
                    direction = "input" if section_type.get(gp) == "input" else (
                               "output" if section_type.get(gp) == "output" else "bidirectional")
                else:
                    continue

            if clbl:
                sig = LABEL_TO_SIGNAL.get(clbl.lower())
                if not sig:
                    for k, s in LABEL_TO_SIGNAL.items():
                        if k in clbl.lower():
                            sig = s
                            break
                ports.append({
                    "id":        child.get("id",""),
                    "label":     clbl,
                    "direction": direction,
                    "signal":    sig or "ethernet",
                })

        dev_center[did] = (dx + dw/2, dy + dh/2)
        devices.append({
            "id": did, "label": label,
            "x": dx, "y": dy, "w": dw, "h": dh,
            "ports": ports, "model": model, "notes": notes,
        })

    # Build edges
    edges: list[dict] = []
    for c in cells:
        if c.get("edge") != "1":
            continue
        src = c.get("source", "")
        tgt = c.get("target", "")
        if not src or not tgt:
            continue

        signal = signal_from_edge(c)
        label  = (c.get("value") or "").strip()

        # Walk up to device container
        def device_of(cell_id):
            cid = cell_id
            for _ in range(4):
                if cid in device_ids:
                    return cid
                cid = parent_of.get(cid, "")
            return None

        src_dev = device_of(src)
        tgt_dev = device_of(tgt)
        if not src_dev or not tgt_dev or src_dev == tgt_dev:
            continue

        sx, sy = dev_center.get(src_dev, (0,0))
        tx, ty = dev_center.get(tgt_dev, (0,0))

        edges.append({
            "src_dev": src_dev, "tgt_dev": tgt_dev,
            "signal": signal, "label": label,
            "sx": sx, "sy": sy, "tx": tx, "ty": ty,
        })

    return devices, edges


# ─── DXF builder ─────────────────────────────────────────────────────────────

def build_dxf(devices: list[dict], edges: list[dict], name: str) -> ezdxf.document.Drawing:
    doc = ezdxf.new(dxfversion="R2013")
    msp = doc.modelspace()

    # ── Set up layers ──────────────────────────────────────────────────────
    for lname, color, _ in LAYERS:
        if lname not in doc.layers:
            doc.layers.add(lname, color=color)

    # ── Sheet border ───────────────────────────────────────────────────────
    border = [
        (MARGIN_MM, MARGIN_MM),
        (PAGE_W_MM - MARGIN_MM, MARGIN_MM),
        (PAGE_W_MM - MARGIN_MM, PAGE_H_MM - MARGIN_MM),
        (MARGIN_MM, PAGE_H_MM - MARGIN_MM),
        (MARGIN_MM, MARGIN_MM),
    ]
    msp.add_lwpolyline(border, dxfattribs={"layer": "BORDER", "lineweight": 50})

    # ── Title block (bottom right) ─────────────────────────────────────────
    tb_x = PAGE_W_MM - MARGIN_MM - 120
    tb_y = MARGIN_MM
    tb_h = 25
    # Outer box
    msp.add_lwpolyline([
        (tb_x, tb_y), (PAGE_W_MM - MARGIN_MM, tb_y),
        (PAGE_W_MM - MARGIN_MM, tb_y + tb_h), (tb_x, tb_y + tb_h), (tb_x, tb_y)
    ], dxfattribs={"layer": "TITLE"})
    # Title text
    msp.add_text(name, dxfattribs={
        "layer": "TITLE", "height": TEXT_H,
        "insert": (tb_x + 2, tb_y + tb_h - 6),
    })
    msp.add_text("AV Signal Flow Diagram", dxfattribs={
        "layer": "TITLE", "height": SMALL_TEXT,
        "insert": (tb_x + 2, tb_y + tb_h - 11),
    })
    import datetime
    msp.add_text(f"Date: {datetime.date.today()}", dxfattribs={
        "layer": "TITLE", "height": SMALL_TEXT,
        "insert": (tb_x + 2, tb_y + 4),
    })
    msp.add_text("Scale: 1:1  Units: mm", dxfattribs={
        "layer": "TITLE", "height": SMALL_TEXT,
        "insert": (tb_x + 60, tb_y + 4),
    })

    # ── Signal legend (top left) ───────────────────────────────────────────
    legend_sigs = [
        ("HDMI",          "hdmi",          40),
        ("SDI",           "sdi",           94),
        ("Dante",         "dante",         191),
        ("Ethernet/RJ45", "ethernet",      84),
        ("USB",           "usb",           84),
        ("Speaker Level", "speaker-level", 1),
        ("Analog Audio",  "analog-audio",  30),
        ("NDI",           "ndi",           30),
        ("HDBaseT",       "hdbaset",       92),
        ("Fiber",         "fiber",         130),
    ]
    lx, ly = MARGIN_MM + 2, PAGE_H_MM - MARGIN_MM - 5
    msp.add_text("SIGNAL LEGEND", dxfattribs={
        "layer": "NOTES", "height": SMALL_TEXT,
        "insert": (lx, ly),
    })
    for i, (lbl, sig, _) in enumerate(legend_sigs):
        row_y = ly - (i+1) * 5
        layer = SIGNAL_TO_LAYER.get(sig, "ETHERNET")
        # Short sample line
        msp.add_line((lx, row_y + 1), (lx + 8, row_y + 1), dxfattribs={"layer": layer, "lineweight": 35})
        msp.add_text(lbl, dxfattribs={"layer": "NOTES", "height": SMALL_TEXT, "insert": (lx + 10, row_y)})

    # ── Devices ────────────────────────────────────────────────────────────
    # draw.io Y axis is top-down; DXF is bottom-up — flip Y
    # Find bounding box of all devices
    if devices:
        min_x = min(d["x"] for d in devices)
        max_y = max(d["y"] + d["h"] for d in devices)
    else:
        min_x, max_y = 0, 0

    ORIGIN_X = MARGIN_MM + 40    # leave room for legend
    ORIGIN_Y = MARGIN_MM + 30    # leave room for title block

    def to_dxf(px_x, px_y):
        """Convert draw.io coords (px, top-left origin) to DXF mm (bottom-left origin)."""
        return (
            ORIGIN_X + (px_x - min_x),
            ORIGIN_Y + (max_y - px_y),
        )

    # Draw device boxes
    for dev in devices:
        x1, y2 = to_dxf(dev["x"],             dev["y"])
        x2, y1 = to_dxf(dev["x"] + dev["w"],  dev["y"] + dev["h"])

        # Device border rectangle
        msp.add_lwpolyline(
            [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)],
            dxfattribs={"layer": "DEVICES", "lineweight": 35}
        )

        # Header fill line (simulate header bar)
        header_h_mm = HEADER_H
        msp.add_lwpolyline(
            [(x1, y2 - header_h_mm), (x2, y2 - header_h_mm)],
            dxfattribs={"layer": "DEVICES"}
        )

        # Device label (in header)
        msp.add_text(dev["label"][:28], dxfattribs={
            "layer":  "DEVICE_TEXT",
            "height": TEXT_H,
            "insert": (x1 + 1, y2 - header_h_mm + 2.5),
        })

        # Model line
        if dev["model"]:
            msp.add_text(dev["model"][:28], dxfattribs={
                "layer":  "DEVICE_TEXT",
                "height": SMALL_TEXT,
                "insert": (x1 + 1, y2 - header_h_mm - ROW_H + 1),
            })

        # Port rows
        port_y = y2 - header_h_mm - ROW_H  # start below model row
        if dev["model"]:
            port_y -= ROW_H

        prev_direction = None
        for port in dev["ports"]:
            if port["direction"] != prev_direction and port["direction"] != "bidirectional":
                # Section divider
                msp.add_line(
                    (x1, port_y + ROW_H), (x2, port_y + ROW_H),
                    dxfattribs={"layer": "DEVICES", "lineweight": 13}
                )
                sec_label = "INPUT" if port["direction"] == "input" else "OUTPUT"
                msp.add_text(sec_label, dxfattribs={
                    "layer": "DEVICE_TEXT", "height": SMALL_TEXT,
                    "insert": (x1 + 1, port_y + ROW_H + 0.5),
                })
                port_y -= SECTION_H
                prev_direction = port["direction"]

            # Port separator
            msp.add_line(
                (x1, port_y + ROW_H), (x2, port_y + ROW_H),
                dxfattribs={"layer": "DEVICES", "lineweight": 5}
            )

            # Port label
            sig_layer = SIGNAL_TO_LAYER.get(port["signal"], "ETHERNET")
            align = "right" if port["direction"] == "output" else "left"
            tx = x2 - 1 if align == "right" else x1 + 1
            msp.add_text(port["label"][:22], dxfattribs={
                "layer":  sig_layer,
                "height": SMALL_TEXT,
                "insert": (tx, port_y + 1),
            })

            # Connection point marker (small cross at port edge)
            cx = x1 if port["direction"] in ("input", "bidirectional") else x2
            cy_p = port_y + ROW_H / 2
            msp.add_line((cx - 1, cy_p), (cx + 1, cy_p), dxfattribs={"layer": sig_layer})
            msp.add_line((cx, cy_p - 1), (cx, cy_p + 1), dxfattribs={"layer": sig_layer})

            port_y -= ROW_H

        # Notes
        if dev["notes"]:
            msp.add_text(dev["notes"][:35], dxfattribs={
                "layer":  "NOTES",
                "height": SMALL_TEXT,
                "insert": (x1 + 1, y1 + 1),
            })

    # ── Connections (orthogonal polylines) ─────────────────────────────────
    # Track used mid-x lanes to avoid overlapping verticals
    used_lanes: dict[float, int] = {}

    def get_lane(mid_x: float) -> float:
        """Nudge mid_x slightly if lane already used, to prevent overlap."""
        key = round(mid_x, 1)
        count = used_lanes.get(key, 0)
        used_lanes[key] = count + 1
        return mid_x + count * 1.5    # offset each additional wire by 1.5mm

    for edge in edges:
        sx, sy = to_dxf(edge["sx"], edge["sy"])
        tx, ty = to_dxf(edge["tx"], edge["ty"])
        signal  = edge["signal"]
        layer   = SIGNAL_TO_LAYER.get(signal, "ETHERNET")

        # Orthogonal routing: exit right → jog vertically → enter left
        mid_x = get_lane((sx + tx) / 2)

        if abs(sy - ty) < 1:
            # Same Y — straight horizontal
            pts = [(sx, sy), (tx, ty)]
        else:
            pts = [
                (sx, sy),
                (mid_x, sy),
                (mid_x, ty),
                (tx, ty),
            ]

        msp.add_lwpolyline(pts, dxfattribs={"layer": layer, "lineweight": 25})

        # Arrow at target end (manual since ezdxf R2013 arrows need dimension style)
        # Draw a small chevron instead
        dx = tx - mid_x
        dy = ty - ty  # always 0 at endpoint
        # Arrowhead: small triangle pointing toward target
        if tx > mid_x:
            msp.add_lwpolyline(
                [(tx - 2, ty + 1), (tx, ty), (tx - 2, ty - 1)],
                dxfattribs={"layer": layer, "lineweight": 18}
            )
        elif tx < mid_x:
            msp.add_lwpolyline(
                [(tx + 2, ty + 1), (tx, ty), (tx + 2, ty - 1)],
                dxfattribs={"layer": layer, "lineweight": 18}
            )

        # Signal label at midpoint
        if edge["label"]:
            lx_e = (mid_x + tx) / 2
            msp.add_text(edge["label"], dxfattribs={
                "layer":  layer,
                "height": SMALL_TEXT,
                "insert": (lx_e, ty + 1.5),
            })

    return doc


# ─── BOM direct path ─────────────────────────────────────────────────────────

def bom_to_dxf(bom_path: str, name: str, output: str):
    """Generate DXF directly from BOM — runs bom_to_drawio in memory then converts."""
    import sys, os
    sys.path.insert(0, str(Path(__file__).parent))

    # Write a temp drawio file then convert
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".drawio", delete=False)
    tmp.close()

    try:
        # Import bom_to_drawio dynamically
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "bom_to_drawio",
            Path(__file__).parent / "bom_to_drawio.py"
        )
        mod = importlib.util.load_from_spec(spec)  # type: ignore
        spec.loader.exec_module(mod)

        bom = mod.load_bom(bom_path)
        xml = mod.build_drawio(bom, None, name)
        Path(tmp.name).write_text(xml, encoding="utf-8")

        devices, edges = parse_drawio(tmp.name)
        doc = build_dxf(devices, edges, name)
        doc.saveas(output)
        print(f"[Done] {output}  ({len(devices)} devices, {len(edges)} connections)")
    finally:
        os.unlink(tmp.name)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Convert draw.io AV schematic → DXF (AutoCAD)")
    parser.add_argument("--input",  help="Input .drawio file")
    parser.add_argument("--bom",    help="Input BOM .csv (alternative to --input)")
    parser.add_argument("--name",   default="AV Schematic", help="Drawing title")
    parser.add_argument("--output", required=True, help="Output .dxf file")
    args = parser.parse_args()

    if args.bom:
        print(f"[BOM]  {args.bom}")
        bom_to_dxf(args.bom, args.name, args.output)
        return

    if not args.input:
        parser.print_help()
        sys.exit(1)

    inp = Path(args.input)
    if not inp.exists():
        sys.exit(f"File not found: {args.input}")

    name = args.name or inp.stem.replace("-", " ").replace("_", " ").title()
    print(f"[Parse] {inp}")
    devices, edges = parse_drawio(str(inp))
    print(f"        {len(devices)} devices, {len(edges)} connections")

    print("[Build] Generating DXF...")
    doc = build_dxf(devices, edges, name)
    doc.saveas(args.output)

    out = Path(args.output)
    size_kb = out.stat().st_size // 1024
    print(f"[Done]  {out}  ({size_kb} KB)")
    print(f"        Open in AutoCAD, BricsCAD, DraftSight, or LibreCAD")
    print(f"        Layers: {', '.join(l for l,_,_ in LAYERS if l not in ('BORDER','TITLE','NOTES','DEVICES','DEVICE_TEXT'))}")


if __name__ == "__main__":
    main()
