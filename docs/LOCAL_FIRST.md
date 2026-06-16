# Local-First & Data Sovereignty

**Short answer:** Yes. By default, nothing leaves the user's computer.
AVdraw is designed exactly like draw.io / Miro desktop — files live on disk,
processing happens locally, and external services are strictly opt-in.

This document explains exactly what runs where, so a security review or an
enterprise client can verify the data-handling story before approving use.

---

## TL;DR Privacy Posture

| What                            | Default        | When does it leave the machine?              |
|---------------------------------|----------------|----------------------------------------------|
| BOM CSV (your device list)      | **Local only** | Only if user passes `ANTHROPIC_API_KEY`      |
| draw.io schematic (.drawio)     | **Local only** | Only if user passes `ANTHROPIC_API_KEY`      |
| EasySchematic JSON              | **Local only** | Never — there's no cloud path for it         |
| AutoCAD DXF                     | **Local only** | Never                                        |
| AI review report                | **Local only** | Generation calls Anthropic if key is set     |
| xStatus / device telemetry      | **Local only** | Only if user passes `--codec` or Webex flag  |
| Webex device list / xAPI calls  | **Local only** | Only if user passes `--webex-device-id/name` |

If you set zero environment variables and pass zero `--codec` /
`--webex-*` flags, this repo makes **zero outbound network connections**.
You can verify with `lsof -i` / Little Snitch / pf rules / etc.

---

## What runs where

### Always local (no network at all)

These tools read CSV/XML/JSON files and write CSV/XML/JSON files. They
never open a socket. Air-gapped machine compatible:

| Tool                              | Function                              |
|-----------------------------------|---------------------------------------|
| `src/bom_to_drawio.py` (no flags) | BOM CSV → draw.io XML                 |
| `src/drawio_to_easyschematic.py`  | draw.io → EasySchematic JSON          |
| `src/drawio_to_dxf.py`            | draw.io → AutoCAD DXF                 |
| `src/bom_validator.py`            | Strict BOM pre-flight check           |
| `src/drawio_validator.py`         | Post-generation drawio assertions     |
| `src/ai_reviewer.py --no-ai`      | Local rule-based signal flow review   |
| `scripts/pipeline.sh`             | Full 4-step pipeline, local-only      |

EasySchematic itself (the Vite dev server at `localhost:5173`) runs
entirely in your browser against your local filesystem — same model as
draw.io desktop. It never contacts an external service.

### LAN only (your own network, not the internet)

These connect to devices on the user's own local network. The codec is
typically on a 192.168.x.x / 10.x.x.x address. No traffic leaves the LAN:

| Tool                                  | Talks to                          |
|---------------------------------------|-----------------------------------|
| `bom_to_drawio.py --codec 192.168.x`  | Cisco RoomOS codec HTTP/HTTPS     |
| `xstatus_diff.py --codec 192.168.x`   | Same — pulls /Status XML          |

Auth is HTTP Basic against the codec's own admin account. No telemetry
is sent anywhere else.

### Opt-in external (only when the user provides a key)

These ONLY make external HTTP calls when the user explicitly enables them
by setting an environment variable AND passing the relevant flag.

| Trigger                              | Endpoint hit                  | Sends to provider                       |
|--------------------------------------|-------------------------------|-----------------------------------------|
| `ANTHROPIC_API_KEY` set + use AI tool| `api.anthropic.com`           | Your drawio XML (for review/NL edits)   |
| `WEBEX_TOKEN` set + `--webex-*` flag | `webexapis.com`               | Device IDs you query                    |

**Without `ANTHROPIC_API_KEY`:** `ai_reviewer.py` auto-falls back to
`--no-ai` mode (local rules only). `nl_editor.py` refuses to start.
Neither leaks anything.

**Without `WEBEX_TOKEN`:** the `--webex-device` flags are simply rejected.
The other xStatus modes (`--codec` LAN, `--xstatus` offline file) still
work.

---

## How to run fully offline

For air-gapped / SCIF / classified environments:

