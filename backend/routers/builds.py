"""Build endpoints — the single-room core loop and the BOM-paste proposal.

Maps to the wireframe flow:
  • POST /builds/parse-bom  → "BOM paste + auto-map" / proposal (no persist)
  • POST /builds/run        → one-shot loop from raw CSV (no persist)
  • POST /projects/{pid}/rooms/{rid}/build → run loop on a stored room (persist)
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException

from .. import pipeline
from ..schemas import BuildRoomRequest, ParseBomRequest, RunBuildRequest
from ..store import NotFoundError, get_store

router = APIRouter(tags=["builds"])


def _write_temp_csv(csv_text: str) -> Path:
    tmp = tempfile.NamedTemporaryFile(
        prefix="bom_", suffix=".csv", delete=False, mode="w", encoding="utf-8"
    )
    tmp.write(csv_text)
    tmp.close()
    return Path(tmp.name)


@router.post("/builds/parse-bom")
def parse_bom(body: ParseBomRequest) -> dict:
    """Parse a pasted BOM into a proposed device list (no schematic, no save)."""
    csv_path = _write_temp_csv(body.csv)
    try:
        bom_result = pipeline.bom_validator.validate_bom(str(csv_path))
        rows = pipeline.bom_to_drawio.load_bom(str(csv_path))
        devices = pipeline.devices_from_bom_rows(rows)
    finally:
        csv_path.unlink(missing_ok=True)
    return {
        "devices": [d.to_dict() for d in devices],
        "validation": pipeline.ValidationReport.from_result(bom_result).to_dict(),
    }


@router.post("/builds/run")
def run_build(body: RunBuildRequest) -> dict:
    """One-shot core loop from raw CSV text (stateless)."""
    csv_path = _write_temp_csv(body.csv)
    try:
        result = pipeline.run_build_from_csv(
            str(csv_path), body.name, strict=body.strict
        )
    except pipeline.BuildError as exc:
        detail = {"message": str(exc)}
        if exc.report:
            detail["validation"] = exc.report.to_dict()
        raise HTTPException(status_code=422, detail=detail)
    finally:
        csv_path.unlink(missing_ok=True)
    return result.to_dict()


@router.post("/projects/{project_id}/rooms/{room_id}/build")
def build_room(project_id: str, room_id: str, body: BuildRoomRequest) -> dict:
    """Run the core loop on a stored room and persist the result."""
    store = get_store()
    try:
        room = store.get_room(project_id, room_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    try:
        result = pipeline.run_room_build(room, strict=body.strict)
    except pipeline.BuildError as exc:
        detail = {"message": str(exc)}
        if exc.report:
            detail["validation"] = exc.report.to_dict()
        raise HTTPException(status_code=422, detail=detail)
    store.save_room_build(project_id, room_id, result)
    return result.to_dict()
