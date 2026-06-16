#!/usr/bin/env python3
"""
AI-powered signal flow validator for draw.io AV schematics.

Usage:
  python3 src/ai_reviewer.py --input output/Boardroom_Pro.drawio --name 'Boardroom Pro'
  python3 src/ai_reviewer.py --input output/Boardroom_Pro.drawio --no-ai
"""

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Port:
    id: str
    value: str
    direction: str          # 'input' | 'output' | 'bidirectional'
    device_id: str
    parent_id: str          # immediate parent (device id or section id)


@dataclass
class Device:
    id: str
    value: str
    ports: list = field(default_factory=list)   # list[Port]


@dataclass
class Edge:
    id: str
    value: str
    source_port_id: str
    target_port_id: str
    signal_type: str


@dataclass
class Topology:
    devices: dict       # str -> Device
    ports: dict         # str -> Port
    edges: list         # list[Edge]


# ── XML parser ────────────────────────────────────────────────────────────────

_COLOR_TO_SIGNAL = {
    '#d6b656': 'HDMI',
    '#0070c0': 'USB',
    '#7030a0': 'Dante',
    '#ff0000': 'Speaker',
    '#006eaf': 'Ethernet',
    '#00b0f0': 'SDI',
    '#ff6600': 'DisplayPort',
}


def _signal_from_style(style: str) -> str:
    """Guess signal type from edge stroke color in style string."""
    sl = style.lower()
    for color, sig in _COLOR_TO_SIGNAL.items():
        if color in sl:
            return sig
    return 'Unknown'


def parse_drawio(filepath: str) -> Topology:
    """
    Parse a draw.io XML file and return a Topology.

    Parsing strategy (three passes):
      Pass 1 – identify device containers:
               mxCell with 'swimlane' + 'childLayout=stackLayout' in style, parent='1'
      Pass 2 – identify Input/Output section swimlanes:
               mxCell with 'swimlane' + 'childLayout=stackLayout' in style,
               parent is a known device id
      Pass 3 – collect port rows:
               mxCell with 'portConstraint=eastwest' in style and 'swimlane' NOT in style,
               parent is a device or a section
      Pass 4 – collect edges:
               mxCell with edge='1'
    """
    tree = ET.parse(filepath)
    xml_root = tree.getroot().find('.//root')
    if xml_root is None:
        raise ValueError("No <root> element found in draw.io file")

    all_cells = {c.get('id'): c for c in xml_root.findall('mxCell')}

    devices: dict[str, Device] = {}
    # sections: section_id -> (device_id, direction_str)
    sections: dict[str, tuple[str, str]] = {}
    ports: dict[str, Port] = {}
    edges: list[Edge] = []

    # Pass 1 – devices
    for cell_id, cell in all_cells.items():
        style = cell.get('style', '')
        if (
            'swimlane' in style
            and 'childLayout=stackLayout' in style
            and cell.get('parent') == '1'
        ):
            devices[cell_id] = Device(
                id=cell_id,
                value=cell.get('value', '').strip(),
            )

    # Pass 2 – section swimlanes (Input / Output headers inside devices)
    for cell_id, cell in all_cells.items():
        style = cell.get('style', '')
        parent = cell.get('parent', '')
        value = cell.get('value', '').strip()
        if (
            'swimlane' in style
            and 'childLayout=stackLayout' in style
            and parent in devices
        ):
            if 'Input' in value:
                direction = 'input'
            elif 'Output' in value:
                direction = 'output'
            else:
                direction = 'bidirectional'
            sections[cell_id] = (parent, direction)

    # Pass 3 – port rows
    for cell_id, cell in all_cells.items():
        style = cell.get('style', '')
        parent = cell.get('parent', '')
        value = cell.get('value', '').strip()

        if 'portConstraint=eastwest' not in style:
            continue
        if 'swimlane' in style:
            continue  # skip section swimlanes themselves

        if parent in sections:
            device_id, direction = sections[parent]
        elif parent in devices:
            device_id = parent
            direction = 'bidirectional'
        else:
            continue  # not reachable from a known device

        port = Port(
            id=cell_id,
            value=value,
            direction=direction,
            device_id=device_id,
            parent_id=parent,
        )
        ports[cell_id] = port
        devices[device_id].ports.append(port)

    # Pass 4 – edges
    for cell_id, cell in all_cells.items():
        if cell.get('edge') != '1':
            continue
        source = cell.get('source', '')
        target = cell.get('target', '')
        if not source or not target:
            continue  # floating edge

        value = cell.get('value', '').strip()
        style = cell.get('style', '')
        signal_type = value if value else _signal_from_style(style)

        edges.append(Edge(
            id=cell_id,
            value=value,
            source_port_id=source,
            target_port_id=target,
            signal_type=signal_type,
        ))

    return Topology(devices=devices, ports=ports, edges=edges)


