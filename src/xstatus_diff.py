#!/usr/bin/env python3
"""
xStatus Diff — draw.io Schematic vs Cisco Codec Reality
---------------------------------------------------------
Compares devices in a draw.io schematic against peripherals reported by
Cisco xStatus and flags mismatches.

Categories:
  MATCHED               - device appears in both schematic and xStatus
  MISSING_FROM_SCHEMATIC - xStatus reports it but it is not in the drawio file
  EXTRA_IN_SCHEMATIC    - drawio has it but xStatus does not report it
  SIGNAL_MISMATCHES     - video input/output connector signal states that look wrong

Source options (pick one):
  --codec IP            Direct HTTP/HTTPS to codec (basic auth, on-prem/LAN)
  --xstatus FILE        Pre-saved xStatus XML or SSH text file (offline)
  --webex-device-id ID  Webex cloud API by device ID (bot/personal token)
  --webex-device NAME   Webex cloud API — find device by display name search

Usage:
  # Direct codec (on-prem / VPN)
  python3 src/xstatus_diff.py --input output/Boardroom_Pro.drawio \\
      --codec 192.168.1.100 --username admin --password cisco

  # Saved xStatus file
  python3 src/xstatus_diff.py --input output/Boardroom_Pro.drawio \\
      --xstatus saved_xstatus.xml

  # Webex cloud — list available devices first
  python3 src/xstatus_diff.py --list-devices --webex-token $WEBEX_TOKEN

  # Webex cloud — diff by device ID
  python3 src/xstatus_diff.py --input output/Boardroom_Pro.drawio \\
      --webex-device-id Y2lzY2... --webex-token $WEBEX_TOKEN

  # Webex cloud — diff by display name (fuzzy)
  python3 src/xstatus_diff.py --input output/Boardroom_Pro.drawio \\
      --webex-device "Boardroom Pro" --webex-token $WEBEX_TOKEN

  # Patch missing devices into schematic
  python3 src/xstatus_diff.py --input output/Boardroom_Pro.drawio \\
      --webex-device "Boardroom Pro" --webex-token $WEBEX_TOKEN --patch

Environment variables (alternative to --webex-token):
  WEBEX_TOKEN   Bearer token from developer.webex.com (bot or personal access token)
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
import ssl
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Webex Devices API (cloud — bot token or personal access token)
# ---------------------------------------------------------------------------

WEBEX_API = "https://webexapis.com/v1"


def _webex_get(path: str, token: str, params: dict | None = None) -> dict:
    """Make a GET request to the Webex API and return parsed JSON."""
    url = f"{WEBEX_API}{path}"
    if params:
        qs = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in params.items())
        url = f"{url}?{qs}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Webex API {exc.code} {exc.reason} for {url}: {body}"
        ) from exc


def webex_list_devices(token: str, display_name: str = "") -> list[dict]:
    """
    Return a list of Webex devices visible to the token.

    Each dict has keys: id, displayName, product, mac, ip, connectionStatus,
    workspaceId, serial, primarySipUrl, tags.
    """
    params = {"type": "roomdesk"}
    if display_name:
        params["displayName"] = display_name
    data = _webex_get("/devices", token, params)
    return data.get("items", [])


def webex_find_device(token: str, name_query: str) -> dict:
    """
    Find a single Webex device by display name (case-insensitive substring).
    Raises RuntimeError if 0 or >1 devices match.
    """
    devices = webex_list_devices(token, display_name=name_query)
    if not devices:
        raise RuntimeError(
            f"No Webex devices found matching '{name_query}'. "
            f"Run with --list-devices to see all available devices."
        )
    if len(devices) == 1:
        return devices[0]

    # Multiple matches — try exact (case-insensitive) first
    query_low = name_query.lower()
    exact = [d for d in devices if d.get("displayName", "").lower() == query_low]
    if len(exact) == 1:
        return exact[0]

    names = "\n  ".join(f"{d['id']}  {d.get('displayName','')}" for d in devices)
    raise RuntimeError(
        f"Multiple Webex devices match '{name_query}'. "
        f"Use --webex-device-id with one of:\n  {names}"
    )


def webex_fetch_xstatus(device_id: str, token: str) -> dict:
    """
    Pull full xStatus from a cloud-registered Webex device via the xAPI.

    Returns the same dict structure as parse_xstatus_xml / parse_xstatus_text
    so the rest of the diff pipeline works unchanged.

    Webex xAPI endpoint:
      GET /xapi/status?deviceId=<id>&name=Status
    Response JSON shape:
      {
        "deviceId": "...",
        "result": {
          "SystemUnit": {"ProductId": "...", ...},
          "Network": [{"IPv4": {"Address": "..."}}],
          "Peripherals": {"ConnectedDevice": [...]},
          "Video": {"Input": {"Connector": [...]}, "Output": {"Connector": [...]}}
        }
      }
    """
    data = _webex_get("/xapi/status", token, {"deviceId": device_id, "name": "Status"})
    result = data.get("result", data)  # some SDK versions omit wrapper

    def txt_path(obj: dict, *keys: str, default: str = "") -> str:
        """Drill into a nested dict/list safely."""
        cur = obj
        for k in keys:
            if isinstance(cur, list):
                cur = cur[0] if cur else {}
            if not isinstance(cur, dict):
                return default
            cur = cur.get(k, {})
        if isinstance(cur, str):
            return cur.strip()
        return default

    codec_model = txt_path(result, "SystemUnit", "ProductId")
    codec_ip = txt_path(result, "Network", "0", "IPv4", "Address")
    # Webex network is a list
    net_list = result.get("Network", [])
    if isinstance(net_list, list) and net_list:
        codec_ip = txt_path(net_list[0], "IPv4", "Address")

    # Peripherals
    peripherals: list[dict] = []
    peri_block = result.get("Peripherals", {})
    connected = peri_block.get("ConnectedDevice", [])
    if isinstance(connected, dict):
        connected = [connected]
    for dev in connected:
        name    = dev.get("Name", "")
        ptype   = dev.get("Type", "")
        status  = dev.get("Status", "")
        serial  = dev.get("SerialNumber", "")
        netaddr = dev.get("NetworkAddress", "")
        if not name and not ptype:
            continue
        peripherals.append({
            "name":            name,
            "type":            ptype,
            "norm_type":       _norm_type(ptype),
            "status":          status,
            "serial":          serial,
            "network_address": netaddr,
        })

    # Video inputs
    video_inputs: list[dict] = []
    vi_list = result.get("Video", {}).get("Input", {}).get("Connector", [])
    if isinstance(vi_list, dict):
        vi_list = [vi_list]
    for conn in vi_list:
        idx = str(conn.get("id", conn.get("item", "?")))
        sig = conn.get("SignalState", "Unknown")
        if isinstance(sig, dict):
            sig = sig.get("Value", sig.get("value", "Unknown"))
        video_inputs.append({"connector": idx, "signal_state": sig})

    # Video outputs
    video_outputs: list[dict] = []
    vo_list = result.get("Video", {}).get("Output", {}).get("Connector", [])
    if isinstance(vo_list, dict):
        vo_list = [vo_list]
    for conn in vo_list:
        idx = str(conn.get("id", conn.get("item", "?")))
        sig = conn.get("SignalState", "Unknown")
        if isinstance(sig, dict):
            sig = sig.get("Value", sig.get("value", "Unknown"))
        video_outputs.append({"connector": idx, "signal_state": sig})

    return {
        "codec_model":   codec_model,
        "codec_ip":      codec_ip,
        "peripherals":   peripherals,
        "video_inputs":  video_inputs,
        "video_outputs": video_outputs,
    }


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Keyword → normalised device type (draw.io side)
LABEL_KEYWORD_TYPE: dict[str, str] = {
    "camera":        "camera",
    "cam":           "camera",
    "touch":         "control-panel",
    "navigator":     "control-panel",
    "nav":           "control-panel",
    "mic":           "microphone",
    "microphone":    "microphone",
    "display":       "display",
    "monitor":       "display",
    "screen":        "display",
    "projector":     "display",
    "speaker":       "speaker",
    "codec":         "video-conferencing",
    "webex":         "video-conferencing",
    "roomkit":       "video-conferencing",
    "board":         "video-conferencing",
    "amplifier":     "amplifier",
    "amp":           "amplifier",
    "switcher":      "matrix-router",
    "switch":        "network-switch",
    "router":        "matrix-router",
    "pc":            "computer",
    "laptop":        "computer",
    "computer":      "computer",
    "dsp":           "audio-interface",
    "dante":         "audio-interface",
    "encoder":       "encoder",
    "decoder":       "decoder",
}

# xStatus peripheral Type → normalised device type
XSTATUS_TYPE_MAP: dict[str, str] = {
    "camera":                  "camera",
    "touchpanel":              "control-panel",
    "touch panel":             "control-panel",
    "microphone":              "microphone",
    "navigationcontroller":    "control-panel",
    "navigation controller":   "control-panel",
    "speakertrack":            "camera",
    "presenter track":         "camera",
    "presentertrack":          "camera",
    "display":                 "display",
    "monitor":                 "display",
    "mediaserver":             "encoder",
    "media server":            "encoder",
}

ROW_HEIGHT  = 26
HEADER_H    = 40
SECTION_H   = 26
INFO_ROW_H  = 26
DEVICE_W    = 160


# ---------------------------------------------------------------------------
# draw.io parsing
# ---------------------------------------------------------------------------

def infer_device_type_from_label(label: str) -> str:
    """Guess device type by scanning label words against LABEL_KEYWORD_TYPE."""
    low = label.lower()
    for kw, dtype in LABEL_KEYWORD_TYPE.items():
        if re.search(r"\b" + re.escape(kw) + r"\b", low):
            return dtype
    return "device"


def parse_drawio(path: str) -> tuple[list[dict], int]:
    """
    Parse a draw.io XML file.

    Returns:
        (devices, max_cell_id)
        devices: list of dicts with keys:
            id, label, device_type, model, serial, x, y, width, height
        max_cell_id: highest integer cell id found in the file
    """
    tree = ET.parse(path)
    root = tree.getroot()

    # Handle optional mxGraphModel wrapping
    if root.tag == "mxGraphModel":
        graph_root = root.find("root")
    else:
        graph_root = root.find(".//root")

    if graph_root is None:
        raise ValueError(f"No <root> element found in {path}")

    # First pass: collect all cells, find swimlane containers with parent="1"
    all_cells: dict[str, dict] = {}
    for cell in graph_root.findall("mxCell"):
        cid    = cell.get("id", "")
        value  = cell.get("value", "")
        parent = cell.get("parent", "")
        style  = cell.get("style", "")
        geo    = cell.find("mxGeometry")
        x = y = width = height = 0
        if geo is not None:
            x      = float(geo.get("x",      0))
            y      = float(geo.get("y",      0))
            width  = float(geo.get("width",  0))
            height = float(geo.get("height", 0))
        all_cells[cid] = {
            "id":     cid,
            "value":  html.unescape(value) if value else "",
            "parent": parent,
            "style":  style,
            "x": x, "y": y, "width": width, "height": height,
        }

    # Top-level swimlane containers are potential devices
    devices: list[dict] = []
    for cid, c in all_cells.items():
        if c["parent"] not in ("1", "0") or "swimlane" not in c["style"]:
            continue
        # Skip section-level swimlanes (Input/Output) — they are children of devices
        label = c["value"].strip()
        if not label or label in ("Input", "Output"):
            continue

        # Find child cells to extract model / serial
        model  = ""
        serial = ""
        for child in all_cells.values():
            if child["parent"] != cid:
                continue
            v = child["value"]
            # Model row typically has fontColor=#333333 and no S/N:
            if v and not model and "S/N:" not in v and "swimlane" not in child["style"]:
                if child["style"] and "fillColor=#f5f5f5" in child["style"]:
                    model = v.strip()
            if "S/N:" in v:
                serial = v.replace("S/N:", "").strip()

        device_type = infer_device_type_from_label(label)

        devices.append({
            "id":          cid,
            "label":       label,
            "device_type": device_type,
            "model":       model,
            "serial":      serial,
            "x":           c["x"],
            "y":           c["y"],
            "width":       c["width"],
            "height":      c["height"],
        })

    # Compute max integer cell id
    max_id = 0
    for cid in all_cells:
        try:
            max_id = max(max_id, int(cid))
        except ValueError:
            pass

    return devices, max_id


# ---------------------------------------------------------------------------
# xStatus fetching / parsing
# ---------------------------------------------------------------------------

def fetch_xstatus_http(ip: str, username: str, password: str) -> str:
    """Fetch /Status XML from a Cisco codec over HTTP (fallback HTTPS)."""
    url     = f"http://{ip}/getxml?location=/Status"
    creds   = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}"}

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError:
        pass  # try HTTPS

    url_https = f"https://{ip}/getxml?location=/Status"
    req2      = urllib.request.Request(url_https, headers=headers)
    ctx       = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    with urllib.request.urlopen(req2, timeout=10, context=ctx) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _norm_type(raw: str) -> str:
    """Normalise an xStatus peripheral Type string to our internal category."""
    key = raw.strip().lower()
    for pattern, mapped in XSTATUS_TYPE_MAP.items():
        if pattern in key:
            return mapped
    return "device"


def parse_xstatus_xml(content: str) -> dict:
    """
    Parse Cisco xStatus XML.

    Returns dict with:
        codec_model, codec_ip,
        peripherals: [{name, type, norm_type, status, serial, network_address}],
        video_inputs:  [{connector, signal_state}],
        video_outputs: [{connector, signal_state}],
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f"XML parse error: {exc}") from exc

    # Some codecs wrap in <Status> directly; handle both
    status_el = root if root.tag == "Status" else root.find("Status")
    if status_el is None:
        status_el = root  # best effort

    # Helper: text of first matching element
    def txt(parent: ET.Element, path: str, default: str = "") -> str:
        el = parent.find(path)
        return el.text.strip() if el is not None and el.text else default

    codec_model = txt(status_el, "SystemUnit/ProductId")
    codec_ip    = txt(status_el, "Network/IPv4/Address")

    # Peripherals
    peripherals: list[dict] = []
    peri_parent = status_el.find("Peripherals")
    if peri_parent is not None:
        for dev in peri_parent.findall("ConnectedDevice"):
            name    = txt(dev, "Name")
            ptype   = txt(dev, "Type")
            status  = txt(dev, "Status")
            serial  = txt(dev, "SerialNumber")
            netaddr = txt(dev, "NetworkAddress")
            if not name and not ptype:
                continue
            peripherals.append({
                "name":            name,
                "type":            ptype,
                "norm_type":       _norm_type(ptype),
                "status":          status,
                "serial":          serial,
                "network_address": netaddr,
            })

    # Video inputs
    video_inputs: list[dict] = []
    vi_parent = status_el.find("Video/Input")
    if vi_parent is not None:
        for conn in vi_parent.findall("Connector"):
            idx    = conn.get("item", conn.get("id", "?"))
            sig    = txt(conn, "SignalState", "Unknown")
            video_inputs.append({"connector": idx, "signal_state": sig})

    # Video outputs
    video_outputs: list[dict] = []
    vo_parent = status_el.find("Video/Output")
    if vo_parent is not None:
        for conn in vo_parent.findall("Connector"):
            idx    = conn.get("item", conn.get("id", "?"))
            sig    = txt(conn, "SignalState", "Unknown")
            video_outputs.append({"connector": idx, "signal_state": sig})

    return {
        "codec_model":   codec_model,
        "codec_ip":      codec_ip,
        "peripherals":   peripherals,
        "video_inputs":  video_inputs,
        "video_outputs": video_outputs,
    }


