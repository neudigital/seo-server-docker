"""
Claude SEO API server.

Wraps the claude CLI (non-interactive mode) in a FastAPI app that persists
every audit run under AUDIT_DATA_DIR and serves a simple read-only web UI for
browsing history.
"""

import json
import os
import re
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
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

# Tracks currently-running claude subprocesses keyed by run_id
_running_jobs: dict[str, subprocess.Popen] = {}


# ── App ───────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_: FastAPI):
    # On startup, mark any audits left in "running" state (from a previous
    # container run) as errored so the UI does not show them as live forever.
    if AUDIT_DATA_DIR.exists():
        for d in AUDIT_DATA_DIR.iterdir():
            meta_path = d / "meta.json"
            if d.is_dir() and meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text())
                    if meta.get("status") == "running":
                        meta.update({"status": "error", "exit_code": -1,
                                     "finished_at": datetime.now(timezone.utc).isoformat()})
                        meta_path.write_text(json.dumps(meta, indent=2))
                except Exception:
                    pass
    yield


app = FastAPI(title="Claude SEO API", version="1.0.0", docs_url="/api/docs", lifespan=lifespan)
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


def _build_prompt(command: str, url: str) -> str:
    """Inject the SKILL.md as context so claude -p understands /seo commands."""
    skill_path = Path("/root/.claude/skills/seo/SKILL.md")
    if skill_path.exists():
        return (
            f"<skill_context>\n{skill_path.read_text()}\n</skill_context>\n\n"
            f"/seo {command} {url}"
        )
    return (
        f"Perform a comprehensive SEO {command} analysis of {url}. "
        "Use available tools (Bash, WebFetch, WebSearch) to gather real data "
        "and provide detailed, actionable findings."
    )


def _audit_worker(run_id: str, run_path: Path, meta: dict, cmd: list) -> None:
    """
    Background thread: runs claude CLI, streams stdout/stderr directly to
    output.txt so the SSE endpoint can tail it in real time.
    """
    output_path = run_path / "output.txt"
    try:
        with open(output_path, "w", buffering=1) as outfile:
            proc = subprocess.Popen(
                cmd,
                stdout=outfile,
                stderr=subprocess.STDOUT,   # merge stderr so it shows in the stream
                text=True,
                cwd=str(run_path),
                env={**os.environ},
            )
            _running_jobs[run_id] = proc
            try:
                proc.wait(timeout=AUDIT_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                outfile.write(f"\n[Audit timed out after {AUDIT_TIMEOUT}s]\n")
                meta.update({"status": "timeout", "exit_code": None,
                             "finished_at": datetime.now(timezone.utc).isoformat()})
                return
        meta.update({
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": "success" if proc.returncode == 0 else "error",
            "exit_code": proc.returncode,
        })
    except Exception as exc:
        with open(output_path, "a") as f:
            f.write(f"\n[Internal error: {exc}]\n")
        meta.update({"status": "error", "exit_code": -1,
                     "finished_at": datetime.now(timezone.utc).isoformat()})
    finally:
        (run_path / "meta.json").write_text(json.dumps(meta, indent=2))
        _running_jobs.pop(run_id, None)


def _start_audit(url: str, command: str) -> tuple[str, dict]:
    """
    Launch the audit in a background thread.  Returns (run_id, meta)
    immediately — the caller does NOT wait for the audit to finish.
    """
    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_"
        + uuid.uuid4().hex[:8]
    )
    run_path = _run_dir(run_id)
    run_path.mkdir(parents=True, exist_ok=True)

    meta: dict = {
        "id": run_id, "url": url, "command": command,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None, "status": "running", "exit_code": None,
    }
    (run_path / "meta.json").write_text(json.dumps(meta, indent=2))

    cmd = [
        "claude", "--bare", "-p", _build_prompt(command, url),
        "--allowedTools", "Bash,Read,Write,WebFetch,WebSearch",
    ]
    threading.Thread(
        target=_audit_worker, args=(run_id, run_path, meta, cmd), daemon=True
    ).start()
    return run_id, meta


# ── Health & debug ────────────────────────────────────────────────────────────


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok"}


def _mask(val: str, show: int = 8) -> str:
    """Show first `show` chars then asterisks, so you can confirm the right key."""
    if not val:
        return "(not set)"
    return val[:show] + "*" * max(0, len(val) - show)


