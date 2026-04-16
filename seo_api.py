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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
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
    """Return all audits sorted newest-first, lazily extracting health scores."""
    if not AUDIT_DATA_DIR.exists():
        return []
    audits: list[dict] = []
    for entry in sorted(AUDIT_DATA_DIR.iterdir(), key=lambda p: p.name, reverse=True):
        meta_path = entry / "meta.json"
        if entry.is_dir() and meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                audits.append(_ensure_health_score(meta, entry))
            except Exception:
                pass
    return audits


def _get_meta(run_id: str) -> dict:
    meta_path = _run_dir(run_id) / "meta.json"
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="Audit not found")
    return json.loads(meta_path.read_text())


# Matches "SEO Health Score: 72/100", "Health Score: 72 / 100", markdown bold, etc.
_SCORE_RE = re.compile(
    r'(?:seo\s+)?health\s+score[^\d]{0,10}(\d{1,3})\s*(?:/|out\s+of)\s*100',
    re.IGNORECASE,
)


def _extract_health_score(run_path: Path) -> Optional[int]:
    """
    Scan output.txt and any .md files in the run directory for a health score
    line like 'SEO Health Score: 72/100'.  Returns the integer or None.
    """
    candidates = [run_path / "output.txt", *sorted(run_path.glob("*.md"))]
    for path in candidates:
        if not path.exists():
            continue
        try:
            m = _SCORE_RE.search(path.read_text(errors="replace"))
            if m:
                score = int(m.group(1))
                if 0 <= score <= 100:
                    return score
        except Exception:
            pass
    return None


def _ensure_health_score(meta: dict, run_path: Path) -> dict:
    """
    If the audit is finished and health_score is not yet in meta, try to
    extract it from output files, persist the result, and return updated meta.
    """
    if "health_score" not in meta and meta.get("status") not in ("running", None):
        score = _extract_health_score(run_path)
        if score is not None:
            meta["health_score"] = score
            try:
                (run_path / "meta.json").write_text(json.dumps(meta, indent=2))
            except Exception:
                pass
    return meta


_FILE_OUTPUT_INSTRUCTIONS = """

<execution_strategy>
Run any specialist subagents SEQUENTIALLY — one at a time, not in parallel. This is required
to stay within API rate limits. If you encounter an HTTP 429 rate-limit error, pause for
60 seconds then retry that specific request before continuing.
</execution_strategy>

<output_requirements>
CRITICAL — you MUST write your findings to files using the Write tool. Do not skip this step
or claim scripts are unavailable. Write the files directly yourself:

1. Write the complete audit report to: FULL-AUDIT-REPORT.md
2. Write the prioritized action plan to: ACTION-PLAN.md

Both files are required. Use the Write tool for each one. Do not ask whether to create them —
always create them unconditionally before finishing.
</output_requirements>"""

_RATE_LIMIT_PHRASES = ("rate limit", "429", "tokens per minute", "request rejected")


def _is_rate_limited(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in _RATE_LIMIT_PHRASES)


def _build_prompt(command: str, url: str) -> str:
    """Inject SKILL.md context + mandatory file-output instructions."""
    skill_path = Path("/root/.claude/skills/seo/SKILL.md")
    if skill_path.exists():
        return (
            f"<skill_context>\n{skill_path.read_text()}\n</skill_context>\n\n"
            f"/seo {command} {url}"
            f"{_FILE_OUTPUT_INSTRUCTIONS}"
        )
    return (
        f"Perform a comprehensive SEO {command} analysis of {url}. "
        "Use available tools (Bash, WebFetch, WebSearch) to gather real data "
        f"and provide detailed, actionable findings.{_FILE_OUTPUT_INSTRUCTIONS}"
    )


