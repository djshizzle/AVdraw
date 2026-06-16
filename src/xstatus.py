#!/usr/bin/env python3
"""
xstatus.py — Shared Cisco xStatus / Webex xAPI fetch + parse module
====================================================================
Provides a single API for pulling Cisco RoomOS device telemetry from
three different sources:

    fetch_xstatus_http(ip, user, pw)        → direct codec HTTP/HTTPS
    webex_fetch_xstatus(device_id, token)   → Webex cloud xAPI
    load_xstatus(path)                      → saved XML or SSH text file

All three return the same normalised dict:

    {
        "codec_model":   str,
        "codec_ip":      str,
        "peripherals":   [{"name", "type", "norm_type", "status",
                           "serial", "network_address"}, ...],
        "video_inputs":  [{"connector", "signal_state"}, ...],
        "video_outputs": [{"connector", "signal_state"}, ...],
    }

so the rest of the AVdraw pipeline (xstatus_diff, ai_reviewer, bom_to_drawio)
doesn't care where the telemetry came from.

Environment:
    WEBEX_TOKEN     Bearer token (alternative to passing token=)

Pure stdlib — no requests / no external deps.
"""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WEBEX_API = "https://webexapis.com/v1"

DEFAULT_TIMEOUT = 5  # seconds — keep tight so pipeline never hangs at scale

# xStatus peripheral Type → normalised device type used elsewhere in AVdraw
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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _norm_type(raw: str) -> str:
    """Normalise an xStatus peripheral Type string to internal category."""
    key = raw.strip().lower()
    for pattern, mapped in XSTATUS_TYPE_MAP.items():
        if pattern in key:
            return mapped
    return "device"


def _warn(msg: str) -> None:
    """Print a warning to stderr (CLAUDE.md guardrail: no print() for errors)."""
    print(f"WARNING: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Direct codec HTTP fetch (on-prem / LAN with basic auth)
# ---------------------------------------------------------------------------

def fetch_xstatus_http(
    ip: str,
    username: str = "admin",
    password: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """
    Fetch /Status XML from a Cisco codec over HTTP (falls back to HTTPS).

    Returns raw XML string. Raises RuntimeError if both HTTP and HTTPS fail.

    On timeout/connection refused, the caller should catch RuntimeError and
    fall back to BOM-only mode per the CLAUDE.md "xStatus enrichment is
    additive" guardrail.
    """
    url     = f"http://{ip}/getxml?location=/Status"
    creds   = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}"}

    # Try HTTP first
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError:
        pass  # fall through to HTTPS

    # Try HTTPS (codec self-signed cert, so disable verification)
    url_https = f"https://{ip}/getxml?location=/Status"
    req2      = urllib.request.Request(url_https, headers=headers)
    ctx       = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req2, timeout=timeout, context=ctx) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Codec {ip} unreachable: {exc}") from exc


# ---------------------------------------------------------------------------
# Webex Devices API (cloud xAPI — bot token or personal access token)
# ---------------------------------------------------------------------------

def _webex_get(
    path: str,
    token: str,
    params: dict | None = None,
    timeout: int = 15,
) -> dict:
    """Issue an authenticated GET against the Webex API. Returns parsed JSON."""
    url = f"{WEBEX_API}{path}"
    if params:
        qs = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in params.items())
        url = f"{url}?{qs}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Webex API {exc.code} {exc.reason} for {url}: {body}"
        ) from exc


def webex_list_devices(token: str, display_name: str = "") -> list[dict]:
    """
    Return Webex room/desk devices visible to the token.
    Pass display_name to server-side filter (substring match).
    """
    params: dict[str, str] = {"type": "roomdesk"}
    if display_name:
        params["displayName"] = display_name
    data = _webex_get("/devices", token, params)
    return data.get("items", [])


def webex_find_device(token: str, name_query: str) -> dict:
    """
    Find a single Webex device by display-name search.
    Raises RuntimeError on 0 or >1 matches (unless one is an exact match).
    """
    devices = webex_list_devices(token, display_name=name_query)
    if not devices:
        raise RuntimeError(
            f"No Webex devices match '{name_query}'. "
            f"Use --list-devices to see what's available."
        )
    if len(devices) == 1:
        return devices[0]

    query_low = name_query.lower()
    exact = [d for d in devices if d.get("displayName", "").lower() == query_low]
    if len(exact) == 1:
        return exact[0]

    names = "\n  ".join(f"{d['id']}  {d.get('displayName','')}" for d in devices)
    raise RuntimeError(
        f"Multiple Webex devices match '{name_query}'. Pick one with "
        f"--webex-device-id:\n  {names}"
    )


