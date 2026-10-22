# Turn-scoped agent token (phase 1)

**Decision (Jonathan, 2026-10-21):** an agent's runner turn calls canopy-web with a short-lived
token canopy-web issues for THAT turn, not the stored, never-expiring `canopy-pat`
(`CANOPY_WEB_PAT` in the agent's vault). Phase 1 is additive: every client prefers the turn
token and falls back to the stored PAT, so it deploys in any order. Phase 2 removes `canopy-pat`.

## Where a runner turn's agent identity comes from today

| Path | Identity it actually uses |
|---|---|
| Cloud runner, CLI / bin scripts (`claude -p` env) | `CANOPY_WEB_PAT` loaded by `_agent_env` from `~/.<slug>/.env` (or the turn's tmpfs `CANOPY_AGENT_ENV_FILE` when secrets are off disk) — the stored canopy-pat |
| Cloud runner, canopy-web MCP (`canopy-web-mcp-headers.js`) | env PAT is stripped by Claude Code, so `CANOPY_AGENT` → that agent's env file → canopy-pat |
| Laptop runner, emdash / claude-desktop / claude-cli | the runner cannot pass env; the CLI's `resolve_token` and the MCP helper find the agent (`CANOPY_AGENT` from the agent repo's settings, the caller envelope, or cwd) and read `~/.<slug>/.env` → canopy-pat |
| Agent bin scripts (Ada `bin/_canopy_http.py`) | env `CANOPY_WEB_PAT`, then `~/.<slug>/.env` → canopy-pat |
| `device_key._child_env` | the RUNNER's own identity (device-key enrollment), never an agent's — out of scope |

## Server

`POST /api/harness/runners/{rid}/turns/{tid}/agent-token` (`apps/harness/api_turn_agent_token.py`),
the shape of the per-turn GitHub token. Issued only when **all** hold, else refused:
the request is authenticated by a runner token (`crt_`) of runner `rid` (a PAT or OAuth token —
even the runner owner's — is 403); the turn is claimed by `rid` and executing (else 404);
`runner_may_hold_agent` (else 404); not `origin=local` (a person's local session acts as the
person), not confined to a capability (a caller's turn gets no agent identity), and the turn
runs as an agent (its own or its chat's) with an active `Agent.user` (409 with the reason).
It mints a `PersonalToken` for `Agent.user` expiring in **12 h**, recorded as a `TurnAgentToken`
(turn, runner, token). Each call mints a fresh one; earlier ones stay valid until the turn ends.
Issuance is logged as an `AgentCredentialRead` (`name=turn-agent-token`, `via=turn`, runner,
turn id — beside the vault reads) and an `agent.canopy_token.issued` event. Mode/acts-as are
untouched (decided at claim).

**Revocation:** every token of a turn is revoked when `finish_turn` runs (done/failed/
cancelled/missed, and its requeue paths), when the lease sweep marks it lost/cancelled, and by
a catch-all on every sweep tick for any token whose turn is no longer executing on the runner
it was issued to (requeues done by queryset updates). The 12 h expiry bounds anything missed.

## Clients (each prefers the turn token, falls back to the stored canopy-pat on any error)

- **Cloud runner** (`_agent_token_env`): for a non-confined agent turn it asks with its runner
  token and puts the token (a) in the turn's env as `CANOPY_WEB_PAT`, layered over the agent's
  `.env` — the CLI, bin scripts and anything reading env use it — and (b) in
  `~/.canopy/agent-token/turn/<turn id>.token` (0600), removed when the turn ends, for the MCP
  headers helper, which cannot see a secret env var but does see `CANOPY_TURN_ID`.
- **canopy CLI** (`canopy_web.resolve_token`): in an agent's session with no env PAT, before
  reading `~/.<slug>/.env`, it resolves on demand — this box's runner token
  (`runner_session.token`) + the session's turn (`provenance.resolve_parent`) — and uses the token
  only if canopy-web says it is for this same agent. Cached per turn (0600), refusals for 15 min.
  This is the laptop seam: emdash cannot pass env, but every CLI call is a fresh process that
  can re-ask, and the turn id is already on disk (the caller-context by-task record).
- **MCP headers helper**: reads the cloud runner's per-turn file before the agent's env file.
  On laptops it keeps the stored PAT in phase 1: an emdash session's MCP connection is made
  once and outlives the turn, so a token revoked at turn end would strand the session's tools.

## Known gaps (phase 2 preconditions)

The laptop emdash create path finishes the turn as soon as the session exists, so those turns
get no token (the CLI falls back). Before removing canopy-pat: laptop emdash turns must stay
executing while their session works (or the token must follow the session binding), and the MCP
helper on a laptop needs a source that survives turn end. Agent bin scripts that read
`~/.<slug>/.env` directly work on the cloud (env wins) but not on a laptop.
