#!/bin/bash
# pipeline.sh — Full pipeline: BOM → draw.io → EasySchematic
# Usage: scripts/pipeline.sh <bom_file> "<Schematic Name>" [codec_ip]
#
# Requires: python3
# Optional: --codec flag for live Cisco xStatus discovery

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

# Step 1: BOM → draw.io
echo ""
echo "Step 1/2: BOM → draw.io"
CODEC_ARG=""
if [ -n "$CODEC" ]; then
  CODEC_ARG="--codec $CODEC"
fi
python3 "$SRC/bom_to_drawio.py" --bom "$BOM" --name "$NAME" --output "$DRAWIO" $CODEC_ARG

echo ""
echo "Step 2/2: draw.io → EasySchematic"
python3 "$SRC/drawio_to_easyschematic.py" --input "$DRAWIO" --output "$JSON" --name "$NAME"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Outputs:"
echo "   draw.io      → $DRAWIO"
echo "   EasySchematic → $JSON"
echo ""
echo " Next steps:"
echo "   1. Open $DRAWIO in draw.io to refine layout/connections"
echo "   2. Re-run step 2 after edits:"
echo "      python3 src/drawio_to_easyschematic.py --input $DRAWIO --output $JSON"
echo "   3. Open $JSON in EasySchematic → http://localhost:5173"
echo "      → Add Rooms, Racks, export Cable Schedule / Pack List"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
