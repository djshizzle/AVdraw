#!/usr/bin/env python3
"""
nl_editor.py - Natural language draw.io schematic editor using Claude API.

Usage:
    python3 src/nl_editor.py --input output/Boardroom_Pro.drawio --command 'add a PTZ camera'
    python3 src/nl_editor.py --input output/Boardroom_Pro.drawio  # interactive mode
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from xml.dom import minidom


# ---------------------------------------------------------------------------
# Signal color map
# ---------------------------------------------------------------------------
SIGNAL_COLORS = {
    "hdmi":          "#d6b656",
    "sdi":           "#6d8764",
    "dante":         "#7030a0",
    "ethernet":      "#006EAF",
    "usb":           "#0070c0",
    "speaker-level": "#ff0000",
    "analog-audio":  "#ff6600",
    "ndi":           "#e36c09",
    "fiber":         "#00b0f0",
    "hdbaset":       "#70ad47",
    "displayport":   "#0070c0",
    "rs422":         "#ffc000",
    "gpio":          "#ffc000",
    "rf":            "#808080",
}

VALID_SIGNALS    = set(SIGNAL_COLORS.keys())
VALID_DIRECTIONS = {"input", "output", "bidirectional"}
VALID_OPS        = {
    "ADD_DEVICE", "REMOVE_DEVICE", "ADD_CONNECTION",
    "REMOVE_CONNECTION", "RENAME_DEVICE", "MOVE_DEVICE", "ADD_PORT",
}


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def parse_drawio(path: str) -> ET.ElementTree:
    """Parse a draw.io XML file and return an ElementTree."""
    tree = ET.parse(path)
    return tree


def get_root_cell(tree: ET.ElementTree) -> ET.Element:
    """Return the <root> element that contains all mxCell nodes."""
    root = tree.getroot()
    # draw.io structure: <mxGraphModel><root>...</root></mxGraphModel>
    # or wrapped in <mxfile><diagram>...
    for elem in root.iter("root"):
        return elem
    raise ValueError("No <root> element found in draw.io file")


def get_all_cells(tree: ET.ElementTree) -> list[ET.Element]:
    return list(get_root_cell(tree))


def max_cell_id(cells: list[ET.Element]) -> int:
    """Return the maximum integer cell ID found in the cell list."""
    max_id = 10
    for cell in cells:
        cid = cell.get("id", "")
        try:
            val = int(cid)
            if val > max_id:
                max_id = val
        except ValueError:
            pass
    return max_id


def find_device_cell(cells: list[ET.Element], label: str) -> ET.Element | None:
    """Find the top-level swimlane cell whose value matches label (case-insensitive)."""
    label_lower = label.strip().lower()
    for cell in cells:
        style = cell.get("style", "")
        if "swimlane" in style and cell.get("parent") in ("1", None):
            val = (cell.get("value") or "").strip().lower()
            if val == label_lower:
                return cell
    return None


def find_device_cell_fuzzy(cells: list[ET.Element], label: str) -> ET.Element | None:
    """Find device cell; tries exact then substring match."""
    exact = find_device_cell(cells, label)
    if exact is not None:
        return exact
    label_lower = label.strip().lower()
    for cell in cells:
        style = cell.get("style", "")
        if "swimlane" in style and cell.get("parent") in ("1", None):
            val = (cell.get("value") or "").strip().lower()
            if label_lower in val or val in label_lower:
                return cell
    return None


def get_children_of(cells: list[ET.Element], parent_id: str) -> list[ET.Element]:
    return [c for c in cells if c.get("parent") == parent_id]


def find_port_cell(cells: list[ET.Element], device_cell: ET.Element, port_label: str) -> ET.Element | None:
    """Find a port cell (leaf child with matching value) under the device."""
    device_id = device_cell.get("id")
    port_label_lower = port_label.strip().lower()

    def search_under(parent_id: str) -> ET.Element | None:
        for cell in cells:
            if cell.get("parent") != parent_id:
                continue
            val = (cell.get("value") or "").strip().lower()
            style = cell.get("style", "")
            # Port rows: not swimlane containers, or swimlane section headers
            if val == port_label_lower and "swimlane" not in style:
                return cell
            # Recurse into section swimlanes
            if "swimlane" in style:
                result = search_under(cell.get("id", ""))
                if result is not None:
                    return result
        return None

    return search_under(device_id)


def find_port_cell_fuzzy(cells: list[ET.Element], device_cell: ET.Element, port_label: str) -> ET.Element | None:
    """Find port cell; exact then substring."""
    exact = find_port_cell(cells, device_cell, port_label)
    if exact is not None:
        return exact
    device_id = device_cell.get("id")
    port_label_lower = port_label.strip().lower()

    def search_under(parent_id: str) -> ET.Element | None:
        for cell in cells:
            if cell.get("parent") != parent_id:
                continue
            val = (cell.get("value") or "").strip().lower()
            style = cell.get("style", "")
            if "swimlane" not in style and (port_label_lower in val or val in port_label_lower):
                return cell
            if "swimlane" in style:
                result = search_under(cell.get("id", ""))
                if result is not None:
                    return result
        return None

    return search_under(device_id)


def collect_descendant_ids(cells: list[ET.Element], root_id: str) -> set[str]:
    """Collect IDs of root_id and all descendants."""
    ids = {root_id}
    changed = True
    while changed:
        changed = False
        for cell in cells:
            if cell.get("parent") in ids and cell.get("id") not in ids:
                ids.add(cell.get("id"))
                changed = True
    return ids


def collect_edge_ids_referencing(cells: list[ET.Element], cell_ids: set[str]) -> set[str]:
    """Return IDs of edge cells whose source or target is in cell_ids."""
    edge_ids = set()
    for cell in cells:
        if cell.get("edge") == "1":
            if cell.get("source") in cell_ids or cell.get("target") in cell_ids:
                edge_ids.add(cell.get("id"))
    return edge_ids


# ---------------------------------------------------------------------------
# Schematic summary builder
# ---------------------------------------------------------------------------

def build_summary(cells: list[ET.Element]) -> str:
    """Build a concise text summary of the schematic for the Claude prompt."""
    devices = []
    connections = []

    for cell in cells:
        style = cell.get("style", "")
        if "swimlane" in style and cell.get("parent") in ("1", None):
            label = cell.get("value") or "(unnamed)"
            # Try to infer type from style or label
            dtype = "device"
            style_lower = style.lower()
            for hint in ("display", "camera", "codec", "switcher", "amplifier",
                         "dsp", "router", "matrix", "controller", "extender"):
                if hint in style_lower or hint in label.lower():
                    dtype = hint
                    break
            devices.append(f"{label} ({dtype})")

    for cell in cells:
        if cell.get("edge") == "1":
            value = cell.get("value") or "unknown"
            src_id = cell.get("source", "")
            tgt_id = cell.get("target", "")
            # Resolve source/target labels
            src_label = _cell_path_label(cells, src_id)
            tgt_label = _cell_path_label(cells, tgt_id)
            connections.append(f"{src_label} -> {tgt_label} ({value})")

    lines = ["Devices:"]
    if devices:
        for d in devices:
            lines.append(f"  - {d}")
    else:
        lines.append("  (none)")
    lines.append("Connections:")
    if connections:
        for c in connections:
            lines.append(f"  - {c}")
    else:
        lines.append("  (none)")
    return "\n".join(lines)


def _cell_path_label(cells: list[ET.Element], cell_id: str) -> str:
    """Return 'DeviceName / PortName' label for a given cell ID."""
    if not cell_id:
        return "(unknown)"
    id_map = {c.get("id"): c for c in cells}
    cell = id_map.get(cell_id)
    if cell is None:
        return f"(id:{cell_id})"
    parts = []
    cur = cell
    while cur is not None:
        val = cur.get("value") or ""
        if val:
            parts.append(val)
        parent_id = cur.get("parent", "")
        if parent_id in ("0", "1", "", None):
            break
        cur = id_map.get(parent_id)
    parts.reverse()
    return " / ".join(parts) if parts else f"(id:{cell_id})"


def count_devices_and_connections(cells: list[ET.Element]) -> tuple[int, int]:
    devices = sum(
        1 for c in cells
        if "swimlane" in c.get("style", "") and c.get("parent") in ("1", None)
    )
    connections = sum(1 for c in cells if c.get("edge") == "1")
    return devices, connections


# ---------------------------------------------------------------------------
# Claude API interaction
# ---------------------------------------------------------------------------

def ask_claude(summary: str, command: str) -> list[dict]:
    """Send the schematic summary + command to Claude and return a list of ops."""
    try:
        import anthropic
    except ImportError:
        print("ERROR: anthropic SDK not installed. Run: pip install anthropic")
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable is not set.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    system_prompt = (
        "You are an AV schematic editor. Given the current schematic state and a natural language command, "
        "output ONLY a JSON array of edit operations. "
        "Valid ops: ADD_DEVICE, REMOVE_DEVICE, ADD_CONNECTION, REMOVE_CONNECTION, RENAME_DEVICE, MOVE_DEVICE, ADD_PORT. "
        "Valid signals: hdmi, sdi, dante, ethernet, usb, speaker-level, analog-audio, ndi, fiber, hdbaset, displayport, rf, rs422, gpio. "
        "Valid directions: input, output, bidirectional."
    )

    user_message = (
        f"Current schematic:\n{summary}\n\n"
        f"Command: {command}\n\n"
        "Output JSON only."
    )

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    raw = response.content[0].text.strip()

    # Strip markdown code fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    raw = raw.strip()

    try:
        ops = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Claude returned invalid JSON: {exc}")
        print(f"Raw response:\n{raw}")
        return []

    if not isinstance(ops, list):
        ops = [ops]

    return ops


# ---------------------------------------------------------------------------
# Edit operation appliers
# ---------------------------------------------------------------------------

def apply_add_device(root_elem: ET.Element, cells: list[ET.Element], op: dict) -> str:
    """Add a new device swimlane to the schematic."""
    label  = op.get("label", "New Device")
    dtype  = op.get("type", "device")
    ports  = op.get("ports", [])
    x      = op.get("x")
    y      = op.get("y")

    # Auto-place below existing devices if x/y not specified
    if x is None or y is None:
        max_y = 0
        for cell in cells:
            geom = cell.find("mxGeometry")
            if geom is not None and "swimlane" in cell.get("style", "") and cell.get("parent") in ("1", None):
                cy = float(geom.get("y", 0))
                ch = float(geom.get("height", 0))
                if cy + ch > max_y:
                    max_y = cy + ch
        x = 200
        y = max_y + 40

    x = int(x)
    y = int(y)

    base_id  = max_cell_id(cells) + 1
    next_id  = [base_id]

    def new_id() -> str:
        cid = str(next_id[0])
        next_id[0] += 1
        return cid

    input_ports  = [p for p in ports if p.get("direction") in ("input", "bidirectional")]
    output_ports = [p for p in ports if p.get("direction") == "output"]

    # Calculate heights
    row_h       = 26
    header_h    = 30
    section_h   = header_h
    if input_ports:
        section_h += len(input_ports) * row_h
    if output_ports:
        section_h += len(output_ports) * row_h
    device_h    = max(header_h + section_h, header_h + 30)
    device_w    = 200

    # Container cell
    container_id = new_id()
    container = ET.SubElement(root_elem, "mxCell")
    container.set("id", container_id)
    container.set("value", label)
    container.set("style",
        "swimlane;fontStyle=1;align=center;startSize=30;container=1;"
        "collapsible=0;childLayout=stackLayout;horizontal=1;"
        "fillColor=#dae8fc;strokeColor=#6c8ebf;"
    )
    container.set("vertex", "1")
    container.set("parent", "1")
    geom = ET.SubElement(container, "mxGeometry")
    geom.set("x", str(x))
    geom.set("y", str(y))
    geom.set("width", str(device_w))
    geom.set("height", str(device_h))
    geom.set("as", "geometry")

    def add_section(parent_id: str, section_label: str, fill_color: str, port_list: list[dict]) -> None:
        sec_id = new_id()
        sec_h  = header_h + len(port_list) * row_h
        sec = ET.SubElement(root_elem, "mxCell")
        sec.set("id", sec_id)
        sec.set("value", section_label)
        sec.set("style",
            f"swimlane;startSize={header_h};fillColor={fill_color};fontStyle=1;"
            "strokeColor=#d6b656;collapsible=0;horizontal=1;"
        )
        sec.set("vertex", "1")
        sec.set("parent", parent_id)
        sec_geom = ET.SubElement(sec, "mxGeometry")
        sec_geom.set("width", str(device_w))
        sec_geom.set("height", str(sec_h))
        sec_geom.set("as", "geometry")

        for port in port_list:
            port_id = new_id()
            port_cell = ET.SubElement(root_elem, "mxCell")
            port_cell.set("id", port_id)
            port_cell.set("value", port.get("label", "Port"))
            port_cell.set("style",
                "text;strokeColor=none;fillColor=none;align=left;vertexLabelPosition=right;"
                "spacingLeft=4;spacingRight=4;rotatable=0;portConstraint=eastwest;"
                f"fontSize=11;"
            )
            port_cell.set("vertex", "1")
            port_cell.set("parent", sec_id)
            p_geom = ET.SubElement(port_cell, "mxGeometry")
            p_geom.set("width", str(device_w))
            p_geom.set("height", str(row_h))
            p_geom.set("as", "geometry")

    if input_ports:
        add_section(container_id, "Input", "#d5e8d4", input_ports)
    if output_ports:
        add_section(container_id, "Output", "#ffe6cc", output_ports)

    return f"Added '{label}' ({dtype}) at position ({x}, {y})"


def apply_remove_device(root_elem: ET.Element, cells: list[ET.Element], op: dict) -> str:
    """Remove a device and all its children and connected edges."""
    device_label = op.get("device_label", "")
    device_cell  = find_device_cell_fuzzy(cells, device_label)
    if device_cell is None:
        return f"WARNING: Device '{device_label}' not found - skipped"

    device_id    = device_cell.get("id")
    desc_ids     = collect_descendant_ids(cells, device_id)
    edge_ids     = collect_edge_ids_referencing(cells, desc_ids)
    remove_ids   = desc_ids | edge_ids

    removed = 0
    for cell in list(root_elem):
        if cell.get("id") in remove_ids:
            root_elem.remove(cell)
            removed += 1

    return f"Removed '{device_label}' ({removed} cells removed)"


def apply_add_connection(root_elem: ET.Element, cells: list[ET.Element], op: dict) -> str:
    """Add an edge between two port cells."""
    from_device = op.get("from_device", "")
    from_port   = op.get("from_port",   "")
    to_device   = op.get("to_device",   "")
    to_port     = op.get("to_port",     "")
    signal      = op.get("signal", "").lower()

    color = SIGNAL_COLORS.get(signal, "#000000")

    # Resolve device cells
    src_device = find_device_cell_fuzzy(cells, from_device)
    tgt_device = find_device_cell_fuzzy(cells, to_device)

    src_port_cell = None
    tgt_port_cell = None

    if src_device is not None and from_port:
        src_port_cell = find_port_cell_fuzzy(cells, src_device, from_port)
    if tgt_device is not None and to_port:
        tgt_port_cell = find_port_cell_fuzzy(cells, tgt_device, to_port)

    # Fall back to device-level if ports not found
    src_id = src_port_cell.get("id") if src_port_cell is not None else (
        src_device.get("id") if src_device is not None else None
    )
    tgt_id = tgt_port_cell.get("id") if tgt_port_cell is not None else (
        tgt_device.get("id") if tgt_device is not None else None
    )

    if src_id is None:
        return f"WARNING: Source '{from_device}' not found - connection skipped"
    if tgt_id is None:
        return f"WARNING: Target '{to_device}' not found - connection skipped"

    edge_id = str(max_cell_id(list(root_elem)) + 1)
    edge = ET.SubElement(root_elem, "mxCell")
    edge.set("id",     edge_id)
    edge.set("value",  signal or "")
    edge.set("style",
        f"edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;"
        f"jettySize=auto;exitX=1;exitY=0.5;exitDx=0;exitDy=-1;"
        f"entryX=0;entryY=0.5;entryDx=0;entryDy=-1;"
        f"strokeColor={color};strokeWidth=2;fontColor={color};fontStyle=1;"
    )
    edge.set("edge",   "1")
    edge.set("source", src_id)
    edge.set("target", tgt_id)
    edge.set("parent", "1")
    geom = ET.SubElement(edge, "mxGeometry")
    geom.set("relative", "1")
    geom.set("as", "geometry")

    return f"Connected {from_device} {from_port} -> {to_device} {to_port} ({signal})"


def apply_remove_connection(root_elem: ET.Element, cells: list[ET.Element], op: dict) -> str:
    """Remove an edge between two devices."""
    from_device = op.get("from_device", "")
    to_device   = op.get("to_device",   "")

    src_dev = find_device_cell_fuzzy(cells, from_device)
    tgt_dev = find_device_cell_fuzzy(cells, to_device)

    if src_dev is None:
        return f"WARNING: Source device '{from_device}' not found"
    if tgt_dev is None:
        return f"WARNING: Target device '{to_device}' not found"

    src_desc = collect_descendant_ids(cells, src_dev.get("id"))
    tgt_desc = collect_descendant_ids(cells, tgt_dev.get("id"))

    removed = 0
    for cell in list(root_elem):
        if cell.get("edge") == "1":
            src_ok = cell.get("source") in src_desc
            tgt_ok = cell.get("target") in tgt_desc
            if src_ok and tgt_ok:
                root_elem.remove(cell)
                removed += 1

    if removed == 0:
        return f"WARNING: No connection found between '{from_device}' and '{to_device}'"
    return f"Removed {removed} connection(s) between '{from_device}' and '{to_device}'"


def apply_rename_device(root_elem: ET.Element, cells: list[ET.Element], op: dict) -> str:
    """Rename a device swimlane."""
    old_label = op.get("old_label", "")
    new_label = op.get("new_label", "")
    cell      = find_device_cell_fuzzy(cells, old_label)
    if cell is None:
        return f"WARNING: Device '{old_label}' not found"
    cell.set("value", new_label)
    return f"Renamed '{old_label}' -> '{new_label}'"


def apply_move_device(root_elem: ET.Element, cells: list[ET.Element], op: dict) -> str:
    """Move a device to a new position."""
    device_label = op.get("device_label", "")
    x            = op.get("x", 0)
    y            = op.get("y", 0)
    cell         = find_device_cell_fuzzy(cells, device_label)
    if cell is None:
        return f"WARNING: Device '{device_label}' not found"
    geom = cell.find("mxGeometry")
    if geom is None:
        geom = ET.SubElement(cell, "mxGeometry")
        geom.set("as", "geometry")
    geom.set("x", str(int(x)))
    geom.set("y", str(int(y)))
    return f"Moved '{device_label}' to ({int(x)}, {int(y)})"


def apply_add_port(root_elem: ET.Element, cells: list[ET.Element], op: dict) -> str:
    """Add a port row to an existing device."""
    device_label = op.get("device_label", "")
    port_label   = op.get("port_label",   "New Port")
    direction    = op.get("direction",    "input")
    signal       = op.get("signal",       "")

    device_cell = find_device_cell_fuzzy(cells, device_label)
    if device_cell is None:
        return f"WARNING: Device '{device_label}' not found"

    device_id  = device_cell.get("id")
    row_h      = 26
    header_h   = 30
    device_w   = 200

    # Find the matching section (Input/Output) or attach directly to device
    section_label = "Input" if direction in ("input", "bidirectional") else "Output"
    section_cell  = None
    for cell in cells:
        if cell.get("parent") == device_id and "swimlane" in cell.get("style", ""):
            if (cell.get("value") or "").strip().lower() == section_label.lower():
                section_cell = cell
                break

    parent_id = section_cell.get("id") if section_cell is not None else device_id

    port_id = str(max_cell_id(list(root_elem)) + 1)
    port_cell = ET.SubElement(root_elem, "mxCell")
    port_cell.set("id", port_id)
    port_cell.set("value", port_label)
    port_cell.set("style",
        "text;strokeColor=none;fillColor=none;align=left;vertexLabelPosition=right;"
        "spacingLeft=4;spacingRight=4;rotatable=0;portConstraint=eastwest;fontSize=11;"
    )
    port_cell.set("vertex", "1")
    port_cell.set("parent", parent_id)
    p_geom = ET.SubElement(port_cell, "mxGeometry")
    p_geom.set("width",  str(device_w))
    p_geom.set("height", str(row_h))
    p_geom.set("as", "geometry")

    # Expand section height
    if section_cell is not None:
        sec_geom = section_cell.find("mxGeometry")
        if sec_geom is not None:
            old_h = float(sec_geom.get("height", header_h + row_h))
            sec_geom.set("height", str(int(old_h + row_h)))
    # Expand device height
    dev_geom = device_cell.find("mxGeometry")
    if dev_geom is not None:
        old_h = float(dev_geom.get("height", 60))
        dev_geom.set("height", str(int(old_h + row_h)))

    return f"Added port '{port_label}' ({direction}/{signal}) to '{device_label}'"


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

OP_DISPATCH = {
    "ADD_DEVICE":        apply_add_device,
    "REMOVE_DEVICE":     apply_remove_device,
    "ADD_CONNECTION":    apply_add_connection,
    "REMOVE_CONNECTION": apply_remove_connection,
    "RENAME_DEVICE":     apply_rename_device,
    "MOVE_DEVICE":       apply_move_device,
    "ADD_PORT":          apply_add_port,
}

OP_LABELS = {
    "ADD_DEVICE":        "ADD_DEVICE",
    "REMOVE_DEVICE":     "REMOVE_DEVICE",
    "ADD_CONNECTION":    "ADD_CONNECTION",
    "REMOVE_CONNECTION": "REMOVE_CONNECTION",
    "RENAME_DEVICE":     "RENAME_DEVICE",
    "MOVE_DEVICE":       "MOVE_DEVICE",
    "ADD_PORT":          "ADD_PORT",
}


# ---------------------------------------------------------------------------
# Core edit pipeline
# ---------------------------------------------------------------------------

def apply_ops(tree: ET.ElementTree, ops: list[dict]) -> list[str]:
    """Apply a list of edit operations to the tree. Returns a list of result messages."""
    root_elem = get_root_cell(tree)
    messages  = []

    for op in ops:
        op_name = op.get("op", "").upper()
        if op_name not in OP_DISPATCH:
            messages.append(f"WARNING: Unknown op '{op_name}' - skipped")
            continue
        # Refresh cells list before each op so we see newly added cells
        cells   = get_all_cells(tree)
        handler = OP_DISPATCH[op_name]
        msg     = handler(root_elem, cells, op)
        label   = OP_LABELS.get(op_name, op_name)
        messages.append(f"[{label}] {msg}")

    return messages


def save_drawio(tree: ET.ElementTree, path: str) -> None:
    """Write the modified tree back to disk with clean indentation."""
    # Use minidom for pretty printing
    rough_string = ET.tostring(tree.getroot(), encoding="unicode")
    reparsed     = minidom.parseString(rough_string)
    pretty       = reparsed.toprettyxml(indent="  ")
    # Remove the auto-added XML declaration if the original didn't have one at top level
    # (draw.io files sometimes have their own encoding declaration)
    lines = pretty.split("\n")
    if lines[0].startswith("<?xml"):
        pretty = "\n".join(lines[1:])
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        fh.write(pretty)


def run_command(tree: ET.ElementTree, command: str, path: str) -> None:
    """Execute a single natural language command against the schematic."""
    cells   = get_all_cells(tree)
    summary = build_summary(cells)

    print(f"  Asking Claude...")
    ops = ask_claude(summary, command)
    if not ops:
        print("  No operations returned.")
        return

    messages = apply_ops(tree, ops)
    for msg in messages:
        print(f"  {msg}")

    save_drawio(tree, path)
    cells2 = get_all_cells(tree)
    d, c   = count_devices_and_connections(cells2)
    print(f"  Saved. ({d} devices, {c} connections)")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Natural language draw.io schematic editor powered by Claude."
    )
    parser.add_argument("--input",   required=True, help="Path to the .drawio file to edit")
    parser.add_argument("--command", default=None,  help="Single NL command (omit for interactive mode)")
    args = parser.parse_args()

    # Validate API key early
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY environment variable is not set.")
        sys.exit(1)

    path = args.input
    if not os.path.isfile(path):
        print(f"ERROR: File not found: {path}")
        sys.exit(1)

    tree  = parse_drawio(path)
    cells = get_all_cells(tree)
    d, c  = count_devices_and_connections(cells)

    # Derive a friendly schematic name from filename
    base_name = os.path.splitext(os.path.basename(path))[0].replace("_", " ")
    print(f"Schematic: {base_name} ({d} devices, {c} connections)")

    if args.command:
        # Single-shot mode
        run_command(tree, args.command, path)
    else:
        # Interactive mode
        try:
            while True:
                try:
                    command = input("> ").strip()
                except EOFError:
                    print()
                    break
                if not command:
                    continue
                if command.lower() in ("exit", "quit", "q"):
                    break
                # Reload tree from disk so multiple edits stack properly
                tree = parse_drawio(path)
                run_command(tree, command, path)
                # Refresh counts for next prompt
                tree2  = parse_drawio(path)
                cells2 = get_all_cells(tree2)
                d, c   = count_devices_and_connections(cells2)
        except KeyboardInterrupt:
            print("\nBye.")


if __name__ == "__main__":
    main()