@app.get("/debug", tags=["system"])
def debug() -> dict:
    """
    Diagnostic endpoint — shows auth config, claude binary info, and key
    environment variables (secrets are partially masked).
    Never exposes full key values.
    """
    home = Path.home()
    claude_json = home / ".claude.json"
    claude_json_in_vol = home / ".claude" / ".claude.json"

    # ── Claude binary ──────────────────────────────────────────────────────
    try:
        ver = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        claude_version = ver.stdout.strip() or ver.stderr.strip()
        claude_found = True
    except FileNotFoundError:
        claude_version = "claude binary not found in PATH"
        claude_found = False
    except Exception as exc:
        claude_version = f"error: {exc}"
        claude_found = False

    # ── Auth files ─────────────────────────────────────────────────────────
    backup_dir = home / ".claude" / "backups"
    backups = sorted(backup_dir.glob(".claude.json.backup.*")) if backup_dir.exists() else []

    # ── Skills ─────────────────────────────────────────────────────────────
    skill_path = home / ".claude" / "skills" / "seo" / "SKILL.md"
    skills_installed = skill_path.exists()

    # ── Quick claude connectivity check (tiny prompt, no tools) ────────────
    connectivity: dict = {"status": "skipped"}
    if claude_found:
        try:
            ping = subprocess.run(
                ["claude", "--bare", "-p", "Reply with only the word PONG"],
                capture_output=True, text=True, timeout=30,
                env={**os.environ},
            )
            out = (ping.stdout + ping.stderr).strip()
            if "PONG" in out.upper():
                connectivity = {"status": "ok", "response": out[:120]}
            else:
                connectivity = {
                    "status": "error",
                    "stdout": ping.stdout[:300],
                    "stderr": ping.stderr[:300],
                    "exit_code": ping.returncode,
                }
        except subprocess.TimeoutExpired:
            connectivity = {"status": "timeout"}
        except Exception as exc:
            connectivity = {"status": "error", "detail": str(exc)}

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")

    return {
        "claude": {
            "found": claude_found,
            "version": claude_version,
            "connectivity": connectivity,
        },
        "auth": {
            "ANTHROPIC_API_KEY": _mask(anthropic_key),
            "ANTHROPIC_API_KEY_length": len(anthropic_key),
            "claude_json_exists": claude_json.exists(),
            "claude_json_is_symlink": claude_json.is_symlink(),
            "claude_json_in_volume_exists": claude_json_in_vol.exists(),
            "backup_count": len(backups),
            "latest_backup": str(backups[-1]) if backups else None,
        },
        "integrations": {
            "DATAFORSEO_USERNAME": _mask(os.environ.get("DATAFORSEO_USERNAME", ""), 4),
            "GOOGLE_API_KEY": _mask(os.environ.get("GOOGLE_API_KEY", ""), 8),
            "GOOGLE_APPLICATION_CREDENTIALS": os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "(not set)"),
            "GSC_PROPERTY": os.environ.get("GSC_PROPERTY", "(not set)"),
        },
        "server": {
            "AUDIT_DATA_DIR": str(AUDIT_DATA_DIR),
            "AUDIT_TIMEOUT_SECONDS": AUDIT_TIMEOUT,
            "API_KEY_set": bool(API_KEY),
            "skills_installed": skills_installed,
        },
    }


# ── Claude login flow ────────────────────────────────────────────────────────
# claude login is interactive (shows an OAuth URL then waits for the browser
# callback).  We run it in a background thread, parse the URL from stdout,
# and expose it via /auth/* endpoints so users can authorize from the web UI.

_login_lock = threading.Lock()
_login_state: dict = {"status": "idle", "url": None, "message": ""}
_login_proc: Optional[subprocess.Popen] = None