# ── Device classification ─────────────────────────────────────────────────────

def classify_device(name: str) -> str:
    """Return a broad device-type label based on the device name."""
    n = name.lower()
    if 'codec' in n:
        return 'codec'
    if 'camera' in n:
        return 'camera'
    if 'display' in n:
        return 'display'
    if 'amplifier' in n:
        return 'amplifier'
    # 'amp' alone but not part of 'amplifier' (e.g. "QSC AMP8")
    if re.search(r'\bamp\b', n):
        return 'amplifier'
    if 'speaker' in n:
        return 'speaker'
    # mic but not 'receiver' (wireless receiver is separate)
    if 'mic' in n and 'receiver' not in n:
        return 'microphone'
    if 'receiver' in n:
        return 'receiver'
    if 'switch' in n:
        return 'switch'
    if 'audio' in n and ('interface' in n or 'network' in n or 'dante' in n):
        return 'audio-interface'
    if 'dante' in n:
        return 'audio-interface'
    if 'navigator' in n or 'touch' in n or 'panel' in n:
        return 'control'
    return 'unknown'


import re as _re_module
re = _re_module  # ensure re is available for classify_device


# ── Topology summary (for AI prompt) ──────────────────────────────────────────

def build_topology_summary(topo: Topology) -> str:
    """Build a plain-text topology summary suitable for the AI prompt."""
    lines: list[str] = []
    lines.append("=== AV SYSTEM TOPOLOGY SUMMARY ===")
    lines.append(f"Total devices : {len(topo.devices)}")
    lines.append(f"Total edges   : {len(topo.edges)}")
    lines.append("")

    lines.append("DEVICES:")
    for dev_id, dev in sorted(topo.devices.items(), key=lambda x: x[1].value):
        dtype = classify_device(dev.value)
        in_ports  = [p for p in dev.ports if p.direction == 'input']
        out_ports = [p for p in dev.ports if p.direction == 'output']
        bi_ports  = [p for p in dev.ports if p.direction == 'bidirectional']
        lines.append(
            f"  [{dtype.upper():16s}] {dev.value}  "
            f"(id={dev_id}, inputs={len(in_ports)}, outputs={len(out_ports)}, bidir={len(bi_ports)})"
        )
        for p in in_ports:
            lines.append(f"      IN  : {p.value}  (port {p.id})")
        for p in out_ports:
            lines.append(f"      OUT : {p.value}  (port {p.id})")

    lines.append("")
    lines.append("SIGNAL CONNECTIONS:")
    for edge in topo.edges:
        src_port = topo.ports.get(edge.source_port_id)
        tgt_port = topo.ports.get(edge.target_port_id)

        src_dev_name = (
            topo.devices[src_port.device_id].value if src_port and src_port.device_id in topo.devices
            else f"(port {edge.source_port_id})"
        )
        tgt_dev_name = (
            topo.devices[tgt_port.device_id].value if tgt_port and tgt_port.device_id in topo.devices
            else f"(port {edge.target_port_id})"
        )
        src_port_name = src_port.value if src_port else edge.source_port_id
        tgt_port_name = tgt_port.value if tgt_port else edge.target_port_id
        src_dir = src_port.direction if src_port else 'unknown'
        tgt_dir = tgt_port.direction if tgt_port else 'unknown'

        lines.append(
            f"  [{edge.signal_type:12s}] "
            f"{src_dev_name}:{src_port_name} ({src_dir})"
            f"  -->  "
            f"{tgt_dev_name}:{tgt_port_name} ({tgt_dir})"
        )

    return "\n".join(lines)