def parse_xstatus_text(content: str) -> dict:
    """
    Parse Cisco xStatus plain-text (SSH output) format.

    Line format examples:
        Peripherals ConnectedDevice 1 Name: Cisco TelePresence Touch 10
        Peripherals ConnectedDevice 1 Type: TouchPanel
        Peripherals ConnectedDevice 1 Status: Connected
        Video Input Connector 1 SignalState: OK
        SystemUnit ProductId: Cisco Webex Codec Pro
        Network 1 IPv4 Address: 192.168.1.100
    """
    peripherals:   dict[str, dict] = {}
    video_inputs:  dict[str, str]  = {}
    video_outputs: dict[str, str]  = {}
    codec_model = ""
    codec_ip    = ""

    for line in content.splitlines():
        line = line.strip()

        # Codec model
        m = re.match(r"SystemUnit\s+ProductId\s*:\s*(.+)", line, re.I)
        if m:
            codec_model = m.group(1).strip()

        # Network IP
        m = re.match(r"Network\s+\d*\s*IPv4\s+Address\s*:\s*(.+)", line, re.I)
        if m:
            codec_ip = m.group(1).strip()

        # Peripheral fields
        m = re.match(
            r"Peripherals\s+ConnectedDevice\s+(\d+)\s+(\w+)\s*:\s*(.+)",
            line, re.I
        )
        if m:
            idx, key, val = m.group(1), m.group(2).lower(), m.group(3).strip()
            if idx not in peripherals:
                peripherals[idx] = {
                    "name": "", "type": "", "norm_type": "device",
                    "status": "", "serial": "", "network_address": "",
                }
            if key == "name":
                peripherals[idx]["name"] = val
            elif key == "type":
                peripherals[idx]["type"]      = val
                peripherals[idx]["norm_type"] = _norm_type(val)
            elif key == "status":
                peripherals[idx]["status"] = val
            elif key in ("serialnumber", "serial"):
                peripherals[idx]["serial"] = val
            elif key in ("networkaddress", "ipaddress"):
                peripherals[idx]["network_address"] = val

        # Video input signal state
        m = re.match(
            r"Video\s+Input\s+Connector\s+(\d+)\s+SignalState\s*:\s*(.+)",
            line, re.I
        )
        if m:
            video_inputs[m.group(1)] = m.group(2).strip()

        # Video output signal state
        m = re.match(
            r"Video\s+Output\s+Connector\s+(\d+)\s+SignalState\s*:\s*(.+)",
            line, re.I
        )
        if m:
            video_outputs[m.group(1)] = m.group(2).strip()

    peri_list = [v for v in peripherals.values() if v["name"] or v["type"]]
    vi_list   = [{"connector": k, "signal_state": v}
                 for k, v in sorted(video_inputs.items())]
    vo_list   = [{"connector": k, "signal_state": v}
                 for k, v in sorted(video_outputs.items())]

    return {
        "codec_model":   codec_model,
        "codec_ip":      codec_ip,
        "peripherals":   peri_list,
        "video_inputs":  vi_list,
        "video_outputs": vo_list,
    }


