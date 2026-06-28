# AV Schematic Builder — Backend

A thin service layer over the existing `src/` pipeline (BOM → draw.io →
EasySchematic). It exposes the **single-room core loop** plus project/room
state to whatever frontend consumes it (the wireframes in
`design/AV Schematic Builder - Wireframes (standalone).html`).

## Design → backend mapping

| Wireframe screen            | Backend surface                                   |
|-----------------------------|---------------------------------------------------|
| Projects list / room tree / building map | `GET /projects`, `GET /projects/{id}` |
| New build → BOM paste/auto-map | `POST /builds/parse-bom`                        |
| Proposal / review (accept)  | `PUT /projects/{id}/rooms/{rid}/devices`          |
| Canvas / schematic          | `cableSchedule` + `schematic` in build result     |
| Cable schedule              | `cableSchedule[]` (derived from edges)            |
| Export                      | `schematic` (EasySchematic JSON) + `drawioPath`   |

## Architecture

```
backend/
├── domain.py     # stdlib dataclasses: Project ▸ Room ▸ Device ▸ Port, CableRun
├── store.py      # JSON-file repo (data/store.json), atomic writes, locked
├── pipeline.py   # ⭐ single-room core loop — reuses src/ modules unchanged
├── schemas.py    # pydantic request bodies (HTTP layer only)
├── config.py     # env-overridable paths + flags (local-first defaults)
├── routers/      # health · projects · builds
└── main.py       # FastAPI app factory (graceful no-op if fastapi absent)
```

**Local-first:** `domain`, `store`, and `pipeline` are stdlib-only. The core
loop runs with no web framework installed — FastAPI is optional transport.

## The core loop (`pipeline.py`)

```
devices/BOM ─▶ BOM CSV ─▶ validate_bom ─▶ build_drawio ─▶ .drawio
                                                 │
                          validate_drawio ◀──────┤
                                                 ▼
                          parse_drawio ─▶ build_schematic ─▶ EasySchematic JSON
                                                 │
                                                 ▼
                                     derive cable schedule
```

Run it directly (no web deps needed):

```bash
python3 -m backend.pipeline --bom templates/sample_bom.csv --name "Boardroom"
# → output/boardroom.drawio + EasySchematic JSON + cable schedule
```

## Run the API

```bash
pip3 install -r backend/requirements.txt
python3 -m backend.main --port 8000          # or: uvicorn backend.main:app --reload
# docs at http://localhost:8000/docs
```

### Endpoints

| Method | Path                                       | Purpose                          |
|--------|--------------------------------------------|----------------------------------|
| GET    | `/health`                                  | liveness + paths                 |
| GET    | `/projects`                                | list project summaries           |
| POST   | `/projects`                                | create project                   |
| GET    | `/projects/{id}`                           | full project + room tree         |
| DELETE | `/projects/{id}`                           | delete project                   |
| POST   | `/projects/{id}/rooms`                     | add room                         |
| GET    | `/projects/{id}/rooms/{rid}`               | room detail                      |
| DELETE | `/projects/{id}/rooms/{rid}`               | delete room                      |
| PUT    | `/projects/{id}/rooms/{rid}/devices`       | set devices (accept proposal)    |
| POST   | `/projects/{id}/rooms/{rid}/build`         | **run core loop, persist**       |
| POST   | `/builds/parse-bom`                         | parse pasted BOM → proposal      |
| POST   | `/builds/run`                               | one-shot core loop from CSV      |

## Configuration (env vars)

| Var                  | Default                          | Notes                       |
|----------------------|----------------------------------|-----------------------------|
| `AVDRAW_OUTPUT_DIR`  | `output/`                        | generated `.drawio`/`.csv`  |
| `AVDRAW_DATA_DIR`    | `data/`                          | JSON store (gitignored)     |
| `AVDRAW_API_KEY`     | *(unset → open)*                 | requires `X-API-Key` header |
| `AVDRAW_CORS_ORIGINS`| `localhost:5173,localhost:3000`  | comma-separated             |

## Scope / next steps

This is the **skeleton + single-room core loop**. Deliberately stubbed for a
follow-up:
- AI proposal for the "describe the room" input mode (currently BOM-only; the
  proposal endpoint parses a pasted BOM). Hook point: `builds.parse_bom`.
- Catalog / product-library browsing and "duplicate a past build".
- DXF / PDF export endpoints (logic exists in `src/drawio_to_dxf.py`).
- xStatus live enrichment (`src/xstatus.py`) on the build endpoints.
- Auth beyond the single shared API key; swap the JSON store for a real DB.
