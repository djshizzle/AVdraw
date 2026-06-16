#!/bin/bash
# pipeline.sh — Full pipeline: BOM → draw.io → EasySchematic → DXF → AI Review
# Usage: scripts/pipeline.sh <bom_file> "<Schematic Name>" [codec_ip]
#
# Requires: python3
# Optional: --codec flag for live Cisco xStatus discovery
#
# Validation stages:
#   Pre-flight  : bom_validator.py  (BOM columns, types, port counts)
#   Post-gen    : drawio_validator.py (duplicate IDs, dangling outputs)
# Both run automatically inside bom_to_drawio.py — see --no-validate to skip.

set -e

BOM="${1:?Usage: pipeline.sh <bom.csv> \"Name\" [codec_ip]}"
NAME="${2:-AV Schematic}"
CODEC="${3:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/../src"
OUTPUT_DIR="$SCRIPT_DIR/../output"
mkdir -p "$OUTPUT_DIR"

SAFE_NAME="${NAME// /_}"
DRAWIO="$OUTPUT_DIR/${SAFE_NAME}.drawio"
JSON="$OUTPUT_DIR/${SAFE_NAME}.json"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " AV Schematic Studio Pipeline"
echo " BOM:    $BOM"
echo " Name:   $NAME"
if [ -n "$CODEC" ]; then
  echo " Codec:  $CODEC (live xStatus)"
fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ────────────────────────────────────────────────────────────────────────────
# Step 0: Standalone BOM validation (informational — bom_to_drawio re-runs it)
# ────────────────────────────────────────────────────────────────────────────
echo ""
echo "Step 0/5: BOM Validation"
python3 "$SRC/bom_validator.py" --bom "$BOM" --quiet || {
  echo "✗ BOM validation failed — aborting pipeline"
  exit 1
}

# ────────────────────────────────────────────────────────────────────────────
# Step 1: BOM → draw.io  (runs BOM + drawio validators internally)
# ────────────────────────────────────────────────────────────────────────────
echo ""
echo "Step 1/5: BOM → draw.io"
CODEC_ARG=""
if [ -n "$CODEC" ]; then
  CODEC_ARG="--codec $CODEC"
fi
python3 "$SRC/bom_to_drawio.py" --bom "$BOM" --name "$NAME" --output "$DRAWIO" $CODEC_ARG

echo ""
echo "Step 2/5: draw.io → EasySchematic"
python3 "$SRC/drawio_to_easyschematic.py" --input "$DRAWIO" --output "$JSON" --name "$NAME"

echo ""
echo "Step 3/5: draw.io → DXF (AutoCAD)"
DXF="$OUTPUT_DIR/${SAFE_NAME}.dxf"
python3 "$SRC/drawio_to_dxf.py" --input "$DRAWIO" --output "$DXF" --name "$NAME"

echo ""
echo "Step 4/5: AI Signal Flow Review"
REVIEW="$OUTPUT_DIR/${SAFE_NAME}_review.json"
if [ -n "$ANTHROPIC_API_KEY" ]; then
  python3 "$SRC/ai_reviewer.py" --input "$DRAWIO" --name "$NAME"
else
  echo "  (Running local rules only — set ANTHROPIC_API_KEY for full AI review)"
  python3 "$SRC/ai_reviewer.py" --input "$DRAWIO" --name "$NAME" --no-ai
fi

# ────────────────────────────────────────────────────────────────────────────
# Step 5: Final drawio validator pass with BOM cross-check
# ────────────────────────────────────────────────────────────────────────────
echo ""
echo "Step 5/5: Final drawio Validation (with BOM cross-check)"
python3 "$SRC/drawio_validator.py" --input "$DRAWIO" --bom "$BOM" --quiet \
  && echo "✓ drawio structural validation passed" \
  || echo "⚠ drawio validator reported issues — see above"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Outputs:"
echo "   draw.io       → $DRAWIO"
echo "   EasySchematic → $JSON"
echo "   AutoCAD DXF   → $DXF"
echo "   Review Report → $REVIEW"
echo ""
echo " Next steps:"
echo "   1. Open $DRAWIO in draw.io to refine layout/connections"
echo "   2. Re-run step 2 after edits:"
echo "      python3 src/drawio_to_easyschematic.py --input $DRAWIO --output $JSON"
echo "   3. Open $JSON in EasySchematic → http://localhost:5173"
echo "      → Add Rooms, Racks, export Cable Schedule / Pack List"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