def webex_fetch_xstatus(device_id: str, token: str) -> dict:
    """
    Pull full xStatus from a cloud-registered Webex device via xAPI.

    Endpoint:
        GET https://webexapis.com/v1/xapi/status?deviceId=<id>&name=Status

    Returns the same dict shape as parse_xstatus_xml so callers are agnostic
    to the transport.
    """
    data = _webex_get("/xapi/status", token, {"deviceId": device_id, "name": "Status"})
    result = data.get("result", data)  # tolerate missing wrapper

    codec_model = ""
    su = result.get("SystemUnit", {})
    if isinstance(su, dict):
        codec_model = str(su.get("ProductId", "")).strip()

    # Network is a list in cloud xAPI responses
    codec_ip = ""
    net_list = result.get("Network", [])
    if isinstance(net_list, list) and net_list:
        ipv4 = net_list[0].get("IPv4", {})
        if isinstance(ipv4, dict):
            codec_ip = str(ipv4.get("Address", "")).strip()
    elif isinstance(net_list, dict):
        ipv4 = net_list.get("IPv4", {})
        if isinstance(ipv4, dict):
            codec_ip = str(ipv4.get("Address", "")).strip()

    # Peripherals
    peripherals: list[dict] = []
    peri_block = result.get("Peripherals", {})
    connected  = peri_block.get("ConnectedDevice", []) if isinstance(peri_block, dict) else []
    if isinstance(connected, dict):
        connected = [connected]
    for dev in connected:
        if not isinstance(dev, dict):
            continue
        name    = str(dev.get("Name", "")).strip()
        ptype   = str(dev.get("Type", "")).strip()
        status  = str(dev.get("Status", "")).strip()
        serial  = str(dev.get("SerialNumber", "")).strip()
        netaddr = str(dev.get("NetworkAddress", "")).strip()
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

    def _connectors(direction_key: str) -> list[dict]:
        out: list[dict] = []
        video = result.get("Video", {})
        block = video.get(direction_key, {}) if isinstance(video, dict) else {}
        clist = block.get("Connector", []) if isinstance(block, dict) else []
        if isinstance(clist, dict):
            clist = [clist]
        for conn in clist:
            if not isinstance(conn, dict):
                continue
            idx = str(conn.get("id", conn.get("item", "?")))
            sig = conn.get("SignalState", "Unknown")
            if isinstance(sig, dict):
                sig = sig.get("Value", sig.get("value", "Unknown"))
            out.append({"connector": idx, "signal_state": str(sig)})
        return out

    return {
        "codec_model":   codec_model,
        "codec_ip":      codec_ip,
        "peripherals":   peripherals,
        "video_inputs":  _connectors("Input"),
        "video_outputs": _connectors("Output"),
    }


# ---------------------------------------------------------------------------
# Offline file parsing — XML (HTTP getxml) and text (SSH paste)
# ---------------------------------------------------------------------------

def parse_xstatus_xml(content: str) -> dict:
    """Parse Cisco xStatus XML (output of `xCommand HttpClient Get`)."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f"XML parse error: {exc}") from exc

    status_el = root if root.tag == "Status" else root.find("Status")
    if status_el is None:
        status_el = root

    def txt(parent: ET.Element, path: str, default: str = "") -> str:
        el = parent.find(path)
        return el.text.strip() if el is not None and el.text else default

    codec_model = txt(status_el, "SystemUnit/ProductId")
    codec_ip    = txt(status_el, "Network/IPv4/Address")

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

    video_inputs: list[dict] = []
    vi_parent = status_el.find("Video/Input")
    if vi_parent is not None:
        for conn in vi_parent.findall("Connector"):
            idx = conn.get("item", conn.get("id", "?"))
            sig = txt(conn, "SignalState", "Unknown")
            video_inputs.append({"connector": idx, "signal_state": sig})

    video_outputs: list[dict] = []
    vo_parent = status_el.find("Video/Output")
    if vo_parent is not None:
        for conn in vo_parent.findall("Connector"):
            idx = conn.get("item", conn.get("id", "?"))
            sig = txt(conn, "SignalState", "Unknown")
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
    Parse Cisco xStatus plain-text (SSH dump) format.

    Examples:
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

        m = re.match(r"SystemUnit\s+ProductId\s*:\s*(.+)", line, re.I)
        if m:
            codec_model = m.group(1).strip()

        m = re.match(r"Network\s+\d*\s*IPv4\s+Address\s*:\s*(.+)", line, re.I)
        if m:
            codec_ip = m.group(1).strip()

        m = re.match(
            r"Peripherals\s+ConnectedDevice\s+(\d+)\s+(\w+)\s*:\s*(.+)",
            line, re.I,
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

        m = re.match(
            r"Video\s+Input\s+Connector\s+(\d+)\s+SignalState\s*:\s*(.+)",
            line, re.I,
        )
        if m:
            video_inputs[m.group(1)] = m.group(2).strip()

        m = re.match(
            r"Video\s+Output\s+Connector\s+(\d+)\s+SignalState\s*:\s*(.+)",
            line, re.I,
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


# ---------------------------------------------------------------------------
# Unified loader — handles file path OR live IP
# ---------------------------------------------------------------------------

def load_xstatus(
    source: str,
    username: str = "admin",
    password: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """
    Load xStatus from either:
      • a file path (XML or SSH text — auto-detected)
      • a codec IP address (live HTTP fetch with basic auth)

    Raises RuntimeError on live fetch failures so callers can catch and fall
    back to BOM-only mode.
    """
    p = Path(source)
    if p.exists():
        content  = p.read_text(encoding="utf-8", errors="replace")
        stripped = content.lstrip()
        if stripped.startswith("<"):
            return parse_xstatus_xml(content)
        return parse_xstatus_text(content)

    # Treat as IP → live fetch
    raw = fetch_xstatus_http(source, username, password, timeout=timeout)
    return parse_xstatus_xml(raw)


def load_xstatus_safe(
    source: str,
    username: str = "admin",
    password: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> dict | None:
    """
    Same as load_xstatus but never raises — returns None on failure with a
    warning to stderr. Use this in pipeline contexts where xStatus is an
    optional enrichment and a network blip shouldn't kill the whole run.
    """
    try:
        return load_xstatus(source, username, password, timeout=timeout)
    except (RuntimeError, ValueError, OSError) as exc:
        _warn(f"xStatus unreachable for '{source}' — falling back to BOM only ({exc})")
        return None


def webex_token_from_env() -> str:
    """Return WEBEX_TOKEN env var or empty string."""
    return os.environ.get("WEBEX_TOKEN", "").strip()
