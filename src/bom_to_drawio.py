#!/usr/bin/env python3
"""
BOM → draw.io AV Signal Flow Generator
---------------------------------------
Generates a .drawio file from a Bill of Materials CSV (or JSON) and optionally
live Cisco xStatus data.

Device shapes match the Fe-Lit/Drawio-AV-Design library style:
  - Swimlane containers with stacked port rows
  - Green header for Input sections, Orange for Output sections
  - Color-coded orthogonal connections per signal type

Usage:
  python3 bom_to_drawio.py --bom devices.csv --output schematic.drawio
  python3 bom_to_drawio.py --bom devices.csv --codec 192.168.1.100 --output schematic.drawio
  python3 bom_to_drawio.py --xstatus xstatus.xml --bom devices.csv --output schematic.drawio

Open output in:
  - draw.io desktop app (https://get.diagrams.net)
  - app.diagrams.net (browser, File > Open)
"""

import argparse
import csv
import html
import json
import re
import sys
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Optional

# ─── Signal type config ───────────────────────────────────────────────────────

SIGNAL_COLORS = {
    "hdmi":          "#d6b656",   # amber
    "sdi":           "#6d8764",   # olive green
    "displayport":   "#0070c0",   # blue
    "usb":           "#0070c0",   # blue
    "ethernet":      "#006EAF",   # teal blue
    "dante":         "#7030a0",   # purple
    "ndi":           "#e36c09",   # orange
    "avb":           "#833c00",   # brown
    "speaker-level": "#ff0000",   # red
    "analog-audio":  "#ff6600",   # orange-red
    "rf":            "#808080",   # grey
    "fiber":         "#00b0f0",   # light blue
    "hdbaset":       "#70ad47",   # green
    "rs422":         "#ffc000",   # yellow
    "gpio":          "#ffc000",   # yellow
    "power":         "#ff0000",   # red
}

SIGNAL_LABELS = {
    "hdmi":          "HDMI",
    "sdi":           "SDI",
    "displayport":   "DisplayPort",
    "usb":           "USB",
    "ethernet":      "Ethernet RJ45",
    "dante":         "Dante",
    "ndi":           "NDI",
    "avb":           "AVB",
    "speaker-level": "Speaker",
    "analog-audio":  "Analog Audio",
    "rf":            "RF",
    "fiber":         "Fiber",
    "hdbaset":       "HDBaseT",
    "rs422":         "RS-422",
    "gpio":          "GPIO",
    "power":         "Power",
}

# ─── Device type → port defaults ─────────────────────────────────────────────

DEVICE_TYPE_MAP = {
    "codec":             "video-conferencing",
    "camera":            "camera",
    "display":           "display",
    "monitor":           "display",
    "projector":         "display",
    "microphone":        "microphone",
    "mic":               "microphone",
    "speaker":           "speaker",
    "amplifier":         "amplifier",
    "amp":               "amplifier",
    "switcher":          "matrix-router",
    "switch":            "network-switch",
    "router":            "matrix-router",
    "pc":                "computer",
    "laptop":            "computer",
    "streaming-device":  "encoder",
    "encoder":           "encoder",
    "decoder":           "decoder",
    "ndi-device":        "encoder",
    "dante-device":      "audio-interface",
    "audio mixer":       "audio-interface",
    "audio interface":   "audio-interface",
    "dsp":               "audio-interface",
    "touch panel":       "control-panel",
    "touch-panel":       "control-panel",
    "control panel":     "control-panel",
    "network":           "audio-interface",
    "receiver":          "audio-interface",
    "wireless-mic":      "microphone",
    "wireless-receiver": "audio-interface",
    "dsp":               "audio-interface",
    "patch-panel":       "device",
    "ups":               "device",
    "generic":           "device",
}