# ── Local rule-based checks ───────────────────────────────────────────────────

@dataclass
class Finding:
    severity: str   # 'ERROR' | 'WARNING' | 'SUGGESTION'
    rule: str
    message: str
    detail: str = ''


def run_local_rules(topo: Topology) -> list[Finding]:
    """
    Execute all local rule checks without any AI call.

    Rules:
      1. output -> output connection (ERROR)
      2. input  -> input  connection (ERROR)
      3. orphaned devices with zero connections (WARNING)
      4. codec must have >= 1 camera on an input port (ERROR)
      5. codec must have >= 1 display on an output port (ERROR)
      6. speaker devices must have >= 1 amplifier upstream (WARNING)
      7. microphone must not connect directly to a display (WARNING)
      8. ethernet-port devices should connect to a switch when one exists (SUGGESTION)
    """
    findings: list[Finding] = []

    # Classify every device
    device_types: dict[str, str] = {
        dev_id: classify_device(dev.value)
        for dev_id, dev in topo.devices.items()
    }

    has_switch = any(t == 'switch' for t in device_types.values())

    # Build set of ports that appear in any edge (connected ports)
    connected_port_ids: set[str] = set()
    for edge in topo.edges:
        connected_port_ids.add(edge.source_port_id)
        connected_port_ids.add(edge.target_port_id)

    # Connected device ids (a device is connected if ANY of its ports has an edge)
    connected_device_ids: set[str] = set()
    for port_id in connected_port_ids:
        port = topo.ports.get(port_id)
        if port:
            connected_device_ids.add(port.device_id)

    # Adjacency list for device-level connections
    # dev_id -> set of neighbour dev_ids (via any edge)
    dev_neighbours: dict[str, set[str]] = {dev_id: set() for dev_id in topo.devices}
    for edge in topo.edges:
        sp = topo.ports.get(edge.source_port_id)
        tp = topo.ports.get(edge.target_port_id)
        if sp and tp and sp.device_id != tp.device_id:
            dev_neighbours[sp.device_id].add(tp.device_id)
            dev_neighbours[tp.device_id].add(sp.device_id)

    # ── Rule 1 & 2: direction conflicts ──────────────────────────────────────
    for edge in topo.edges:
        sp = topo.ports.get(edge.source_port_id)
        tp = topo.ports.get(edge.target_port_id)
        if not sp or not tp:
            continue

        src_dir = sp.direction
        tgt_dir = tp.direction
        src_dev = topo.devices.get(sp.device_id)
        tgt_dev = topo.devices.get(tp.device_id)
        src_name = src_dev.value if src_dev else '?'
        tgt_name = tgt_dev.value if tgt_dev else '?'

        if src_dir == 'output' and tgt_dir == 'output':
            findings.append(Finding(
                severity='ERROR',
                rule='output-to-output',
                message='Output port connected to another output port',
                detail=(
                    f"{src_name}:{sp.value} [output]"
                    f"  -->  {tgt_name}:{tp.value} [output]"
                ),
            ))

        if src_dir == 'input' and tgt_dir == 'input':
            findings.append(Finding(
                severity='ERROR',
                rule='input-to-input',
                message='Input port connected to another input port',
                detail=(
                    f"{src_name}:{sp.value} [input]"
                    f"  -->  {tgt_name}:{tp.value} [input]"
                ),
            ))

    # ── Rule 3: orphaned devices ─────────────────────────────────────────────
    for dev_id, dev in topo.devices.items():
        if dev_id not in connected_device_ids:
            findings.append(Finding(
                severity='WARNING',
                rule='orphaned-device',
                message='Device has no connections',
                detail=f"{dev.value}  (id={dev_id}, type={device_types[dev_id]})",
            ))

    # ── Rule 4: codec needs at least one camera input ─────────────────────────
    for dev_id, dev in topo.devices.items():
        if device_types[dev_id] != 'codec':
            continue
        input_port_ids = {p.id for p in dev.ports if p.direction == 'input'}
        camera_on_input = False
        for edge in topo.edges:
            if edge.target_port_id not in input_port_ids:
                continue
            sp = topo.ports.get(edge.source_port_id)
            if sp and device_types.get(sp.device_id) == 'camera':
                camera_on_input = True
                break
        if not camera_on_input:
            findings.append(Finding(
                severity='ERROR',
                rule='codec-no-camera',
                message='Codec has no camera connected to an input',
                detail=f"{dev.value}  (id={dev_id})",
            ))

    # ── Rule 5: codec needs at least one display output ───────────────────────
    for dev_id, dev in topo.devices.items():
        if device_types[dev_id] != 'codec':
            continue
        output_port_ids = {p.id for p in dev.ports if p.direction == 'output'}
        display_on_output = False
        for edge in topo.edges:
            if edge.source_port_id not in output_port_ids:
                continue
            tp = topo.ports.get(edge.target_port_id)
            if tp and device_types.get(tp.device_id) == 'display':
                display_on_output = True
                break
        if not display_on_output:
            findings.append(Finding(
                severity='ERROR',
                rule='codec-no-display',
                message='Codec has no display connected to an output',
                detail=f"{dev.value}  (id={dev_id})",
            ))

    # ── Rule 6: speakers need an amplifier upstream ───────────────────────────
    for dev_id, dev in topo.devices.items():
        if device_types[dev_id] != 'speaker':
            continue
        input_port_ids = {p.id for p in dev.ports if p.direction == 'input'}
        amp_upstream = False
        for edge in topo.edges:
            if edge.target_port_id not in input_port_ids:
                continue
            sp = topo.ports.get(edge.source_port_id)
            if sp and device_types.get(sp.device_id) == 'amplifier':
                amp_upstream = True
                break
        if not amp_upstream:
            findings.append(Finding(
                severity='WARNING',
                rule='speaker-no-amp',
                message='Speaker has no amplifier upstream',
                detail=f"{dev.value}  (id={dev_id})",
            ))

    # ── Rule 7: microphones must not connect directly to displays ─────────────
    for dev_id, dev in topo.devices.items():
        if device_types[dev_id] != 'microphone':
            continue
        all_port_ids = {p.id for p in dev.ports}
        for edge in topo.edges:
            if edge.source_port_id not in all_port_ids:
                continue
            tp = topo.ports.get(edge.target_port_id)
            if tp and device_types.get(tp.device_id) == 'display':
                tgt_dev = topo.devices.get(tp.device_id)
                findings.append(Finding(
                    severity='WARNING',
                    rule='mic-to-display',
                    message='Microphone connected directly to display (should route via codec or audio interface)',
                    detail=f"{dev.value}  -->  {tgt_dev.value if tgt_dev else '?'}",
                ))

    # ── Rule 8: ethernet devices should connect to switch (if one exists) ─────
    if has_switch:
        switch_ids = {did for did, t in device_types.items() if t == 'switch'}
        for dev_id, dev in topo.devices.items():
            if device_types[dev_id] == 'switch':
                continue
            has_eth_port = any(
                'ethernet' in p.value.lower() or 'rj45' in p.value.lower()
                for p in dev.ports
            )
            if not has_eth_port:
                continue
            if not switch_ids.intersection(dev_neighbours[dev_id]):
                findings.append(Finding(
                    severity='SUGGESTION',
                    rule='eth-no-switch',
                    message='Device with Ethernet port has no switch connection',
                    detail=f"{dev.value}  (id={dev_id})",
                ))

    return findings


