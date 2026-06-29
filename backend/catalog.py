"""Product catalog / library.

Backs the "Pick from catalog" input door and the "Add a device" picker.
Sourced from the rich BOM template (``templates/bom_template.csv``) — the same
schema the pipeline already understands — so catalog items drop straight into a
build with their port counts intact.
"""

from __future__ import annotations

import csv
import io
from functools import lru_cache
from typing import Any

from . import config

# Port-count columns we surface as catalog metadata / device attrs.
PORT_COLUMNS = [
    "hdmi_in", "hdmi_out", "sdi_in", "sdi_out", "displayport_in", "displayport_out",
    "usb_in", "usb_out", "ethernet_ports", "dante_ports", "ndi_ports",
    "hdbaset_in", "hdbaset_out", "analog_audio_in", "analog_audio_out",
    "fiber_ports", "rf_in", "rf_out",
]


def _read_rows() -> list[dict[str, str]]:
    path = config.REPO_ROOT / "templates" / "bom_template.csv"
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#")]
    reader = csv.DictReader(io.StringIO("".join(lines)))
    reader.fieldnames = [(c or "").strip().lower() for c in (reader.fieldnames or [])]
    rows = []
    for row in reader:
        rows.append({(k or "").strip().lower(): (v or "").strip() for k, v in row.items() if k})
    return rows


@lru_cache(maxsize=1)
def load_catalog() -> list[dict[str, Any]]:
    """Return catalog products with normalised fields and non-zero port counts."""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in _read_rows():
        name = row.get("name", "")
        model = row.get("model", "")
        if not name:
            continue
        key = f"{name}|{model}".lower()
        if key in seen:
            continue
        seen.add(key)
        ports = {c: row[c] for c in PORT_COLUMNS if str(row.get(c, "")).strip() not in ("", "0")}
        items.append({
            "name": name,
            "type": row.get("type", ""),
            "manufacturer": row.get("manufacturer", ""),
            "model": model,
            "notes": row.get("notes", ""),
            "ports": ports,
        })
    return items


def categories() -> list[str]:
    return sorted({i["type"] for i in load_catalog() if i["type"]})