DEFAULT_PORTS_BY_TYPE = {
    "video-conferencing": {
        "inputs":  [("HDMI In", "hdmi", 4), ("USB In", "usb", 2)],
        "outputs": [("HDMI Out", "hdmi", 2), ("USB Out", "usb", 2)],
        "info":    [("Ethernet RJ45", "ethernet")],
    },
    "camera": {
        "inputs":  [],
        "outputs": [("HDMI Out", "hdmi", 1), ("USB Out", "usb", 1)],
        "info":    [("Ethernet RJ45", "ethernet")],
    },
    "display": {
        "inputs":  [("HDMI In", "hdmi", 2)],
        "outputs": [],
        "info":    [("Ethernet RJ45", "ethernet")],
    },
    "microphone": {
        "inputs":  [],
        "outputs": [("USB Out", "usb", 1)],
        "info":    [("Ethernet RJ45", "ethernet")],
    },
    "speaker": {
        "inputs":  [("Speaker In", "speaker-level", 1)],
        "outputs": [],
        "info":    [],
    },
    "amplifier": {
        "inputs":  [("Dante In", "dante", 2)],
        "outputs": [("Speaker Out", "speaker-level", 8)],
        "info":    [("Ethernet RJ45", "ethernet")],
    },
    "audio-interface": {
        "inputs":  [("Dante", "dante", 4)],
        "outputs": [("Dante", "dante", 4)],
        "info":    [("Ethernet RJ45", "ethernet")],
    },
    "control-panel": {
        "inputs":  [],
        "outputs": [],
        "info":    [("Ethernet RJ45", "ethernet")],
    },
    "network-switch": {
        "inputs":  [],
        "outputs": [],
        "info":    [("RJ45", "ethernet", 24)],
    },
    "matrix-router": {
        "inputs":  [("HDMI In", "hdmi", 8)],
        "outputs": [("HDMI Out", "hdmi", 4)],
        "info":    [("Ethernet RJ45", "ethernet")],
    },
    "encoder": {
        "inputs":  [("HDMI In", "hdmi", 1)],
        "outputs": [],
        "info":    [("Ethernet RJ45", "ethernet")],
    },
    "decoder": {
        "inputs":  [],
        "outputs": [("HDMI Out", "hdmi", 1)],
        "info":    [("Ethernet RJ45", "ethernet")],
    },
    "computer": {
        "inputs":  [],
        "outputs": [("HDMI Out", "hdmi", 2), ("USB Out", "usb", 2)],
        "info":    [("Ethernet RJ45", "ethernet")],
    },
    "device": {
        "inputs":  [],
        "outputs": [],
        "info":    [("Ethernet RJ45", "ethernet")],
    },
}

# Signal flow row order (top to bottom)
ROLE_ORDER = [
    "camera",
    "computer",
    "microphone",
    "video-conferencing",
    "control-panel",
    "audio-interface",
    "amplifier",
    "display",
    "speaker",
    "matrix-router",
    "network-switch",
    "encoder",
    "decoder",
    "device",
]

# ─── ID helpers ───────────────────────────────────────────────────────────────

_id_counter = 10

def new_id() -> str:
    global _id_counter
    _id_counter += 1
    return str(_id_counter)

# ─── XML helpers ──────────────────────────────────────────────────────────────

def h(s: str) -> str:
    """HTML-escape a string for XML attribute values."""
    return html.escape(str(s), quote=True)

# ─── Device block builder ────────────────────────────────────────────────────

ROW_HEIGHT   = 26    # px per port row
HEADER_H     = 40    # px for device name header
SECTION_H    = 26    # px for section header (Input/Output)
INFO_ROW_H   = 26
DEVICE_W     = 160   # px device width

def port_rows_from_bom(device: dict, device_type: str) -> dict:
    """Build port row config from BOM columns or fall back to defaults."""
    port_keys = ["hdmi_in","hdmi_out","usb_in","usb_out","ethernet_ports",
                 "sdi_in","sdi_out","dante_ports","ndi_ports",
                 "displayport_in","displayport_out","speaker_out","speaker_in","rf_in"]
    has_explicit = any(device.get(k) for k in port_keys)

    if has_explicit:
        inputs, outputs, info = [], [], []
        def add(lst, count_key, label, sig, count_default=0):
            c = int(device.get(count_key, count_default) or 0)
            if c > 0:
                lst.append((label, sig, c))

        add(inputs,  "hdmi_in",           "HDMI In",       "hdmi")
        add(inputs,  "usb_in",            "USB In",        "usb")
        add(inputs,  "sdi_in",            "SDI In",        "sdi")
        add(inputs,  "displayport_in",    "DP In",         "displayport")
        add(inputs,  "hdbaset_in",        "HDBaseT In",    "hdbaset")
        add(inputs,  "speaker_in",        "Speaker In",    "speaker-level")
        add(inputs,  "analog_audio_in",   "Analog In",     "analog-audio")
        add(inputs,  "rf_in",             "RF In",         "rf")
        add(inputs,  "fiber_ports",       "Fiber",         "fiber")
        add(inputs,  "rs422_ports",       "RS-422",        "rs422")
        add(inputs,  "gpio_ports",        "GPIO",          "gpio")
        add(outputs, "hdmi_out",          "HDMI Out",      "hdmi")
        add(outputs, "usb_out",           "USB Out",       "usb")
        add(outputs, "sdi_out",           "SDI Out",       "sdi")
        add(outputs, "displayport_out",   "DP Out",        "displayport")
        add(outputs, "hdbaset_out",       "HDBaseT Out",   "hdbaset")
        add(outputs, "speaker_out",       "Speaker Out",   "speaker-level")
        add(outputs, "analog_audio_out",  "Analog Out",    "analog-audio")
        add(info,    "ethernet_ports",    "Ethernet RJ45", "ethernet")
        add(info,    "dante_ports",       "Dante",         "dante")
        add(info,    "ndi_ports",         "NDI",           "ndi")
        return {"inputs": inputs, "outputs": outputs, "info": info}
    else:
        cfg = DEFAULT_PORTS_BY_TYPE.get(device_type, DEFAULT_PORTS_BY_TYPE["device"])
        return {
            "inputs":  list(cfg.get("inputs", [])),
            "outputs": list(cfg.get("outputs", [])),
            "info":    list(cfg.get("info", [])),
        }