def _run_login_background() -> None:
    global _login_proc, _login_state
    try:
        _login_proc = subprocess.Popen(
            ["claude", "login"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ},
        )
        assert _login_proc.stdout
        for line in _login_proc.stdout:
            urls = re.findall(r"https?://\S+", line)
            if urls and not _login_state["url"]:
                with _login_lock:
                    _login_state["url"] = urls[0].rstrip(".")
            # Look for success signals in the output
            low = line.lower()
            if any(w in low for w in ("success", "logged in", "authenticated")):
                with _login_lock:
                    _login_state["status"] = "complete"
                    _login_state["message"] = line.strip()
        _login_proc.wait()
        with _login_lock:
            if _login_state["status"] == "pending":
                if _login_proc.returncode == 0:
                    _login_state["status"] = "complete"
                    _login_state["message"] = "Login completed."
                else:
                    _login_state["status"] = "error"
                    _login_state["message"] = "claude login exited with an error."
    except Exception as exc:
        with _login_lock:
            _login_state["status"] = "error"
            _login_state["message"] = str(exc)


@app.post("/auth/login", tags=["auth"])
def auth_login_start() -> dict:
    """
    Start the claude login OAuth flow in the background.
    Returns the authorization URL as soon as it is emitted by the CLI.
    Visit that URL in your browser to authorize.  Poll /auth/status to track
    completion.  After login completes, remove ANTHROPIC_API_KEY from the
    container env so Claude Code uses the OAuth session instead.
    """
    global _login_state, _login_proc
    with _login_lock:
        if _login_state["status"] == "pending":
            return {"status": "pending", "url": _login_state["url"], "message": "Login already in progress"}
        _login_state = {"status": "pending", "url": None, "message": "Starting claude login…"}

    t = threading.Thread(target=_run_login_background, daemon=True)
    t.start()

    # Wait up to 15 s for the URL to appear
    import time
    for _ in range(30):
        time.sleep(0.5)
        if _login_state["url"] or _login_state["status"] in ("complete", "error"):
            break

    with _login_lock:
        return dict(_login_state)


@app.get("/auth/status", tags=["auth"])
def auth_login_status() -> dict:
    """Poll this after calling POST /auth/login to check if authorization completed."""
    with _login_lock:
        return dict(_login_state)


@app.get("/auth", response_class=HTMLResponse, include_in_schema=False)
def auth_ui(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "auth.html", {})


# ── JSON API ──────────────────────────────────────────────────────────────────


@app.post("/api/run-audit", tags=["audits"], dependencies=[Depends(require_api_key)])
def api_run_audit(req: AuditRequest) -> JSONResponse:
    """
    Start an SEO audit in the background.  Returns 202 immediately with the
    run_id.  Poll GET /api/audits/{id} for status, or stream output via
    GET /audits/{id}/stream (SSE).
    """
    if not req.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
    if req.command not in ALLOWED_COMMANDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown command '{req.command}'. Allowed: {sorted(ALLOWED_COMMANDS)}",
        )
    run_id, meta = _start_audit(req.url, req.command)
    return JSONResponse({"id": run_id, "status": meta["status"]}, status_code=202)


@app.get("/audits/{run_id}/stream", tags=["audits"])
def audit_stream(run_id: str) -> StreamingResponse:
    """
    Server-Sent Events stream of live audit output.
    Sends chunks as the claude CLI writes them; emits a final 'done' event
    with the status when the audit finishes (or if it was already finished).
    """
    meta_path = _run_dir(run_id) / "meta.json"
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="Audit not found")
    output_path = _run_dir(run_id) / "output.txt"

    def generate():
        pos = 0
        while True:
            # Drain any new bytes written to the output file
            if output_path.exists():
                new_size = output_path.stat().st_size
                if new_size > pos:
                    with open(output_path, "r", errors="replace") as f:
                        f.seek(pos)
                        chunk = f.read(new_size - pos)
                    pos = new_size
                    if chunk:
                        yield f"data: {json.dumps(chunk)}\n\n"

            if run_id not in _running_jobs:
                # Final drain (last bytes the worker may have flushed)
                if output_path.exists():
                    new_size = output_path.stat().st_size
                    if new_size > pos:
                        with open(output_path, "r", errors="replace") as f:
                            f.seek(pos)
                            chunk = f.read()
                        if chunk:
                            yield f"data: {json.dumps(chunk)}\n\n"
                try:
                    final_status = json.loads(meta_path.read_text()).get("status", "unknown")
                except Exception:
                    final_status = "unknown"
                yield f"event: done\ndata: {json.dumps({'status': final_status})}\n\n"
                return

            time.sleep(0.25)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


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