def load_xstatus(source: str, username: str = "admin",
                 password: str = "") -> dict:
    """Load xStatus from a file path or live IP address."""
    p = Path(source)
    if p.exists():
        content = p.read_text(encoding="utf-8", errors="replace")
        # Detect XML vs text
        stripped = content.lstrip()
        if stripped.startswith("<"):
            return parse_xstatus_xml(content)
        else:
            return parse_xstatus_text(content)
    else:
        # Treat as IP address — live fetch
        raw = fetch_xstatus_http(source, username, password)
        return parse_xstatus_xml(raw)


# ---------------------------------------------------------------------------
# Matching / diff logic
# ---------------------------------------------------------------------------

def _keywords(text: str) -> set[str]:
    """Return significant lowercase words from a label string."""
    words = re.findall(r"[a-zA-Z]+", text.lower())
    stop  = {"cisco", "the", "and", "for", "in", "out", "a", "an", "of",
              "to", "1", "2", "3", "4", "5", "6", "7", "8"}
    return {w for w in words if w not in stop and len(w) > 1}


def _types_compatible(dt1: str, dt2: str) -> bool:
    """Return True if two normalised device types are considered the same."""
    if dt1 == dt2:
        return True
    # control-panel covers both touch-panel and navigation-controller
    cp = {"control-panel"}
    if dt1 in cp and dt2 in cp:
        return True
    return False


