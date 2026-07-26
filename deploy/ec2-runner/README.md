# Canopy cloud runner on EC2 (CloudFormation)

Infrastructure-as-code for an ephemeral EC2 box that runs a **`kind=cloud`** canopy
runner: it pairs with canopy-web, claims harness `Turn`s whose target is in its
capabilities, runs `claude -p` on the turn's prompt, streams the assistant/tool
output into the `TurnEvent` ledger, and finishes the turn. As of the
**cloud-agent-bootstrap** milestone it is agent-ready — a real third standby, not
just a `canopy-web` repo runner — see
`docs/superpowers/specs/2026-07-25-cloud-agent-bootstrap-design.md`. As of
**run-convergence PR2** it is also **session-capable**: it also posts the raw
transcript for every turn and can `--resume` a claude CLI session across turns —
see "Session-capable, transcript-posting, resumable" below.

Everything is declared in **`runner.cfn.yaml`** (CloudFormation, matching
`deploy/aws/canopy-web.cfn.yaml`): the instance, security group, IAM role, and a
CFN-managed key pair. The box configures itself via **cloud-init** (no imperative
SSH provisioning), and reads its secrets from **Secrets Manager** using the
instance role at boot — secrets are never baked into the template or copied over the
wire.

## Files
- `runner.cfn.yaml` — the whole stack (instance + SG + IAM role + key pair + cloud-init).
- `cloud_runner.py` — the self-contained (stdlib-only) runner. `up.sh` splices it into the template as base64 at deploy time (single source of truth).
- `bootstrap_agents.sh` — idempotent agent-fleet bootstrap (tooling, gog, per-agent clones + secrets, claude plugins). Runs ON the box, from a live `canopy-web` clone — not baked into the template. See "Bootstrap" below.
- `secrets.sh` — put/update this runner's secrets in Secrets Manager (values read from a file/stdin, never shell history).
- `up.sh` — validate + render + `cloudformation deploy`; pulls the private key from SSM for SSH.
- `wire.sh` — operator-side: stage the fresh runner's credential bundle, retire its predecessor, swap agent assignments onto it, optionally drill it. Run this after `up.sh`.
- `down.sh` — `delete-stack` (add `--purge-secrets` to also remove the secrets).

`*.pem` and the rendered template are gitignored.

## Lifecycle

```bash
cd deploy/ec2-runner
aws sso login --profile labs   # you, once per session
./up.sh                        # stand up the box (~3 min to fully boot)
./wire.sh --drill               # stage credentials, retire the predecessor,
                                 # swap agent assignments onto it, drill it
# ...runner is live, claiming turns for its assigned agents/projects...
./down.sh                       # tear it down (it bills hourly) — keeps secrets
```

`up.sh` alone is not enough to make the box do agent work: it pairs a runner with
no credential, which sits waiting (see "Ordering" below). `wire.sh` is the second
half — it is what turns a paired-but-idle box into the fleet's active cloud
standby. `wire.sh --runner-id <uuid>` skips discovery if you already know the id;
`wire.sh --agents ace,echo` narrows which agents' assignment lists get swapped
(default: every agent that already has an assignment list). Re-running `wire.sh`
(e.g. after a `down.sh && up.sh` recycle) is the normal way to promote a fresh box
over its predecessor — it finds the new runner, stages it, retires the old one, and
moves every assignment across in one command.

Watch it come up / work:
```bash
ssh -i canopy-cloud-runner-key.pem ubuntu@<ip> 'journalctl -u canopy-runner -f'
# cloud-init progress: ssh ... 'sudo cat /var/log/cloud-init-output.log'
```

## Bootstrap (agent-fleet provisioning)