def build_device_xml(label: str, model: str, serial: str, notes: str,
                     device_type: str, port_cfg: dict,
                     x: int, y: int) -> tuple[str, dict]:
    """
    Build draw.io XML for one device swimlane.
    Returns (xml_string, port_id_map) where port_id_map maps
    "signal:direction:index" -> cell_id for edge wiring.
    """
    port_id_map: dict = {}  # "signal:in:0" -> cell_id
    cells = []
    container_id = new_id()

    # Calculate total height
    n_inputs  = sum(c for _, _, c in port_cfg["inputs"])
    n_outputs = sum(c for _, _, c in port_cfg["outputs"])
    n_info    = sum(c if isinstance(c, int) else 1 for item in port_cfg["info"]
                    for c in ([item[2]] if len(item) == 3 else [1]))
    input_block_h  = (SECTION_H + n_inputs * ROW_HEIGHT)  if port_cfg["inputs"]  else 0
    output_block_h = (SECTION_H + n_outputs * ROW_HEIGHT) if port_cfg["outputs"] else 0

    # Info rows (not in a section, just plain rows)
    info_h = 0
    for item in port_cfg["info"]:
        if len(item) == 3:
            _, _, count = item
            info_h += int(count) * INFO_ROW_H
        else:
            info_h += INFO_ROW_H

    total_h = HEADER_H + info_h + input_block_h + output_block_h
    if notes:
        total_h += INFO_ROW_H

    # Container cell
    info_line = h(model) if model else ""
    cells.append(
        f'<mxCell id="{container_id}" value="{h(label)}" '
        f'style="swimlane;fontStyle=1;childLayout=stackLayout;horizontal=1;'
        f'startSize={HEADER_H};fillColor=#f5f5f5;horizontalStack=0;resizeParent=1;'
        f'resizeParentMax=0;resizeLast=0;collapsible=1;marginBottom=0;html=1;'
        f'fontSize=13;points=[];strokeColor=default;rounded=1;swimlaneLine=1;'
        f'fontColor=#333333;strokeWidth=2;swimlaneBody=0;absoluteArcSize=1;arcSize=10;" '
        f'vertex="1" parent="1">'
        f'<mxGeometry x="{x}" y="{y}" width="{DEVICE_W}" height="{total_h}" as="geometry"/>'
        f'</mxCell>'
    )

    cur_y = HEADER_H

    # Model/info row
    if model:
        rid = new_id()
        cells.append(
            f'<mxCell id="{rid}" value="{h(model)}" '
            f'style="text;strokeColor=default;fillColor=#f5f5f5;align=left;verticalAlign=top;'
            f'spacingLeft=4;spacingRight=4;overflow=hidden;rotatable=0;'
            f'points=[[0,0.5],[1,0.5]];portConstraint=eastwest;whiteSpace=wrap;html=1;fontColor=#333333;" '
            f'vertex="1" parent="{container_id}">'
            f'<mxGeometry y="{cur_y}" width="{DEVICE_W}" height="{INFO_ROW_H}" as="geometry"/>'
            f'</mxCell>'
        )
        cur_y += INFO_ROW_H

    # Serial/notes row
    if notes:
        nid = new_id()
        cells.append(
            f'<mxCell id="{nid}" value="{h(notes)}" '
            f'style="text;strokeColor=default;fillColor=#f5f5f5;align=left;verticalAlign=top;'
            f'spacingLeft=4;spacingRight=4;overflow=hidden;rotatable=0;'
            f'points=[[0,0.5],[1,0.5]];portConstraint=eastwest;whiteSpace=wrap;html=1;fontColor=#333333;" '
            f'vertex="1" parent="{container_id}">'
            f'<mxGeometry y="{cur_y}" width="{DEVICE_W}" height="{INFO_ROW_H}" as="geometry"/>'
            f'</mxCell>'
        )
        cur_y += INFO_ROW_H

    # Info ports (ethernet, dante etc — shown as neutral rows connecting both sides)
    for item in port_cfg["info"]:
        if len(item) == 3:
            port_label, sig, count = item
            count = int(count)
        else:
            port_label, sig = item[0], item[1]
            count = 1
        for i in range(count):
            pid = new_id()
            row_label = f"{port_label} {i+1}" if count > 1 else port_label
            cells.append(
                f'<mxCell id="{pid}" value="{h(row_label)}" '
                f'style="text;strokeColor=default;fillColor=default;align=center;verticalAlign=top;'
                f'spacingLeft=4;spacingRight=4;overflow=hidden;rotatable=0;'
                f'points=[[0,0.5],[1,0.5]];portConstraint=eastwest;whiteSpace=wrap;html=1;" '
                f'vertex="1" parent="{container_id}">'
                f'<mxGeometry y="{cur_y}" width="{DEVICE_W}" height="{ROW_HEIGHT}" as="geometry"/>'
                f'</mxCell>'
            )
            port_id_map[f"{sig}:bi:{i}"] = pid
            cur_y += ROW_HEIGHT

    # Input section
    if port_cfg["inputs"]:
        sec_id = new_id()
        cells.append(
            f'<mxCell id="{sec_id}" value="Input" '
            f'style="swimlane;fontStyle=0;childLayout=stackLayout;horizontal=1;startSize={SECTION_H};'
            f'fillColor=#d5e8d4;horizontalStack=0;resizeParent=1;resizeParentMax=0;resizeLast=0;'
            f'collapsible=0;marginBottom=0;html=1;rounded=0;swimlaneFillColor=default;points=[];'
            f'strokeColor=default;" '
            f'vertex="1" parent="{container_id}">'
            f'<mxGeometry y="{cur_y}" width="{DEVICE_W}" height="{SECTION_H + n_inputs * ROW_HEIGHT}" as="geometry">'
            f'<mxRectangle y="{n_inputs * ROW_HEIGHT}" width="{DEVICE_W}" height="30" as="alternateBounds"/>'
            f'</mxGeometry>'
            f'</mxCell>'
        )
        sec_y = SECTION_H
        cur_y += SECTION_H + n_inputs * ROW_HEIGHT
        in_idx: dict = defaultdict(int)
        for port_label, sig, count in port_cfg["inputs"]:
            count = int(count)
            for i in range(count):
                pid = new_id()
                row_label = f"{port_label} {i+1}" if count > 1 else port_label
                cells.append(
                    f'<mxCell id="{pid}" value="{h(row_label)}" '
                    f'style="text;strokeColor=default;fillColor=none;align=left;verticalAlign=top;'
                    f'spacingLeft=4;spacingRight=4;overflow=hidden;rotatable=0;'
                    f'points=[[0,0.5],[1,0.5]];portConstraint=eastwest;whiteSpace=wrap;html=1;" '
                    f'vertex="1" parent="{sec_id}">'
                    f'<mxGeometry y="{sec_y}" width="{DEVICE_W}" height="{ROW_HEIGHT}" as="geometry"/>'
                    f'</mxCell>'
                )
                idx = in_idx[sig]
                port_id_map[f"{sig}:in:{idx}"] = pid
                in_idx[sig] += 1
                sec_y += ROW_HEIGHT

    # Output section
    if port_cfg["outputs"]:
        sec_id = new_id()
        cells.append(
            f'<mxCell id="{sec_id}" value="Output" '
            f'style="swimlane;fontStyle=0;childLayout=stackLayout;horizontal=1;startSize={SECTION_H};'
            f'fillColor=#ffe6cc;horizontalStack=0;resizeParent=1;resizeParentMax=0;resizeLast=0;'
            f'collapsible=0;marginBottom=0;html=1;rounded=0;swimlaneFillColor=default;'
            f'strokeColor=default;connectable=0;" '
            f'vertex="1" parent="{container_id}">'
            f'<mxGeometry y="{cur_y}" width="{DEVICE_W}" height="{SECTION_H + n_outputs * ROW_HEIGHT}" as="geometry">'
            f'<mxRectangle y="{n_outputs * ROW_HEIGHT}" width="{DEVICE_W}" height="30" as="alternateBounds"/>'
            f'</mxGeometry>'
            f'</mxCell>'
        )
        sec_y = SECTION_H
        out_idx: dict = defaultdict(int)
        for port_label, sig, count in port_cfg["outputs"]:
            count = int(count)
            for i in range(count):
                pid = new_id()
                row_label = f"{port_label} {i+1}" if count > 1 else port_label
                cells.append(
                    f'<mxCell id="{pid}" value="{h(row_label)}" '
                    f'style="text;strokeColor=default;fillColor=none;align=right;verticalAlign=top;'
                    f'spacingLeft=4;spacingRight=4;overflow=hidden;rotatable=0;'
                    f'points=[[0,0.5],[1,0.5]];portConstraint=eastwest;whiteSpace=wrap;html=1;" '
                    f'vertex="1" parent="{sec_id}">'
                    f'<mxGeometry y="{sec_y}" width="{DEVICE_W}" height="{ROW_HEIGHT}" as="geometry"/>'
                    f'</mxCell>'
                )
                idx = out_idx[sig]
                port_id_map[f"{sig}:out:{idx}"] = pid
                out_idx[sig] += 1
                sec_y += ROW_HEIGHT

    xml = "\n".join(cells)
    return xml, port_id_map, total_h