def fuzzy_match_peripheral(
    peri: dict,
    drawio_devices: list[dict],
    already_matched: set[str],
) -> Optional[dict]:
    """
    Try to match an xStatus peripheral to a draw.io device.

    Strategy (in order):
      1. Serial number match (if both have serials)
      2. Type + partial label keyword overlap
      3. Type-only match (pick first unmatched of same type)

    Returns the matched drawio device dict or None.
    """
    ptype  = peri["norm_type"]
    pname  = peri["name"]
    pserial = peri.get("serial", "")
    pkws   = _keywords(pname)

    # --- Pass 1: serial match ---
    if pserial:
        for d in drawio_devices:
            if d["id"] in already_matched:
                continue
            dsn = d.get("serial", "")
            if dsn and dsn.lower() == pserial.lower():
                return d

    # --- Pass 2: type + keyword overlap ---
    best: Optional[dict] = None
    best_score = 0
    for d in drawio_devices:
        if d["id"] in already_matched:
            continue
        if not _types_compatible(ptype, d["device_type"]):
            continue
        dkws  = _keywords(d["label"])
        score = len(pkws & dkws)
        if score > best_score:
            best_score = score
            best = d

    if best and best_score > 0:
        return best

    # --- Pass 3: type-only (first unmatched) ---
    for d in drawio_devices:
        if d["id"] in already_matched:
            continue
        if _types_compatible(ptype, d["device_type"]):
            return d

    return None


