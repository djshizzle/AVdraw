"""FastAPI application for the AV Schematic Builder backend.

Run:
    pip3 install -r backend/requirements.txt
    python3 -m backend.main --port 8000
    # or: uvicorn backend.main:app --reload --port 8000

If FastAPI isn't installed, importing this module still succeeds (``app`` is
None) and running it prints an install hint — the stdlib core loop in
``backend.pipeline`` remains fully usable on its own.
"""

from __future__ import annotations

import logging
import sys

from . import __version__, config

logger = logging.getLogger("avdraw.backend")

try:
    from fastapi import FastAPI, Header, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse

    _FASTAPI_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - exercised when deps absent
    _FASTAPI_AVAILABLE = False


def create_app():
    """Build and return the FastAPI app (or None if FastAPI isn't installed)."""
    if not _FASTAPI_AVAILABLE:
        return None

    from .routers import builds, health, projects

    config.ensure_dirs()

    app = FastAPI(
        title="AV Schematic Builder",
        version=__version__,
        description="BOM → draw.io → EasySchematic, one room at a time.",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Optional API-key gate (set AVDRAW_API_KEY to enable). /health is open.
    @app.middleware("http")
    async def _api_key_guard(request: Request, call_next):
        if config.API_KEY and request.url.path not in ("/health", "/", "/docs",
                                                        "/openapi.json"):
            if request.headers.get("X-API-Key") != config.API_KEY:
                return JSONResponse(status_code=401,
                                    content={"detail": "invalid or missing X-API-Key"})
        return await call_next(request)

    app.include_router(health.router)
    app.include_router(projects.router)
    app.include_router(builds.router)

    # Optional demo UI: if a frontend/ dir exists, serve it at /app same-origin
    # (no CORS needed). Replaced by the real build when one lands.
    frontend_dir = config.REPO_ROOT / "frontend"
    has_ui = (frontend_dir / "index.html").exists()
    if has_ui:
        from fastapi.staticfiles import StaticFiles

        app.mount("/app", StaticFiles(directory=str(frontend_dir), html=True),
                  name="app")

    @app.get("/")
    def root():
        if has_ui:
            from fastapi.responses import RedirectResponse

            return RedirectResponse(url="/app/")
        return {
            "service": "av-schematic-builder-backend",
            "version": __version__,
            "docs": "/docs",
        }

    return app


# Module-level app for `uvicorn backend.main:app`.
app = create_app()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the AV Schematic Builder API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not _FASTAPI_AVAILABLE:
        print(
            "FastAPI is not installed.\n"
            "  pip3 install -r backend/requirements.txt\n"
            "The stdlib core loop still works without it:\n"
            "  python3 -m backend.pipeline --bom templates/sample_bom.csv "
            '--name "My Room"',
            file=sys.stderr,
        )
        return 1

    try:
        import uvicorn
    except ModuleNotFoundError:
        print("uvicorn is not installed — pip3 install -r backend/requirements.txt",
              file=sys.stderr)
        return 1

    uvicorn.run("backend.main:app", host=args.host, port=args.port,
                reload=args.reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
