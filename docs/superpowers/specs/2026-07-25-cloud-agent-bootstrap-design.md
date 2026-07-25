# Cloud Agent Bootstrap — an agent-ready EC2 runner, one-command lifecycle

**Date:** 2026-07-25
**Status:** Approved design, pre-implementation
**Builds on:** `deploy/ec2-runner/` (SP2b box), `2026-07-24-directed-runner-routing-design.md` (assignments, drills, pins)

## Problem

The EC2 cloud runner executes `claude -p` in an empty scratch dir. Drills proved it
can reach the control plane but cannot do agent work: no agent repos, no agent env,
no gmail tooling, no Claude plugins. The operator wants it as a REAL third standby —
but torn down when idle (it bills hourly), stood up in one command, and always
up-to-date when it comes back. Everything must derive from code + secret stores
(infra-as-code); nothing hand-provisioned on the box.

Separately, the fleet is migrating its 1Password layout: away from one big
`AI-Agents` vault to **`Agent-<Name>` per agent + `Canopy-Shared` for fleet-wide
items** (kebab-case item names: `gog-token`, `canopy-pat`, `claude-oauth-token`,
`gdrive-root-folder`; shared: `gog-oauth-client`, `github-token`,
`canopy-drive-folder`). Ada is furthest along; the other agents' provisioning
manifests still reference `AI-Agents`. The cloud bootstrap must be built against the
NEW layout, and the agent repos must finish the migration so laptop and cloud
provision identically.

## Decisions

- **Reuse the fleet's provisioning conventions, don't invent cloud ones.** Each
  agent repo's `config/secrets.yaml` + `canopy provision` (op-driven, headless with
  a service-account token) is the single provisioning path on laptop AND cloud.
  The worktree-clean `~/.<slug>/.env` convention already makes plain clones work.
- **Vault standard:** manifests reference `op://Agent-<Name>/...` and
  `op://Canopy-Shared/...` only. `AI-Agents` remains readable during transition but
  nothing new points at it.
- **gog refresh tokens** (the one interactive OAuth artifact) are staged in
  1Password — `Agent-<X>/gog-token/credential` holds the `gog auth tokens export`
  payload (done 2026-07-25 for all five agents) — and imported on the box with the
  `file` keyring backend. Re-export is only needed if Google revokes a token.
- **Same service-account token** as the laptops for now (staged via the runner
  credential bundle / Secrets Manager); a dedicated cloud SA can be minted and
  rotated later without design change (one secret swap).
- **Bootstrap runs on the box at service start, from the repo** — not baked into
  UserData. cloud-init stays a thin kernel (install claude/node/gh/op/uv/gog, write
  the service); the service clones/pulls canopy-web and runs
  `deploy/ec2-runner/bootstrap_agents.sh` from it before starting the runner. That
  makes bootstrap logic updatable by `git push` + service restart (closing most of
  the UserData/cloud-init update gap) and idempotently re-synced on every restart —
  the "kept up to date" property.
- **Operator wiring is a script, not a runbook**: `wire.sh` does what was done by
  hand on 2026-07-25 (await pairing → credential bundle → retire predecessors →
  swap assignments → optional drill wave).
- **Retiring a runner cascades its assignment rows** (server fix): lingering rows
  of a retired runner made every matrix PUT 422 ("unknown or retired runner id").

## Components

### 1. `deploy/ec2-runner/bootstrap_agents.sh` (runs ON the box, idempotent)

Inputs from env (staged by the runner service before it runs): `CANOPY_TOKEN`,
`OP_SERVICE_ACCOUNT_TOKEN`, `GITHUB_TOKEN` (via git credential store), plus
`AGENT_SLUGS` (default `ace,ada,echo,eva,hal`) and `AGENT_ROOT=/opt/agents`.

Steps, each `OK`-skipped when already satisfied:
1. Tooling: `uv` (+ `canopy` CLI as uv tool from the canopy plugin repo), `gog`
   (Linux release binary), verify `op`, `gh`, `claude`, `git` on PATH.