def build_diff(drawio_devices: list[dict], xstatus: dict) -> dict:
    """
    Compare drawio devices with xStatus peripherals and return diff dict.

    Returns:
        {
            codec_model, codec_ip,
            matched, missing_from_schematic, extra_in_schematic,
            signal_mismatches, peripheral_warnings,
        }
    """
    matched:                list[dict] = []
    missing_from_schematic: list[dict] = []
    extra_in_schematic:     list[dict] = []
    signal_mismatches:      list[dict] = []
    peripheral_warnings:    list[dict] = []

    peripherals    = xstatus.get("peripherals", [])
    video_inputs   = xstatus.get("video_inputs", [])
    video_outputs  = xstatus.get("video_outputs", [])

    already_matched: set[str] = set()  # drawio device ids

    for peri in peripherals:
        # Warn about non-Connected peripherals
        if peri["status"] and peri["status"].lower() not in ("connected", ""):
            peripheral_warnings.append({
                "peripheral": peri["name"] or peri["type"],
                "status":     peri["status"],
                "type":       peri["type"],
                "serial":     peri.get("serial", ""),
            })

        match = fuzzy_match_peripheral(peri, drawio_devices, already_matched)

        if match:
            already_matched.add(match["id"])
            matched.append({
                "drawio_label":  match["label"],
                "drawio_id":     match["id"],
                "drawio_type":   match["device_type"],
                "xstatus_name":  peri["name"],
                "xstatus_type":  peri["type"],
                "xstatus_status": peri["status"],
                "serial":        peri.get("serial", ""),
            })
        else:
            missing_from_schematic.append({
                "xstatus_name":  peri["name"],
                "xstatus_type":  peri["type"],
                "norm_type":     peri["norm_type"],
                "xstatus_status": peri["status"],
                "serial":        peri.get("serial", ""),
                "network_address": peri.get("network_address", ""),
            })

    # Anything in drawio that was NOT matched
    for d in drawio_devices:
        if d["id"] not in already_matched:
            # Skip codec itself — it's the host, not a peripheral
            if d["device_type"] == "video-conferencing":
                continue
            extra_in_schematic.append({
                "drawio_label": d["label"],
                "drawio_id":    d["id"],
                "drawio_type":  d["device_type"],
                "model":        d.get("model", ""),
                "serial":       d.get("serial", ""),
            })

    # Signal state mismatches (warn on any non-OK signal)
    for vi in video_inputs:
        state = vi["signal_state"]
        if state.lower() not in ("ok", "connected", ""):
            signal_mismatches.append({
                "direction": "input",
                "connector": vi["connector"],
                "signal_state": state,
                "note": f"Video Input Connector {vi['connector']} is {state}",
            })

    for vo in video_outputs:
        state = vo["signal_state"]
        if state.lower() not in ("ok", "connected", ""):
            signal_mismatches.append({
                "direction": "output",
                "connector": vo["connector"],
                "signal_state": state,
                "note": f"Video Output Connector {vo['connector']} is {state}",
            })

    return {
        "codec_model":             xstatus.get("codec_model", ""),
        "codec_ip":                xstatus.get("codec_ip", ""),
        "matched":                 matched,
        "missing_from_schematic":  missing_from_schematic,
        "extra_in_schematic":      extra_in_schematic,
        "signal_mismatches":       signal_mismatches,
        "peripheral_warnings":     peripheral_warnings,
    }