```bash
# 1. Make sure NO cloud env vars are set in your shell
unset ANTHROPIC_API_KEY
unset WEBEX_TOKEN
unset OPENAI_API_KEY

# 2. Run the pipeline normally — it auto-detects missing keys
bash scripts/pipeline.sh my_room.csv "My Room"

# 3. Or run individual tools without any --codec / --webex-* flags
python3 src/bom_to_drawio.py --bom my_room.csv --output room.drawio
python3 src/drawio_to_dxf.py --input room.drawio --output room.dxf
python3 src/ai_reviewer.py --input room.drawio --no-ai
```

You can verify zero outbound connections:

```bash
# macOS
sudo lsof -i -P -n | grep python

# Linux
sudo ss -tnp | grep python

# Or use Little Snitch (mac) / OpenSnitch (Linux) / pf rules to firewall it
```

---

## Why this matters

AV systems documentation often contains:
- Network IPs and VLANs (could expose attack surface)
- Device serial numbers (asset tracking)
- Floor layouts (physical security)
- Vendor / pricing data (commercial sensitivity)
- Client room names (NDAs)

Forcing all of that through a SaaS doc tool is a non-starter for many
enterprise/government/healthcare/finance clients. AVdraw treats privacy
as the default, exactly like the desktop tools (draw.io desktop, AutoCAD,
LibreOffice) that integrators already use.

---

## What about the AvaI webhook?

`src/avai_webhook.py` is a FastAPI server that **the user starts on their
own machine** (`python3 src/avai_webhook.py --port 8765`). It binds to
`0.0.0.0` by default but you can restrict it:

```bash
# Localhost only — only programs on this machine can call it
python3 src/avai_webhook.py --host 127.0.0.1 --port 8765

# Require an API key for any caller
export AVDRAW_API_KEY=$(openssl rand -hex 32)
python3 src/avai_webhook.py --host 0.0.0.0 --port 8765
# clients must now send: X-API-Key: <your-key>
```

The webhook itself makes no outbound calls beyond what the underlying
tools would make. If the user starts it without `ANTHROPIC_API_KEY` /
`WEBEX_TOKEN` set, every endpoint is local-only.

---

## Comparison to other tools

| Tool                  | Default storage      | Cloud required? |
|-----------------------|----------------------|-----------------|
| Miro                  | SaaS (their servers) | Yes             |
| Lucidchart            | SaaS                 | Yes             |
| draw.io web           | Local OR cloud       | No (with desktop)|
| draw.io desktop       | Local files          | No              |
| AutoCAD desktop       | Local files          | No              |
| **AVdraw**            | **Local files**      | **No**          |

AVdraw is local-first by design. Cloud features (Anthropic AI review,
Webex cloud xAPI) are additive integrations, never a dependency.

---

## What gets sent when you DO opt in

For full transparency:

### When `ANTHROPIC_API_KEY` is set
- `ai_reviewer.py`: sends the full drawio XML + a system prompt to
  `api.anthropic.com/v1/messages`. Receives findings JSON.
- `nl_editor.py`: sends the drawio XML + your natural-language command
  to the same endpoint. Receives an edit-operations JSON.
- **What's NOT sent:** your BOM CSV, your EasySchematic JSON, your DXF,
  your codec IP/credentials. Only the drawio XML (which has device names
  and connection topology — no IPs, MACs, serials, or pricing).

If even the drawio XML is too sensitive to leave the machine, set
`--no-ai` on `ai_reviewer` and skip `nl_editor` entirely. The local
rule-based reviewer catches the most common wiring errors (input→input,
orphaned devices, missing codec/camera/display, etc.) without any
external call.

### When `WEBEX_TOKEN` is set + `--webex-*` flag is passed
- `xstatus_diff.py`: makes authenticated GETs to `webexapis.com/v1` for
  device listing and xAPI status. Receives device metadata + xStatus JSON.
- **What's sent:** your bearer token (in `Authorization` header), and
  the device IDs you're querying.
- **What's NOT sent:** your drawio XML, your BOM, the diff results.
  All comparison and reporting happens locally after the xStatus is
  pulled down.

### What never leaves, even with all keys set
- BOM CSV
- EasySchematic JSON
- AutoCAD DXF
- The final diff/review report on disk
- Any credentials beyond the one explicitly set in the env var being used
