# syntax=docker/dockerfile:1
# ── Claude SEO API Docker image ───────────────────────────────────────────────
#
# Layers (order optimised for cache reuse):
#   1. System packages (Node 20, Python 3.11, git, curl)
#   2. Claude Code CLI (npm global)
#   3. claude-seo skills + Python deps (pinned tag)
#   4. FastAPI app layer (changes most often)
#
# Build args
#   CLAUDE_SEO_TAG   — claude-seo release tag to pin (default: v1.9.0)
#   PORT             — default exposed port (default: 5300)

FROM python:3.11-slim

ARG CLAUDE_SEO_TAG=v1.9.0
ARG PORT=5300

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=${PORT} \
    AUDIT_DATA_DIR=/data/audits \
    # Tell claude-seo install where to find itself (non-interactive mode)
    CLAUDE_SEO_TAG=${CLAUDE_SEO_TAG}

# ── 1. System packages ────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        git \
        ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── 2. Claude Code CLI ────────────────────────────────────────────────────────
RUN npm install -g @anthropic-ai/claude-code

# ── 3. claude-seo (skills, agents, Python deps) ───────────────────────────────
#
# We clone the full repo at the pinned tag so the entrypoint can reference
# the DataForSEO field-config.json at build time.  The main install.sh copies
# skills into ~/.claude; we also pre-download the dataforseo-mcp-server package
# to avoid npm cold-start latency on the first audit.
WORKDIR /app

RUN git clone --depth 1 --branch "${CLAUDE_SEO_TAG}" \
        https://github.com/AgriciDaniel/claude-seo.git /app/claude-seo \
    && CLAUDE_SEO_TAG="${CLAUDE_SEO_TAG}" bash /app/claude-seo/install.sh \
    && pip install --no-cache-dir -r /app/claude-seo/requirements.txt \
    # Pre-warm dataforseo-mcp-server package (avoids first-run npm fetch delay)
    && npx --yes --package=dataforseo-mcp-server -- node -e "" >/dev/null 2>&1 || true \
    # Install Playwright browsers used by seo-visual skill
    && python3 -m playwright install chromium --with-deps 2>/dev/null || true

# ── 4. FastAPI app ────────────────────────────────────────────────────────────
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY seo_api.py          /app/seo_api.py
COPY templates/          /app/templates/
COPY static/             /app/static/
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

WORKDIR /app

VOLUME ["/data/audits", "/root/.claude", "/root/.config/claude-seo"]

EXPOSE ${PORT}

ENTRYPOINT ["/app/docker-entrypoint.sh"]