# ---------------------------------------------------------------------------
# Patch mode: add missing devices to draw.io
# ---------------------------------------------------------------------------

def _html_attr(s: str) -> str:
    return html.escape(str(s), quote=True)


def build_patch_cell(
    label:       str,
    device_type: str,
    serial:      str,
    net_addr:    str,
    x:           int,
    y:           int,
    start_id:    int,
) -> tuple[str, int]:
    """
    Build draw.io XML snippet for a new device swimlane.

    Returns (xml_string, next_available_id).
    """
    cells   = []
    cur_id  = start_id
    cont_id = str(cur_id); cur_id += 1
    total_h = HEADER_H + INFO_ROW_H  # header + ethernet row (minimal)

    cells.append(
        f'<mxCell id="{cont_id}" value="{_html_attr(label)}" '
        f'style="swimlane;fontStyle=1;childLayout=stackLayout;horizontal=1;'
        f'startSize={HEADER_H};fillColor=#fff2cc;horizontalStack=0;resizeParent=1;'
        f'resizeParentMax=0;resizeLast=0;collapsible=1;marginBottom=0;html=1;'
        f'fontSize=13;points=[];strokeColor=#d6b656;rounded=1;swimlaneLine=1;'
        f'fontColor=#333333;strokeWidth=2;swimlaneBody=0;absoluteArcSize=1;arcSize=10;" '
        f'vertex="1" parent="1">'
        f'<mxGeometry x="{x}" y="{y}" width="{DEVICE_W}" height="{total_h}" as="geometry"/>'
        f'</mxCell>'
    )

    # Ethernet row
    eth_id = str(cur_id); cur_id += 1
    cells.append(
        f'<mxCell id="{eth_id}" value="Ethernet RJ45" '
        f'style="text;strokeColor=default;fillColor=default;align=center;verticalAlign=top;'
        f'spacingLeft=4;spacingRight=4;overflow=hidden;rotatable=0;'
        f'points=[[0,0.5],[1,0.5]];portConstraint=eastwest;whiteSpace=wrap;html=1;" '
        f'vertex="1" parent="{cont_id}">'
        f'<mxGeometry y="{HEADER_H}" width="{DEVICE_W}" height="{INFO_ROW_H}" as="geometry"/>'
        f'</mxCell>'
    )

    return "\n".join(cells), cur_id


