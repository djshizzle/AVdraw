#!/usr/bin/env python3
"""
bom_validator.py — Strict pre-flight BOM validation
====================================================
Validates a BOM CSV file before it goes into bom_to_drawio.py.

Two severity levels:
  ERROR    — abort the pipeline (missing required columns, malformed counts)
  WARNING  — print to stderr, continue (unknown type, suspicious values)

Usage as CLI:
    python3 src/bom_validator.py --bom my_room.csv
    python3 src/bom_validator.py --bom my_room.csv --strict   # warnings → errors

Usage as module:
    from bom_validator import validate_bom, ValidationResult

    result = validate_bom("my_room.csv")
    if not result.ok:
        sys.exit(1)
    for row in result.rows:
        ...  # row is a normalised dict with lower-case keys
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Schema constants — mirror templates/bom_template.csv
# ---------------------------------------------------------------------------

REQUIRED_COLUMNS = {"name", "type", "model"}

# Required values per row — model is recommended but not strictly required
# (logical groupings like "Dante Network" or "Ceiling Zone" may have no model)
REQUIRED_VALUES = {"name", "type"}

# Canonical type whitelist (lower-case, from bom_template.csv header docs).
# Anything outside this set still works but generates a WARNING — the
# bom_to_drawio.py type map falls back to "device" for unknown types.
KNOWN_TYPES = {
    "codec", "camera", "display", "projector", "microphone", "speaker",
    "amplifier", "dsp", "dante-device", "audio-interface",
    "wireless-mic", "wireless-receiver", "switcher", "switch", "router",
    "encoder", "decoder", "ndi-device", "pc", "laptop", "touch-panel",
    "streaming-device", "patch-panel", "ups", "generic",
    # Common aliases seen in real-world BOMs — accepted without warning
    "monitor",         # → display
    "amp",             # → amplifier
    "mic",             # → microphone
    "video-conferencing",  # → codec
    "control-panel",   # → touch-panel
    "touch panel",     # → touch-panel (Title Case from sample_bom.csv)
    "audio mixer",     # → audio-interface
    "network",         # → audio-interface
    "device",
}

# Port-count columns — must be non-negative integers when present
PORT_COLUMNS = {
    "hdmi_in", "hdmi_out",
    "sdi_in", "sdi_out",
    "displayport_in", "displayport_out",
    "usb_in", "usb_out",
    "ethernet_ports",
    "dante_ports",
    "ndi_ports",
    "hdbaset_in", "hdbaset_out",
    "speaker_in", "speaker_out",
    "analog_audio_in", "analog_audio_out",
    "rf_in",
    "fiber_ports",
    "rs422_ports",
    "gpio_ports",
}

# Column aliases (alias → canonical). Validation normalises before checking.
COLUMN_ALIASES = {
    "device_name":    "name",
    "device":         "name",
    "device_type":    "type",
    "model_number":   "model",
    "model_no":       "model",
    "serial_number":  "serial",
    "sn":             "serial",
    "ip":             "ip_address",
    "mac":            "mac_address",
    "xlr_in":         "analog_audio_in",
    "xlr_out":        "analog_audio_out",
    "manufacturer":   "manufacturer",  # idempotent — keep
}

IP_REGEX  = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
MAC_REGEX = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$|^[0-9A-Fa-f]{12}$")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ValidationIssue:
    severity: str      # "ERROR" | "WARNING"
    row:      int      # CSV row number (1 = header, 2 = first data row)
    column:   str      # column name or "" for row-level issues
    message:  str

    def format(self) -> str:
        loc = f"row {self.row}"
        if self.column:
            loc += f" col '{self.column}'"
        return f"[{self.severity}] {loc}: {self.message}"


@dataclass
class ValidationResult:
    ok:       bool                = True
    errors:   list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)
    rows:     list[dict]           = field(default_factory=list)
    columns:  list[str]            = field(default_factory=list)

    def add_error(self, row: int, column: str, msg: str) -> None:
        self.errors.append(ValidationIssue("ERROR", row, column, msg))
        self.ok = False

    def add_warning(self, row: int, column: str, msg: str) -> None:
        self.warnings.append(ValidationIssue("WARNING", row, column, msg))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _normalise_columns(fieldnames: list[str]) -> list[str]:
    """Lower-case, strip, and apply alias map."""
    out = []
    for f in fieldnames or []:
        key = (f or "").strip().lower()
        out.append(COLUMN_ALIASES.get(key, key))
    return out


def _strip_comments(lines: list[str]) -> list[str]:
    """Drop lines starting with '#' (comment headers in bom_template.csv)."""
    return [ln for ln in lines if not ln.lstrip().startswith("#")]


def validate_bom(path: str, strict: bool = False) -> ValidationResult:
    """
    Validate a BOM CSV and return a ValidationResult.

    Arguments:
        path:   path to BOM CSV
        strict: when True, warnings are promoted to errors

    Always returns a result object — never raises on validation failures.
    Raises FileNotFoundError / UnicodeDecodeError only on I/O problems.
    """
    result = ValidationResult()
    p = Path(path)
    if not p.exists():
        result.add_error(0, "", f"BOM file not found: {path}")
        return result

    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    clean = _strip_comments(lines)
    if not clean:
        result.add_error(0, "", "BOM file is empty (only comments)")
        return result

    reader = csv.DictReader(clean)
    raw_fields = reader.fieldnames or []
    norm_fields = _normalise_columns(raw_fields)
    result.columns = norm_fields

    # ── Column presence check ──────────────────────────────────────────────
    missing_required = REQUIRED_COLUMNS - set(norm_fields)
    if missing_required:
        result.add_error(
            1, "",
            f"Missing required column(s): {sorted(missing_required)}. "
            f"Found: {raw_fields}"
        )
        return result  # no point validating rows without required cols

    # Index map: canonical column name → raw column name (for row access)
    col_map = dict(zip(norm_fields, raw_fields))

    # ── Row-level validation ───────────────────────────────────────────────
    data_row_num = 1  # human-friendly: count non-comment data rows
    for csv_row_num, raw_row in enumerate(reader, start=2):
        # Skip rows that are entirely empty (after comment-stripping
        # csv.DictReader sometimes emits an all-empty dict)
        if all((v is None or str(v).strip() == "") for v in raw_row.values()):
            continue

        # Re-key by canonical column names
        row: dict = {}
        for canonical, raw_col in col_map.items():
            val = raw_row.get(raw_col, "")
            row[canonical] = (val or "").strip()

        # Required values present?
        for col in REQUIRED_VALUES:
            if not row.get(col):
                result.add_error(csv_row_num, col, f"empty required field '{col}'")

        # Model is recommended but not strictly required — empty = warning
        if "model" in row and not row["model"]:
            result.add_warning(
                csv_row_num, "model",
                "empty model — OK for logical groupings but flag for review"
            )

        # Type whitelist check
        type_val = row.get("type", "").lower().strip()
        if type_val and type_val not in KNOWN_TYPES:
            result.add_warning(
                csv_row_num, "type",
                f"unknown type '{type_val}' — will fall back to 'device' "
                f"(known: {sorted(KNOWN_TYPES)[:8]}...)"
            )

        # Port counts must parse as non-negative ints
        for pcol in PORT_COLUMNS & set(row.keys()):
            v = row[pcol]
            if v == "":
                continue  # blank is fine — treated as 0
            try:
                n = int(v)
            except ValueError:
                result.add_error(
                    csv_row_num, pcol,
                    f"port count '{v}' is not an integer"
                )
                continue
            if n < 0:
                result.add_error(
                    csv_row_num, pcol,
                    f"port count {n} is negative"
                )
            elif n > 256:
                result.add_warning(
                    csv_row_num, pcol,
                    f"port count {n} is suspiciously large (>256)"
                )

        # Quantity sanity
        qty = row.get("quantity", "")
        if qty:
            try:
                qn = int(qty)
                if qn <= 0:
                    result.add_error(
                        csv_row_num, "quantity",
                        f"quantity {qn} must be ≥ 1"
                    )
                elif qn > 1000:
                    result.add_warning(
                        csv_row_num, "quantity",
                        f"quantity {qn} is suspiciously large"
                    )
            except ValueError:
                result.add_error(
                    csv_row_num, "quantity",
                    f"quantity '{qty}' is not an integer"
                )

        # IP / MAC format sanity (warning only — some real BOMs use TBD etc.)
        ip = row.get("ip_address", "")
        if ip and ip.lower() not in ("tbd", "dhcp", "n/a") \
                and not IP_REGEX.match(ip):
            result.add_warning(
                csv_row_num, "ip_address",
                f"'{ip}' doesn't look like an IPv4 address"
            )

        mac = row.get("mac_address", "")
        if mac and mac.lower() not in ("tbd", "n/a") \
                and not MAC_REGEX.match(mac):
            result.add_warning(
                csv_row_num, "mac_address",
                f"'{mac}' doesn't look like a MAC address"
            )

        # Keep the row metadata for downstream tools
        row["_csv_row"] = csv_row_num
        row["_data_row"] = data_row_num
        result.rows.append(row)
        data_row_num += 1

    if not result.rows and not result.errors:
        result.add_error(0, "", "BOM has no data rows (only headers/comments)")

    # Promote warnings to errors in strict mode
    if strict and result.warnings:
        for w in result.warnings:
            result.errors.append(
                ValidationIssue("ERROR", w.row, w.column,
                                f"(strict) {w.message}")
            )
        result.ok = False
        result.warnings = []

    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_report(result: ValidationResult, path: str = "") -> None:
    """Print a human-readable validation report to stdout/stderr."""
    width = 72
    print("=" * width)
    print(f"  BOM Validation Report  {path}")
    print("=" * width)
    print(f"  Columns found  : {len(result.columns)}")
    print(f"  Data rows      : {len(result.rows)}")
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
        print(f"✓ BOM is valid ({len(result.rows)} rows accepted)")
    else:
        print(f"✗ BOM has {len(result.errors)} error(s) — aborting", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate an AVdraw BOM CSV file"
    )
    parser.add_argument("--bom", "-b", required=True, help="Path to BOM CSV")
    parser.add_argument(
        "--strict", action="store_true",
        help="Treat warnings as errors (non-zero exit on any issue)"
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true",
        help="Print only errors (suppress success report)"
    )
    args = parser.parse_args()

    try:
        result = validate_bom(args.bom, strict=args.strict)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"ERROR: cannot read BOM '{args.bom}': {exc}", file=sys.stderr)
        return 2

    if args.quiet:
        for e in result.errors:
            print(e.format(), file=sys.stderr)
    else:
        print_report(result, args.bom)

    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
