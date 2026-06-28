# Task: Backend skeleton + single-room core loop

Source of truth: `design/AV Schematic Builder - Wireframes (standalone).html`
(7 screens × 3 directions): Projects/Builds list → New build (input) →
Proposal/Review → Canvas/Schematic → Port editor → Cable schedule → Export.

## Decisions
- **Stack:** Python 3.10+ / FastAPI. Reuses existing `src/` pipeline
  (`bom_to_drawio.build_drawio`, `drawio_to_easyschematic.parse_drawio` +
  `build_schematic`, `bom_validator.validate_bom`, `drawio_validator`).
- **Local-first:** core domain + pipeline are stdlib-only (no web deps needed
  to run the loop). FastAPI is the optional transport layer with graceful
  fallback if not installed (same pattern as `avai_webhook.py`).
- **Persistence:** JSON-file store under `data/` (gitignored). Hierarchy
  Project → Room → Device(+Ports) → Connection, faithful to the wireframes.

## Plan
- [x] 1. Read design handoff + existing pipeline surface.
- [x] 2. `backend/domain.py` — dataclasses: Project, Room, Device, Port,
      CableRun, TitleBlock (+ to/from dict).
- [x] 3. `backend/store.py` — JSON-file repo (CRUD projects/rooms/devices),
      atomic + locked.
- [x] 4. `backend/pipeline.py` — **single-room core loop**:
      devices/BOM → validate → drawio → validate → EasySchematic → cable
      schedule. Reuses src/ modules; writes artifacts to `output/`.
- [x] 5. `backend/schemas.py` — pydantic request models (API layer).
- [x] 6. `backend/routers/` — health, projects (+rooms), builds.
- [x] 7. `backend/main.py` — app factory, CORS, optional API-key gate,
      graceful fallback if fastapi missing.
- [x] 8. `backend/requirements.txt`, `backend/README.md`, `.gitignore` data/.

## Result (verified)
- `python3 -m backend.pipeline --bom templates/sample_bom.csv` →
  16 nodes, 25 edges, 25 cables, both validators green.
- Full HTTP flow via TestClient: create project/room → parse-bom proposal
  (10 devices) → accept devices → build+persist → one-shot run. All pass.
- `import backend.main` succeeds with fastapi absent (`app is None`).

## DONE — scaffold + single-room core loop complete.

## Risk
- src/ modules are CLI-first; `parse_drawio`/`validate_bom` take file paths →
  core loop writes intermediate files to `output/` then parses (acceptable).
- fastapi/pydantic not installed in this env → keep core loop stdlib-only so
  it's verifiable now; web layer degrades gracefully.

## Verification
- Run core loop directly on `templates/sample_bom.csv` → assert it produces a
  valid .drawio, EasySchematic JSON with N nodes, and a non-empty cable
  schedule. (No web deps required.)
- `python3 -c "import backend.main"` imports cleanly.
