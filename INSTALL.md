# AVdraw — Install & Run Guide

AI-powered AV signal flow schematic toolkit.
BOM → draw.io → EasySchematic → AutoCAD DXF, with live Cisco/Webex integration.

---

## Requirements

| Tool        | Version  | Notes                                    |
|-------------|----------|------------------------------------------|
| Python      | 3.9+     | macOS: `python3 --version`               |
| Node.js     | 18+      | Required for EasySchematic only          |
| npm         | 9+       | Comes with Node.js                       |
| draw.io     | Any      | Free desktop app or app.diagrams.net     |
| git         | Any      | To clone the repo                        |

---

## 1. Clone the Repo

```bash
git clone https://github.com/djshizzle/AVdraw.git
cd AVdraw
```

---

## 2. Install Python Dependencies

```bash
pip3 install ezdxf anthropic --user
```

- `ezdxf`    — AutoCAD DXF export
- `anthropic` — AI signal flow reviewer (optional, needs API key)

Verify:
```bash
python3 -c "import ezdxf; print('ezdxf ok')"
```

---

## 3. Install & Start EasySchematic

EasySchematic is a local web app that runs in your browser.
It handles rack builder, cable schedules, and PDF export.

```bash
# Clone EasySchematic (separate repo)
git clone https://github.com/duremovich/EasySchematic.git ~/EasySchematic
cd ~/EasySchematic

# Install Node dependencies
npm install

# Generate device library (required before first launch)
npm run generate-fallback

# Start the dev server
npm run dev
```

EasySchematic is now running at: http://localhost:5173

Leave this terminal open. Come back to the AVdraw folder for the rest.

```bash
cd ~/AVdraw
```

---

## 4. Install draw.io (Desktop App)

Download from: https://github.com/jgraph/drawio-desktop/releases

Or use the web version (no install): https://app.diagrams.net

---

## 5. (Optional) Set API Keys

### Anthropic — for AI signal flow review and natural language editor

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Add to your shell profile to make it permanent:
```bash
echo 'export ANTHROPIC_API_KEY=sk-ant-...' >> ~/.zshrc
source ~/.zshrc
```

### Webex — for live device discovery via cloud API

```bash
export WEBEX_TOKEN=your_token_here
```

Get a token at: https://developer.webex.com
(Personal Access Token works — valid 12 hours. For persistent use, create a Bot.)

---

## 6. Quick Start — Full Pipeline

### Step 1: Create or edit your BOM

Start from the included template:
```bash
cp templates/bom_template.csv my_room.csv
```

Edit my_room.csv — minimum required columns:
```
Name, Type, Model
```

Full column reference is in the template headers (44 columns documented).

### Step 2: Run the pipeline

```bash
bash scripts/pipeline.sh my_room.csv "My Room Name"
```

This runs 4 steps automatically:
```
Step 1/4  BOM → draw.io schematic
Step 2/4  draw.io → EasySchematic JSON
Step 3/4  draw.io → AutoCAD DXF
Step 4/4  AI signal flow review (local rules, or full AI if key is set)
```

Outputs land in the `output/` folder:
```
output/
  My_Room_Name.drawio       ← open in draw.io to refine
  My_Room_Name.json         ← open in EasySchematic at http://localhost:5173
  My_Room_Name.dxf          ← send to client for AutoCAD
  My_Room_Name_review.json  ← AI reviewer findings
```

---

## 7. Individual Tools

### BOM Validator (pre-flight)

```bash
python3 src/bom_validator.py --bom my_room.csv
python3 src/bom_validator.py --bom my_room.csv --strict   # warnings → errors
```

Checks: required columns, known type whitelist, port count integers,
quantity sanity, IP/MAC format. Outputs human-readable report. Exit code 0
if valid, 1 if errors, 2 if file unreadable.

### draw.io Validator (post-generation)

```bash
python3 src/drawio_validator.py --input output/room.drawio
python3 src/drawio_validator.py --input output/room.drawio --bom my_room.csv
```

