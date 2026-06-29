"""JSON-file-backed repository for projects / rooms / devices.

Deliberately simple (local-first, no DB). One JSON document under
``data/store.json`` holds every project. Good enough for a single-machine
build tool and trivially swappable for a real DB later — all access goes
through the ``Store`` class.

Thread-safety: a process-wide lock guards read-modify-write cycles so the
FastAPI layer (which may serve concurrent requests) can't corrupt the file.
"""

from __future__ import annotations

import json
import threading
from typing import Optional

from . import config
from .domain import Device, Project, Room, stable_id


class NotFoundError(KeyError):
    """Raised when a requested project/room/device id does not exist."""


class Store:
    """In-memory project map persisted to a single JSON file."""

    def __init__(self, path=None) -> None:
        self._path = path or config.STORE_PATH
        self._lock = threading.RLock()
        self._projects: dict[str, Project] = {}
        self._load()

    # ── persistence ────────────────────────────────────────────────────────
    def _load(self) -> None:
        with self._lock:
            if not self._path.exists():
                self._projects = {}
                return
            raw = json.loads(self._path.read_text(encoding="utf-8") or "{}")
            self._projects = {
                p["id"]: Project.from_dict(p) for p in raw.get("projects", [])
            }

    def _save(self) -> None:
        with self._lock:
            config.ensure_dirs()
            doc = {"projects": [p.to_dict() for p in self._projects.values()]}
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            tmp.replace(self._path)  # atomic on POSIX

    # ── projects ───────────────────────────────────────────────────────────
    def list_projects(self) -> list[Project]:
        with self._lock:
            return list(self._projects.values())

    def get_project(self, project_id: str) -> Project:
        with self._lock:
            try:
                return self._projects[project_id]
            except KeyError:
                raise NotFoundError(f"project {project_id!r} not found")

    def create_project(
        self, name: str, *, client: str = "", status: str = "draft"
    ) -> Project:
        with self._lock:
            pid = stable_id("prj", name)
            # If the deterministic id already exists, disambiguate by count.
            if pid in self._projects:
                pid = stable_id("prj", name, str(len(self._projects)))
            project = Project(id=pid, name=name, client=client, status=status)
            self._projects[pid] = project
            self._save()
            return project

    def update_project(self, project_id: str, **fields) -> Project:
        """Patch project name/status/client (only provided, non-None fields)."""
        with self._lock:
            p = self.get_project(project_id)
            for k in ("name", "status", "client"):
                if fields.get(k) is not None:
                    setattr(p, k, fields[k])
            self._save()
            return p

    def delete_project(self, project_id: str) -> None:
        with self._lock:
            if project_id not in self._projects:
                raise NotFoundError(f"project {project_id!r} not found")
            del self._projects[project_id]
            self._save()

    # ── rooms ──────────────────────────────────────────────────────────────
    def get_room(self, project_id: str, room_id: str) -> Room:
        project = self.get_project(project_id)
        for room in project.rooms:
            if room.id == room_id:
                return room
        raise NotFoundError(f"room {room_id!r} not found in project {project_id!r}")

    def add_room(
        self, project_id: str, name: str, *, building: str = ""
    ) -> Room:
        with self._lock:
            project = self.get_project(project_id)
            rid = stable_id("room", project_id, name)
            if any(r.id == rid for r in project.rooms):
                rid = stable_id("room", project_id, name, str(len(project.rooms)))
            room = Room(id=rid, name=name, building=building)
            project.rooms.append(room)
            self._save()
            return room

    def delete_room(self, project_id: str, room_id: str) -> None:
        with self._lock:
            project = self.get_project(project_id)
            before = len(project.rooms)
            project.rooms = [r for r in project.rooms if r.id != room_id]
            if len(project.rooms) == before:
                raise NotFoundError(f"room {room_id!r} not found")
            self._save()

    def update_room(self, project_id: str, room_id: str, **fields) -> Room:
        """Patch room name/status/building and titleBlock fields."""
        from .domain import TitleBlock
        with self._lock:
            room = self.get_room(project_id, room_id)
            for k in ("name", "status", "building"):
                if fields.get(k) is not None:
                    setattr(room, k, fields[k])
            tb = fields.get("titleBlock")
            if tb is not None:
                room.titleBlock = TitleBlock.from_dict({**room.titleBlock.to_dict(), **tb})
            self._save()
            return room

    def replace_room_devices(
        self, project_id: str, room_id: str, devices: list[Device]
    ) -> Room:
        """Set the device list for a room (used by the build/proposal step)."""
        with self._lock:
            room = self.get_room(project_id, room_id)
            room.devices = devices
            self._save()
            return room

    def save_room_build(self, project_id: str, room_id: str, build) -> Room:
        """Persist the result of the single-room core loop onto the room."""
        with self._lock:
            room = self.get_room(project_id, room_id)
            room.schematic = build.schematic
            room.cableSchedule = build.cable_schedule
            room.drawioPath = build.drawio_path
            room.status = "in-design"
            self._save()
            return room

    def persist(self) -> None:
        """Explicit flush (the mutating methods already save)."""
        self._save()


# Module-level singleton used by the HTTP layer.
_default: Optional[Store] = None


def get_store() -> Store:
    global _default
    if _default is None:
        _default = Store()
    return _default
