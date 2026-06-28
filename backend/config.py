"""Backend configuration — paths and feature flags.

All settings are overridable via environment variables so the same code runs
locally (local-first default) or in a container without edits.
"""

from __future__ import annotations

import os
from pathlib import Path

# Repo root = parent of the backend/ package.
REPO_ROOT = Path(__file__).resolve().parent.parent

# Existing pipeline modules live here; pipeline.py adds this to sys.path.
SRC_DIR = REPO_ROOT / "src"

# Generated artifacts (.drawio / .json) — never write into src/ or scripts/.
OUTPUT_DIR = Path(os.environ.get("AVDRAW_OUTPUT_DIR", REPO_ROOT / "output"))

# Runtime persistence (project/room store). Gitignored.
DATA_DIR = Path(os.environ.get("AVDRAW_DATA_DIR", REPO_ROOT / "data"))
STORE_PATH = DATA_DIR / "store.json"

# Optional API-key gate for the HTTP layer (mirrors avai_webhook.py).
API_KEY = os.environ.get("AVDRAW_API_KEY", "")

# CORS origins for the wireframed frontend during local dev.
CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "AVDRAW_CORS_ORIGINS",
        "http://localhost:5173,http://localhost:3000",
    ).split(",")
    if o.strip()
]


def ensure_dirs() -> None:
    """Create the runtime directories if they don't yet exist."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