# ── AI review ────────────────────────────────────────────────────────────────

_AI_PROMPT_TEMPLATE = """\
You are an expert AV systems engineer reviewing a draw.io signal flow schematic.
Below is a topology summary. Analyse it for correctness and completeness.

{summary}

Respond with a structured review using EXACTLY these three section headers (no markdown, plain text):

ERRORS:
- <one error per line, or "None found" if there are none>

WARNINGS:
- <one warning per line, or "None found" if there are none>

SUGGESTIONS:
- <one suggestion per line, or "None found" if there are none>

Focus on:
1. Missing critical connections (codec without camera / without display, etc.)
2. Impossible signal paths (output-to-output, input-to-input)
3. Orphaned devices
4. Audio chain completeness: mic -> DSP/codec -> amp -> speaker
5. Video chain completeness: source -> codec/switcher -> display
6. AV best-practice violations
"""


def run_ai_review(summary: str) -> Optional[dict]:
    """
    Send topology summary to Claude and return a dict with keys
    'text' (raw response) and 'model'.  Returns None on failure.
    """
    try:
        import anthropic
    except ImportError:
        return None

    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return None

    client = anthropic.Anthropic(api_key=api_key)
    prompt = _AI_PROMPT_TEMPLATE.format(summary=summary)

    for model in ('claude-opus-4-5', 'claude-3-5-sonnet-20241022'):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=2048,
                messages=[{'role': 'user', 'content': prompt}],
            )
            return {
                'text': response.content[0].text,
                'model': response.model,
            }
        except Exception as exc:
            # Try next model
            print(f"  [AI] Model {model} failed: {exc}", file=sys.stderr)

    return None