def patch_drawio(
    input_path:  str,
    output_path: str,
    missing:     list[dict],
    max_id:      int,
) -> None:
    """
    Add missing peripherals to the draw.io file and write to output_path.

    Missing devices are placed in a new row below existing content.
    """
    # Parse existing file to find bounding box
    tree   = ET.parse(input_path)
    root   = tree.getroot()

    # Read raw XML so we can do string-level insertion before </root>
    raw = Path(input_path).read_text(encoding="utf-8")

    # Find lowest y + height (to place new devices below)
    if root.tag == "root":
        graph_root_el = root
    else:
        graph_root_el = root.find("root")
        if graph_root_el is None:
            graph_root_el = root.find(".//root")
    max_bottom = 0
    if graph_root_el is not None:
        for cell in graph_root_el.findall("mxCell"):
            geo = cell.find("mxGeometry")
            if geo is not None:
                try:
                    cy = float(geo.get("y", 0))
                    ch = float(geo.get("height", 0))
                    max_bottom = max(max_bottom, cy + ch)
                except ValueError:
                    pass

    new_y    = int(max_bottom) + 60
    new_x    = 40
    cur_id   = max_id + 1
    gap      = DEVICE_W + 40
    new_xml_parts: list[str] = []

    for i, peri in enumerate(missing):
        label   = peri.get("xstatus_name") or peri.get("xstatus_type") or "Unknown Device"
        dtype   = peri.get("norm_type", "device")
        serial  = peri.get("serial", "")
        netaddr = peri.get("network_address", "")
        x       = new_x + (i % 8) * gap
        y       = new_y + (i // 8) * (HEADER_H + INFO_ROW_H + 60)

        snippet, cur_id = build_patch_cell(
            label=label, device_type=dtype,
            serial=serial, net_addr=netaddr,
            x=x, y=y, start_id=cur_id,
        )
        new_xml_parts.append(snippet)

    if not new_xml_parts:
        Path(output_path).write_text(raw, encoding="utf-8")
        return

    insertion = "\n" + "\n".join(new_xml_parts) + "\n"

    # Insert before </root>
    if "</root>" in raw:
        patched = raw.replace("</root>", insertion + "</root>", 1)
    else:
        patched = raw + insertion

    Path(output_path).write_text(patched, encoding="utf-8")


# ---------------------------------------------------------------------------
# Report printing
# ---------------------------------------------------------------------------

def print_report(diff: dict) -> None:
    w = 72
    sep = "-" * w

    def section(title: str) -> None:
        print(f"\n{sep}")
        print(f"  {title}")
        print(sep)

    print("=" * w)
    print("  xStatus Diff Report")
    if diff.get("codec_model"):
        print(f"  Codec  : {diff['codec_model']}")
    if diff.get("codec_ip"):
        print(f"  IP     : {diff['codec_ip']}")
    print("=" * w)

    section(f"MATCHED ({len(diff['matched'])})")
    if diff["matched"]:
        for m in diff["matched"]:
            status_tag = "" if m["xstatus_status"].lower() == "connected" \
                else f"  [STATUS: {m['xstatus_status']}]"
            print(f"  [OK]  {m['drawio_label']}")
            print(f"        xStatus: {m['xstatus_name']} ({m['xstatus_type']}){status_tag}")
    else:
        print("  (none)")

    section(f"MISSING FROM SCHEMATIC ({len(diff['missing_from_schematic'])})")
    if diff["missing_from_schematic"]:
        for m in diff["missing_from_schematic"]:
            print(f"  [MISS] {m['xstatus_name'] or '(unnamed)'} [{m['xstatus_type']}]")
            if m.get("serial"):
                print(f"         Serial : {m['serial']}")
            if m.get("network_address"):
                print(f"         IP     : {m['network_address']}")
            if m["xstatus_status"] and m["xstatus_status"].lower() != "connected":
                print(f"         Status : {m['xstatus_status']}")
    else:
        print("  (none)")

    section(f"EXTRA IN SCHEMATIC ({len(diff['extra_in_schematic'])})")
    if diff["extra_in_schematic"]:
        for e in diff["extra_in_schematic"]:
            print(f"  [XTRA] {e['drawio_label']} (type: {e['drawio_type']})")
            if e.get("model"):
                print(f"         Model  : {e['model']}")
            if e.get("serial"):
                print(f"         Serial : {e['serial']}")
    else:
        print("  (none)")

    section(f"SIGNAL MISMATCHES ({len(diff['signal_mismatches'])})")
    if diff["signal_mismatches"]:
        for s in diff["signal_mismatches"]:
            print(f"  [SIG]  {s['note']}")
    else:
        print("  (none)")

    if diff.get("peripheral_warnings"):
        section(f"PERIPHERAL WARNINGS ({len(diff['peripheral_warnings'])})")
        for w_item in diff["peripheral_warnings"]:
            print(f"  [WARN] {w_item['peripheral']} — status: {w_item['status']}")

    print("\n" + "=" * w)
    total = (len(diff["matched"])
             + len(diff["missing_from_schematic"])
             + len(diff["extra_in_schematic"]))
    print(f"  Summary: {len(diff['matched'])} matched, "
          f"{len(diff['missing_from_schematic'])} missing from schematic, "
          f"{len(diff['extra_in_schematic'])} extra in schematic, "
          f"{len(diff['signal_mismatches'])} signal issues")
    print("=" * w)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare draw.io schematic vs Cisco xStatus peripherals"
    )
    parser.add_argument(
        "--input", "-i",
        help="Path to draw.io file (e.g. output/Boardroom_Pro.drawio)"
    )

    src_group = parser.add_mutually_exclusive_group()
    src_group.add_argument(
        "--codec",
        help="Codec IP address for live xStatus pull (on-prem/LAN, basic auth)"
    )
    src_group.add_argument(
        "--xstatus",
        help="Path to saved xStatus XML or text file"
    )
    src_group.add_argument(
        "--webex-device-id",
        dest="webex_device_id",
        help="Webex device ID (from --list-devices) for cloud xAPI pull"
    )
    src_group.add_argument(
        "--webex-device",
        dest="webex_device",
        help="Webex device display name (fuzzy match) for cloud xAPI pull"
    )

    # Webex auth
    parser.add_argument(
        "--webex-token",
        dest="webex_token",
        default=os.environ.get("WEBEX_TOKEN", ""),
        help="Webex API bearer token (or set $WEBEX_TOKEN env var)"
    )

    # Direct codec auth
    parser.add_argument("--username", "-u", default="admin",
                        help="Codec username for --codec mode (default: admin)")
    parser.add_argument("--password", "-p", default="",
                        help="Codec password for --codec mode")

    # Utility
    parser.add_argument(
        "--list-devices", action="store_true",
        help="List all Webex devices visible to --webex-token and exit"
    )
    parser.add_argument(
        "--patch", action="store_true",
        help="Auto-add MISSING_FROM_SCHEMATIC devices to the draw.io file"
    )
    parser.add_argument(
        "--output-json",
        help="Override JSON report output path (default: output/<name>_xstatus_diff.json)"
    )

    args = parser.parse_args()

    # ── --list-devices shortcut ─────────────────────────────────────────────
    if args.list_devices:
        token = args.webex_token
        if not token:
            print("ERROR: --list-devices requires --webex-token or $WEBEX_TOKEN", file=sys.stderr)
            sys.exit(1)
        print("Fetching Webex device list...")
        try:
            devices = webex_list_devices(token)
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        if not devices:
            print("No room/desk devices found for this token.")
            sys.exit(0)
        print(f"\n{'ID':<52}  {'Status':<12}  Name")
        print("-" * 100)
        for d in devices:
            did    = d.get("id", "")
            status = d.get("connectionStatus", "")
            name   = d.get("displayName", "")
            print(f"{did:<52}  {status:<12}  {name}")
        print(f"\n{len(devices)} device(s) found.")
        sys.exit(0)

    # ── Validate that a source was given ────────────────────────────────────
    if not any([args.codec, args.xstatus, args.webex_device_id, args.webex_device]):
        parser.error(
            "Specify a source: --codec IP | --xstatus FILE "
            "| --webex-device-id ID | --webex-device NAME"
        )

    if not args.input:
        parser.error("--input is required when performing a diff")

    # ── Parse draw.io ──────────────────────────────────────────────────────
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: draw.io file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Parsing draw.io file: {input_path}")
    drawio_devices, max_id = parse_drawio(str(input_path))
    print(f"  Found {len(drawio_devices)} top-level device(s)")

    # ── Load xStatus ───────────────────────────────────────────────────────
    if args.codec:
        print(f"Fetching xStatus from codec at {args.codec} ...")
        try:
            xstatus = load_xstatus(args.codec, args.username, args.password)
        except Exception as exc:
            print(f"ERROR: Failed to fetch xStatus: {exc}", file=sys.stderr)
            sys.exit(1)

    elif args.xstatus:
        xstatus_path = Path(args.xstatus)
        if not xstatus_path.exists():
            print(f"ERROR: xStatus file not found: {xstatus_path}", file=sys.stderr)
            sys.exit(1)
        print(f"Loading xStatus from file: {xstatus_path}")
        try:
            xstatus = load_xstatus(str(xstatus_path))
        except Exception as exc:
            print(f"ERROR: Failed to parse xStatus file: {exc}", file=sys.stderr)
            sys.exit(1)

    elif args.webex_device_id or args.webex_device:
        token = args.webex_token
        if not token:
            print(
                "ERROR: Webex token required. Use --webex-token or export WEBEX_TOKEN=...",
                file=sys.stderr,
            )
            sys.exit(1)

        # Resolve device ID
        if args.webex_device_id:
            device_id   = args.webex_device_id
            device_name = device_id
        else:
            print(f"Looking up Webex device: '{args.webex_device}' ...")
            try:
                device = webex_find_device(token, args.webex_device)
            except RuntimeError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                sys.exit(1)
            device_id   = device["id"]
            device_name = device.get("displayName", device_id)
            print(f"  Found: {device_name}  (id: {device_id})")
            conn_status = device.get("connectionStatus", "")
            if conn_status and conn_status.lower() != "connected":
                print(f"  WARNING: Device connection status is '{conn_status}'")

        print(f"Fetching xStatus via Webex cloud API for device: {device_name} ...")
        try:
            xstatus = webex_fetch_xstatus(device_id, token)
        except Exception as exc:
            print(f"ERROR: Failed to fetch xStatus from Webex cloud: {exc}", file=sys.stderr)
            sys.exit(1)

    pcount = len(xstatus.get("peripherals", []))
    print(f"  Found {pcount} peripheral(s) in xStatus")

    # ── Diff ───────────────────────────────────────────────────────────────
    diff = build_diff(drawio_devices, xstatus)

    # ── Print report ───────────────────────────────────────────────────────
    print_report(diff)

    # ── Write JSON report ──────────────────────────────────────────────────
    output_dir = input_path.parent
    stem       = input_path.stem
    json_path  = Path(args.output_json) if args.output_json \
        else output_dir / f"{stem}_xstatus_diff.json"

    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(diff, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    print(f"\nJSON report written to: {json_path}")

    # ── Patch mode ─────────────────────────────────────────────────────────
    if args.patch:
        missing = diff.get("missing_from_schematic", [])
        if not missing:
            print("No missing devices — nothing to patch.")
        else:
            patched_path = output_dir / f"{stem}_patched.drawio"
            print(f"Patching draw.io file with {len(missing)} missing device(s)...")
            patch_drawio(
                input_path=str(input_path),
                output_path=str(patched_path),
                missing=missing,
                max_id=max_id,
            )
            print(f"Patched file written to: {patched_path}")


if __name__ == "__main__":
    main()
