# seo-server-docker

A self-hosted Docker container that exposes [claude-seo](https://github.com/AgriciDaniel/claude-seo) as an HTTP API with a simple web UI for browsing past audit runs.

**Default port: 5300** (S-E-O-O)

## What's inside

| Component | Details |
|-----------|---------|
| FastAPI server | `POST /api/run-audit` triggers an audit; JSON responses |
| Web UI | `GET /` — table of past audits; `GET /audits/{id}` — full output |
| Audit archive | Every run is persisted under `AUDIT_DATA_DIR` as `meta.json` + `output.txt` |
| claude-seo | Pinned tag installed at build time; all skills and sub-agents included |
| DataForSEO MCP | Auto-configured from env vars at container start (optional) |
| Google APIs | PageSpeed, CrUX, Search Console, GA4 via env vars (optional) |

## Quick start

```bash
cp .env.example .env
# Fill in ANTHROPIC_API_KEY at minimum, then:
docker compose up -d
```

Open **http://localhost:5300** in your browser.

## Using the pre-built image

Replace `build: .` in `docker-compose.yml` with:

```yaml
image: ghcr.io/<your-github-username>/seo-server-docker:latest
```

After pushing to the `main` branch, GitHub Actions builds and publishes the image to GHCR automatically.  Set the package visibility to **Public** in GitHub (Package settings) so Unraid can pull it without authentication.

## API reference

### Trigger an audit

```bash
curl -X POST http://localhost:5300/api/run-audit \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-secret-key" \          # only required when API_KEY env is set
  -d '{"url": "https://example.com", "command": "audit"}'
```

**Allowed commands:** `audit`, `backlinks`, `content`, `dataforseo`, `geo`, `google`, `local`, `page`, `plan`, `schema`, `sitemap`, `sxo`, `technical`

The request blocks until the audit completes (up to `AUDIT_TIMEOUT_SECONDS`).  For long audits, raise the HTTP client timeout accordingly.

### List past audits

```bash
curl http://localhost:5300/api/audits
```

### Get audit detail

```bash
curl http://localhost:5300/api/audits/<run-id>        # metadata + 2 000-char preview
curl http://localhost:5300/api/audits/<run-id>/raw    # full output text
```

Interactive API docs: **http://localhost:5300/api/docs**

## Environment variables

Copy `.env.example` to `.env` and fill in the values you need.

### Required

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Your Anthropic API key for Claude Code |

### API security (optional)

| Variable | Description |
|----------|-------------|
| `API_KEY` | When set, `POST /api/run-audit` requires `X-API-Key: <value>` in the request header.  Leave empty to disable (suitable for LAN-only access). |

### DataForSEO (optional)

Enables live SERP, keyword, backlink, and on-page data across all audit commands.

| Variable | Description |
|----------|-------------|
| `DATAFORSEO_USERNAME` | DataForSEO account email |
| `DATAFORSEO_PASSWORD` | DataForSEO account password |
| `ENABLED_MODULES` | Comma-separated module list (default: all 9 modules) |

The entrypoint script writes these into `~/.claude/settings.json` as an MCP server block on every container start, so credentials are never baked into the image.

### Google APIs (optional)

Claude-seo's `scripts/google_auth.py` reads these env vars directly (config file is also written by the entrypoint as a convenience).

| Variable | Tier | Description |
|----------|------|-------------|
| `GOOGLE_API_KEY` | 0 | PageSpeed Insights, CrUX, CrUX History, Knowledge Graph |
| `GOOGLE_APPLICATION_CREDENTIALS` | 1+ | Path to service account JSON (mount the file via a volume) |
| `GSC_PROPERTY` | 1+ | Default Search Console property, e.g. `sc-domain:example.com` |
| `GA4_PROPERTY_ID` | 2 | GA4 property, e.g. `properties/123456789` |

**Service account setup (for GSC / Indexing API / GA4):**
1. Create a Google Cloud project and enable the required APIs (Search Console, PageSpeed Insights, Chrome UX Report, Web Search Indexing, Google Analytics Data).
2. Create a service account and download the JSON key.
3. Mount the key file into the container via `volumes` in `docker-compose.yml`:
   ```yaml
   - /path/on/host/service_account.json:/secrets/service_account.json:ro
   ```
4. Set `GOOGLE_APPLICATION_CREDENTIALS=/secrets/service_account.json`.
5. Add the service account email as a user in Search Console and/or Google Analytics.

OAuth tokens are **not** recommended for headless Docker use.  If you already have an `oauth-token.json`, persist `~/.config/claude-seo` via the `google-config` volume and place the token there manually.

### Audit settings

| Variable | Default | Description |
|----------|---------|-------------|
| `AUDIT_DATA_DIR` | `/data/audits` | Where run folders are stored |
| `AUDIT_TIMEOUT_SECONDS` | `5400` | Kill the claude process after this many seconds (90 min) |
| `PORT` | `5300` | Port uvicorn binds to inside the container |

## Volumes

| Mount point | Purpose |
|-------------|---------|
| `/data/audits` | Audit history (map to a persistent host path) |
| `/root/.claude` | Claude Code state and `settings.json` (DataForSEO MCP config) |
| `/root/.config/claude-seo` | Google API config and OAuth token |
| `/secrets/service_account.json` | Google service account key (read-only, optional) |

## Unraid deployment

1. **Add container** — set Repository to `ghcr.io/<you>/seo-server-docker:latest`.
2. **Port** — map host `5300` → container `5300`.
3. **Volumes** — map Unraid appdata paths to the four mount points above.
4. **Environment** — add the env vars listed above via the Unraid Docker UI.
5. **Watchtower** — install Watchtower from Community Applications.  The container already has the `com.centurylinklabs.watchtower.enable=true` label, so Watchtower will pull new images automatically when GitHub Actions pushes to GHCR.

## CI / CD

Pushing to `main` triggers `.github/workflows/docker-publish.yml`, which:
1. Builds the image using Docker Buildx with layer caching.
2. Tags it as `latest` + short commit SHA (e.g. `sha-a1b2c3d`).
3. Pushes to `ghcr.io/<your-github-username>/seo-server-docker`.

Pushing a semver tag (e.g. `v1.2.0`) additionally tags the image with that version.

## Security notes

- **Do not expose port 5300 directly to the public internet.** Put the container behind a reverse proxy (nginx, Caddy, Traefik) with TLS, or restrict access via Tailscale or a VPN.
- **Set `API_KEY`** to protect the audit-triggering endpoint from unauthorized use, even on a LAN.
- Audit output is stored in plain text under `AUDIT_DATA_DIR`.  Treat that volume as you would any other sensitive application data.
- The `ANTHROPIC_API_KEY` and DataForSEO credentials are passed at runtime via environment variables and never written into the container image layer.

## Development

```bash
# Install dependencies locally (Python 3.11+)
pip install -r requirements.txt

# Run the server (audits won't work without claude CLI installed)
AUDIT_DATA_DIR=./data/audits uvicorn seo_api:app --reload --port 5300
```

Build the image locally:

```bash
docker build -t claude-seo-server .
```