def parse_ai_response(text: str) -> dict:
    """Parse Claude's structured response into errors / warnings / suggestions."""
    result: dict[str, list[str]] = {
        'errors': [],
        'warnings': [],
        'suggestions': [],
    }
    section_map = {
        'ERRORS:': 'errors',
        'WARNINGS:': 'warnings',
        'SUGGESTIONS:': 'suggestions',
    }
    current: Optional[str] = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Check section header
        for header, key in section_map.items():
            if line.startswith(header):
                current = key
                break
        else:
            if current and line.startswith('- '):
                item = line[2:].strip()
                if item.lower() not in ('none found', 'none'):
                    result[current].append(item)

    return result


# ── Report formatting ─────────────────────────────────────────────────────────

def _severity_tag(sev: str) -> str:
    return {'ERROR': '[ERROR]', 'WARNING': '[WARN] ', 'SUGGESTION': '[INFO] '}.get(sev, f'[{sev}]')


def format_local_report(findings: list[Finding], label: str) -> str:
    sep = '=' * 64
    lines = [f"\n{sep}", f"  {label}", sep]

    for severity in ('ERROR', 'WARNING', 'SUGGESTION'):
        group = [f for f in findings if f.severity == severity]
        plural = {'ERROR': 'ERRORS', 'WARNING': 'WARNINGS', 'SUGGESTION': 'SUGGESTIONS'}
        lines.append(f"\n{plural[severity]} ({len(group)}):")
        if group:
            for f in group:
                lines.append(f"  {_severity_tag(severity)} {f.message}")
                if f.detail:
                    lines.append(f"           {f.detail}")
        else:
            lines.append("  None found.")

    return "\n".join(lines)