2. `gog auth keyring file`; write `~/.config/gogcli/config.json` account→client map
   (`ace:ace, echo:echo, ada|eva|hal:canopy`).
3. Per agent: clone or `git pull` `github.com/dimagi-internal/<slug>` into
   `/opt/agents/<slug>`; run `canopy provision --repo /opt/agents/<slug>` (writes
   gog client creds + `~/.<slug>/.env` from the vaults); import the gmail token:
   `op read "op://Agent-<Name>/gog-token/credential" > tmp && gog auth tokens
   import tmp` (skip if a live token already validates).
4. Claude plugins: `claude plugin marketplace add https://github.com/jjackson/canopy.git`
   + `claude plugin install canopy@canopy` (idempotent).
5. Print a per-agent readiness summary; exit non-zero only on total failure (a
   single agent's failure logs loudly but leaves the runner serving the others —
   drills are the per-agent verdict).

### 2. Runner integration (`cloud_runner.py` + `canopy-runner.service`)

- The systemd unit gains `ExecStartPre` (or the runner calls it before its loop,
  after `fetch_and_stage_credential` — implementation's choice, but credentials
  must be staged first): clone/pull canopy-web to `/opt/canopy-web` and run
  `bootstrap_agents.sh`.
- **Agent turns execute in the agent's clone**: `run_claude` cwd becomes
  `/opt/agents/<slug>` when the turn's target resolves to an agent with a clone
  (fresh `git pull` at claim); scratch dir stays for project/session turns and for
  agents without clones.

### 3. `deploy/ec2-runner/wire.sh` (operator side, after `up.sh`)

1. Poll `GET /api/harness/runners/` until a cloud runner with no credential appears
   (or `--runner-id`).
2. `POST /runners/{id}/credential` from Secrets Manager (`claude-oauth-token`,
   `op-service-account-token`) + 1Password (`Canopy-Shared/github-token`).
3. Retire every OTHER non-retired cloud runner with the same name.
4. For each agent (default: all with assignments): replace the retired
   predecessor's assignment row with the new id (preserving order + enabled), or
   append at the tail if absent.
5. `--drill`: fire the drill wave and poll to completion, printing the grid.

Bearer token: `~/.claude/canopy/workbench-token`. Pure curl+python3, no deps.

### 4. Server fix: retire cascades assignments

`retire_runner` deletes the runner's `RunnerAssignment` rows in the same
transaction (their ranks close up implicitly; rank values need not be compacted —
ordering is relative). Test: retire a runner listed at rank 1 of an agent → GET
shows the remaining rows; a subsequent PUT of the remaining list succeeds.

### 5. Agent repo manifest migration (separate PRs, one per repo)

For `ace, ada, echo, eva, hal`: `config/secrets.yaml` references become
`op://Agent-<Name>/<kebab-item>/<field>` and `op://Canopy-Shared/...`. Verified
with `canopy provision --check` on this Mac (new SA token sees all vaults) before
each PR. Repos whose manifest is already migrated (ada, likely) get verification
only.

## Validation & landing

1. Land canopy-web PR; recycle the stack (`down.sh && up.sh && wire.sh --drill`).
2. Target: 5/5 EC2 drills report honestly — pass, or fail only on genuine
   agent-level gaps (not environment plumbing). Iterate bootstrap until plumbing
   failures are gone.
3. `down.sh` — the stack stays down until needed; the runner row is retired by
   `wire.sh` next time it stands up. Cost while down: only the Secrets Manager
   secrets (~$0.40/secret/mo) and the 1P items.

## Out of scope

- Dedicated cloud service-account token (mint + rotate later; single secret swap).
- Warm ECS pool / autoscaling (2026-07-16 program spec still future).
- Emdash on cloud — cloud agent turns run bare `claude -p` in the clone; live
  session streaming for cloud agent turns stays at ledger-event fidelity.