`cloud_runner.py`'s `main()` clones/pulls `canopy-web` to `/opt/canopy-web` and runs
`deploy/ec2-runner/bootstrap_agents.sh` from that clone once per service start —
**after** `fetch_and_stage_credential()` has staged this runner's Claude token, 1Password
service-account token, and GitHub token (see the docstring on
`bootstrap_agent_fleet()` for exactly why the ordering has to be this way round, not
a cloud-init `ExecStartPre`). The script is idempotent — each of its five steps is
`OK`-skipped when already satisfied — and best-effort per agent: one agent's
manifest being broken logs loudly but does not stop the other four from bootstrapping,
and does not block the runner from starting up and claiming turns.

Steps: (1) install/verify `uv`, the `canopy` CLI (a `uv tool`), `gog` (latest Linux
release, no version pin), `op`/`gh`/`claude`/`git`; (2) set gog's keyring backend to
`file` (headless Linux has no OS keychain) and write the account→client map into
`gog`'s own `config.json`; (3) per agent in `AGENT_SLUGS` (default
`ace,ada,echo,eva,hal`): clone/pull `github.com/dimagi-internal/<slug>` into
`/opt/agents/<slug>`, run `canopy provision --repo /opt/agents/<slug>`, and import
the agent's gmail refresh token from 1Password only if it isn't already live; (4)
add + install the `canopy` Claude plugin; (5) print a per-agent readiness summary.

**Agent turns run in the agent's clone.** A turn whose target resolves to an agent
with a clone under `/opt/agents/<slug>` runs `claude -p` there (freshly `git
pull`ed at claim), not in a throwaway scratch dir — so it sees the agent's real
config, skills, and state. Project/session turns, and an agent bootstrap hasn't
reached yet, keep the original `WORK_DIR` scratch-dir behavior.

