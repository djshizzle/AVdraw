"""Project + room CRUD — backs the list / card-grid / building-map screens."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..domain import Device
from ..schemas import (
    CreateProjectRequest,
    CreateRoomRequest,
    SetDevicesRequest,
    UpdateProjectRequest,
    UpdateRoomRequest,
)
from ..store import NotFoundError, get_store

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("")
def list_projects() -> dict:
    """Project summaries (no nested rooms) — the projects list view."""
    store = get_store()
    return {"projects": [p.to_dict(include_rooms=False) for p in store.list_projects()]}


@router.post("", status_code=201)
def create_project(body: CreateProjectRequest) -> dict:
    store = get_store()
    project = store.create_project(
        body.name, client=body.client, status=body.status
    )
    return project.to_dict()


@router.get("/{project_id}")
def get_project(project_id: str) -> dict:
    """Full project with its room tree — the room-tree / building-map view."""
    try:
        return get_store().get_project(project_id).to_dict()
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.patch("/{project_id}")
def update_project(project_id: str, body: UpdateProjectRequest) -> dict:
    try:
        return get_store().update_project(
            project_id, name=body.name, status=body.status, client=body.client
        ).to_dict()
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: str) -> None:
    try:
        get_store().delete_project(project_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ── Rooms ────────────────────────────────────────────────────────────────────
@router.post("/{project_id}/rooms", status_code=201)
def add_room(project_id: str, body: CreateRoomRequest) -> dict:
    try:
        room = get_store().add_room(project_id, body.name, building=body.building)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return room.to_dict()


@router.get("/{project_id}/rooms/{room_id}")
def get_room(project_id: str, room_id: str) -> dict:
    try:
        return get_store().get_room(project_id, room_id).to_dict()
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.patch("/{project_id}/rooms/{room_id}")
def update_room(project_id: str, room_id: str, body: UpdateRoomRequest) -> dict:
    try:
        return get_store().update_room(
            project_id, room_id, name=body.name, status=body.status,
            building=body.building, titleBlock=body.titleBlock,
        ).to_dict()
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/{project_id}/rooms/{room_id}", status_code=204)
def delete_room(project_id: str, room_id: str) -> None:
    try:
        get_store().delete_room(project_id, room_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.put("/{project_id}/rooms/{room_id}/devices")
def set_room_devices(project_id: str, room_id: str, body: SetDevicesRequest) -> dict:
    """Replace a room's device list — the proposal/review accept step."""
    from ..domain import stable_id

    devices = [
        Device(
            id=stable_id("dev", d.name, d.type, d.model),
            name=d.name, type=d.type, model=d.model,
            quantity=d.quantity, serial=d.serial, notes=d.notes,
            confidence=d.confidence, attrs=d.attrs,
        )
        for d in body.devices
    ]
    try:
        room = get_store().replace_room_devices(project_id, room_id, devices)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return room.to_dict()
