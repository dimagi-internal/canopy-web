# Multi-CLI agents: Codex beside Claude Code

**Date:** 2026-10-26
**Status:** Design — probed against Codex CLI 0.164.0 on a laptop (see Evidence)

## Problem

Every canopy turn runs on Claude Code. canopy-web makes no model calls of its own, so
"use OpenAI models" means "let a turn run on OpenAI's agent CLI, Codex", end to end:
routed to a box that has it, given the agent's instructions and guards, recorded as a
canopy session like any other, and confined exactly as tightly as a Claude turn.

The Claude assumption is spread across layers that never had to name it:

- `Runner.engine` (`emdash` / `claude-desktop` / `claude-cli`) mixes *where a session
  opens* with *which vendor runs it*; `Agent.runtime_engine` (`emdash` / `cloud_p` /
  `any`) is a third, older word for a host preference.
- The claim's "this box can confine" (`routing.profile_q`) and "this box is signed in"
  (`Runner.signed_out`) are per box. On a box with two CLIs they must be per CLI.
- Credentials (`RunnerLogin`) are Claude's kinds only.
- Turn prompts are Claude plugin commands (`/{slug}:turn`, `/ace:ask`).
- Durable session rows come from Claude's `~/.claude/projects/*.jsonl`.

## Vocabulary (the clarity half of this change)

| Word | Means | Values | Lives on |
|---|---|---|---|
| **cli** | the agent CLI that runs a turn | `claude-code`, `codex` | `Agent.clis` (allowed, ordered), `Turn.cli` (what ran), `Session.cli` (sticky) |
| **model** | a model inside a CLI | free text, e.g. `opus[1m]`, `gpt-6.1-sol` | `Agent.cli_models` (optional, per CLI) |
| **surface** | where a laptop opens a session | `emdash`, `claude-desktop`, `claude-cli` (headless) | `Runner.engine` (unchanged) |

Not "harness": `apps/harness` already names the whole turn-execution system.
A CLI is never inferred from a surface. `claude-desktop` and `emdash` run
`claude-code` only; the headless surface runs any CLI the box reports.
`Agent.runtime_engine` is left as it is (only `AgentIn` reads it) and is not the CLI.

## Evidence (probed 2026-10-26, Codex CLI 0.164.0)

- **Plugins:** `codex plugin marketplace add <repo>` reads our existing
  `.claude-plugin/marketplace.json` unchanged; `codex plugin add echo@echo` installs to
  `$CODEX_HOME/plugins/cache/echo/echo/<version>`. `skills/list` (app-server) then lists
  every echo skill as `echo:<name>` (including `echo:turn`) and every canopy skill as
  `canopy:<name>`. `commands/*.md` are **not** loaded.
- **Hooks:** `hooks/list` shows the canopy plugin's own `hooks/hooks.json` loaded as-is —
  same events, same matchers (`Bash|…|mcp__.*`), `trustStatus: "untrusted"` until
  trusted. Codex sets `CLAUDE_PLUGIN_ROOT` for compatibility. `PreToolUse` covers shell
  (`Bash`), `apply_patch` (matchable as `Edit`/`Write`), MCP tools (`mcp__server__tool`)
  and local function tools; deny is the same `hookSpecificOutput.permissionDecision:
  "deny"` or exit 2. Hosted tools (web search) are **not** hookable. Untrusted hooks are
  skipped; `--dangerously-bypass-hook-trust` or managed `requirements.toml` hooks run
  without the `/hooks` review.
- **Context injection:** a probe `SessionStart` hook's `additionalContext` reached the
  model as a developer message (rollout ordinal 11); `AGENTS.md` was loaded (ordinal 7).
- **Sandbox:** OS-enforced (Seatbelt on macOS, bubblewrap on Linux). Permission profiles
  take filesystem entries `{path | glob_pattern, access: read | write | deny}` and a
  network domain allow/deny list — reads can be denied, which Claude Code's own layer
  could not do for us.
- **Headless:** `codex exec --json` (JSONL events: `thread.started`, `turn.started`,
  `item.*`, `turn.completed|failed`), `exec resume <id>`, `--ephemeral`,
  `--ignore-user-config`, `-c key=value`, `-C <dir>`. A prompt argument with an open stdin
  waits for stdin — the runner must close it.
