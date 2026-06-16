#!/usr/bin/env python3
"""
draw.io → EasySchematic Converter
-----------------------------------
Reads a .drawio file produced by bom_to_drawio.py (or manually edited in draw.io)
and converts it to an EasySchematic-compatible .json file.

Mapping:
  draw.io swimlane container  → EasySchematic DeviceNode
  Input section child rows    → input ports
  Output section child rows   → output ports
  Info rows (neutral)         → bidirectional ports
  Edges (by strokeColor)      → ConnectionEdge with signalType
  x/y from mxGeometry         → node.position

Usage:
  python3 src/drawio_to_easyschematic.py --input schematic.drawio --output schematic.json
  python3 src/drawio_to_easyschematic.py --input schematic.drawio --output schematic.json --rooms
"""

import argparse
import json
import re
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = 41  # EasySchematic CURRENT_SCHEMA_VERSION

# ─── Signal color → type reverse map ─────────────────────────────────────────

COLOR_TO_SIGNAL = {
    "#d6b656": "hdmi",
    "#6d8764": "sdi",
    "#0070c0": "usb",        # also displayport — label wins
    "#006eaf": "ethernet",
    "#7030a0": "dante",
    "#e36c09": "ndi",
    "#833c00": "avb",
    "#ff0000": "speaker-level",
    "#ff6600": "analog-audio",
    "#808080": "rf",
    "#00b0f0": "fiber",
    "#70ad47": "hdbaset",
    "#ffc000": "rs422",
    "#0070c0": "displayport",
}

LABEL_TO_SIGNAL = {
    "hdmi":          "hdmi",
    "sdi":           "sdi",
    "usb":           "usb",
    "ethernet":      "ethernet",
    "ethernet rj45": "ethernet",
    "rj45":          "ethernet",
    "dante":         "dante",
    "ndi":           "ndi",
    "avb":           "avb",
    "speaker":       "speaker-level",
    "speaker-level": "speaker-level",
    "analog audio":  "analog-audio",
    "analog-audio":  "analog-audio",
    "rf":            "rf",
    "fiber":         "fiber",
    "hdbaset":       "hdbaset",
    "displayport":   "displayport",
    "dp":            "displayport",
    "rs-422":        "rs422",
    "rs422":         "rs422",
    "gpio":          "gpio",
    "control":       "ethernet",
    "network":       "ethernet",
}

# draw.io deviceType label heuristics → EasySchematic deviceType
LABEL_TO_DEVICE_TYPE = {
    "codec":            "video-conferencing",
    "webex":            "video-conferencing",
    "room kit":         "video-conferencing",
    "camera":           "camera",
    "ptz":              "camera",
    "display":          "display",
    "monitor":          "display",
    "projector":        "display",
    "microphone":       "microphone",
    "mic":              "microphone",
    "speaker":          "speaker",
    "amplifier":        "amplifier",
    "amp":              "amplifier",
    "amp8":             "amplifier",
    "qsc":              "amplifier",
    "switcher":         "matrix-router",
    "switch":           "network-switch",
    "catalyst":         "network-switch",
    "navigator":        "control-panel",
    "touch":            "control-panel",
    "panel":            "control-panel",
    "dante":            "audio-interface",
    "ulxd":             "audio-interface",
    "receiver":         "audio-interface",
    "q-sys":            "audio-interface",
    "pc":               "computer",
    "laptop":           "computer",
    "encoder":          "encoder",
    "decoder":          "decoder",
}

# EasySchematic connector types by signal
SIGNAL_TO_CONNECTOR = {
    "hdmi":          "hdmi",
    "sdi":           "bnc",
    "displayport":   "displayport",
    "usb":           "usb-c",
    "ethernet":      "rj45",
    "dante":         "rj45",
    "ndi":           "rj45",
    "avb":           "rj45",
    "speaker-level": "speakon",
    "analog-audio":  "xlr-3",
    "rf":            "bnc",
    "fiber":         "lc",
    "hdbaset":       "rj45",
    "rs422":         "db9",
    "gpio":          "phoenix",
}


def new_id() -> str:
    return str(uuid.uuid4())


def infer_device_type(label: str) -> str:
    label_l = label.lower()
    for key, dtype in LABEL_TO_DEVICE_TYPE.items():
        if key in label_l:
            return dtype
    return "device"


def signal_from_edge(cell: ET.Element) -> str:
    """Determine signal type from edge label or strokeColor."""
    label = (cell.get("value") or "").strip().lower()
    if label in LABEL_TO_SIGNAL:
        return LABEL_TO_SIGNAL[label]
    style = cell.get("style", "").lower()
    m = re.search(r"strokecolor=(#[0-9a-f]+)", style)
    if m:
        color = m.group(1).lower()
        if color in COLOR_TO_SIGNAL:
            return COLOR_TO_SIGNAL[color]
    return "ethernet"


