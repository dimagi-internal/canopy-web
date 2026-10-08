# canopy-web — Product Context

## What It Is
A collaborative web workspace for building reusable AI skills from conversations, evolving into a multi-section workbench — project dashboard, skills feed, cross-portfolio insights, AI assistant integration.

## Who Uses It
- **Primary users**: Jonathan + small Dimagi team running 10+ projects concurrently. Power users who context-switch constantly between repos and want a single "what was I working on?" surface.
- **Usage pattern**: Daily check-in (homepage = projects dashboard), ad-hoc skill discovery and authoring, AI assistants (open claws, CLI agents) read/write the same API as humans. Single-tenant internal tool gated to `@dimagi.com` via Google OAuth.

## What Matters Most
1. The API is the real product. UI is one consumer. Design API-first so CLI agents and open claws can drive everything humans can.
2. The project workbench surface — reduce time-to-context when returning to a repo. Insights feed, action tracking, batch APIs are the spine.
3. Visual + interaction polish (Warm Earth dark theme) — this is the surface Jonathan and team look at every day; it must feel premium.

## Tech Stack
- Backend: Django 5 ASGI + uvicorn, PostgreSQL, `uv` for deps
- Frontend: React 19, Vite, Tailwind CSS 4, shadcn/ui
- AI: Anthropic Claude API (SSE streaming), dual backend (`api` direct or `cli` via Claude Code subscription), runtime-switchable via `/api/ai/switch/`
- Skill runtime adapters: `web`, `claude_code`, `open_claw`
- Deploy: AWS ECS Fargate on the shared labs platform, served at https://canopy.dimagi.com/ via the GitHub "Deploy to Labs (AWS)" workflow; infra in deploy/aws/canopy-web.cfn.yaml
- Auth: Google OAuth for `@dimagi.com`; debug session-cookie minting endpoint for handing access to AI assistants

## Current State
Active product, shipping multiple PRs/week. Recent work: cross-portfolio insights feed (#16, #17, #30), token auth and Bearer-write on `/guide/` (#20, #29), debug session-cookie minting (#26), action tracking and skills scanner (#13, #14, #27, #28), Warm Earth dark theme rollout (#11). The shape is solidifying around the projects-dashboard-as-homepage pattern.

## Known Considerations
- No multi-tenant auth in V1 — single allowed domain (`@dimagi.com`). Auth model could shift later; sweep stale guards when it does.
- Canopy CLI plugin is a git submodule at `./canopy/`; some skills test against this app.
- Deploy step in CI is a separate manual job — no auto-deploy on main merges.
- Health endpoint: `/health/`; production: `https://canopy.dimagi-ai.com/` (verify before relying).
- TODOS.md catalogues V2 work: proactive detection, MCP layer, prompt hardening, OAuth integrations, multi-tenant auth, cowork adapter.