- **App server:** `codex app-server --listen stdio://`, a versioned JSON-RPC protocol
  (`generate-json-schema`): `thread/start|resume|fork|read`, `thread/items/list`,
  `turn/start|steer|interrupt`, `account/login/start`, `account/rateLimits/read`,
  `skills/list`, `hooks/list`, `plugin/install`; server notifications `item/*`,
  `turn/*`, `thread/tokenUsage/updated`, `account/rateLimits/updated`; server requests for
  approvals and `item/tool/requestUserInput`.
- **Storage:** `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl`, each
  record carrying a monotonic `ordinal`. Codex warns this file is not a stable interface.
- **Auth:** `codex login` (ChatGPT, browser or `--device-auth`), `--with-api-key`,
  `--with-access-token`; state in `$CODEX_HOME/auth.json`, whose refresh token rotates.
- **ACP:** `@zed-industries/codex-acp` last shipped 0.16.0 on 2026-06-08 and lags the CLI.
  We do not build on it.

**Not yet probed live** (the laptop's ChatGPT login had expired): a completed turn, a
`PreToolUse` deny on a real tool call, invoking a plugin skill from a prompt, canopy-web
MCP over a turn token. Plan 2's first task is that probe; nothing in Plan 1 depends on it.

## Design

### 1. Harness is chosen at the claim, from what the agent allows and the box can do

- `Agent.clis`: ordered list, default `["claude-code"]`, declared with the agent's
  definition (`AgentIn`, `DEFINITION_FIELDS`). `Agent.cli_models`:
  `{"<cli>": "<model>"}`, optional.
- A runner reports, on the heartbeat, `capabilities.clis`:
  `{"<cli>": {"signed_in": bool, "profiles": int, "envelope": int, "version": str}}`.
  A runner that reports none is read as today's box:
  `{"claude-code": {"signed_in": not runner.signed_out, "profiles": caps.profiles,
  "envelope": caps.envelope}}` — so nothing changes for a box until it reports.
- **One rule**, `apps/harness/clis.py::choose(turn, runner) -> str | None`, asked
  by the claim, by coverage (`_refined_allows`), and by `turn_reach`, so the queue's
  "why is this waiting" can never disagree with what claiming does:
  1. candidates = `[session.cli]` if the turn's session already has one, else the
     acting agent's `clis`, else `["claude-code"]` (repo and project turns);
  2. keep those the runner reports `signed_in`;
  3. a confined turn (`Turn.capability` set) keeps only CLIs reporting
     `profiles >= PROFILES_VERSION` and `envelope >= ENVELOPE_VERSION`;
  4. a turn requiring `sensitive_data` keeps only `SENSITIVE_CLIS`
     (`{"claude-code"}` until a Codex ZDR runner exists);
  5. the first left, in the agent's order, wins; none → this runner does not take it.
- The claim stamps `Turn.cli`, sets `Session.cli` on a session's first claim,
  and sends `cli` and `model` in the claim payload. A session never changes CLI:
  its history lives in one CLI's store.
- `routing.profile_q` and the `runner.signed_out` early return become per-CLI: the
  SQL prefilter admits a confined turn when *any* CLI on the box confines, and
  `choose` decides. A pin still never bypasses either (unchanged property).
- `turn_reach` gains a reason: "no runner has <cli> signed in" when the
  counterfactual without step 2/1 would cover it.

### 2. Credentials carry their CLI

`RunnerLogin.cli` (default `claude-code`). Codex kinds: `login` (ChatGPT, held on the
box in `~/.codex-logins/<key>` as `CODEX_HOME`, signed in through the existing mint relay
with `codex login --device-auth`) and `api_key` (an OpenAI key, sealed in the runner's
vault, applied with `codex login --with-api-key` into a fresh `CODEX_HOME`). The cascade
is per CLI; the probe, caps and alerts machinery is keyed by entry and labels its
checks `codex.probe.<label>`. The last-fallback refusal applies per CLI.

### 3. Instructions reach Codex through the plugin we already publish

- The runner installs the agent's plugin and canopy's into the turn's `CODEX_HOME` with
  `codex plugin marketplace add` + `codex plugin add` (verified to read
  `.claude-plugin/marketplace.json`). No second packaging.
- **Persona:** Codex reads `AGENTS.md`; the runner passes
  `-c project_doc_fallback_filenames=["CLAUDE.md"]` so an agent repo needs no new file
  (verify the key in the Plan 2 probe; fallback: the factory writes `AGENTS.md`).
- **Prompts:** canopy-web keeps sending `/{slug}:turn …`. The runner translates a leading
  plugin command to the skill of the same name for Codex (`canopy_codex.prompts`), and
  refuses — failed turn, saying so — a command with no skill. Agents whose turn exists
  only as `commands/turn.md` get a `skills/turn` from the factory (echo already has one).
- **MCP:** the runner writes the turn's canopy-web MCP server (turn-scoped token) and the
  project gateway into `-c mcp_servers.*` overrides, the analogue of `--mcp-config`.

### 4. Guards: the same hooks, trusted by the box, plus Codex's own sandbox

- The canopy plugin's hooks run on Codex unchanged in shape. Cloud boxes trust them by
  installing them as **managed** hooks (`requirements.toml`, root-owned `managed_dir`),
  so a turn cannot untrust or edit them; laptops pass `--dangerously-bypass-hook-trust`
  only for hooks whose source is the installed canopy plugin.
- Guards (canopy repo) learn Codex's payload: `apply_patch` as an edit, no `Read`/`Grep`
  tools (reads arrive as shell commands, which `profile_guard` already parses),
  `CLAUDE_PROJECT_DIR` absent (fall back to `cwd`).
- The second layer is Codex's own permission profile, written per confined turn
  (`canopy_codex.native_permissions`, the analogue of `_native_settings`): filesystem
  `deny` for everything outside the turn's `read_paths`/`write_paths`, network limited to
  the egress allowlist, web search off.
- A box reports `profiles`/`envelope` for `codex` only after a startup self-test runs the
  guard on a recorded Codex payload and sees a deny. Until then `choose` keeps confined
  turns off Codex — fail closed, by construction.

### 5. Sessions are stored like any other runner session

- The cloud runner drives Codex through `codex app-server` (stdio), one process per turn
  (`canopy_codex`, a sibling of `canopy_acp`). Live: `item/*` notifications become the
  same frames ACP updates become (`tool_call`/`tool_call_update`, message chunks, usage).
  `turn/steer` delivers a follow-up into a running turn (`may_ride`).
- Durable: at each item completion and at turn end the runner reads
  `thread/items/list` and maps items to canopy's transcript row shape
  (`canopy_transcript.codex_rows`: user/assistant text, reasoning as thinking, command
  and MCP calls as `tool_use` + `tool_result`, patches as an edit tool call), ordinal =
  item position × `BLOCK_STRIDE` + block. They reach `persist_transcript_rows`
  unchanged; `transcript_sourced` is true. Backfill reads the same API (the protocol is
  versioned; the rollout file is not).
- `Session.cli_session_id` holds the Codex thread id; resume is `thread/resume`.
- Usage: `thread/tokenUsage/updated` feeds `SessionUsage` with the CLI recorded;
  `account/rateLimits/*` feeds the cap/park logic.

### 6. Laptop

The headless surface runs Codex through the same `canopy_codex` driver. emdash and the
Claude desktop app stay Claude-only; a box on those surfaces reports only `claude-code`.

## Plans

1. **canopy-web: the CLI axis** — fields, `choose`, claim/coverage/reach, payload,
   `RunnerLogin.cli`, API + UI, docs. Ships alone: every box reads as `claude-code`.
2. **Cloud runner: Codex executor** — live probe, `canopy_codex` (app-server driver,
   prompts, MCP overrides, native permissions), credentials and mint relay, session rows,
   usage and caps, CLI report.
3. **canopy plugin (canopy repo): guards and caller context on Codex** — payload
   adapters, self-test, factory (`skills/turn`, persona fallback).
4. **Laptop headless runner on Codex.**

## Not doing

- A multi-provider harness (opencode et al.): a Claude subscription may not be used in a
  third-party harness, so it would split the Claude fleet onto API billing.
- `codex-acp`: stale; the app-server protocol is first-party and versioned.
- Codex on sensitive-data runners: needs an OpenAI ZDR story; `SENSITIVE_CLIS` is
  the switch.