def parse_drawio(path: str) -> tuple[list[dict], list[dict]]:
    """
    Parse a .drawio file.
    Returns (nodes, edges) in EasySchematic format.
    """
    tree = ET.parse(path)
    root = tree.getroot()

    # Handle both <mxGraphModel> root and <mxfile><diagram> wrapper
    if root.tag == "mxfile":
        diagram = root.find(".//mxGraphModel")
        if diagram is None:
            sys.exit("No mxGraphModel found in file")
        root = diagram

    cells = root.findall(".//mxCell")

    # Build lookup maps
    cell_by_id:  dict[str, ET.Element] = {c.get("id", ""): c for c in cells}
    parent_of:   dict[str, str]        = {c.get("id", ""): c.get("parent", "") for c in cells}
    label_of:    dict[str, str]        = {c.get("id", ""): (c.get("value") or "") for c in cells}

    # ── Identify device containers ─────────────────────────────────────────
    # A device container is a swimlane with childLayout=stackLayout at parent=1
    device_ids: set[str] = set()
    for c in cells:
        style = c.get("style", "")
        if ("swimlane" in style and "childLayout=stackLayout" in style
                and c.get("parent") == "1" and c.get("vertex") == "1"):
            device_ids.add(c.get("id", ""))

    # ── Identify section swimlanes (Input/Output headers) inside devices ───
    section_ids:      dict[str, str] = {}  # section_cell_id → parent device_id
    section_type:     dict[str, str] = {}  # section_cell_id → "input"|"output"|"info"
    section_y:        dict[str, float] = {}

    for c in cells:
        style = c.get("style", "")
        cid   = c.get("id", "")
        pid   = c.get("parent", "")
        if pid in device_ids and "childLayout=stackLayout" in style and "swimlane" in style:
            lbl = (c.get("value") or "").strip().lower()
            if "input" in lbl:
                section_type[cid] = "input"
            elif "output" in lbl:
                section_type[cid] = "output"
            else:
                section_type[cid] = "info"
            section_ids[cid] = pid
            geo = c.find("mxGeometry")
            section_y[cid] = float(geo.get("y", 0)) if geo is not None else 0

    # ── Build port lists for each device ──────────────────────────────────
    # port_rows[device_id] = list of {id, label, direction, signal, connector_type}
    port_rows: dict[str, list] = {did: [] for did in device_ids}

    # Track cell → (device_id, direction, signal) for edge endpoint resolution
    cell_port_info: dict[str, dict] = {}

    for c in cells:
        cid   = c.get("id", "")
        pid   = c.get("parent", "")
        style = c.get("style", "")
        if "portConstraint=eastwest" not in style:
            continue
        if "swimlane" in style:
            continue  # skip section headers

        label = (c.get("value") or "").strip()
        if not label:
            continue

        # Determine which device this port belongs to
        device_id = None
        direction = "bidirectional"

        if pid in device_ids:
            # Direct child of device (info rows)
            device_id = pid
            direction = "bidirectional"
        elif pid in section_ids:
            # Child of a section swimlane
            device_id = section_ids[pid]
            stype     = section_type.get(pid, "info")
            direction = "input" if stype == "input" else (
                        "output" if stype == "output" else "bidirectional")
        else:
            # Grand-child — walk up
            gpid = parent_of.get(pid, "")
            if gpid in device_ids:
                device_id = gpid
                stype     = section_type.get(pid, "info")
                direction = "input" if stype == "input" else (
                            "output" if stype == "output" else "bidirectional")
            elif gpid in section_ids:
                device_id = section_ids[gpid]
                stype     = section_type.get(gpid, "info")
                direction = "input" if stype == "input" else (
                            "output" if stype == "output" else "bidirectional")

        if not device_id:
            continue

        # Infer signal from label
        signal = LABEL_TO_SIGNAL.get(label.lower())
        if not signal:
            for key, sig in LABEL_TO_SIGNAL.items():
                if key in label.lower():
                    signal = sig
                    break
        if not signal:
            signal = "ethernet"

        connector = SIGNAL_TO_CONNECTOR.get(signal, "rj45")
        port_id   = new_id()

        port_rows[device_id].append({
            "id":            port_id,
            "label":         label,
            "direction":     direction,
            "signalType":    signal,
            "connectorType": connector,
        })

        cell_port_info[cid] = {
            "device_id": device_id,
            "port_id":   port_id,
            "signal":    signal,
        }

    # ── Build EasySchematic nodes ──────────────────────────────────────────
    nodes: list[dict] = []
    device_node_id: dict[str, str] = {}  # device_cell_id → node uuid

    for did in device_ids:
        c = cell_by_id.get(did)
        if c is None:
            continue
        geo   = c.find("mxGeometry")
        x     = float(geo.get("x", 0)) if geo is not None else 0
        y     = float(geo.get("y", 0)) if geo is not None else 0
        label = (c.get("value") or "Device").strip()
        dtype = infer_device_type(label)
        ports = port_rows.get(did, [])

        node_id = new_id()
        device_node_id[did] = node_id

        nodes.append({
            "id":       node_id,
            "type":     "device",
            "position": {"x": round(x), "y": round(y)},
            "data": {
                "label":      label,
                "deviceType": dtype,
                "model":      label,
                "ports":      ports,
            },
        })

    # ── Build EasySchematic edges ──────────────────────────────────────────
    edges: list[dict] = []

    for c in cells:
        if c.get("edge") != "1":
            continue

        src_cell = c.get("source", "")
        tgt_cell = c.get("target", "")
        if not src_cell or not tgt_cell:
            continue

        signal = signal_from_edge(c)

        # Resolve source port
        src_port_id = None
        src_node_id = None
        if src_cell in cell_port_info:
            info = cell_port_info[src_cell]
            src_port_id = info["port_id"]
            src_node_id = device_node_id.get(info["device_id"])
        else:
            # Edge connects directly to a device container
            src_node_id = device_node_id.get(src_cell)
            if src_node_id:
                # Find first matching output port
                for port in port_rows.get(src_cell, []):
                    if port["signalType"] == signal and port["direction"] in ("output","bidirectional"):
                        src_port_id = port["id"]
                        break
                if not src_port_id and port_rows.get(src_cell):
                    src_port_id = port_rows[src_cell][0]["id"]

        # Resolve target port
        tgt_port_id = None
        tgt_node_id = None
        if tgt_cell in cell_port_info:
            info = cell_port_info[tgt_cell]
            tgt_port_id = info["port_id"]
            tgt_node_id = device_node_id.get(info["device_id"])
        else:
            tgt_node_id = device_node_id.get(tgt_cell)
            if tgt_node_id:
                for port in port_rows.get(tgt_cell, []):
                    if port["signalType"] == signal and port["direction"] in ("input","bidirectional"):
                        tgt_port_id = port["id"]
                        break
                if not tgt_port_id and port_rows.get(tgt_cell):
                    tgt_port_id = port_rows[tgt_cell][0]["id"]

        if not src_node_id or not tgt_node_id:
            continue

        cable_label = (c.get("value") or "").strip()

        edge: dict = {
            "id":     new_id(),
            "source": src_node_id,
            "target": tgt_node_id,
            "type":   "connection",
            "data":   {"signalType": signal},
        }
        if src_port_id:
            edge["sourceHandle"] = src_port_id
        if tgt_port_id:
            edge["targetHandle"] = tgt_port_id
        if cable_label:
            edge["data"]["label"] = cable_label

        edges.append(edge)

    return nodes, edges