def format_ai_report(parsed: dict, model: str) -> str:
    sep = '=' * 64
    lines = [f"\n{sep}", f"  AI REVIEW  (model: {model})", sep]

    mapping = [
        ('errors',      'ERRORS',      '[ERROR]'),
        ('warnings',    'WARNINGS',    '[WARN] '),
        ('suggestions', 'SUGGESTIONS', '[INFO] '),
    ]
    for key, label, tag in mapping:
        items = parsed[key]
        lines.append(f"\n{label} ({len(items)}):")
        if items:
            for item in items:
                lines.append(f"  {tag} {item}")
        else:
            lines.append("  None found.")

    return "\n".join(lines)


# ── Main entry point ──────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI-powered signal flow validator for draw.io AV schematics"
    )
    parser.add_argument('--input',  required=True, help='Path to the .drawio file')
    parser.add_argument('--name',   default='',    help='Human-readable schematic name')
    parser.add_argument('--no-ai',  action='store_true',
                        help='Skip AI review; run local rules only')
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    schematic_name = args.name or input_path.stem
    output_json_path = input_path.parent / f"{input_path.stem}_review.json"

    # Header
    print()
    print("AV Schematic Reviewer")
    print(f"  Schematic : {schematic_name}")
    print(f"  File      : {input_path}")
    print()

    # Parse
    print("Parsing draw.io file ...")
    topo = parse_drawio(str(input_path))
    print(
        f"  Found {len(topo.devices)} devices, "
        f"{len(topo.ports)} ports, "
        f"{len(topo.edges)} edges."
    )

    # Build topology summary (used by AI prompt)
    summary = build_topology_summary(topo)

    # Local rules
    print("\nRunning local rule checks ...")
    local_findings = run_local_rules(topo)

    print(format_local_report(local_findings, f"LOCAL RULES: {schematic_name}"))

    # JSON report skeleton
    report = {
        'schematic_name': schematic_name,
        'file': str(input_path),
        'stats': {
            'devices': len(topo.devices),
            'ports': len(topo.ports),
            'edges': len(topo.edges),
        },
        'local_checks': {
            'errors': [
                {'rule': f.rule, 'message': f.message, 'detail': f.detail}
                for f in local_findings if f.severity == 'ERROR'
            ],
            'warnings': [
                {'rule': f.rule, 'message': f.message, 'detail': f.detail}
                for f in local_findings if f.severity == 'WARNING'
            ],
            'suggestions': [
                {'rule': f.rule, 'message': f.message, 'detail': f.detail}
                for f in local_findings if f.severity == 'SUGGESTION'
            ],
        },
        'ai_review': None,
    }

    # AI review
    use_ai = not args.no_ai

    if use_ai and not os.environ.get('ANTHROPIC_API_KEY'):
        print(
            "\n[WARNING] ANTHROPIC_API_KEY is not set -- "
            "falling back to local rules only."
        )
        use_ai = False

    if use_ai:
        try:
            import anthropic  # noqa: F401
        except ImportError:
            print(
                "\n[WARNING] anthropic package not installed -- "
                "skipping AI review."
            )
            use_ai = False

    if use_ai:
        print("\nRunning AI review (may take a few seconds) ...")
        ai_response = run_ai_review(summary)
        if ai_response is None:
            print("[WARNING] AI review returned no result; showing local rules only.")
        else:
            ai_parsed = parse_ai_response(ai_response['text'])
            model_used = ai_response['model']
            print(format_ai_report(ai_parsed, model_used))
            report['ai_review'] = {
                'model': model_used,
                'raw_response': ai_response['text'],
                'errors': ai_parsed['errors'],
                'warnings': ai_parsed['warnings'],
                'suggestions': ai_parsed['suggestions'],
            }

    # Write JSON report
    with open(output_json_path, 'w', encoding='utf-8') as fh:
        json.dump(report, fh, indent=2)

    print(f"\nReport written to: {output_json_path}")


if __name__ == '__main__':
    main()
