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
from fastapi.responses import Response

from .. import ai, exports, pipeline
from ..schemas import (BuildRoomRequest, DescribeRequest, ExportRequest,
                       ParseBomRequest, RunBuildRequest)
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


@router.post("/builds/describe")
def describe(body: DescribeRequest) -> dict:
    """AI 'describe the room' → proposed equipment (Claude or heuristic)."""
    return ai.describe_room(body.brief)


def _file_response(data: bytes, fmt: str, stem: str) -> Response:
    return Response(
        content=data,
        media_type=exports.MIME.get(fmt, "application/octet-stream"),
        headers={"Content-Disposition": f'attachment; filename="{stem}.{fmt}"'},
    )


@router.post("/builds/export")
def export_build(body: ExportRequest) -> Response:
    """Stateless: build from CSV, then return the requested format as a file."""
    fmt = body.format.lower()
    if fmt not in exports.MIME:
        raise HTTPException(status_code=400, detail=f"unknown format {fmt!r}")
    csv_path = _write_temp_csv(body.csv)
    try:
        result = pipeline.run_build_from_csv(str(csv_path), body.name)
    except pipeline.BuildError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    finally:
        csv_path.unlink(missing_ok=True)
    stem = pipeline._slug(body.name)
    try:
        data = _render(fmt, result.to_dict(), result.drawio_path, body.name, {})
    except exports.ExportUnavailable as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    return _file_response(data, fmt, stem)


def _render(fmt: str, build: dict, drawio_path: str, name: str, title_block: dict) -> bytes:
    if fmt == "json":
        import json
        return json.dumps(build["schematic"], indent=2).encode("utf-8")
    if fmt == "csv":
        return exports.cable_csv(build["cableSchedule"])
    if fmt == "drawio":
        return exports.drawio_bytes(drawio_path)
    if fmt == "dxf":
        return exports.dxf_bytes(drawio_path, name)
    if fmt == "pdf":
        return exports.pdf_bytes(name, build["devices"], build["cableSchedule"], title_block)
    raise exports.ExportUnavailable(f"unsupported format {fmt!r}")


@router.get("/projects/{project_id}/rooms/{room_id}/export/{fmt}")
def export_room(project_id: str, room_id: str, fmt: str) -> Response:
    """Export a persisted room's last build in the requested format."""
    fmt = fmt.lower()
    if fmt not in exports.MIME:
        raise HTTPException(status_code=400, detail=f"unknown format {fmt!r}")
    store = get_store()
    try:
        room = store.get_room(project_id, room_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if not room.schematic:
        raise HTTPException(status_code=409, detail="room has no build yet — build it first")
    build = {
        "schematic": room.schematic,
        "cableSchedule": [c.to_dict() for c in room.cableSchedule],
        "devices": [d.to_dict() for d in room.devices],
    }
    try:
        data = _render(fmt, build, room.drawioPath, room.name, room.titleBlock.to_dict())
    except exports.ExportUnavailable as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    return _file_response(data, fmt, pipeline._slug(room.name))


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