def build_schematic(nodes: list[dict], edges: list[dict], name: str) -> dict:
    return {
        "version":           SCHEMA_VERSION,
        "name":              name,
        "nodes":             nodes,
        "edges":             edges,
        "customTemplates":   [],
        "cableNamingScheme": "type-prefix",
        "showCableIdLabels": True,
        "showLineJumps":     True,
        "autoRoute":         True,
        "titleBlock": {
            "showName":    name,
            "venue":       "",
            "designer":    "",
            "engineer":    "",
            "date":        __import__("datetime").date.today().isoformat(),
            "drawingTitle": f"{name} Signal Flow",
            "company":     "",
            "revision":    "A",
            "logo":        "",
            "customFields": [],
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Convert draw.io AV schematic → EasySchematic JSON")
    parser.add_argument("--input",  required=True, help="Input .drawio file")
    parser.add_argument("--output", required=True, help="Output .json file for EasySchematic")
    parser.add_argument("--name",   default="",    help="Schematic name (defaults to filename)")
    args = parser.parse_args()

    inp = Path(args.input)
    if not inp.exists():
        sys.exit(f"File not found: {args.input}")

    name = args.name or inp.stem.replace("-", " ").replace("_", " ").title()

    print(f"[Parse]  {inp}")
    nodes, edges = parse_drawio(str(inp))
    print(f"         {len(nodes)} devices, {len(edges)} connections")

    schematic = build_schematic(nodes, edges, name)

    out = Path(args.output)
    out.write_text(json.dumps(schematic, indent=2), encoding="utf-8")
    print(f"[Done]   {out}")
    print(f"         Open in EasySchematic → http://localhost:5173 → File > Open")


if __name__ == "__main__":
    main()
