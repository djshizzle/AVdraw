"""Export service — turn a built room into deliverable files.

Formats:
  • drawio  — the generated source diagram (always available)
  • json    — EasySchematic project (always available)
  • csv     — cable schedule (always available)
  • dxf     — AutoCAD, via src/drawio_to_dxf (needs `ezdxf`)
  • pdf     — printable schedule + equipment + title block (needs `reportlab`)

Optional dependencies fail soft: callers get :class:`ExportUnavailable` with a
clear install hint instead of a 500.
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path
from typing import Any, Optional

from . import config

if str(config.SRC_DIR) not in sys.path:
    sys.path.insert(0, str(config.SRC_DIR))


class ExportUnavailable(RuntimeError):
    """An optional export dependency isn't installed."""


MIME = {
    "drawio": "application/xml",
    "json": "application/json",
    "csv": "text/csv",
    "dxf": "application/dxf",
    "pdf": "application/pdf",
}


def cable_csv(cable_schedule: list[dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "From", "To", "Signal", "Length"])
    for c in cable_schedule:
        w.writerow([c.get("id", ""), c.get("fromRef", ""), c.get("toRef", ""),
                    c.get("signalType", ""), c.get("length", "")])
    return buf.getvalue().encode("utf-8")


def drawio_bytes(drawio_path: str) -> bytes:
    p = Path(drawio_path)
    if not p.exists():
        raise ExportUnavailable(f"draw.io file not found: {drawio_path} — rebuild the room first")
    return p.read_bytes()


def dxf_bytes(drawio_path: str, name: str) -> bytes:
    try:
        import ezdxf  # noqa: F401
        import drawio_to_dxf as dxf_mod
    except ModuleNotFoundError:
        raise ExportUnavailable("DXF export needs the 'ezdxf' package — pip install ezdxf")
    p = Path(drawio_path)
    if not p.exists():
        raise ExportUnavailable(f"draw.io file not found: {drawio_path} — rebuild the room first")
    devices, edges = dxf_mod.parse_drawio(str(p))
    doc = dxf_mod.build_dxf(devices, edges, name)
    out = config.OUTPUT_DIR / (p.stem + ".dxf")
    doc.saveas(str(out))
    return out.read_bytes()


def pdf_bytes(name: str, devices: list[dict], cable_schedule: list[dict],
              title_block: Optional[dict] = None) -> bytes:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                        Paragraph, Spacer)
    except ModuleNotFoundError:
        raise ExportUnavailable("PDF export needs the 'reportlab' package — pip install reportlab")

    tb = title_block or {}
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                            leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm,
                            title=f"{name} — System Schematic")
    styles = getSampleStyleSheet()
    story: list = []

    story.append(Paragraph(f"<b>{name}</b> — System Schematic", styles["Title"]))
    meta = " &nbsp;·&nbsp; ".join(filter(None, [
        f"Job {tb.get('jobNo')}" if tb.get("jobNo") else "",
        f"Client {tb.get('client')}" if tb.get("client") else "",
        f"Drawn by {tb.get('drawnBy')}" if tb.get("drawnBy") else "",
        f"Rev {tb.get('revision', 'A')}",
    ]))
    story.append(Paragraph(meta, styles["Normal"]))
    story.append(Spacer(1, 8 * mm))

    # Equipment list
    story.append(Paragraph("<b>Equipment</b>", styles["Heading2"]))
    eq = [["Device", "Type", "Model", "Qty"]]
    for d in devices:
        eq.append([d.get("name", ""), d.get("type", ""), d.get("model", ""),
                   str(d.get("quantity", 1))])
    t = Table(eq, repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f1f1f")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c9c7c0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f6f5f1")]),
    ]))
    story.append(t)
    story.append(Spacer(1, 8 * mm))

    # Cable schedule
    story.append(Paragraph(f"<b>Cable schedule</b> — {len(cable_schedule)} runs", styles["Heading2"]))
    cs = [["ID", "From", "To", "Signal", "Len"]]
    for c in cable_schedule:
        cs.append([c.get("id", "").replace("cbl-", ""), c.get("fromRef", ""),
                   c.get("toRef", ""), c.get("signalType", ""), c.get("length", "")])
    t2 = Table(cs, repeatRows=1, hAlign="LEFT")
    t2.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f1f1f")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c9c7c0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f6f5f1")]),
    ]))
    story.append(t2)

    doc.build(story)
    return buf.getvalue()