def _fmt_tool_call(name: str, inp: dict) -> str:
    """Format a tool_use block into a readable one-liner for the live terminal."""
    if name in ("bash", "computer"):
        cmd_str = inp.get("command") or inp.get("action") or ""
        return f"\n$ {cmd_str[:300]}\n" if cmd_str else f"\n[{name}]\n"
    if name.lower() in ("web_fetch", "webfetch"):
        target = inp.get("url") or ""
        return f"\n> {target[:300]}\n"
    if name.lower() in ("web_search", "websearch"):
        target = inp.get("query") or ""
        return f"\n? {target[:300]}\n"
    if name.lower() in ("read", "write"):
        path = inp.get("path") or inp.get("file_path") or ""
        return f"\n[{name}] {path[:200]}\n"
    snippet = str(inp)[:120] if inp else ""
    return f"\n[{name}] {snippet}\n"


def _process_events(events_path: Path, events_pos: int, outfile, meta: dict) -> int:
    """
    Read new events from events_pos in events.jsonl, write human-readable
    text to outfile, harvest billing data from 'result' events.
    Returns the new file position.
    """
    try:
        cur_size = events_path.stat().st_size
    except FileNotFoundError:
        return events_pos

    if cur_size <= events_pos:
        return events_pos

    with open(events_path, "r", errors="replace") as ef:
        ef.seek(events_pos)
        chunk = ef.read(cur_size - events_pos)

    for raw_line in chunk.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            outfile.write(raw_line + "\n")
            outfile.flush()
            continue

        etype = event.get("type", "")

        if etype == "assistant":
            for block in event.get("message", {}).get("content", []):
                btype = block.get("type", "")
                if btype == "text":
                    outfile.write(block["text"])
                    outfile.flush()
                elif btype == "tool_use":
                    outfile.write(_fmt_tool_call(
                        block.get("name", "tool"), block.get("input", {})))
                    outfile.flush()

        elif etype == "tool":
            result = event.get("result", {})
            if result.get("is_error"):
                err = str(result.get("output", ""))[:400]
                outfile.write(f"[tool error] {err}\n")
                outfile.flush()

        elif etype == "result":
            cost = event.get("total_cost_usd") or event.get("cost_usd")
            usage = event.get("usage") or {}
            dur_ms = event.get("duration_ms")
            if cost is not None:
                meta["cost_usd"] = round(float(cost), 6)
            if usage:
                meta["usage"] = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                    "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
                }
            if dur_ms is not None:
                meta["duration_ms"] = int(dur_ms)
            if event.get("subtype") == "error_during_execution":
                err_text = str(event.get("result", ""))[:600]
                outfile.write(f"\n[Claude error] {err_text}\n")
                outfile.flush()

        # system / ping / unknown events — skip

    return cur_size


def _audit_worker(run_id: str, run_path: Path, meta: dict, cmd: list) -> None:
    """
    Background thread: writes stream-json events directly to events.jsonl
    (subprocess stdout → file, zero Python pipe buffering), polls that file
    every 150 ms to translate events into human-readable output.txt, and
    extracts cost/token usage from the final 'result' event.
    """
    events_path = run_path / "events.jsonl"
    output_path = run_path / "output.txt"
    try:
        # Open the events file first, then hand the fd to the subprocess.
        # Closing our Python handle afterwards is safe — the subprocess keeps
        # its own copy of the file descriptor and can write to it freely.
        ef = open(events_path, "w")
        proc = subprocess.Popen(
            cmd,
            stdout=ef,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(run_path),
            env={**os.environ},
        )
        ef.close()
        _running_jobs[run_id] = proc

        events_pos = 0
        deadline = time.monotonic() + AUDIT_TIMEOUT

        with open(output_path, "w", buffering=1) as outfile:
            while True:
                if time.monotonic() > deadline:
                    proc.kill()
                    proc.wait()
                    outfile.write(f"\n[Audit timed out after {AUDIT_TIMEOUT}s]\n")
                    outfile.flush()
                    meta.update({"status": "timeout", "exit_code": None,
                                 "finished_at": datetime.now(timezone.utc).isoformat()})
                    return

                events_pos = _process_events(events_path, events_pos, outfile, meta)

                if proc.poll() is not None:
                    # Final drain — pick up any events written between the last
                    # poll and process exit.
                    _process_events(events_path, events_pos, outfile, meta)
                    break

                time.sleep(0.15)

        stderr = proc.stderr.read() if proc.stderr else ""
        if proc.returncode != 0 and stderr:
            with open(output_path, "a") as f:
                f.write(f"\n--- stderr ---\n{stderr[:4000]}\n")

        # Determine final status — detect rate-limit errors in combined output
        if proc.returncode == 0:
            final_status = "success"
        else:
            combined = stderr
            try:
                combined += output_path.read_text(errors="replace")
            except Exception:
                pass
            final_status = "rate_limited" if _is_rate_limited(combined) else "error"

        meta.update({
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": final_status,
            "exit_code": proc.returncode,
        })
    except Exception as exc:
        with open(output_path, "a") as f:
            f.write(f"\n[Internal error: {exc}]\n")
        meta.update({"status": "error", "exit_code": -1,
                     "finished_at": datetime.now(timezone.utc).isoformat()})
    finally:
        # Extract health score from output before persisting meta
        if meta.get("status") != "running":
            score = _extract_health_score(run_path)
            if score is not None:
                meta["health_score"] = score
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
        "claude", "--output-format", "stream-json", "--verbose",
        "-p", _build_prompt(command, url),
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


