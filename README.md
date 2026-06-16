# AV Schematic Studio

A workflow toolkit that bridges **draw.io** (drafting) and **EasySchematic** (engineering deliverables).

**🔒 Local-first by design** — runs 100% on your computer. No cloud account
required. Optional AI/Webex integrations are strictly opt-in. See
[docs/LOCAL_FIRST.md](docs/LOCAL_FIRST.md) for the full data-handling story.

```
BOM / xStatus
     ↓
  bom_to_drawio.py     ← draft in draw.io: clean lines, fast layout
     ↓
  drawio_to_easyschematic.py  ← convert to EasySchematic for:
                                   Rack Builder · Room groups · Cable schedule
                                   Device docs · Pack list · PDF export
```

## Structure

```
av-schematic-studio/
├── src/
│   ├── bom_to_drawio.py           # BOM + xStatus → draw.io
│   ├── drawio_to_easyschematic.py # draw.io → EasySchematic JSON
│   └── xstatus.py                 # Cisco xStatus fetch/parse (shared)
├── scripts/
│   └── pipeline.sh                # One-shot: BOM → drawio → EasySchematic
├── templates/
│   └── sample_bom.csv             # BOM template
└── docs/
    └── signal_colors.md           # Signal type → color reference
```

## Quick Start

```bash
# 1. BOM → draw.io (draft)
python3 src/bom_to_drawio.py \
  --bom my_room.csv \
  --name "Boardroom A" \
  --output output/boardroom_a.drawio

# 2. Open boardroom_a.drawio in draw.io, tweak layout/connections

# 3. draw.io → EasySchematic (engineering)
python3 src/drawio_to_easyschematic.py \
  --input output/boardroom_a.drawio \
  --output output/boardroom_a.json

# 4. Open boardroom_a.json in EasySchematic (http://localhost:5173)
#    → Add rooms, assign devices to racks, export cable schedule/PDF

# Full pipeline (steps 1 + 3):
scripts/pipeline.sh my_room.csv "Boardroom A"
```

## draw.io → EasySchematic mapping

| draw.io                  | EasySchematic                        |
|--------------------------|--------------------------------------|
| Swimlane container       | DeviceNode                           |
| Input section rows       | Input ports                          |
| Output section rows      | Output ports                         |
| Info rows (Ethernet etc) | Bidirectional ports                  |
| Edge strokeColor         | signalType (color → type reverse map)|
| edge label               | connection cableId                   |
| x/y geometry             | node position                        |

## EasySchematic extras (not in draw.io)

After importing into EasySchematic you can:
- Right-click → assign device to a **Room** (drag border to group)
- Use **Rack Builder** pages to build rack elevations
- Run **Reports** → Cable Schedule, Pack List, Network Report, Power Report
- **File > Save** → portable `.json` project file

## Signal colors

| Signal        | draw.io color | EasySchematic type |
|---------------|---------------|--------------------|
| HDMI          | #d6b656       | hdmi               |
| SDI           | #6d8764       | sdi                |
| Dante         | #7030a0       | dante              |
| Ethernet      | #006EAF       | ethernet           |
| USB           | #0070c0       | usb                |
| Speaker       | #ff0000       | speaker-level      |
| Analog Audio  | #ff6600       | analog-audio       |
| NDI           | #e36c09       | ndi                |
| DisplayPort   | #0070c0       | displayport        |
| Fiber         | #00b0f0       | fiber              |
| HDBaseT       | #70ad47       | hdbaset            |

## Cisco xStatus integration

Pull live device discovery from any Cisco codec:

```bash
python3 src/bom_to_drawio.py \
  --bom my_room.csv \
  --codec 192.168.1.100 \
  --username admin --password cisco \
  --output output/room_live.drawio
```

Discovered peripherals (cameras, touch panels, mics) are placed
automatically and wired to the codec.
