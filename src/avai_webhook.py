#!/usr/bin/env python3
"""
avai_webhook.py — FastAPI hook for AvaI BuildReadinessAgent integration
========================================================================
Exposes the AVdraw pipeline over HTTP so AvaI / AI Maestro can call it
remotely. Designed for batch operation over 500+ rooms.

Endpoints:
    POST /generate
        Body: { bom_csv, room_name, codec_ip?, webex_token?, webex_device? }
        Resp: { drawio_path, json_path, dxf_path, review, validation, status }

    POST /validate
        Body: { bom_csv, drawio_path? }
        Resp: { bom_validation, drawio_validation, status }

    POST /diff
        Body: { drawio_path, codec_ip? | xstatus_file? | webex_device? }
        Resp: { diff: {matched, missing, extra, ...} }

    GET  /health
        Resp: { status: "ok", outputs_dir, python_version, ... }

Run:
    pip3 install fastapi uvicorn --user
    python3 src/avai_webhook.py --host 0.0.0.0 --port 8765
    # or:
    uvicorn avai_webhook:app --host 0.0.0.0 --port 8765 --reload

Auth (optional):
    export AVDRAW_API_KEY=secret
    # then clients must send: X-API-Key: secret
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

# Make sibling src/ modules importable when run directly
SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Repo root (parent of src/)
REPO_ROOT  = SRC_DIR.parent
OUTPUT_DIR = REPO_ROOT / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

API_KEY = os.environ.get("AVDRAW_API_KEY", "").strip()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

try:
    from fastapi import FastAPI, HTTPException, Header, Request
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, Field
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False


if HAS_FASTAPI:

    app = FastAPI(
        title="AVdraw Webhook",
        description="BOM → draw.io → EasySchematic pipeline for AvaI integration",
        version="1.0.0",
    )

    # ── Request models ─────────────────────────────────────────────────────

    class GenerateRequest(BaseModel):
        bom_csv:        str            = Field(..., description="BOM as CSV string OR absolute path")
        room_name:      str            = Field("AV Room", description="Display name for schematic")
        codec_ip:       Optional[str]  = Field(None, description="Optional Cisco codec IP for live xStatus")
        username:       str            = Field("admin", description="Codec basic-auth username")
        password:       str            = Field("",      description="Codec basic-auth password")
        webex_token:    Optional[str]  = Field(None, description="Webex bot/PAT token")
        webex_device:   Optional[str]  = Field(None, description="Webex device display name")
        strict_validate:bool           = Field(False, description="Promote validator warnings to errors")
        skip_dxf:       bool           = Field(False, description="Skip DXF export")
        skip_review:    bool           = Field(False, description="Skip AI signal-flow review")

    class ValidateRequest(BaseModel):
        bom_csv:        Optional[str]  = Field(None, description="BOM CSV string or path")
        drawio_path:    Optional[str]  = Field(None, description="Path to .drawio file")
        strict:         bool           = Field(False)

    class DiffRequest(BaseModel):
        drawio_path:    str
        codec_ip:       Optional[str]  = None
        username:       str            = "admin"
        password:       str            = ""
        xstatus_file:   Optional[str]  = None
        webex_token:    Optional[str]  = None
        webex_device:   Optional[str]  = None
        webex_device_id:Optional[str]  = None
        patch:          bool           = False

    # ── Auth helper ────────────────────────────────────────────────────────

    def _check_auth(x_api_key: Optional[str]) -> None:
        if API_KEY and x_api_key != API_KEY:
            raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

    # ── BOM input resolution ───────────────────────────────────────────────

    def _resolve_bom(bom_csv: str) -> str:
        """
        Accept either:
          - an absolute path to an existing CSV (returned as-is)
          - raw CSV content (written to a temp file, path returned)
        """
        if not bom_csv:
            raise HTTPException(status_code=400, detail="bom_csv is required")

        candidate = Path(bom_csv)
        if candidate.is_absolute() and candidate.exists():
            return str(candidate)

        # Heuristic: if it contains a newline or comma but no path separator,
        # treat as raw CSV. Otherwise relative path under REPO_ROOT.
        if "\n" in bom_csv or "," in bom_csv:
            tmp = Path(tempfile.mkstemp(suffix=".csv", prefix="avai_bom_")[1])
            tmp.write_text(bom_csv, encoding="utf-8")
            return str(tmp)

        # Try relative to repo
        rel = REPO_ROOT / bom_csv
        if rel.exists():
            return str(rel)

        raise HTTPException(
            status_code=400,
            detail=f"bom_csv does not exist as file and doesn't look like CSV content"
        )

    def _safe_name(s: str) -> str:
        return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in s)

    def _run(cmd: list[str], cwd: Path = REPO_ROOT) -> dict:
        """Run a subprocess, return {stdout, stderr, returncode}."""
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, cwd=str(cwd),
                timeout=300,
            )
            return {
                "returncode": r.returncode,
                "stdout":     r.stdout[-4000:],
                "stderr":     r.stderr[-4000:],
            }
        except subprocess.TimeoutExpired:
            return {"returncode": -1, "stdout": "", "stderr": "Timed out after 300s"}
        except Exception as exc:
            return {"returncode": -1, "stdout": "", "stderr": str(exc)}

    # ── Endpoints ──────────────────────────────────────────────────────────

    @app.get("/health")
    def health():
        return {
            "status":     "ok",
            "service":    "avdraw-webhook",
            "version":    "1.0.0",
            "repo_root":  str(REPO_ROOT),
            "output_dir": str(OUTPUT_DIR),
            "python":     sys.version.split()[0],
            "auth":       "required" if API_KEY else "open",
        }

    @app.post("/generate")
    def generate(req: GenerateRequest, x_api_key: Optional[str] = Header(None)):
        _check_auth(x_api_key)

        bom_path  = _resolve_bom(req.bom_csv)
        safe      = _safe_name(req.room_name)
        drawio    = OUTPUT_DIR / f"{safe}.drawio"
        json_out  = OUTPUT_DIR / f"{safe}.json"
        dxf_out   = OUTPUT_DIR / f"{safe}.dxf"
        review    = OUTPUT_DIR / f"{safe}_review.json"

        t0 = time.time()
        steps: list[dict] = []

        # ── Step 1: BOM → drawio ───────────────────────────────────────────
        cmd = [
            "python3", "src/bom_to_drawio.py",
            "--bom", bom_path,
            "--name", req.room_name,
            "--output", str(drawio),
        ]
        if req.codec_ip:
            cmd += ["--codec", req.codec_ip,
                    "--username", req.username,
                    "--password", req.password]
        if req.strict_validate:
            cmd.append("--strict")
        r = _run(cmd)
        steps.append({"step": "bom_to_drawio", **r})
        if r["returncode"] not in (0,) or not drawio.exists():
            raise HTTPException(status_code=422,
                                detail={"error": "BOM → drawio failed",
                                        "steps": steps})

        # ── Step 2: drawio → EasySchematic ─────────────────────────────────
        r = _run([
            "python3", "src/drawio_to_easyschematic.py",
            "--input",  str(drawio),
            "--output", str(json_out),
            "--name",   req.room_name,
        ])
        steps.append({"step": "drawio_to_easyschematic", **r})

        # ── Step 3: drawio → DXF ───────────────────────────────────────────
        if not req.skip_dxf:
            r = _run([
                "python3", "src/drawio_to_dxf.py",
                "--input",  str(drawio),
                "--output", str(dxf_out),
                "--name",   req.room_name,
            ])
            steps.append({"step": "drawio_to_dxf", **r})

        # ── Step 4: AI review (no-AI fallback when key missing) ────────────
        if not req.skip_review:
            ai_cmd = ["python3", "src/ai_reviewer.py",
                      "--input", str(drawio),
                      "--name",  req.room_name]
            if not os.environ.get("ANTHROPIC_API_KEY"):
                ai_cmd.append("--no-ai")
            r = _run(ai_cmd)
            steps.append({"step": "ai_review", **r})

        # ── Load review JSON if it exists ──────────────────────────────────
        review_data: Optional[dict] = None
        if review.exists():
            try:
                review_data = json.loads(review.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                review_data = None

        elapsed = round(time.time() - t0, 2)

        return {
            "status":        "ok",
            "room_name":     req.room_name,
            "elapsed_sec":   elapsed,
            "drawio_path":   str(drawio) if drawio.exists() else None,
            "json_path":     str(json_out) if json_out.exists() else None,
            "dxf_path":      str(dxf_out)  if dxf_out.exists() else None,
            "review_path":   str(review)   if review.exists() else None,
            "review":        review_data,
            "steps":         steps,
        }

    @app.post("/validate")
    def validate_endpoint(
        req: ValidateRequest, x_api_key: Optional[str] = Header(None)
    ):
        _check_auth(x_api_key)

        from bom_validator import validate_bom
        from drawio_validator import validate_drawio

        out: dict = {"status": "ok"}

        if req.bom_csv:
            bom_path = _resolve_bom(req.bom_csv)
            r = validate_bom(bom_path, strict=req.strict)
            out["bom_validation"] = {
                "ok":        r.ok,
                "rows":      len(r.rows),
                "errors":    [e.format() for e in r.errors],
                "warnings":  [w.format() for w in r.warnings],
            }

        if req.drawio_path:
            r = validate_drawio(req.drawio_path,
                                bom_path=_resolve_bom(req.bom_csv) if req.bom_csv else "",
                                strict=req.strict)
            out["drawio_validation"] = {
                "ok":         r.ok,
                "device_count": r.device_count,
                "edge_count":   r.edge_count,
                "errors":     [e.format() for e in r.errors],
                "warnings":   [w.format() for w in r.warnings],
            }

        if "bom_validation" not in out and "drawio_validation" not in out:
            raise HTTPException(400, "Provide bom_csv and/or drawio_path")

        any_bad = (out.get("bom_validation",    {}).get("ok") is False
                or  out.get("drawio_validation", {}).get("ok") is False)
        if any_bad:
            out["status"] = "invalid"

        return out

    @app.post("/diff")
    def diff_endpoint(req: DiffRequest, x_api_key: Optional[str] = Header(None)):
        _check_auth(x_api_key)

        cmd = ["python3", "src/xstatus_diff.py", "--input", req.drawio_path]

        if req.codec_ip:
            cmd += ["--codec", req.codec_ip,
                    "--username", req.username, "--password", req.password]
        elif req.xstatus_file:
            cmd += ["--xstatus", req.xstatus_file]
        elif req.webex_device_id:
            cmd += ["--webex-device-id", req.webex_device_id]
        elif req.webex_device:
            cmd += ["--webex-device", req.webex_device]
        else:
            raise HTTPException(400,
                                "Provide one of: codec_ip, xstatus_file, "
                                "webex_device, webex_device_id")

        if req.webex_token:
            cmd += ["--webex-token", req.webex_token]
        if req.patch:
            cmd.append("--patch")

        r = _run(cmd)

        # Try to load the JSON report bom_to_drawio writes alongside the input
        drawio = Path(req.drawio_path)
        diff_json = drawio.parent / f"{drawio.stem}_xstatus_diff.json"
        diff_data = None
        if diff_json.exists():
            try:
                diff_data = json.loads(diff_json.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass

        return {
            "status":     "ok" if r["returncode"] == 0 else "error",
            "diff_path":  str(diff_json) if diff_json.exists() else None,
            "diff":       diff_data,
            "stdout":     r["stdout"],
            "stderr":     r["stderr"],
            "returncode": r["returncode"],
        }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="AVdraw FastAPI webhook for AvaI")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true",
                        help="Hot-reload on code changes (dev only)")
    args = parser.parse_args()

    if not HAS_FASTAPI:
        print("ERROR: FastAPI not installed.", file=sys.stderr)
        print("       pip3 install fastapi uvicorn --user", file=sys.stderr)
        return 2

    try:
        import uvicorn
    except ImportError:
        print("ERROR: uvicorn not installed — pip3 install uvicorn --user",
              file=sys.stderr)
        return 2

    print(f"Starting AVdraw webhook on http://{args.host}:{args.port}")
    print(f"  Repo root  : {REPO_ROOT}")
    print(f"  Output dir : {OUTPUT_DIR}")
    print(f"  Auth       : {'X-API-Key required' if API_KEY else 'open (set AVDRAW_API_KEY to enable)'}")
    print(f"  Endpoints  : /health  /generate  /validate  /diff")

    uvicorn.run(
        "avai_webhook:app" if not args.reload else "avai_webhook:app",
        host=args.host, port=args.port, reload=args.reload,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