Checks: well-formed XML, no duplicate cell IDs, at least one device,
dangling outputs, orphan devices, recognised signal colors. With `--bom`,
cross-checks that every BOM row produced a swimlane in the drawio.

### BOM → draw.io

```bash
python3 src/bom_to_drawio.py \
  --bom my_room.csv \
  --name "Boardroom A" \
  --output output/boardroom_a.drawio
```

By default this auto-runs `bom_validator` (pre-flight) and `drawio_validator`
(post-gen). Use `--no-validate` to skip, `--strict` to fail on any warning.

### draw.io → EasySchematic

```bash
python3 src/drawio_to_easyschematic.py \
  --input output/boardroom_a.drawio \
  --output output/boardroom_a.json \
  --name "Boardroom A"
```

Open output/boardroom_a.json in EasySchematic (http://localhost:5173):
- Assign devices to rooms
- Build rack elevations (Rack Builder)
- Export cable schedule, pack list, network report, power report

### draw.io → AutoCAD DXF

```bash
python3 src/drawio_to_dxf.py \
  --input output/boardroom_a.drawio \
  --output output/boardroom_a.dxf \
  --name "Boardroom A"
```

Compatible with AutoCAD 2013+, BricsCAD, DraftSight, LibreCAD.

### AI Signal Flow Reviewer

```bash
# Local rules only (no API key needed)
python3 src/ai_reviewer.py \
  --input output/boardroom_a.drawio \
  --name "Boardroom A" \
  --no-ai

# Full AI review (requires ANTHROPIC_API_KEY)
python3 src/ai_reviewer.py \
  --input output/boardroom_a.drawio \
  --name "Boardroom A"
```

Checks: output→output wiring, orphaned devices, codec missing camera/display,
speakers without amp, and more.

### Natural Language Schematic Editor

```bash
# Interactive mode (requires ANTHROPIC_API_KEY)
python3 src/nl_editor.py --input output/boardroom_a.drawio

# Single command
python3 src/nl_editor.py \
  --input output/boardroom_a.drawio \
  --command "Add a Shure MXA920 ceiling mic connected to the Dante network"
```

---

## 8. Cisco / Webex xStatus Integration

Compares your draw.io schematic against what the codec actually reports.
Flags MATCHED / MISSING / EXTRA devices and can auto-patch the schematic.

### Option A — Direct codec (on-prem / VPN)

```bash
python3 src/xstatus_diff.py \
  --input output/boardroom_a.drawio \
  --codec 192.168.1.100 \
  --username admin --password cisco
```

### Option B — Saved xStatus file (offline)

```bash
python3 src/xstatus_diff.py \
  --input output/boardroom_a.drawio \
  --xstatus saved_xstatus.xml
```

### Option C — Webex cloud API (bot or personal access token)

```bash
# List all devices your token can see
python3 src/xstatus_diff.py --list-devices

# Diff by device display name
python3 src/xstatus_diff.py \
  --input output/boardroom_a.drawio \
  --webex-device "Boardroom A"

# Diff by device ID (from --list-devices output)
python3 src/xstatus_diff.py \
  --input output/boardroom_a.drawio \
  --webex-device-id Y2lzY2Fzc2...

# Auto-add missing devices to the schematic
python3 src/xstatus_diff.py \
  --input output/boardroom_a.drawio \
  --webex-device "Boardroom A" \
  --patch
```

---

## 9. AvaI Webhook (REST API)

Run AVdraw as an HTTP service for AvaI BuildReadinessAgent or any other
caller. Useful for batch processing 500+ rooms.

### Install FastAPI deps

```bash
pip3 install fastapi uvicorn --user
```

### Start the server

```bash
python3 src/avai_webhook.py --host 0.0.0.0 --port 8765
```

Optional auth — set `AVDRAW_API_KEY=secret` env var, then clients send
`X-API-Key: secret` header.

### Endpoints

```
GET  /health
POST /generate    full pipeline: BOM → drawio + json + dxf + review
POST /validate    run bom_validator and/or drawio_validator
POST /diff        xstatus_diff against codec / file / Webex device
```

### Example: generate a schematic

```bash
curl -X POST http://localhost:8765/generate \
  -H 'Content-Type: application/json' \
  -d '{
    "bom_csv": "templates/sample_bom.csv",
    "room_name": "Boardroom Pro",
    "skip_dxf": false,
    "skip_review": false
  }'
```

Response includes paths to generated files and AI review findings.

`bom_csv` accepts either an absolute path, a path relative to the repo
root, OR raw CSV content as a string (the server writes it to a temp
file).

---

## 10. Load AV Shapes in draw.io

A complete AV device shape library is included if you cloned
https://github.com/Fe-Lit/Drawio-AV-Design to ~/Drawio-AV-Design.

In draw.io:
1. File > Open Library
2. Select ~/Drawio-AV-Design/AV_Devices_Complete.xml
3. AV shapes appear in the left panel

---

## Folder Structure

```
AVdraw/
├── src/
│   ├── bom_to_drawio.py           BOM + xStatus → draw.io XML
│   ├── drawio_to_easyschematic.py draw.io → EasySchematic JSON
│   ├── drawio_to_dxf.py           draw.io → AutoCAD DXF
│   ├── ai_reviewer.py             Signal flow validator (local + Claude AI)
│   ├── xstatus_diff.py            Live codec vs schematic diff
│   └── nl_editor.py               Natural language schematic editor
├── scripts/
│   └── pipeline.sh                Full 4-step pipeline
├── templates/
│   ├── bom_template.csv           44-column BOM template with docs
│   └── sample_bom.csv             Quick-start 10-device example
├── docs/
│   └── signal_colors.md           Signal type → color reference
├── output/                        Generated files go here
├── INSTALL.md                     This file
└── README.md                      Project overview
```

---

## Signal Color Reference

| Signal Type  | draw.io Color | DXF Layer       |
|--------------|---------------|-----------------|
| HDMI         | #d6b656       | SIGNAL_HDMI     |
| SDI          | #6d8764       | SIGNAL_SDI      |
| Dante        | #7030a0       | SIGNAL_DANTE    |
| Ethernet     | #006EAF       | SIGNAL_ETHERNET |
| USB          | #0070c0       | SIGNAL_USB      |
| Speaker      | #ff0000       | SIGNAL_SPEAKER  |
| Analog Audio | #ff6600       | SIGNAL_ANALOG   |
| NDI          | #e36c09       | SIGNAL_NDI      |
| DisplayPort  | #0070c0       | SIGNAL_DP       |
| Fiber        | #00b0f0       | SIGNAL_FIBER    |
| HDBaseT      | #70ad47       | SIGNAL_HDBASET  |
| RS-422       | #ffc000       | SIGNAL_RS422    |
| GPIO         | #ffc000       | SIGNAL_GPIO     |
| RF           | #808080       | SIGNAL_RF       |

---

## Troubleshooting

**"No module named ezdxf"**
```bash
pip3 install ezdxf --user
```

**"No module named anthropic"**
```bash
pip3 install anthropic --user
```

**EasySchematic blank on load**
```bash
cd ~/EasySchematic
npm run generate-fallback   # regenerate device library
npm run dev                  # restart dev server
```

**Webex API 401 Unauthorized**
Your token expired (personal tokens last 12 hours).
Go to developer.webex.com and copy a fresh token, or use a Bot token which doesn't expire.

**draw.io lines are curved**
In draw.io: Edit > Select All, then Format > Connection > Waypoints: Orthogonal.
Or right-click a connector > Edit Style > ensure `edgeStyle=orthogonalEdgeStyle;rounded=0;`

**Pipeline fails on Step 3 (DXF)**
```bash
python3 -c "import ezdxf; print(ezdxf.__version__)"
# Should be 1.0+ — if missing: pip3 install ezdxf --user
```