# ─── Edge builder ─────────────────────────────────────────────────────────────

def build_edge(src_port_id: str, tgt_port_id: str, signal: str, label: str = "") -> str:
    color = SIGNAL_COLORS.get(signal, "#000000")
    sig_label = SIGNAL_LABELS.get(signal, signal.upper())
    edge_label = label or sig_label
    eid = new_id()
    return (
        f'<mxCell id="{eid}" value="{h(edge_label)}" '
        f'style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;'
        f'exitX=1;exitY=0.5;exitDx=0;exitDy=0;entryX=0;entryY=0.5;entryDx=0;entryDy=0;'
        f'strokeColor={color};strokeWidth=2;fontColor={color};fontSize=9;'
        f'labelBackgroundColor=none;labelBorderColor=none;align=center;" '
        f'edge="1" source="{src_port_id}" target="{tgt_port_id}" parent="1">'
        f'<mxGeometry relative="1" as="geometry"/>'
        f'</mxCell>'
    )


# ─── BOM parsing ──────────────────────────────────────────────────────────────

def parse_bom_csv(path: str) -> list[dict]:
    devices = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        # Skip comment lines starting with #
        lines = [l for l in f.readlines() if not l.strip().startswith("#")]
    import io
    reader = csv.DictReader(io.StringIO("".join(lines)))
    reader.fieldnames = [h2.strip().lower().replace(" ", "_")
                         for h2 in (reader.fieldnames or [])]
    for row in reader:
        row = {k.strip().lower().replace(" ", "_"): v.strip()
               for k, v in row.items() if k}

        # ── Name aliases ──────────────────────────────────────────────────
        name = row.get("name") or row.get("device_name") or row.get("device") or ""
        if not name:
            continue
        row["name"] = name

        # ── Type aliases ──────────────────────────────────────────────────
        if not row.get("device_type"):
            row["device_type"] = row.get("type", "generic")

        # ── Model aliases ─────────────────────────────────────────────────
        if not row.get("model"):
            row["model"] = row.get("model_number") or row.get("model_no") or ""

        # ── Serial → notes ────────────────────────────────────────────────
        serial = row.get("serial") or row.get("serial_number") or row.get("sn") or ""
        notes  = row.get("notes", "")
        if serial:
            row["notes"] = f"S/N: {serial}" + (f"  {notes}" if notes else "")

        # ── Network info → notes append ───────────────────────────────────
        ip  = row.get("ip_address") or row.get("ip") or ""
        mac = row.get("mac_address") or row.get("mac") or ""
        if ip:
            row["hostname"] = row.get("hostname", "")
            row["notes"] = (row.get("notes","") + f"  IP: {ip}").strip()
        if mac:
            row["notes"] = (row.get("notes","") + f"  MAC: {mac}").strip()

        # ── Port column aliases ───────────────────────────────────────────
        # hdbaset_in / hdbaset_out
        if not row.get("hdbaset_in"):
            row["hdbaset_in"] = row.get("hdbaset_ports_in", "0")
        if not row.get("hdbaset_out"):
            row["hdbaset_out"] = row.get("hdbaset_ports_out", "0")
        # analog audio
        if not row.get("analog_audio_in"):
            row["analog_audio_in"] = row.get("xlr_in", "0")
        if not row.get("analog_audio_out"):
            row["analog_audio_out"] = row.get("xlr_out", "0")
        # fiber
        if not row.get("fiber_ports"):
            row["fiber_ports"] = row.get("fiber", "0")

        qty = int(row.get("quantity", 1) or 1)
        for i in range(qty):
            d = dict(row)
            d["_label"] = f"{name} {i+1}" if qty > 1 else name
            d["_index"] = i
            devices.append(d)
    return devices


