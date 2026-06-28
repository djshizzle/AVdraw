"""Health / capability endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from .. import __version__, config

router = APIRouter(tags=["meta"])


@router.get("/health")
def health() -> dict:
    """Liveness + a peek at where artifacts and state live."""
    return {
        "status": "ok",
        "service": "av-schematic-builder-backend",
        "version": __version__,
        "paths": {
            "src": str(config.SRC_DIR),
            "output": str(config.OUTPUT_DIR),
            "data": str(config.DATA_DIR),
        },
        "authRequired": bool(config.API_KEY),
    }
