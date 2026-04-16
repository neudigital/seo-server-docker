#!/usr/bin/env bash
# docker-entrypoint.sh
#
# Runs at container start before uvicorn.  Responsibilities:
#   1. Merge DataForSEO MCP config into ~/.claude/settings.json (if credentials set)
#   2. Write ~/.config/claude-seo/google-api.json (if GOOGLE_API_KEY or
#      GOOGLE_APPLICATION_CREDENTIALS are set and the file doesn't already exist)
#   3. Exec the FastAPI server on ${PORT:-5300}
#
set -euo pipefail

# ── Restore claude-seo skills if volume mount wiped them ─────────────────────
# When /root/.claude is a Docker named volume it starts empty, erasing the
# skills installed during the image build.  We keep a snapshot at
# /opt/claude-default and restore from it on first start (or any time the
# skills directory is missing).
if [ ! -d "${HOME}/.claude/skills/seo" ] && [ -d /opt/claude-default ]; then
    echo "→ Restoring claude-seo skills from image snapshot…"
    cp -r /opt/claude-default/. "${HOME}/.claude/"
    echo "  ✓ Skills restored"
fi

# ── Restore ~/.claude.json if missing ────────────────────────────────────────
# Claude Code stores its auth config at ~/.claude.json (outside ~/.claude/).
# That file is NOT inside our volume mount, so it disappears on every restart.
# Strategy (in priority order):
#   1. ANTHROPIC_API_KEY env var  →  write a minimal config from it
#   2. Backup file left by Claude Code  →  restore it
CLAUDE_JSON="${HOME}/.claude.json"
if [ ! -f "${CLAUDE_JSON}" ]; then
    if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        echo "→ Writing ${CLAUDE_JSON} from ANTHROPIC_API_KEY…"
        python3 - <<PYEOF
import json, os
cfg = {
    "primaryApiKey": os.environ["ANTHROPIC_API_KEY"],
    "hasCompletedOnboarding": True,
    "hasAgreedToTerms": True,
}
with open("${CLAUDE_JSON}", "w") as f:
    json.dump(cfg, f, indent=2)
print("  ✓ ~/.claude.json written")
PYEOF
    else
        # Fall back to the latest backup Claude Code left behind
        BACKUP=$(ls -t "${HOME}/.claude/backups/.claude.json.backup."* 2>/dev/null | head -1)
        if [ -n "${BACKUP}" ]; then
            echo "→ Restoring ${CLAUDE_JSON} from backup ${BACKUP}…"
            cp "${BACKUP}" "${CLAUDE_JSON}"
            echo "  ✓ Restored"
        else
            echo "⚠  No ANTHROPIC_API_KEY and no backup found."
            echo "   Set ANTHROPIC_API_KEY in your environment or log in interactively first."
        fi
    fi
else
    # File exists — make sure the API key in it matches the current env var
    # (handles key rotation without requiring a manual volume update)
    if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        python3 - <<PYEOF
import json, os
path = "${CLAUDE_JSON}"
key  = os.environ["ANTHROPIC_API_KEY"]
try:
    with open(path) as f:
        cfg = json.load(f)
    if cfg.get("primaryApiKey") != key:
        cfg["primaryApiKey"] = key
        with open(path, "w") as f:
            json.dump(cfg, f, indent=2)
        print("  ✓ ~/.claude.json API key updated")
except Exception:
    pass
PYEOF
    fi
fi

SETTINGS_FILE="${HOME}/.claude/settings.json"
GOOGLE_CONFIG_DIR="${HOME}/.config/claude-seo"
GOOGLE_CONFIG_FILE="${GOOGLE_CONFIG_DIR}/google-api.json"
DATAFORSEO_FIELD_CONFIG="/app/claude-seo/extensions/dataforseo/field-config.json"

# ── DataForSEO MCP config ─────────────────────────────────────────────────────
if [ -n "${DATAFORSEO_USERNAME:-}" ] && [ -n "${DATAFORSEO_PASSWORD:-}" ]; then
    echo "→ Configuring DataForSEO MCP in ${SETTINGS_FILE}…"
    mkdir -p "$(dirname "${SETTINGS_FILE}")"

    python3 - <<PYEOF
import json, os

settings_path = "${SETTINGS_FILE}"
username = os.environ["DATAFORSEO_USERNAME"]
password = os.environ["DATAFORSEO_PASSWORD"]
enabled_modules = os.environ.get(
    "ENABLED_MODULES",
    "SERP,KEYWORDS_DATA,ONPAGE,DATAFORSEO_LABS,BACKLINKS,DOMAIN_ANALYTICS,BUSINESS_DATA,CONTENT_ANALYSIS,AI_OPTIMIZATION",
)
field_config = "${DATAFORSEO_FIELD_CONFIG}"

settings = {}
if os.path.exists(settings_path):
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except (json.JSONDecodeError, OSError):
        pass

settings.setdefault("mcpServers", {})
settings["mcpServers"]["dataforseo"] = {
    "command": "npx",
    "args": ["-y", "dataforseo-mcp-server"],
    "env": {
        "DATAFORSEO_USERNAME": username,
        "DATAFORSEO_PASSWORD": password,
        "ENABLED_MODULES": enabled_modules,
        "FIELD_CONFIG_PATH": field_config,
    },
}

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)

print("  ✓ DataForSEO MCP block written")
PYEOF
else
    echo "→ DATAFORSEO_USERNAME/PASSWORD not set; skipping DataForSEO MCP config"
fi

# ── Google API config ─────────────────────────────────────────────────────────
# claude-seo's google_auth.py reads env vars directly; the JSON file is optional
# but is written here for completeness when running the /seo google scripts.
if [ -n "${GOOGLE_API_KEY:-}" ] || [ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
    if [ ! -f "${GOOGLE_CONFIG_FILE}" ]; then
        echo "→ Writing ${GOOGLE_CONFIG_FILE}…"
        mkdir -p "${GOOGLE_CONFIG_DIR}"

        python3 - <<PYEOF
import json, os

cfg = {}
api_key = os.environ.get("GOOGLE_API_KEY", "")
sa_path  = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
gsc_prop = os.environ.get("GSC_PROPERTY", "")
ga4_id   = os.environ.get("GA4_PROPERTY_ID", "")

if api_key:   cfg["api_key"]              = api_key
if sa_path:   cfg["service_account_path"] = sa_path
if gsc_prop:  cfg["default_property"]     = gsc_prop
if ga4_id:    cfg["ga4_property_id"]       = ga4_id

with open("${GOOGLE_CONFIG_FILE}", "w") as f:
    json.dump(cfg, f, indent=2)

print("  ✓ Google API config written")
PYEOF
    else
        echo "→ ${GOOGLE_CONFIG_FILE} already exists; skipping auto-write"
    fi
else
    echo "→ No Google API credentials in env; /seo google commands will use unauthenticated mode"
fi

# ── Ensure audit data directory exists ───────────────────────────────────────
AUDIT_DATA_DIR="${AUDIT_DATA_DIR:-/data/audits}"
mkdir -p "${AUDIT_DATA_DIR}"

# ── Start server ─────────────────────────────────────────────────────────────
PORT="${PORT:-5300}"
echo "→ Starting Claude SEO API on port ${PORT}…"
exec uvicorn seo_api:app --host 0.0.0.0 --port "${PORT}"
