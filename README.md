# Canopy Web

Collaborative web workspace for the canopy agent ecosystem — portfolio insights, first-class AI agents, live multiplayer agent chat, demo-driven development (DDD), walkthroughs, and shareouts.

Canopy Web is both a **product** (a workbench for supervising a fleet of AI agents and sharing the work they produce) and a **framework harvest**: its generic apps (agents, runs, turns, workspaces, realtime, chat sessions) are being extracted as the reusable substrate under the wider canopy ecosystem. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the framework/product boundary — the repo's one structural invariant.

## What's inside

- **Agent supervision** — first-class `Agent` entities with task boards, packaged turns, schedules, an Items inbox ("what needs *you*"), and a cross-fleet `/supervisor` surface (installable as a phone PWA with push notifications).
- **Execution control plane** — paired runners (laptop emdash daemons, cloud containers) heartbeat against `/api/harness`, claim queued turns, and stream results back over an append-only event ledger. Routing is an explicit per-agent runner cascade with per-source rules.
- **Live multiplayer chat** — durable, workspace-tenanted chat sessions with an agent: co-edited draft, presence, streamed replies (including live tool calls), driven over WebSockets by the shared `canopy-ui/chat` kit.
- **Demo-driven development (DDD)** — narrative → version → run → package (video + deck + narrative), with review gates, public storyboards (`/storyboard/:slug`), and a scene-by-scene reviewer surface.
- **Sharing** — walkthrough uploads (token-gated links), shared Claude Code session transcripts (`/share/:token`), dated team shareouts, and a cross-app activity timeline.

## Stack

| Layer | Tech |
|---|---|
| Backend | Django 5 (ASGI/uvicorn) + Django Ninja 1.x + Pydantic v2, PostgreSQL, Channels |
| API | OpenAPI 3.1 at `/api/openapi.json`; Scalar UI at `/api/docs/`; RFC 7807 errors |
| MCP | FastMCP 3.x Streamable-HTTP server mounted at `/api/mcp/` (per-user PAT auth) |
| Frontend | React 19 + Vite + Tailwind CSS 4 + shadcn/ui; types generated from the OpenAPI schema |
| AI | Anthropic Claude API (SSE streaming); dual backend — API key or Claude Code CLI |
| Deploy | AWS ECS Fargate (CloudFormation-owned), via the "Deploy to Labs (AWS)" GitHub workflow |

## Quickstart

Backend dependencies are managed with [`uv`](https://docs.astral.sh/uv/):

```bash
# Backend
cp .env.example .env          # set AI_BACKEND=api + ANTHROPIC_API_KEY, or AI_BACKEND=cli
uv sync --extra dev
uv run python manage.py migrate
uv run python manage.py runserver

# Frontend
cd frontend && npm install && npm run dev

# Or both at once
uv run honcho start -f Procfile.dev

# Or everything in Docker (backend + frontend + Postgres)
docker compose up
```

Run the tests:

```bash
uv run pytest                 # backend
cd frontend && npm run build  # frontend type check + build
```

## Where to read next

- [`CLAUDE.md`](CLAUDE.md) — the living operational reference: every route, endpoint, and design decision, kept current for AI agents working in this repo (humans find it just as useful).
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the framework/product tier boundary and per-app table.
- [`docs/superpowers/specs/`](docs/superpowers/specs/) — dated design specs (the "why" record for every major feature).
- [`docs/architecture/mcp-surface.md`](docs/architecture/mcp-surface.md) — the MCP server surface.

Deployed at `https://labs.connect.dimagi.com/canopy/` (Dimagi login required).