@app.post("/api/audits/{run_id}/retry", tags=["audits"], dependencies=[Depends(require_api_key)])
def api_retry_audit(run_id: str) -> JSONResponse:
    """Re-run a failed or rate-limited audit with the same URL and command."""
    meta = _get_meta(run_id)
    if meta.get("status") == "running":
        raise HTTPException(status_code=409, detail="Audit is still running")
    new_id, new_meta = _start_audit(meta["url"], meta["command"])
    return JSONResponse({"id": new_id, "status": new_meta["status"]}, status_code=202)


@app.get("/api/audits", tags=["audits"])
def api_list_audits() -> list[dict]:
    """List all past audits, newest first."""
    return _list_audits()


@app.get("/api/audits/{run_id}", tags=["audits"])
def api_get_audit(run_id: str) -> dict:
    """Fetch metadata and a 2 000-character preview of the audit output."""
    run_path = _run_dir(run_id)
    meta = _ensure_health_score(_get_meta(run_id), run_path)
    output_path = run_path / "output.txt"
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


# Files written by claude into the audit directory (e.g. FULL-AUDIT-REPORT.md)
_EXTRA_FILE_SUFFIXES = {".md", ".csv", ".html", ".txt"}
_EXCLUDED_FILES = {"meta.json", "output.txt", "events.jsonl"}


@app.get("/api/audits/{run_id}/files", tags=["audits"])
def api_list_audit_files(run_id: str) -> list[dict]:
    """List extra files written by Claude into the audit directory."""
    _get_meta(run_id)
    run_path = _run_dir(run_id)
    files = []
    for f in sorted(run_path.iterdir()):
        if (f.is_file()
                and f.name not in _EXCLUDED_FILES
                and f.suffix.lower() in _EXTRA_FILE_SUFFIXES):
            files.append({"name": f.name, "size": f.stat().st_size})
    return files


@app.get("/api/audits/{run_id}/files/{filename}", tags=["audits"])
def api_get_audit_file(run_id: str, filename: str, download: bool = False):
    """
    Return the content of a file written by Claude into the audit directory.
    Pass ?download=true to receive it as a file attachment.
    """
    _get_meta(run_id)
    run_path = _run_dir(run_id).resolve()
    file_path = (run_path / filename).resolve()
    # Path-traversal guard
    try:
        file_path.relative_to(run_path)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if download:
        return FileResponse(
            path=str(file_path),
            filename=filename,
            media_type="application/octet-stream",
        )
    return {"name": filename, "content": file_path.read_text(errors="replace")}


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
    run_path = _run_dir(run_id)
    meta = _ensure_health_score(_get_meta(run_id), run_path)
    output_path = run_path / "output.txt"
    output = output_path.read_text() if output_path.exists() else "(no output recorded)"
    return templates.TemplateResponse(
        request,
        "audit_detail.html",
        {"meta": meta, "output": output},
    )