def parse_bom_json(path: str) -> list[dict]:
    with open(path) as f:
        data = json.load(f)
    items = data if isinstance(data, list) else data.get("devices", [])
    devices = []
    for item in items:
        qty = int(item.get("quantity", 1))
        name = item.get("name", item.get("label", "Device"))
        for i in range(qty):
            d = dict(item)
            d["_label"] = f"{name} {i+1}" if qty > 1 else name
            if not d.get("device_type"):
                d["device_type"] = d.get("type", "generic")
            devices.append(d)
    return devices


def load_bom(path: str) -> list[dict]:
    p = Path(path)
    if p.suffix.lower() == ".json":
        return parse_bom_json(path)
    return parse_bom_csv(path)


# ─── Cisco xStatus ────────────────────────────────────────────────────────────

def fetch_xstatus_http(ip: str, username: str = "admin", password: str = "") -> str:
    import urllib.request, urllib.error, base64
    url = f"http://{ip}/getxml?location=/Status"
    creds = base64.b64encode(f"{username}:{password}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {creds}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:
        url = f"https://{ip}/getxml?location=/Status"
        req = urllib.request.Request(url, headers={"Authorization": f"Basic {creds}"})
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            return r.read().decode("utf-8", errors="replace")


def parse_xstatus(raw: str) -> dict:
    info = {
        "codec_name": None, "codec_model": None, "codec_sw": None,
        "peripherals": [], "video_inputs": [], "video_outputs": [],
        "network": {},
    }
    if "<Status>" in raw or "<?xml" in raw.lower():
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(raw)
            def find(path):
                el = root.find(path)
                return el.text.strip() if el is not None and el.text else None
            info["codec_name"]  = find(".//UserInterface/ContactInfo/Name")
            info["codec_model"] = find(".//SystemUnit/ProductId")
            info["codec_sw"]    = find(".//SystemUnit/Software/Version")
            info["network"]["ip"] = find(".//Network/IPv4/Address")
            for p in root.findall(".//Peripherals/ConnectedDevice"):
                status = p.find("Status")
                ptype  = p.find("Type")
                name   = p.find("Name")
                info["peripherals"].append({
                    "type":   (ptype.text.strip() if ptype is not None and ptype.text else "Unknown"),
                    "name":   (name.text.strip()  if name  is not None and name.text  else "Peripheral"),
                    "status": (status.text.strip() if status is not None and status.text else "Unknown"),
                })
            for vi in root.findall(".//Video/Input/Connector"):
                sig = vi.find("SignalState")
                info["video_inputs"].append({
                    "id": vi.get("item",""), "signal": sig.text.strip() if sig is not None and sig.text else "None"
                })
            for vo in root.findall(".//Video/Output/Connector"):
                sig = vo.find("SignalState")
                info["video_outputs"].append({
                    "id": vo.get("item",""), "signal": sig.text.strip() if sig is not None and sig.text else "None"
                })
        except Exception as e:
            print(f"[xStatus] XML parse error: {e}", file=sys.stderr)
    else:
        def fv(pat): m=re.search(pat,raw,re.I|re.M); return m.group(1).strip() if m else None
        info["codec_model"] = fv(r"SystemUnit ProductId:\s*(.+)")
        info["codec_name"]  = fv(r"UserInterface ContactInfo Name:\s*(.+)")
        info["codec_sw"]    = fv(r"SystemUnit Software Version:\s*(.+)")
        info["network"]["ip"] = fv(r"Network \d+ IPv4 Address:\s*(.+)")
        blocks: dict = {}
        for m in re.finditer(r"Peripherals ConnectedDevice (\d+) (\w+(?:\s\w+)*):\s*(.+)", raw, re.I):
            idx, key, val = m.group(1), m.group(2).strip(), m.group(3).strip()
            blocks.setdefault(idx, {})[key] = val
        for idx, block in sorted(blocks.items()):
            info["peripherals"].append({
                "type":   block.get("Type", "Unknown"),
                "name":   block.get("Name", f"Peripheral {idx}"),
                "status": block.get("Status", "Unknown"),
            })
    return info


# ─── Main builder ─────────────────────────────────────────────────────────────

def build_drawio(bom_devices: list[dict], xstatus: Optional[dict], name: str) -> str:
    """Build the full draw.io XML."""

    all_cells_xml = []   # device + edge XML fragments
    # node_info: list of {device_type, port_id_map, x, y, label}
    node_info: list[dict] = []

    COL_W   = 200   # horizontal gap between devices
    ROW_GAP = 60    # vertical gap between rows
    PAGE_W  = 1600
    START_Y = 80

    # ── Group devices by type ──────────────────────────────────────────────
    type_groups: dict = defaultdict(list)

    def add_device(label, device_type, port_cfg, model="", serial="", notes=""):
        xml, pid_map, h_px = build_device_xml(label, model, "", notes, device_type, port_cfg, 0, 0)
        entry = {"label": label, "device_type": device_type,
                 "pid_map": pid_map, "h": h_px, "xml": xml, "x": 0, "y": 0}
        type_groups[device_type].append(entry)

    # xStatus codec + peripherals
    if xstatus:
        codec_label = xstatus.get("codec_name") or xstatus.get("codec_model") or "Cisco Codec"
        n_vi = len(xstatus.get("video_inputs", []))
        n_vo = len(xstatus.get("video_outputs", []))
        codec_cfg = {
            "inputs":  [("HDMI In", "hdmi", max(n_vi, 1))],
            "outputs": [("HDMI Out", "hdmi", max(n_vo, 1))],
            "info":    [("Ethernet RJ45", "ethernet")],
        }
        notes = ""
        if xstatus.get("codec_sw"): notes += f"SW: {xstatus['codec_sw']}"
        if xstatus.get("network", {}).get("ip"): notes += f"  IP: {xstatus['network']['ip']}"
        add_device(codec_label, "video-conferencing", codec_cfg, xstatus.get("codec_model",""), notes=notes.strip())

        for p in xstatus.get("peripherals", []):
            if p["status"].lower() in ("disconnected", "lost"):
                continue
            ptype = p["type"].lower()
            if "camera" in ptype:
                dt = "camera"
            elif "touch" in ptype or "nav" in ptype:
                dt = "control-panel"
            elif "mic" in ptype or "audio" in ptype:
                dt = "microphone"
            else:
                dt = "device"
            pcfg = DEFAULT_PORTS_BY_TYPE.get(dt, DEFAULT_PORTS_BY_TYPE["device"])
            add_device(p["name"], dt, dict(pcfg), notes=f"Status: {p['status']}")

    # BOM devices
    for device in bom_devices:
        label       = device.get("_label", device.get("name", "Device"))
        raw_dtype   = device.get("device_type", device.get("type", "generic")).lower()
        device_type = DEVICE_TYPE_MAP.get(raw_dtype, raw_dtype)
        model       = device.get("model", "")
        notes       = device.get("notes", "")
        port_cfg    = port_rows_from_bom(device, device_type)
        add_device(label, device_type, port_cfg, model=model, notes=notes)

    # ── Assign positions ───────────────────────────────────────────────────
    placed = set()
    current_y = START_Y

    def place_group(dt):
        nonlocal current_y
        group = type_groups.get(dt, [])
        if not group:
            return
        placed.add(dt)
        count   = len(group)
        total_w = count * (DEVICE_W + COL_W) - COL_W
        start_x = max(40, (PAGE_W - total_w) // 2)
        max_h   = max(e["h"] for e in group)
        for col, entry in enumerate(group):
            entry["x"] = start_x + col * (DEVICE_W + COL_W)
            entry["y"] = current_y
        current_y += max_h + ROW_GAP

    for role in ROLE_ORDER:
        place_group(role)

    for dt in type_groups:
        if dt not in placed:
            place_group(dt)

    # ── Emit device XML with final positions ───────────────────────────────
    all_entries = []
    for dt in ROLE_ORDER:
        all_entries.extend(type_groups.get(dt, []))
    for dt, grp in type_groups.items():
        if dt not in placed:
            all_entries.extend(grp)

    for entry in all_entries:
        # Inject final x/y into xml (replace x="0" y="0")
        xml = entry["xml"].replace(f'x="0" y="0"', f'x="{entry["x"]}" y="{entry["y"]}"', 1)
        all_cells_xml.append(xml)

    # ── Auto-wire connections ──────────────────────────────────────────────
    def nodes_of_type(*types):
        return [e for e in all_entries if e["device_type"] in types]

    def first_port(entry, sig, direction, used):
        """Find first unused port id for a signal/direction."""
        pid_map = entry["pid_map"]
        if direction in ("out", "output"):
            keys = [k for k in pid_map if k.startswith(f"{sig}:out:") or k.startswith(f"{sig}:bi:")]
        elif direction in ("in", "input"):
            keys = [k for k in pid_map if k.startswith(f"{sig}:in:") or k.startswith(f"{sig}:bi:")]
        else:
            keys = [k for k in pid_map if k.startswith(f"{sig}:")]
        keys.sort()
        for k in keys:
            pid = pid_map[k]
            if pid not in used:
                return pid
        return None

    used: set = set()

    def wire(src_entry, src_sig, src_dir, tgt_entry, tgt_sig, tgt_dir, label=""):
        if not src_entry or not tgt_entry:
            return
        sp = first_port(src_entry, src_sig, src_dir, used)
        tp = first_port(tgt_entry, tgt_sig, tgt_dir, used)
        if sp and tp:
            all_cells_xml.append(build_edge(sp, tp, src_sig, label))
            used.add(sp)
            used.add(tp)

    codecs   = nodes_of_type("video-conferencing")
    cameras  = nodes_of_type("camera")
    displays = nodes_of_type("display")
    mics     = nodes_of_type("microphone")
    amps     = nodes_of_type("amplifier")
    speakers = nodes_of_type("speaker")
    panels   = nodes_of_type("control-panel")
    audio_ifs = nodes_of_type("audio-interface")
    pcs      = nodes_of_type("computer")
    switches = nodes_of_type("network-switch")

    codec = codecs[0] if codecs else None

    # Cameras → Codec (HDMI)
    for cam in cameras:
        wire(cam, "hdmi", "out", codec, "hdmi", "in", "HDMI")

    # Cameras → Codec (USB — PTZ P60)
    for cam in cameras:
        wire(cam, "usb", "out", codec, "usb", "in", "USB")

    # Codec → Displays (HDMI)
    for disp in displays:
        wire(codec, "hdmi", "out", disp, "hdmi", "in", "HDMI")

    # PCs → Codec (content share)
    for pc in pcs:
        wire(pc, "hdmi", "out", codec, "hdmi", "in", "HDMI")

    # Ceiling mics → Codec (USB)
    for mic in mics:
        wire(mic, "usb", "out", codec, "usb", "in", "USB")

    # Dante hub = audio-interface with most dante ports
    dante_hub = sorted(audio_ifs,
                       key=lambda e: sum(1 for k in e["pid_map"] if "dante" in k),
                       reverse=True)[0] if audio_ifs else None

    # Other audio interfaces → Dante hub
    for ai in audio_ifs:
        if ai is dante_hub:
            continue
        wire(ai, "dante", "bi", dante_hub, "dante", "bi", "Dante")

    # Dante hub → Amps
    for amp in amps:
        wire(dante_hub, "dante", "bi", amp, "dante", "in", "Dante")

    # Codec → Dante hub (program audio)
    if codec and dante_hub:
        wire(codec, "dante", "bi", dante_hub, "dante", "bi", "Dante")

    # Amps → Speakers
    for amp in amps:
        for spk in speakers:
            wire(amp, "speaker-level", "out", spk, "speaker-level", "in", "Speaker")

    # Touch panels → Codec
    for tp in panels:
        wire(tp, "ethernet", "bi", codec, "ethernet", "bi", "Ethernet")

    # All → Switch
    if switches:
        sw = switches[0]
        for entry in all_entries:
            if entry in switches:
                continue
            wire(entry, "ethernet", "bi", sw, "ethernet", "bi", "Ethernet")

    # ── Assemble final XML ─────────────────────────────────────────────────
    body = "\n".join(all_cells_xml)
    return f'''<mxGraphModel dx="1422" dy="762" grid="1" gridSize="10" guides="1" tooltips="1"
  connect="1" arrows="1" fold="1" page="1" pageScale="1"
  pageWidth="1654" pageHeight="1169" math="0" shadow="0">
  <root>
    <mxCell id="0" />
    <mxCell id="1" parent="0" />
{body}
  </root>
</mxGraphModel>'''


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate draw.io AV schematic from BOM + Cisco xStatus")
    parser.add_argument("--bom",       help="BOM file (.csv or .json)")
    parser.add_argument("--codec",     help="Cisco codec IP (live xStatus pull)")
    parser.add_argument("--xstatus",   help="Saved xStatus file")
    parser.add_argument("--username",  default="admin")
    parser.add_argument("--password",  default="")
    parser.add_argument("--output",    default="schematic.drawio")
    parser.add_argument("--name",      default="AV Schematic")
    args = parser.parse_args()

    if not args.bom and not args.codec and not args.xstatus:
        parser.print_help(); sys.exit(1)

    bom_devices = []
    if args.bom:
        print(f"[BOM] Loading {args.bom}...")
        bom_devices = load_bom(args.bom)
        print(f"[BOM] {len(bom_devices)} devices")

    xstatus = None
    if args.codec:
        print(f"[xStatus] Connecting to {args.codec}...")
        raw = fetch_xstatus_http(args.codec, args.username, args.password)
        xstatus = parse_xstatus(raw)
        print(f"[xStatus] {xstatus.get('codec_model')} — {len(xstatus['peripherals'])} peripherals")
    elif args.xstatus:
        print(f"[xStatus] Loading {args.xstatus}...")
        raw = Path(args.xstatus).read_text(encoding="utf-8", errors="replace")
        xstatus = parse_xstatus(raw)

    print("[Build] Generating draw.io XML...")
    xml = build_drawio(bom_devices, xstatus, args.name)

    out = Path(args.output)
    out.write_text(xml, encoding="utf-8")
    print(f"[Done] {out}")
    print(f"       Open in draw.io desktop app or https://app.diagrams.net")


if __name__ == "__main__":
    main()