**Updating bootstrap logic** is a `git push` to `canopy-web` main + a
`systemctl restart canopy-runner` on the box (or just wait for the next restart) —
unlike `cloud_runner.py` itself (baked into UserData; see "Updating the runner
code" below), it is NOT tied to the stack's cloud-init generation.

## Session-capable, transcript-posting, resumable (run-convergence PR2)

The runner declares **`capabilities.sessions: true`** by default (env
`RUNNER_SESSIONS`, "0"/"false"/"no" to opt a specific box out) — this is what makes
it eligible to claim chat/session-targeted `Turn`s at all (`claim_next_turn` gates
those on `Runner.session_capable()`, apps/harness/models.py). It is **not yet a
CloudFormation parameter** (`runner.cfn.yaml` only templates
`RUNNER_PROJECTS`/`RUNNER_AGENTS`/`RUNNER_WORKSPACE`/`RUNNER_NAME` into
`runner.env`); since the default is on, no template change is needed to enable it —
to disable it on a specific box, add `RUNNER_SESSIONS=0` to `/opt/canopy-runner/runner.env`
by hand and restart the service. Capabilities are re-synced on every restart
(`pair_or_load` now `PATCH`es an already-paired runner's capabilities in place, via
`PATCH /api/harness/runners/{id}`), so flipping the env on an existing box takes
effect on the next restart without re-pairing (which would otherwise orphan its
`RunnerBinding`s).

**Raw transcript, forwarded verbatim.** Every raw `claude -p --output-format
stream-json` line — whether or not it parses as JSON — is ALSO forwarded to
`POST /api/harness/turns/{id}/transcript`, in addition to the reduced
`assistant`/`tool_start`/`tool_end` `TurnEvent`s already streamed for the live UI.
This is the durable, re-derivable artifact cost/structure aggregators read from
(apps/harness's `TurnTranscript` model); the ledger stays a live, lossy stream on
purpose. Batching is **by bytes, not line count** — the server caps a single
request at 1 MiB of line bytes (`TRANSCRIPT_APPEND_MAX_BYTES` in
`apps/harness/api.py`) and 422s over it, since a single large tool result can
exceed a naive per-N-lines batch — `cloud_runner.py` flushes at 512 KiB
(`TRANSCRIPT_FLUSH_BYTES`) or every 10s (`TRANSCRIPT_FLUSH_SECONDS`), whichever
comes first, well under the server's cap, and always flushes whatever remains at
the end of the turn. Every batch carries a unique `batch_id` scoped to
`(turn, attempt, seq)` so a resume-fallback retry (see below) can never collide
with — and silently no-op-drop — an earlier attempt's batch. A transcript POST
failure is logged and swallowed: **it never fails the turn.**

**Real `--resume` continuity.** A session turn's CLI session id is captured from
the first stream-json line that carries one (normally `system`/`init`, which fires
before any other output) and round-tripped through the EXISTING
resolve-session/record-session RPCs (`POST /api/harness/runners/{id}/resolve-session`
/ `record-session`) so a later turn on the same canopy `Session` can pass it back as
`--resume <id>` instead of cold-starting. Concretely: `_session_resume_plan` asks
resolve-session for a reusable handle before running claude; `_record_session_resume`
reports the fresh (or reused) session id back after the turn. This reuses
`RunnerBinding.session_key` — documented as "engine-agnostic ... was emdash_task" —
rather than the newer `Session.cli_session_id` column
(`apps/canopy_sessions/models.py`), which the docstring says is unwired for exactly
this purpose but which nothing in `apps/harness/services.py` actually persists yet
(`record_session`'s `session_id` param is accepted for wire-compat and currently
discarded). `cloud_runner.py` sends BOTH fields on every record-session call, so
wiring that column server-side later needs zero runner-side changes. Reuse is
additionally gated on a stable `RUNNER_HOST` value (new env var, defaults to
`RUNNER_NAME`) sent on every heartbeat/pairing call — `RunnerBinding.reusable_by`
requires the runner id AND host to match the ones that recorded the binding, so a
brand-new EC2 instance (a new `RUNNER_HOST`, since `RUNNER_NAME` defaults from the
hostname) correctly cannot "resume" a session it never actually ran, and falls back
to a fresh spawn instead.

If `--resume <id>` yields **nothing** — the CLI exits non-zero having emitted no
stream-json lines at all, its behavior when the resume target no longer exists —
`run_claude` retries **once** as a fresh spawn (no `--resume`), mirroring the
reuse-then-fall-back-to-create pattern `packages/canopy_runner/execute.py` already
uses for emdash sessions. A failure that happens AFTER real output was produced
under a resumed session is treated as a genuine task failure and is never retried
(retrying would silently duplicate work under a new session).

**What was and was not verified.** There is no live cloud runner instance running
today (the EC2 stack is down), so none of this has been exercised against a real
`claude` binary or a real canopy-web deployment. What IS covered, by unit tests
under `deploy/ec2-runner/tests/` (mocking `subprocess.Popen` and the runner's own
`_api` helper — see the test file for exactly what's stubbed): capability
env-gating and the pair-time `PATCH`-in-place sync; byte-bounded transcript
batching (`_chunk_transcript_lines`); `--resume` argv construction; the
resolve-session/record-session request shapes (including the agentless-and-
projectless-session degrade-to-fresh-spawn case); and `run_claude`'s session-id
capture, transcript forwarding, and the resume-fails-then-falls-back-to-fresh-spawn
behavior (and that a genuine post-resume failure does NOT retry). Not verified:
the real `claude -p --output-format stream-json` event shape (the `system`/`init`
event carrying `session_id` is documented CLI behavior, not something captured
from a live run here), real network behavior against `apps/harness`'s actual
endpoints, and end-to-end resume against a real, previously-`--resume`d Claude
session.

## Per-agent canopy-web PAT (TODO — not yet provisioned)

Every agent currently fails `canopy doctor`'s workbench-token check on this box.
The check is being fixed to honor `CANOPY_WEB_PAT` (jjackson/canopy#400), because
the file it wants can only be produced by `/canopy:canopy-web-pat-mint` — a
browser loopback flow that cannot run on a headless host. But the box should also
stop borrowing the runner's own `CANOPY_TOKEN` for agent work: agents are already
their own canopy-web users, so a **per-agent PAT** gives per-agent attribution and
per-agent revocation for free.

This follows the normal fleet standard (`.env.tpl` + `op inject`) — no new auth
primitive, and `bootstrap_agents.sh` already runs the injection successfully for
all five agents. Three steps per agent, none of them automatable from a laptop
(minting for another user needs a one-off task against prod):

```bash
# 1. Mint on labs, one per agent. 180d is the default; use --ttl-days 0 for a
#    non-expiring token (see canopy-web PR #412 for the expiry model).
uv run python manage.py create_token \
  --email <slug>@dimagi-ai.com --label "cloud-ec2-1" --create-user

# 2. Store the raw value in that agent's vault as item `canopy-pat`, field
#    `credential` (kebab-case — item names with spaces/parens do NOT parse in
#    an op:// reference).

# 3. Add ONE line to the agent repo's tracked .env.tpl:
#      CANOPY_WEB_PAT=op://<vault>/<item>/<field>
#    op inject materializes it into ~/.<slug>/.env on the next bootstrap.
```

`canopy_web.resolve_pat()` already reads `CANOPY_WEB_PAT` ahead of the token file,
so nothing else has to change once the line is there.

> **Do not put a literal `op://vault/item/field` in a comment.** `op inject`
> resolves references anywhere in the file, comments included, and the resolved
> secret can leak to stdout. Use angle-bracket placeholders, as above.

## Vault standard

Agent secrets manifests (`config/secrets.yaml` in each agent repo) reference
**`op://Agent-<Name>/<kebab-item>/<field>`** (e.g. `Agent-Ace/gog-token/credential`)
for per-agent items, and **`op://Canopy-Shared/<kebab-item>/<field>`** for
fleet-wide ones (`github-token`, `gog-oauth-client`, `canopy-drive-folder`). The old
`AI-Agents` vault stays readable during the migration but nothing new should point
at it. `wire.sh` reads `Canopy-Shared/github-token/credential` for this runner's own
git access; `bootstrap_agents.sh` reads each agent's own
`Agent-<Name>/gog-token/credential` for its gmail refresh token. See
`docs/superpowers/specs/2026-07-25-cloud-agent-bootstrap-design.md`.

## Gmail token re-staging

Each agent's gmail auth lives as a **refresh token**, exported once via `gog auth
tokens export` on a machine where the agent is already logged in, and staged as the
`credential` field of that agent's `Agent-<Name>/gog-token` 1Password item.
`bootstrap_agents.sh` only re-imports it when the token ISN'T already live (`gog
gmail search --account <email> --client <client> in:inbox --max 1` fails) — a
healthy token is left alone. If Google revokes it (rare — the shared `canopy` OAuth
app is "External" + "In Production", so refresh tokens don't expire on their own),
re-export it from a working machine and `op write` it back into that agent's
`gog-token` item; the next bootstrap run (next service restart) picks it up. The
imported file is never left on disk — `bootstrap_agents.sh` `shred -u`s it (falling
back to `rm -f`) whether the import succeeded or not.

## Secrets (in Secrets Manager, under `canopy/cloud-runner/`)
- `canopy/cloud-runner/canopy-pat` — a canopy-web PAT (the runner pairs + claims as this user). **Required** — `up.sh` refuses to deploy without it.
- `canopy/cloud-runner/claude-oauth-token` — a **dedicated** claude setup-token (`CLAUDE_CODE_OAUTH_TOKEN`). Mint with `claude setup-token` as `ace@dimagi-ai.com` (Max subscription). It's long-lived and non-rotating, so the runner is self-sufficient after one bootstrap. **Do not copy ace-web's live OAuth blob** — its refresh tokens rotate on every use, so a second consumer gets invalidated (verified: it 401s / can't refresh). **Required**.
- `canopy/cloud-runner/op-service-account-token` — a 1Password service-account token, same one the laptop runners use for now (see the design spec's "out of scope: dedicated cloud SA token"). **Optional** — without it, `bootstrap_agents.sh`'s `canopy provision` and gmail-token-import steps skip (logged, not fatal); the runner still comes up and can serve `canopy-web`-repo turns.

Stage them with `./secrets.sh {canopy|claude|op} <file|->`. `wire.sh` reads the
`canopy`/`claude`/`op` secrets from Secrets Manager and `Canopy-Shared/github-token`
from 1Password, and `POST`s them all into the freshly-paired runner's credential
bundle (`/api/harness/runners/{id}/credential`) — that's what unblocks
`fetch_and_stage_credential()` and lets bootstrap actually run.

## Prove it end to end
Enqueue a turn the runner can claim (target must be in its caps — default
`RunnerProjects=canopy-web`):
```bash
curl -sS -X POST "https://labs.connect.dimagi.com/canopy/api/harness/turns/" \
  -H "Authorization: Bearer <canopy-pat>" -H 'Content-Type: application/json' \
  -d '{"project":"canopy-web","workspace":"dimagi","origin":"api","prompt":"Reply with a one-line hello.","idempotency_key":"ec2-smoke-1"}'
```
The runner claims it, runs claude, and events + result land at
`GET /api/harness/turns/<id>` and stream live over the SP1 realtime `turn.{id}` socket.
For an agent, `wire.sh --drill` is the equivalent proof — it fires a readiness drill
per assigned agent and prints pass/fail.

## Tear down (it's billed hourly)
```bash
./down.sh                    # delete the stack (keeps the secrets for next time)
./down.sh --purge-secrets    # also delete the secrets
```
The runner row itself is retired the next time `wire.sh` stands up a replacement,
not by `down.sh` — while the stack is down, the only ongoing cost is the Secrets
Manager secrets (~$0.40/secret/mo) and the 1Password items.

## Config (CloudFormation parameters)
Override with `EXTRA_PARAMS='Key=Val Key=Val' ./up.sh`:
`InstanceType` (t3.medium), `CanopyBaseUrl`, `RunnerProjects`, `RunnerAgents`,
`AgentSlugs` (`ace,ada,echo,eva,hal` — which agents `bootstrap_agents.sh`
provisions; distinct from `RunnerAgents`, which is which agents this runner may
CLAIM turns for), `RunnerWorkspace` (dimagi), `RunnerName`. `SshCidr` is set to
your IP automatically.

## Updating the runner code
`up.sh` splices `cloud_runner.py` into the template's UserData as base64, but
**CloudFormation applies a UserData change to an existing instance as a
stop/start — it does NOT re-run cloud-init.** cloud-init's `write_files` (and
everything else in the `#cloud-config` block) only runs on an instance's
*first* boot. So editing `cloud_runner.py` and running `./up.sh` again updates
the *template*, but a box that's already up keeps running the old bytes on
disk until it's replaced — a silent deploy gap, not a redeploy.

Until there's a real re-provisioning mechanism, ship a `cloud_runner.py` change by
recycling the stack:
```bash
./down.sh          # keeps the secrets (no --purge-secrets)
./up.sh && ./wire.sh --drill   # fresh instance, fresh cloud-init, new code; re-wire it
```
`bootstrap_agents.sh` doesn't have this gap — see "Bootstrap" above.

## Notes
- **Ephemeral by design.** The claude token lives only in Secrets Manager + in
  `/opt/canopy-runner/runner.env` (chmod 600) on the box; `down.sh` removes the box.
  The env is re-fetched from Secrets Manager on every `systemctl restart`, so a
  rotated token is picked up without a redeploy.
- **Runner identity:** the runner pairs on first boot and caches its id in
  `~/.canopy-cloud-runner.json`; a restart reuses the same runner row. A stack
  recycle (`down.sh && up.sh`) is a fresh instance, so it pairs a NEW row —
  `wire.sh` is what finds it and retires the old one.
