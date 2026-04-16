"""
Claude SEO API server.

Wraps the claude CLI (non-interactive mode) in a FastAPI app that persists
every audit run under AUDIT_DATA_DIR and serves a simple read-only web UI for
browsing history.
"""

import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────

AUDIT_DATA_DIR = Path(os.environ.get("AUDIT_DATA_DIR", "/data/audits"))
API_KEY = os.environ.get("API_KEY", "")
AUDIT_TIMEOUT = int(os.environ.get("AUDIT_TIMEOUT_SECONDS", str(60 * 90)))

ALLOWED_COMMANDS = frozenset(
    {
        "audit",
        "backlinks",
        "content",
        "dataforseo",
        "geo",
        "google",
        "local",
        "page",
        "plan",
        "schema",
        "sitemap",
        "sxo",
        "technical",
    }
)

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Claude SEO API", version="1.0.0", docs_url="/api/docs")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ── Auth ──────────────────────────────────────────────────────────────────────


class AuditRequest(BaseModel):
    url: str
    command: str = "audit"


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    """Check X-API-Key header when API_KEY env is set."""
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")


# ── Audit helpers ─────────────────────────────────────────────────────────────


def _run_dir(run_id: str) -> Path:
    return AUDIT_DATA_DIR / run_id


def _list_audits() -> list[dict]:
    """Return all audits sorted newest-first."""
    if not AUDIT_DATA_DIR.exists():
        return []
    audits: list[dict] = []
    for entry in sorted(AUDIT_DATA_DIR.iterdir(), key=lambda p: p.name, reverse=True):
        meta_path = entry / "meta.json"
        if entry.is_dir() and meta_path.exists():
            try:
                audits.append(json.loads(meta_path.read_text()))
            except Exception:
                pass
    return audits


def _get_meta(run_id: str) -> dict:
    meta_path = _run_dir(run_id) / "meta.json"
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="Audit not found")
    return json.loads(meta_path.read_text())


def _execute_audit(url: str, command: str) -> tuple[str, dict]:
    """
    Invoke the claude CLI in non-interactive mode and persist the result.
    Returns (run_id, meta).  Blocks until the process finishes or times out.
    """
    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_"
        + uuid.uuid4().hex[:8]
    )
    run_path = _run_dir(run_id)
    run_path.mkdir(parents=True, exist_ok=True)

    meta: dict = {
        "id": run_id,
        "url": url,
        "command": command,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "status": "running",
        "exit_code": None,
    }
    (run_path / "meta.json").write_text(json.dumps(meta, indent=2))

    prompt = f"/seo {command} {url}"
    cmd = [
        "claude",
        "--bare",
        "-p",
        prompt,
        "--allowedTools",
        "Bash,Read,Write,WebFetch,WebSearch",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=AUDIT_TIMEOUT,
            cwd=str(run_path),
        )
        output = result.stdout
        if result.returncode != 0 and result.stderr:
            output += "\n--- stderr ---\n" + result.stderr[:8000]
        meta.update(
            {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status": "success" if result.returncode == 0 else "error",
                "exit_code": result.returncode,
            }
        )
    except subprocess.TimeoutExpired:
        output = f"[Audit timed out after {AUDIT_TIMEOUT} seconds]"
        meta.update(
            {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status": "timeout",
                "exit_code": None,
            }
        )
    except Exception as exc:
        output = f"[Internal error: {exc}]"
        meta.update(
            {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status": "error",
                "exit_code": -1,
            }
        )

    (run_path / "output.txt").write_text(output)
    (run_path / "meta.json").write_text(json.dumps(meta, indent=2))
    return run_id, meta


# ── Health ────────────────────────────────────────────────────────────────────


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok"}


# ── JSON API ──────────────────────────────────────────────────────────────────


@app.post("/api/run-audit", tags=["audits"], dependencies=[Depends(require_api_key)])
def api_run_audit(req: AuditRequest) -> dict:
    """
    Trigger an SEO audit.  Blocks until complete (up to AUDIT_TIMEOUT_SECONDS).
    Protected by X-API-Key header when API_KEY env is set.
    """
    if not req.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
    if req.command not in ALLOWED_COMMANDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown command '{req.command}'. Allowed: {sorted(ALLOWED_COMMANDS)}",
        )

    run_id, meta = _execute_audit(req.url, req.command)

    if meta["status"] == "timeout":
        raise HTTPException(status_code=504, detail=f"Audit timed out after {AUDIT_TIMEOUT}s")

    output_path = _run_dir(run_id) / "output.txt"
    return {
        "id": run_id,
        "status": meta["status"],
        "output": output_path.read_text() if output_path.exists() else "",
    }


@app.get("/api/audits", tags=["audits"])
def api_list_audits() -> list[dict]:
    """List all past audits, newest first."""
    return _list_audits()


@app.get("/api/audits/{run_id}", tags=["audits"])
def api_get_audit(run_id: str) -> dict:
    """Fetch metadata and a 2 000-character preview of the audit output."""
    meta = _get_meta(run_id)
    output_path = _run_dir(run_id) / "output.txt"
    preview = ""
    if output_path.exists():
        preview = output_path.read_text()[:2000]
    return {**meta, "preview": preview}


@app.get("/api/audits/{run_id}/raw", tags=["audits"])
def api_get_audit_raw(run_id: str) -> dict:
    """Return the full audit output text."""
    _get_meta(run_id)
    output_path = _run_dir(run_id) / "output.txt"
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="No output recorded for this audit")
    return {"output": output_path.read_text()}


# ── HTML UI ───────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def ui_index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "audits": _list_audits(),
            "commands": sorted(ALLOWED_COMMANDS),
            "api_key_required": bool(API_KEY),
        },
    )


@app.get("/audits/{run_id}", response_class=HTMLResponse, include_in_schema=False)
def ui_audit_detail(request: Request, run_id: str) -> HTMLResponse:
    meta = _get_meta(run_id)
    output_path = _run_dir(run_id) / "output.txt"
    output = output_path.read_text() if output_path.exists() else "(no output recorded)"
    return templates.TemplateResponse(
        request,
        "audit_detail.html",
        {"meta": meta, "output": output},
    )
