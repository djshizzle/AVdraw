"""Product catalog / library endpoint (the 'Pick from catalog' door)."""

from __future__ import annotations

from fastapi import APIRouter

from .. import catalog

router = APIRouter(tags=["catalog"])


@router.get("/catalog")
def get_catalog() -> dict:
    return {"categories": catalog.categories(), "items": catalog.load_catalog()}
