# Canopy cloud runner on EC2 (CloudFormation)

Infrastructure-as-code for an ephemeral EC2 box that runs a **`kind=cloud`** canopy
runner: it pairs with canopy-web, claims harness `Turn`s whose target is in its
capabilities, runs `claude -p` on the turn's prompt, streams the assistant/tool
output into the `TurnEvent` ledger, and finishes the turn. As of the
**cloud-agent-bootstrap** milestone it is agent-ready — a real third standby, not
just a `canopy-web` repo runner — see
`docs/superpowers/specs/2026-07-25-cloud-agent-bootstrap-design.md`. As of
**run-convergence PR2** it also posts the raw transcript for every turn and CAN
(opt-in, off by default — see below) become session-capable and `--resume` a
claude CLI session across turns — see "Session-capable, transcript-posting,
resumable" below.

Everything is declared in **`runner.cfn.yaml`** (CloudFormation, matching
`deploy/aws/canopy-web.cfn.yaml`): the instance, security group, IAM role, and a
CFN-managed key pair. The box configures itself via **cloud-init** (no imperative
SSH provisioning), and reads its secrets from **Secrets Manager** using the
instance role at boot — secrets are never baked into the template or copied over the
wire.

## Files
- `runner.cfn.yaml` — the whole stack (instance + SG + IAM role + key pair + cloud-init).
- `cloud_runner.py` — the self-contained (stdlib-only) runner. `up.sh` publishes it to Secrets Manager as the first-boot seed (`seed.py`: a manifest + parts); after that the box updates itself from git (see "Updating the runner code" below).
- `kms_broker.py` — the root-owned broker that derives with each runner's own KMS ECDH key, for that runner's uid only (stdlib-only; see "Several runners on one machine").
- `_lib.sh` — what the three lifecycle scripts must agree on: the runner-name → stack mapping and the seed's secret names.
- `seed.py` — builds the first-boot seed (manifest + ≤60 KB parts, sha256-checked on the box).
- `prune_caches.py` — bootstrap step 4b: drops plugin-cache versions nothing has installed, and npm/uv download caches.
- `update_runner.sh` — the auto-updater: installs the DEPLOYED runner sha when this box is behind and idle. Run on a 30-minute systemd timer via a thin `/usr/local/bin/canopy-runner-update` shim, and read from the canopy-web clone so it ships by deploy like everything else.
- `bootstrap_agents.sh` — idempotent bootstrap of the BOX (tooling, gog's keyring, the canopy plugin + CLI, Claude Code itself, caches); each agent is prepared by canopy's `canopy agent prepare --runner`. Runs ON the box, from the runner's own `canopy-web` clone (`RUNNER_SRC_DIR`) — not baked into the template. See "Bootstrap" below.
- `secrets.sh` — put/update this runner's secrets in Secrets Manager (values read from a file/stdin, never shell history).
- `up.sh` — preflight every one-time prerequisite, publish the seed, `cloudformation deploy` (updates via a reviewed change set); pulls the private key from SSM for SSH; invites a new box to pair (prints the approve link) and waits for it.
- `wire.sh` — operator-side: stage the fresh runner's credential bundle, wait for the box to prove one credential works, retire its predecessor, swap agent assignments onto it, optionally drill it. `--standby` instead adds it as a DISABLED row and touches nothing else; `--test` stages credentials, waits for the proof and stops. Run this after `up.sh`.
- `fleet.cfn.yaml`, `fleet_template.py`, `fleet.sh` — a runner FLEET: an Auto Scaling group with a warm pool whose members join on AWS's word under one passkey approval and whose size canopy-web sets (see "Runner fleets").
- `down.sh` — delete the stack, remove the SSH key, retire the runner row (dropping every route to it); `--keep-runner` for a recycle, `--purge-secrets` to also remove the shared secrets.

`*.pem` and the rendered template are gitignored.

## Lifecycle — commands only

Every runner is identified by its **name** (`Runner.name` in canopy-web); the stack
is derived from it (`_lib.sh`): `cloud-ec2-1` → `canopy-cloud-runner` (the live box,
and what every script means with no `--name`), `cloud-ec2-<x>` → `canopy-cloud-runner-<x>`.
Pass `--stack` to override.

**Replace / rebuild the live box** (the new box takes the old one's slots):
```bash
cd runner/ec2
aws sso login --profile labs      # you, once per session
./up.sh                           # stand up the box (refuses to touch a RUNNING instance — see below)
./wire.sh --drill                 # stage creds, retire the predecessor, swap its rows/rules/orders
                                  # onto the new box, drill it (non-zero exit unless all PASS)
```

**A new box pairs by invitation and holds no PAT.** Creating a stack needs
`--workspace <slug>` (the workspace it pairs into; up.sh lists yours when it is
missing). Once the stack's instance role exists, `up.sh` uses YOUR workbench token
to create a pairing invitation for that role and name (with the `<stack>-ecdh` KMS
public key), prints `approve: <link>` (also emailed to you) and waits up to
`--approve-minutes` (default 30) for the box to pair. Open the link and approve with
one passkey touch; only you, the invitation's creator, can, and no token can. The
box proves its instance role at `POST /api/harness/runners/pair-by-role`, becomes
your runner in that workspace with its role as an approved device key, and signs in
keyless from then on. `up.sh` records `<stack>-invitation.json` (`{id, runner_id}`),
from which `wire.sh --name` takes the runner id. An invitation expires after an hour;
re-run `up.sh` for a new one. An update of a stack whose runner exists needs no
invitation: a replaced instance re-attaches by its role. Flow and rules:
`docs/architecture/runners.md` § How a runner signs in.

**A second box, proven without routing any work to it** (what was run on 2026-10-04):
```bash
./up.sh   --name cloud-ec2-test --workspace dimagi --standby # stack canopy-cloud-runner-test; claims no
                                                             # repo or chat turns (RunnerProjects='', RunnerSessions=0)
./wire.sh --name cloud-ec2-test --standby --agents ada,eva --drill
                                                             # DISABLED row for ada+eva only, wait for the first
                                                             # bootstrap, drill ada+eva; nothing else touched
./down.sh --name cloud-ec2-test                              # delete the stack + key, retire the row
                                                             # (which deletes its assignment rows), verify none remain
```
`--standby` refuses an agent that **follows its workspace's runner order**: giving it a
row of its own — even a disabled one — would replace that order with a list holding
no enabled runner. Drill such an agent by adding the box to the workspace order.
Promote a proven standby by enabling its row on the agent's Settings → Runners.

**A test box that serves nothing until a chat is put on it** (`--test`):
```bash
./up.sh   --name cloud-ec2-fork --test --workspace canopy [--projects canopy-web,connect-labs]
                                  # directed-only (its invitation pairs it directed), RunnerSessions=1, RunnerAgents='',
                                  # RunnerProjects=--projects (default canopy-web), RunnerWorkspace=canopy
./wire.sh --name cloud-ec2-fork --test
                                  # discover it, stage its credentials, wait for one to answer
                                  # (--probe-minutes), stop: no retire, no assignment, rule or
                                  # workspace-order change, no --agents. Prints the chat URL.
./down.sh --name cloud-ec2-fork
```
A directed-only box (`Runner.directed`, a setting on its runner page) claims only turns pinned to it and
chats bound to it — never routed agent work, a repo turn, an unbound chat or an
inherited workspace order, even where a list names it. It stays session-capable,
so the chat "Run on" picker offers it ("directed only"): start a chat on it and the
chat binds to it. `--test` refuses the live name and needs `--workspace`;
`--workspace <slug>` (→ `RunnerWorkspace`) also works on any other `up.sh` run.

**Tear down** (it bills hourly): `./down.sh [--name X]`. It deletes the stack, then
retires the runner row — only a row that has STOPPED heartbeating, so a same-name
replacement that is still alive is never touched — then checks no agent's list or
rules still name it. Secrets stay (they are shared by every runner stack).

**Which Claude credentials a new box gets: none, until its owner signs it in.** A
box's tokens and API keys live only in a vault its own device key opens (see
`docs/architecture/vault.md`); canopy-web keeps their names and order, never a value.
So `wire.sh` copies nothing and holds nothing: it gives the new box its own vault
(`POST /runners/<id>/vault`), or with `--vault <id>` attaches one the box shares
inside its isolation boundary, and prints the box's Credentials tab
(`/w/<ws>/runners/<id>/credentials`). There the owner clicks **Make its key**
(one passkey touch) and then **+ Token** (a `claude setup-token` sign-in the box
runs and seals itself) or **+ API key** (sealed in the browser). `--credential-from`
is refused: credentials are never copied between boxes. A full login is signed in
on the same tab ("+ Add a login"); its refresh token rotates, so it never travels.

**A credential has to work before anything routes to it.** The box probes each
credential with one short prompt (`claude.probe.<label>` health checks, see
`docs/architecture/runners.md`). `wire.sh` waits up to `--probe-minutes` (default
15 — long enough to sign the box in; `0` skips) for a probe to be `ok`, prints every
probe, and when none is, exits 3 with the Credentials link before it retires, swaps
or adds anything.

**Finding the new box.** `wire.sh --runner-id <uuid>` skips discovery. Otherwise it
takes the cloud runner with that name that is online, heartbeated within the last 2
minutes, and has **no credential staged**
(`GET …/credential/status`: no token, no API key, no signed-in login), most recent
heartbeat first — a box waiting for its bundle already heartbeats (online, not ready),
so it is found by what it lacks. `wire.sh --agents ace,echo` narrows which
agents' lists a (non-standby) wire swaps. `wire.sh` waits up to 15 min for the box to
pair (`--discover-minutes`) and, with `--drill`, up to 40 min for its first bootstrap
before drilling — a drill fired earlier just queues behind the bootstrap.

**A box that serves no agents prepares none.** `bootstrap_agents.sh` provisions what
`GET /runners/{id}/agents` answers, an empty list included ("this box serves no agents
yet"); only no answer falls back to the registry.

Watch it come up / work:
```bash
ssh -i canopy-cloud-runner-test-key.pem ubuntu@<ip> 'journalctl -u canopy-runner -f'
# cloud-init progress: ssh ... 'sudo cat /var/log/cloud-init-output.log'
```

### One-time human steps (checked up front, never discovered mid-build)
Nothing in a build needs a human EXCEPT these, each done once and then reused by
every box. `up.sh`/`wire.sh` check each before writing anything and print the fix:

| prerequisite | checked by | fix |
|---|---|---|
| AWS SSO session for `labs` | `up.sh`, `down.sh` | `aws sso login --profile labs` |
| operator token `~/.claude/canopy/workbench-token` authenticates (checked FIRST, before AWS) | `up.sh` | `/canopy:canopy-web-pat-mint` |
| a NEW box's workspace (`--workspace <slug>`, one you belong to) | `up.sh` (lists yours) | pass `--workspace`; an update keeps the stack's own `RunnerWorkspace` |
| approving the new box's pairing invitation (one passkey touch) | `up.sh` (prints the link, waits `--approve-minutes`, default 30) | open the `approve:` link; it is also emailed to you |
| a Claude credential in the new box's vault | `wire.sh` (waits for a probe) | the box's Credentials tab (`wire.sh` prints it): Make its key, then + Token / + API key |
| `canopy/cloud-runner/gog-keyring-password` | `up.sh` | any strong password, `./secrets.sh gog <file>` |
| operator token `~/.claude/canopy/workbench-token` | `wire.sh`, `down.sh` | `/canopy:canopy-web-pat-mint` |
| per agent still on 1Password: its vault + service-account key | `wire.sh` (refuses in `--standby`) | `canopy agent vault --slug <slug> --workspace <ws>` (the setup checklist's Vault step) |
| per agent: GitHub delegation (owner's token) | `wire.sh` | agent Settings → Credentials → GitHub (the owner) |

## Where secrets live, and which key reads them

Every secret an agent uses is in a **canopy vault**, zero-knowledge: canopy-web stores
ciphertext and the copies of each vault's key, wrapped to the people and machines the
vault lists, and no 1Password key at all.

| what | vault | the box opens it with |
|---|---|---|
| what every agent in a tenant shares (the `canopy` / `canopy-web` gog OAuth **clients**) | a **workspace vault** attached at the tenant's root (Dimagi's is "Shared"), layered under each agent's own | this box's device key's copy of that vault's key |
| an agent's own secrets (its `canopy-pat`, `gog-token`, its `.env` values) | the **agent's vault** | this box's copy of the agent's vault key |
| a token a person delegated to the agent (its GitHub token) | the agent's vault, entry marked `delegated`, sealed to the machines that run it | its device key, from the resolve's own `delegated` field |
| the box's own Claude login | the **runner's vault** | its device key |

Set them on the vault's page (`/w/<ws>/vaults/<id>`), the agent's **Settings →
Credentials**, and **`/supervisor` → Runners → the box**. An agent still on 1Password
(`credential_source: 1password`) keeps its own vault key in canopy-web
(`PUT /api/agents/<slug>/vault`); one on the canopy vault is refused one.

## Is it healthy? (no shell needed)

Open `/supervisor` → **Runners** → the box. The **Health** panel is the box's own
report of which features work, sent on every heartbeat; the list shows a red/amber
`N issues` chip when anything is off. Checks the cloud runner makes:

| check | fails / warns when |
|---|---|
| `code_clone` | the runner's own clone (`RUNNER_SRC_DIR`, default `/opt/canopy-runner/src`) could not be synced to origin/main |
| `packages.transcripts` | `canopy_transcript` did not import — chat sessions write no durable rows |
| `packages.inbox` | `canopy_runner` did not import — the box reads no mail; the Gmail doorbell does nothing |
| `packages.acp` | (`RUNNER_EXECUTOR=acp` only) `canopy_acp` did not import — no stop, turns fall back to `claude -p` |
| `bootstrap` | `bootstrap_agents.sh` exited non-zero or did not run |
| `claude.credentials` | 0 credentials (fail) or 1 (warn: a usage cap stops every agent here) |
| `claude.version` | `claude --version` does not run |
| `canopy.cli` | the canopy CLI did not run (fail) or is behind the marketplace plugin (warn) — Refresh |
| `disk` | root volume ≥ 80% (warn) / ≥ 90% (fail) — the runner sweeps scratch and prunes caches on its own at its next idle moment; a fail that stays is not scratch: grow `VolumeSize` (see "Disk") |
| `scratch` | the disk sweep kept an old dir in `/tmp` or `WORK_DIR/sessions` because it holds work that exists nowhere else (uncommitted, or commits on no remote) — push or discard it; nothing else needs doing (see "Disk") |
| `github.<slug>` | the agent's owner has lent no GitHub token, or it is expired / cannot open a PR on the agent's repo — the OWNER sets it at the agent's Settings → Credentials → GitHub; checked at boot, so Refresh (or wait for the next bootstrap) to re-check |
| `env.<slug>` | the agent's `~/.<slug>/.env` could not be re-read from its vault when a turn was claimed (op's reason in the detail) — turns run on the last good file, which may be stale. Fix the vault/key; the next claim after `AGENT_ENV_TTL_SECONDS` (300) retries |

Per-agent results (mailbox, secrets via `op inject` — `env_ok`, with op's own
reason in `detail`) stay on `GET /api/agents/{slug}/readiness`. A readiness
**drill** (the Drills panel, or `POST /api/harness/runners/{id}/drill`) is still
the end-to-end proof that an agent can run a turn here and reach canopy-web.

**Refresh** (the button on the Health panel, or `POST /api/harness/runners/{id}/refresh`)
asks the box to re-run its bootstrap at its next idle moment: it restarts itself,
which pulls the canopy plugin + CLI and each agent's clone, re-injects secrets and
updates Claude Code (into `~/.local`, which shadows cloud-init's frozen copy in
`/usr`). The box also refreshes itself once its last bootstrap is 24h old, and
never restarts itself twice within 30 minutes. The request stays pending until
the box reports a newer bootstrap.

**Why the runner has its own clone.** `/opt/canopy-web` is reachable by agent
turns, and on 2026-09-22 one left it on a local branch: the next start's
`git pull --ff-only` failed and the three packages above were never loaded, while
the box read online + ready for six hours. `RUNNER_SRC_DIR` belongs to the runner
alone and is RESET to origin/main on every start (never merged into), and a clone
that cannot sync is still loaded as it was. `/opt/canopy-web` stays for the
auto-updater, which only ever `git show`s from it, and its `pre-commit` hook
refuses commits there (#1131).

**Where turns work on a repo.** Every checkout a turn works in is its own
worktree of one shared bare clone per repo (`$WORK_DIR/repos/.clones/<repo>.git`),
keyed by the conversation, or by the turn when there is none:

- a project turn (a repo, no agent) starts IN its worktree of that repo;
- any other turn runs `cd "$(canopy-repo-worktree <repo>)"`, the helper
  bootstrap installs in `~/.local/bin`, to get its own checkout of a repo that
  is not its agent's.

A clean worktree on a detached HEAD is brought forward to origin/main on its next
use; one with local changes, or on a branch, is left alone.

## Bootstrap (agent-fleet provisioning)

`cloud_runner.py`'s `main()` clones/pulls `canopy-web` to `/opt/canopy-web` and runs
`runner/ec2/bootstrap_agents.sh` from that clone once per service start —
**after** `fetch_and_stage_credential()` has staged this runner's credential bundle (the box holds
no 1Password key and no GitHub token — those come per agent from canopy-web; see the docstring on
`bootstrap_agent_fleet()` for exactly why the ordering has to be this way round, not
a cloud-init `ExecStartPre`). The script is idempotent — each step is `OK`-skipped when
already satisfied — and best-effort per agent: one agent's problems log loudly but do not
stop the others, and do not block the runner from starting up and claiming turns.

**The script prepares the BOX; canopy prepares each AGENT** (canopy
`docs/architecture/agent-setup-lifecycle.md`). Steps: (1) install/verify `uv`, the
`canopy` CLI (a `uv tool`), `gog` (latest Linux release), `op`/`gh`/`claude`/`git`;
(2) gog's keyring backend = `file` (headless Linux has no OS keychain) and the roster
from canopy-web (`AGENT_SLUGS` only offline); (4) the `canopy` Claude plugin, Claude Code
and the CLI brought current — BEFORE (3), because the CLI is what prepares the agents;
(3) per agent, `canopy agent prepare --runner --slug <s>` — the ONE implementation,
shared with laptops: credentials from canopy-web (`/credentials/resolve`, with this box's
token), clone/pull the repo canopy-web records into `/opt/agents/<slug>`, `op inject`
its `.env.tpl` with the agent's own key (`env_ok`), its plugin and required plugins
(node deps, key files), `bin/<slug>-setup`, the Gmail client and the NEWEST of the
vault's and canopy-web's token, a real gmail call (`mailbox_ok`, `turn_ready`), then the
readiness report (`GET /api/agents/<slug>/readiness`); (4b) prune caches; (5) summary.
Drilled on a standby (cloud-ec2-prep, 2026-10-15) — readiness matched the old bash agent
for agent before that bash was removed.

Bootstrap is the FIRST writer of `~/.<slug>/.env`, not the only one: when a turn is
claimed, the runner runs `canopy cred sync` for that agent (its own key, the vault
canopy-web records; at most once per `AGENT_ENV_TTL_SECONDS`, default 300) — the same
command an agent session runs at start — so a value changed in the agent's vault
reaches its next turn without a restart. A failed refresh keeps the last good file and
reports `env.<slug>` on the runner's health. An agent this box has never seen is
prepared on its first claim, alone (`prepare_agent`).

**Agent turns run in their own worktree of the agent's clone.** A turn whose target
resolves to an agent with a clone under `/opt/agents/<slug>` runs `claude -p` in a
worktree of it at `WORK_DIR/agents/<slug>/<turn>` (the clone is freshly `git
pull`ed at claim), not in a throwaway scratch dir — so it sees the agent's real
config and skills, and two concurrent turns for one agent no longer share a
working tree (#1141). The clone's git-ignored files (`.env`, `node_modules`, local
settings) are symlinked in. A clean worktree is removed when the turn finishes;
one with uncommitted work or an unpushed branch is kept and logged, and swept
after a day once clean. Where each turn's CLI session ran is recorded, so a chat
reply can resume it and the stream tail can find its transcript. State an agent
keeps across turns that is neither tracked nor ignored belongs in `~/.<slug>/`.
Project/session turns, and an agent bootstrap hasn't reached yet, keep their own
`WORK_DIR` directories.

**Updating bootstrap logic** is a `git push` to `canopy-web` main + a
`systemctl restart canopy-runner` on the box (or just wait for the next restart) —
unlike `cloud_runner.py` itself (baked into UserData; see "Updating the runner
code" below), it is NOT tied to the stack's cloud-init generation.

## Session-capable, transcript-posting, resumable (run-convergence PR2)

The runner CAN declare **`capabilities.sessions: true`**, opt-in via
**`RUNNER_SESSIONS=1`** (default **OFF**) — this is what makes it eligible to claim
chat/session-targeted `Turn`s at all (`claim_next_turn` gates those on
`Runner.session_capable()`, apps/harness/models.py). **Do not turn this on yet.**
This runner has no durable-record path for a chat session: on labs
(`CHAT_STUB_EXECUTOR=False`), every session is stamped `transcript_sourced` at
creation, which means the reduced `TurnEvent`s this runner posts to
`/turns/{id}/events` never become durable `Message` rows and the user's own line is
never durably written either — the only durable path is
`POST /runners/{id}/session-stream` with per-line transcript ordinals, which today
has exactly one caller, `runner/canopy_runner` (the laptop runner). Flipping
`sessions` on today means a chat this runner claims streams a perfect-looking live
reply and then loses the ENTIRE conversation the moment the user reloads the page.
Flip the default only once that path (`session-stream` + `/streams` + `/backfills`)
is built here too. It is **not yet a CloudFormation parameter**
(`runner.cfn.yaml` only templates
`RUNNER_PROJECTS`/`RUNNER_AGENTS`/`RUNNER_WORKSPACE`/`RUNNER_NAME` into
`runner.env`); enabling it today means adding `RUNNER_SESSIONS=1` to
`/opt/canopy-runner/runner.env` by hand and restarting the service — don't, until
the durable-record work above ships. Capabilities are re-synced on every restart
(`pair_or_load` `PATCH`es an already-paired runner's capabilities in place, via
`PATCH /api/harness/runners/{id}`), so a later flip of the default takes effect on
the next restart without re-pairing (which would otherwise orphan its
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
(`TRANSCRIPT_FLUSH_BYTES`) or **every 10s on a dedicated background thread**
(`TRANSCRIPT_FLUSH_SECONDS`), whichever comes first, well under the server's cap.
The background thread is what makes the time trigger actually periodic — the byte
trigger alone only runs when a NEW line arrives, so a long-quiet stdout (a
multi-minute tool call) would otherwise hold buffered lines in memory indefinitely.
A single line too large to ever fit the server's cap is replaced with a synthetic
`canopy_runner_line_dropped` marker (never silently dropped with no trace). Every
batch carries a unique `batch_id` scoped to `(turn, attempt, seq)` so a
resume-fallback retry (see below) can never collide with — and silently no-op-drop
— an earlier attempt's batch, and a transient POST failure (5xx/timeout) retries a
few times with that SAME id before giving up on just that slice; the server's
`truncated` response flag is honored (further posting for that turn stops once the
server has stopped storing it). A transcript failure is logged and swallowed:
**it never fails the turn**, and buffered-but-unflushed content is still flushed
in a `finally` even if the read loop itself raises (e.g. a dropped WS `emit`).

**Real `--resume` continuity, with a stable per-session working directory.**
A session turn's CLI session id is captured from the first stream-json line that
carries one (normally `system`/`init`, which fires before any other output) and
round-tripped through the EXISTING resolve-session/record-session RPCs
(`POST /api/harness/runners/{id}/resolve-session` / `record-session`) so a later
turn on the same canopy `Session` can pass it back as `--resume <id>` instead of
cold-starting. Concretely: `_session_resume_plan` asks resolve-session for a
reusable handle before running claude; `_record_session_resume` reports the fresh
(or reused) session id back after the turn. This reuses `RunnerBinding.session_key`
— documented as "engine-agnostic ... was emdash_task" — rather than a
`Session.cli_session_id`-style column (no such field exists anywhere in the
codebase despite the originating plan naming one — confirmed by grep; see
`_session_resume_plan`'s docstring). `cloud_runner.py` also sends a `session_id`
field on every record-session call (currently a documented no-op server-side), so
wiring a real column for this later needs zero runner-side changes. Reuse is
additionally gated on a stable `RUNNER_HOST` value (defaults to `RUNNER_NAME`) sent
on every heartbeat/pairing call — `RunnerBinding.reusable_by` requires the runner id
AND host to match the ones that recorded the binding, so a brand-new EC2 instance
correctly cannot "resume" a session it never actually ran.

Critically, **every turn on the same canopy Session runs in the SAME working
directory** (`WORK_DIR/sessions/<chat_session_id>`, keyed on the canopy Session id,
never the turn id) — Claude Code resolves a `--resume` target by a cwd-derived
project directory (`~/.claude/projects/<cwd with '/','.' -> '-'>/<session-id>.jsonl`),
so a session id captured under one cwd is invisible under a different one. Before
ever invoking `--resume`, `_resume_target_exists` checks that file actually exists
under the turn's cwd — the cheap local equivalent of
`runner/canopy_runner/execute.py`'s verify-before-reuse (it reads emdash's DB
before driving a task; this reads the filesystem before resuming a transcript) — and
drops straight to a fresh spawn if it doesn't, rather than ever invoking a doomed
`--resume`. As a second-layer safety net, if the file existed but the CLI still
yields NOTHING (exits non-zero having emitted no stream-json lines at all), `run_claude`
retries **once** as a fresh spawn (no `--resume`). A failure that happens AFTER real
output was produced under a resumed session is treated as a genuine task failure and
is never retried (retrying would silently duplicate work under a new session).

**What was and was not verified.** There is no live cloud runner instance running
today (the EC2 stack is down), so none of this has been exercised against a real
`claude` binary or a real canopy-web deployment. What IS covered, by unit tests
under `runner/ec2/tests/` (mocking `subprocess.Popen` and the runner's own
`_api` helper — see the test file for exactly what's stubbed): capability
env-gating (default OFF) and the pair-time `PATCH`-in-place sync; byte-bounded
transcript batching including the oversized-line marker and driving `run_claude`'s
own byte-threshold flush (not just the pure helper); the periodic background flush
actually firing during a simulated multi-second-quiet stdout; transcript POST retry
+ truncation handling; `--resume` argv construction; the resume-target
filesystem-existence gate (including the stable-per-session-cwd behavior itself —
two different turn ids on the same chat_session_id resolve to the same directory);
the resolve-session/record-session request shapes (including the
agentless-and-projectless-session degrade-to-fresh-spawn case, with a
non-vacuous-pass assertion that `_api` was actually called); `run_claude`'s
session-id capture, transcript forwarding, the resume-fails-then-falls-back
behavior (and that a genuine post-resume failure does NOT retry); and that a
mid-loop exception (e.g. a broken `emit`) still flushes buffered transcript/events
in the `finally` rather than dropping them. Not verified: the real
`claude -p --output-format stream-json` event shape (the `system`/`init` event
carrying `session_id`, and Claude Code's cwd-encoding convention for
`~/.claude/projects/`, are both documented CLI behavior, not something captured
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
primitive. `canopy agent vault --slug <slug>` does it: it mints the agent's own token
(`POST /api/agents/<slug>/canopy-token`, which creates the agent's canopy login if it has
none — never adopts an existing account) and writes it to the vault item `canopy-pat`,
field `credential`, without printing it. The agent's `.env.tpl` carries one line:

```bash
CANOPY_WEB_PAT=op://${AGENT_VAULT}/canopy-pat/credential
```

`op inject` materializes it into `~/.<slug>/.env` when the agent's next turn is claimed
(and at every bootstrap).

`canopy_web.resolve_pat()` already reads `CANOPY_WEB_PAT` ahead of the token file,
so nothing else has to change once the line is there.

> **Do not put a literal `op://vault/item/field` in a comment.** `op inject`
> resolves references anywhere in the file, comments included, and the resolved
> secret can leak to stdout. Use angle-bracket placeholders, as above.

## Device binding: the box's instance role (Canopy Vault, #1549)

An agent with `require_device_binding` has its credentials resolved only for a request
carrying a proof from an APPROVED device key of a runner it routes to. An EC2 box has no
Secure Enclave or TPM, so its key is its **instance role** (`kind=aws-iam`, the HashiCorp
Vault AWS-auth pattern):

- **At start** `cloud_runner.py` reads the role's credentials from IMDSv2, sends ONE
  `sts:GetCallerIdentity` to learn the role ARN, and registers it:
  `POST /api/runner-device-keys/` `{runner, kind: "aws-iam", role_arn, label, proof}`.
  The log says `device key: PENDING — approve … <approve_url>` until a person approves it in
  canopy-web (passkey step-up). Idempotent on every restart. Opt out: `CANOPY_DEVICE_KEY=0`.
- **Every resolve** carries `X-Canopy-Aws-Proof`: standard base64 of
  `{method: "POST", url: "https://sts.amazonaws.com/", headers, body:
  "Action=GetCallerIdentity&Version=2011-06-15"}` — a SigV4-signed request that is never
  sent. `X-Canopy-Proof-Htu` (the resolve URL, no query) and `X-Canopy-Proof-Iat` (unix
  seconds) are among its SIGNED headers. canopy-web replays it to its own pinned STS
  endpoint; the ARN AWS answers with must be the approved role.
- The same proof goes on the canopy CLI's resolves on the box (`canopy agent prepare
  --runner`, `cred sync`): enrollment writes canopy's cache (`~/.canopy/device-key/
  aws-iam.json` + `status.json`) and canopy's `device_key_aws.py` signs identically.
- The signer is stdlib (this file must stay stdlib-only); `tests/test_aws_device_proof.py`
  checks it byte-for-byte against botocore's `SigV4Auth`. Each stack has its own
  `RunnerRole`, so approving a role approves exactly that box.
- **Zero-knowledge vault (#1550).** A role cannot do ECDH, so the box also holds a software
  ECDH key (`~/.canopy/device-key/ecdh.pem`, 0600) that a ZK agent's vault key is wrapped
  to. `cloud_runner.py` stays stdlib-only: it asks the canopy CLI to make it
  (`canopy vault device --ecdh-jwk --kind aws-iam` → the PUBLIC JWK) and sends it as
  `ecdh_public_jwk` in the same enrollment (adding it to an approved key needs
  re-approval). It never decrypts: a ZK agent's values are opened by the canopy CLI on the
  box (`agent prepare --runner`, `cred sync`). A 409 `zk_wrap_missing` is a failing
  `zk.<slug>` health check — an admin must grant this runner's device.

## Vault standard

See "Where secrets live" above for WHICH KEY reads which vault — this section is
only about naming. Agent `.env.tpl` files reference
**`op://Agent-<Name>/<kebab-item>/<field>`** (e.g. `Agent-Ace/gog-token/credential`)
for per-agent items, and the tenant's shared vault (**`op://Canopy-Shared/...`** in
the `connect`/`dimagi` workspaces) for fleet-wide ones (`gog-oauth-client`,
`canopy-drive-folder`). The old `AI-Agents` vault stays readable
during the migration but nothing new should point at it. The vault NAME is whatever
canopy-web has registered for that agent or workspace — it is no longer derived from
the slug, so an agent whose vault is not registered is simply not provisioned.
**GitHub is not in any vault.** A box holds no GitHub credential: an agent's
GitHub identity is its OWNER's, lent to that one agent (`AgentDelegation`, set on
the agent's Settings → Credentials → GitHub). Each turn asks canopy-web for it
(`POST /api/harness/runners/{id}/turns/{id}/github-token`) and it lives only in
that turn's environment; bootstrap uses the same token for that agent's private
clones. `bootstrap_agents.sh` reads each agent's own
`Agent-<Name>/gog-token/credential` with that agent's key. See
`docs/superpowers/specs/2026-07-25-cloud-agent-bootstrap-design.md`.

## Gmail token re-staging

Each agent's gmail auth is a **refresh token** in the agent's `gog-token` slot, and
it can come from either of two places — both equally valid, because the fleet's two
Google OAuth clients are one app (`canopy` = Desktop, `canopy-web` = Web; a token
under either serves every turn, canopy `agent_email.FLEET_CLIENTS`):

- **Browser (no terminal, no 1Password):** the agent's Settings → Credentials
  (`/w/<ws>/agents/<slug>/settings#credentials`) → **Connect Google mailbox**, as the
  agent's owner or an admin, signing in as the agent's mailbox. Mints under
  `canopy-web`; one of the agent's online runners exchanges the code and stores the
  token encrypted in the agent's vault, so canopy-web never sees it
  (`_drain_google_mint`, `canopy vault google-mint`; `docs/architecture/vault.md`).
- **Terminal:** `gog login <mailbox> --client canopy --services gmail,drive,docs,sheets,forms,appscript`
  on a laptop, then `gog auth tokens export` and `op` it into that agent's
  `Agent-<Name>/gog-token` item, field `credential`.

`bootstrap_agents.sh` leaves a live mailbox alone; otherwise it imports the NEWER of
the two by the token's `created_at`. So after re-minting, press **Refresh** on the
runner (or wait for its next bootstrap) and read `GET /api/agents/<slug>/readiness`.
The imported file is never left on disk (`shred -u`, falling back to `rm -f`).

## Secrets (in Secrets Manager, under `canopy/cloud-runner/`)
- `canopy/cloud-runner/canopy-pat` — **no longer needed by a new box.** A new stack sets `CanopyTokenSecret=''`, so the box holds no PAT and pairs by invitation (below); `up.sh` neither checks nor reads it. A stack made before that keeps its `CanopyTokenSecret` on update, and its box (already paired, keyless) does not need the PAT either; the boot script tolerates a named secret that is gone. So the secret can be deleted once no stack reads it — a **human AWS step after the deploy** (`aws --profile labs secretsmanager delete-secret --secret-id canopy/cloud-runner/canopy-pat`), not done by any script.
- ~~`canopy/cloud-runner/claude-oauth-token`~~ — **gone.** A box's Claude credentials live only in its vault; nothing reads this secret, and it can be deleted (`aws --profile labs secretsmanager delete-secret --secret-id canopy/cloud-runner/claude-oauth-token`).
- `canopy/cloud-runner/gog-keyring-password` — read only by a box built before it ran several runners. A box of several runners derives each agent's keyring password from its runner's own secret; `up.sh` no longer requires this one. **Do not copy ace-web's live OAuth blob** — its refresh tokens rotate on every use, so a second consumer gets invalidated (verified: it 401s / can't refresh).
- ~~`canopy/cloud-runner/op-service-account-token`~~ — **gone.** `wire.sh` no longer reads it, the credential bundle no longer carries it, and `RunnerCredential` no longer stores it. A box-wide 1Password key lived in the runner's environment, which every turn inherits: one credential that reads EVERY agent's vault, handed to each of them. Agent secrets are read with the per-agent and per-tenant keys canopy-web issues (see "Where secrets live"). The Secrets Manager entry can be deleted; nothing reads it.

`up.sh` publishes the first-boot seed itself (`seed.py`), not staged by hand:
- `canopy/cloud-runner/runner-seed` — a JSON manifest: `{format, parts, sha256, sha, committed_at}`. The `sha`/`committed_at` are the seed's provenance, which the box stamps into `/opt/canopy-runner/build-info.json` so its first auto-update check can answer.
- `canopy/cloud-runner/runner-seed-part-<i>` — `cloud_runner.py` as gzip+base64, in ≤60 000-char slices. Written before the manifest; the box concatenates, decodes, and refuses bytes whose sha256 does not match the manifest (a box booting mid-publish fails its start and systemd retries).

Why parts: Secrets Manager caps a value at 65 536 bytes, and on 2026-10-04 the
single-value seed (`runner-code`, 91 KB encoded) made every `up.sh` fail on
`PutSecretValue`. **The legacy `canopy/cloud-runner/runner-code` + `runner-code-sha`
are no longer written** but are left exactly as they were: stacks created before
the change (the live `canopy-cloud-runner`, as of 2026-10-04) have the old
`canopy-fetch-env` baked into their UserData and read them. See "Migrating the live
stack" below. `--purge-secrets` removes both formats.

Stage them with `./secrets.sh {canopy|claude|gog} <file|->` (`op` is legacy — nothing reads it). `wire.sh` copies a live
runner's Claude credential bundle (or, with none, the Secrets Manager fallback) into the
freshly-paired runner's bundle (`/api/harness/runners/{id}/credential`) — that's what unblocks
`fetch_and_stage_credential()` and lets bootstrap actually run.

## Prove it end to end
Enqueue a turn the runner can claim (target must be in its caps — default
`RunnerProjects=canopy-web`):
```bash
curl -sS -X POST "https://canopy.dimagi.com/api/harness/turns/" \
  -H "Authorization: Bearer <canopy-pat>" -H 'Content-Type: application/json' \
  -d '{"project":"canopy-web","workspace":"dimagi","origin":"api","prompt":"Reply with a one-line hello.","idempotency_key":"ec2-smoke-1"}'
```
The runner claims it, runs claude, and events + result land at
`GET /api/harness/turns/<id>` and stream live over the SP1 realtime `turn.{id}` socket.
For an agent, `wire.sh --drill` is the equivalent proof — it fires a readiness drill
per assigned agent and prints pass/fail.

## Tear down (it's billed hourly)
```bash
./down.sh [--name X]                 # delete the stack + SSH key, retire the runner row
./down.sh [--name X] --keep-runner   # recycle: leave the row for wire.sh to swap from
./down.sh [--name X] --purge-secrets # also delete the shared secrets — refused while any
                                     # other canopy-cloud-runner* stack still reads them
```
Retiring the row deletes its assignment rows, source rules and workspace-order
entries server-side (`POST /api/harness/runners/{id}/retire`); `down.sh` then reads
every agent's list and rules back to confirm none still name it.

## Config (CloudFormation parameters)
Flags: `--name` (→ `RunnerName`), `--workspace` (→ `RunnerWorkspace`; required for a new
stack and for `--test`, an update passes back the stack's own value, so the template default no longer
decides where a box pairs), `--approve-minutes` (how long to wait for the invitation),
`--standby` (→ `RunnerProjects=''`, `RunnerSessions=0`),
`--test` (→ `RunnerSessions=1`, `RunnerAgents=''`, `RunnerProjects` from `--projects`, and an
invitation that pairs it directed),
`--volume-size` (→ `VolumeSize`, default **40** GiB — 20 filled up), `--instance-type`.
Anything else: `EXTRA_PARAMS='Key=Val Key=Val' ./up.sh`:
`InstanceType` (t3.medium), `CanopyBaseUrl`, `RunnerProjects`, `RunnerAgents`,
`AgentSlugs` (`ace,ada,echo,eva,hal` — which agents `bootstrap_agents.sh`
provisions; distinct from `RunnerAgents`, which is which agents this runner may
CLAIM turns for), `RunnerName`, `VolumeSize`, `Runners` (`--runners`; '' = one runner,
see "Several runners on one machine"). The template reads no PAT, Claude token or gog password secret:
a box holds none (`CanopyTokenSecret`, `ClaudeTokenSecret` and `GogKeyringSecret` are
gone). `SshCidr` is
set to your IP automatically. Parameters not passed fall back to their defaults on
EVERY update — that is why an update goes through a change set (next section).

### Updates are reviewed: up.sh will not bounce a running box by accident
On an existing stack `up.sh` creates a change set first. If it touches the
`Instance` — a UserData change is a stop/start, a `VolumeSize`/AMI change is a
**replacement** (a new instance and a new runner row) — it prints the change, deletes
the change set and exits, unless you pass `--allow-instance-change`. Any template
edit since the stack was created changes UserData, so this is the normal state of
the live stack, not an edge case.

## Disk
The 20 GB root of `cloud-ec2-1` hit 100% on 2026-10-04 (every plugin version ever
installed — 33 canopy, 24 ace, ~9 GB — plus npm/uv caches). Now:
- `VolumeSize` defaults to 40 GiB;
- bootstrap step 4b (`prune_caches.py`, every service start and daily self-refresh)
  keeps each plugin's installed version(s) plus the newest one other and deletes
  the rest (nothing at all if `installed_plugins.json` is unreadable), cleans npm's
  cache past 500 MB, and runs `uv cache prune`;
- the heartbeat health carries a **`disk`** check — warn ≥ 80%, fail ≥ 90% — so the
  runner page says so before turns start failing on writes.
- the runner **sweeps its own scratch** (`cloud_runner.maybe_sweep_disk`, after a
  heartbeat, only while no turn runs, on its own thread): every 6 h, and 30 min after
  the last sweep while the disk is at warn — when it then also runs `prune_caches.py`.
  It removes entries in `/tmp` (`SCRATCH_ROOT`) owned by the runner's user and untouched
  for 72 h (newest mtime anywhere inside), and chat-session worktrees under
  `WORK_DIR/sessions` idle for 7 days (worktree and transcript both; the next turn
  re-makes the worktree at the same path, so `--resume` still finds the conversation).
  It never removes hidden/system entries, the runner's own dirs, or anything holding
  work that exists nowhere else — uncommitted changes, or commits on no remote (a
  worktree judged by its HEAD) — which it keeps and names in the **`scratch`** check.
  cloud-ec2-2 reached 100% on 2026-10-19 on exactly this scratch (~3.4 GB of week-old
  agent clones in `/tmp`, 2.1 GB of session worktrees). `DISK_SWEEP=0` turns it off.
- a runner's sweep reaches only its own files, so the **update timer** (`update_runner.sh`,
  root on a box of several runners, every tick) handles what no runner owns:
  - it installs `/etc/tmpfiles.d/tmp.conf` (`D /tmp 1777 root root 3d`), so
    `systemd-tmpfiles-clean.timer` ages out anything in `/tmp` untouched for 3 days,
    whoever owns it;
  - on a box of several runners, it deletes the caches of the user the box ran as before
    (`LEGACY_USER`, default `ubuntu`), when that user is not a runner's and runs nothing.
    The caches are `.claude/plugins/cache`, `.cache/uv`, `.cache/ms-playwright` and
    `.npm/_cacache`; never work, keys or config;
  - at ≥ 80% it measures `/home/*` and `/tmp` into `/opt/canopy-runner/disk-usage.json`,
    and the `disk` check's detail then names the largest.

  cloud-ec2-2 warned at 89% on 2026-10-26 while each runner's sweep freed 0 MB: 35 GB
  sat in `/home/ubuntu` and 12 GB in `/tmp`, owned by users the runners cannot touch.

## Migrating the live stack (`canopy-cloud-runner`, created 2026-09-05)
The live box keeps working untouched: its baked-in `canopy-fetch-env` reads the
legacy `runner-code` seed only when `cloud_runner.py` is missing (it never is — the
auto-updater owns the file), and it picks up the new runner code, the `disk` check
and the cache pruning through the normal deploy → auto-update → restart path.
What it does NOT get without action: the 40 GB volume and the new seed format.
Either, when convenient:
- **Blue/green (recommended)** — `./up.sh --allow-instance-change && ./wire.sh --drill`.
  The volume change makes CloudFormation REPLACE the instance: it boots the new one
  (fresh cloud-init, new seed format, 40 GB), then terminates the old. `wire.sh`
  finds the new, not-yet-staged `cloud-ec2-1`, retires the old row and swaps every
  agent row, rule and workspace order onto it, then drills. Expect a few minutes in
  which cloud work queues.
- **Grow in place, no new runner row** — `aws ec2 modify-volume --volume-id <vol> --size 40`,
  then on the box `sudo growpart /dev/nvme0n1 1 && sudo resize2fs /dev/nvme0n1p1`.
  The stack's template stays the old one; the next `up.sh` will still show (and
  refuse) the instance change until you take the blue/green path.

## Sensitive-data mode (`RunnerDataMode=sensitive_data`): a box that may hold a host's sensitive data

A box in sensitive-data mode is the only kind canopy routes a sensitive conversation to
(spec `docs/superpowers/specs/2026-10-22-sensitive-data-agents-design.md`). In it:

- **One credential.** Claude runs only on the bundle's **API-key slot**, which must hold
  the ZDR API key: a key from an Anthropic organisation with zero data retention. Box
  logins and subscriptions are never used, and there is no fallback (`claude.*` health
  shows only `api-key`). Without the key the box reports `ready: false` and
  `claude.sensitive_data` fails.
- **A locked profile on every turn**: `claude -p --permission-mode dontAsk` with a
  settings document that denies Bash, file writes, the web, subagents and scheduling,
  allows only read tools and canopy-web's MCP, and disables bypass and auto mode. The read
  tools reach only the turn's own working directory, its own sessions' oversized tool
  output (the settings are rewritten as each session the turn opens starts) and its own
  caller envelope; every Claude transcript on the box, the kept-conversation records,
  profiles, chat keys and tokens, the runner's state, the per-turn env root, `/proc` and
  browser profiles are denied, and every other session's directory and every other turn's
  envelope are denied by pattern (every name but the turn's own, so one made later is
  covered too). Rules govern tool calls and `@` file mentions, not Claude Code's own
  files, so it still writes and resumes its own transcript. A caller's words reach the
  prompt with every `@` that could start a file mention turned into `＠`: Claude Code
  reads an `@/path` into the prompt with no tool call, past the allow list. ACP
  is not used. Telemetry, error reporting, non-essential model calls and the canopy
  plugin's fleet auto-update are off. Only confined turns run, each with its own
  permission layer (a turn whose layer cannot be built is not run).
- **canopy-web's MCP alone**: `--strict-mcp-config` with a per-turn config holding one
  server, canopy-web at this box's `CANOPY_BASE_URL`, with the turn's caller token as
  its bearer. It is named `plugin_canopy_canopy-web`, so tool names match the canopy
  plugin's (`mcp__plugin_canopy_canopy-web__*`).
- **Nothing kept but a conversation's own session**: when a turn ends, however it ends,
  the box deletes its caller envelope, profiles and MCP config and, for every session the
  turn opened (one per credential it tried), the transcript, its sidecar directory
  (`tool-results/`), todos, file history and session env, plus the turn's stderr logs and
  the box-wide `~/.claude/canopy/session-log.jsonl` and `~/.claude/history.jsonl`.
  The one exception is a turn in a **sensitive conversation** (the claim says
  `sensitive_conversation`): the box keeps that conversation's last session, its
  transcript and sidecar directory only, recorded in
  `~/.canopy/kept-conversations/<chat session>.json` (session id, capability, agent, cwd,
  last used). The conversation's next turn resumes it (`claude -p --resume`) when it runs
  on this box, in the same capability for the same agent, and the transcript is still
  there; otherwise it starts fresh and its envelope says so (`conversation.starts_fresh`).
  A kept session is deleted once the conversation has been idle
  `SENSITIVE_KEEP_IDLE_HOURS` (24), or when canopy-web says it is closed (the box asks
  `POST /runners/{id}/kept-conversations` every `SENSITIVE_KEEP_SWEEP_SECONDS`, 900, and
  at start); never while a turn runs in it, nor when a turn kept a newer session while
  canopy-web was being asked. A 401, 403 or 404 to that ask (a retired box) forgets every
  kept conversation, as does a retired runner's start and the box's own shutdown
  (`systemctl is-system-running` says `stopping`; a plain restart, an update, keeps them):
  the stop handler only sets flags, the turns in flight get `STOP_DRAIN_SECONDS` (60) to
  finish and keep nothing new, then every kept conversation goes.
  A confined chat turn's rows also ship while it runs (a live tail with a last tick at
  the end), and the turn-end ship sends, in chunks and with retries, what comes after the
  records canopy-web says it holds (the claim's `transcript_held`), so an earlier turn's
  failed ship is sent again. At start the box also sweeps whatever a
  crashed turn left (envelopes, profiles, every transcript but a kept conversation's).
- **No transcript leaves the box** except a confined turn's turn-end rows, which
  canopy-web seals: no live stream, no backfill, and the usage report carries token
  counts only. canopy-web refuses the rest too: a session it has no record of is
  not kept, and backfill and reset are refused (409).
- **The claim log** names who and where a turn came from, never its prompt.
- **Egress locked** (below): sensitive-data mode locks every runner. A runner whose lock is not
  holding reports `ready: false`.
- **It says so.** Every heartbeat carries `data_mode` and a config hash (which covers
  the egress setting); canopy counts a person's `sensitive_data` declaration only while
  the box reports `sensitive_data`, and believes the report only from a cloud box signed
  in with its device key (never a person's token, never a laptop).
- **Dedicated.** canopy-web lets a sensitive-data box hold only agents with a
  `data: sensitive` capability (`runner_requirements.is_sensitive_data_box`).
- **Fails closed.** Any other non-empty `RUNNER_DATA_MODE` (a typo, the retired `zdr`)
  locks the box down the same way, fails health (`claude.sensitive_data`, naming the
  value), reports `ready: false` and claims no turn.

Standing one up: deploy with `EXTRA_PARAMS="RunnerDataMode=sensitive_data"`,
seal the ZDR API key into the box's vault from its Credentials tab (+ API key), then a person
declares `sensitive_data` on the runner page (passkey).

## Egress lock: a box that reaches only what it must

A locked runner's processes (the runner itself, Claude, every tool) get
`HTTPS_PROXY=http://127.0.0.1:<its port>` (`20000 + uid % 10000`) and reach the network
only through `canopy-egress.service`, a CONNECT-only proxy (`cloud_runner.py
--egress-proxy`, its own system user, one loopback listener per runner) that admits port
443 to:

- `api.anthropic.com`, canopy-web's host (from `CANOPY_BASE_URL`), and GitHub
  (`github.com`, `api.github.com`, `codeload.github.com`, `objects.githubusercontent.com`,
  `release-assets.githubusercontent.com`) for the runner's code, the plugin and clones;
- that runner's extra hosts (`host.example.com`, or `.example.com` for every subdomain).

Root's iptables chain `CANOPY-EGRESS` rejects everything else a locked runner's uid sends —
DNS on loopback (a lookup is itself a way out) and the other runners' proxy ports
included — so a tool that ignores the proxy fails instead of getting out. Root and the
proxy's user are outside the chain; runners cannot become either. A refused host is
logged by name (`journalctl -u canopy-egress`), never content. The proxy narrows hosts,
not paths.

**Who locks a runner — it only ratchets up:**

- **canopy-web** (the usual way): the runner's page, **Egress lock**, or MCP
  `set_runner_egress` / `PUT /api/harness/runners/{id}/egress`, per runner: locked, plus
  extra hosts. Root reads it every minute by the box's instance role
  (`canopy-egress-sync.timer` → `--egress-sync` → `POST /api/harness/runners/box-egress`)
  — never relayed by a runner, which could otherwise loosen its own lock. If canopy-web
  cannot be read, root keeps the last answer it had.
- **Sensitive-data mode** (`RunnerDataMode=sensitive_data`, or any non-empty value):
  every runner is locked, whatever canopy-web says, and canopy-web refuses to unlock a
  sensitive-data box.
- **The box's floor**, `RunnerEgress=locked` / `RunnerEgressAllow` in box.env: every
  runner locked, with those hosts. Only a NEW box gets it from the stack: cloud-init
  writes box.env's script on an instance's first boot only, so a parameter change does
  not reach a live box — use canopy-web for that.

A lock turning on or off rewrites the runner's env and restarts it, so it waits
until the runner carries no turn; host changes apply at once. `--setup-runners` applies it
all at boot, and the auto-updater re-runs it after an update. The runner page's `egress`
check says whether a locked runner's lock holds: its proxy answers, and a direct
connection out is refused.

## Several runners on one machine (`Runners`)

The canopy-web half is `docs/architecture/runners.md` § "Several runners on one machine".

A box built from this template runs one **runner** per entry of `Runners`
(`./up.sh --runners tree:dimagi,tree:marshellis,agent:eva`; empty = one runner
`tree:<RunnerWorkspace>`), each serving one workspace tree or one agent. Each runner has:

| | where |
|---|---|
| a Unix user and home (0700) | `cu-<id>`, `/home/cu-<id>` (`cu-dimagi`, `cu-agent-eva`) |
| a runner process and `Runner` row | `canopy-runner@<id>.service`; named `<box>` for the box's own workspace, else `<box>-<id>` |
| its env (no PAT, no Claude token, no gog password) | `/etc/canopy-runner/<id>.env` (0600, the runner user's) |
| its gog secret (each agent's keyring password is derived from it) | `/etc/canopy-runner/<id>.gog-secret` (32 bytes, 0400) |
| its per-turn secrets (tmpfs, gone when the turn ends) | `/run/canopy/<uid>/<turn>/env` |
| its signing key (software, made by the canopy CLI) | `~/.canopy/device-key/` in its home |
| its zero-knowledge ECDH key | KMS, `<stack>-ecdh` — the box's original key for the box-named runner, `alias/canopy-runner-ecdh/<role>/<id>` for the others (four slots) |

Root's parts, all written by `cloud_runner.py --install-runners`: `canopy-runners.service`
(every boot: users, env files, secrets, and the iptables rule that rejects IMDS for
every uid but root's), `canopy-kms-broker.service` (the only thing that uses a runner's
KMS key, for that runner's uid only), `canopy-pair.service` (pairs every runner in ONE
`pair-by-role` call), and the auto-updater (root: one install, every
`canopy-runner@*` restarted, deferred while any runner has a turn in flight).
`/etc/canopy-runner/box.env` holds the stack's parameters and no secret.

**Pairing takes one passkey touch for the whole box.** `up.sh --runners …` makes one
invitation that lists every runner (the approve page shows them all); `canopy-pair`
pairs them all once you approve. A replaced instance (same role, same runner names)
re-attaches every runner with no approval, keeping each runner id and every vault wrap;
each re-key is recorded (`runner.rekeyed`) and mailed to the runner's approvers.
Adopting a box paired before it ran several runners always takes an approved invitation.
Each runner keeps its `-ecdh` key slot across re-runs; `./up.sh … --remove-runner <id>` is the only
way to drop one, and the key is retained in KMS either way. A NEW runner's zero-knowledge agents need their vault key granted to it once (the
existing "Grant pending" flow on the agent's page).

**Wiring.** The box's own runner: `./wire.sh --name <box>`. Each other runner:
`./wire.sh --name <box> --instance <id> [--standby --agents …]`. Each runner gets its
own vault (`--vault <id>` shares one, only inside the runner's isolation boundary);
sign it in on that runner's Credentials tab. Nothing is copied between runners.

**What separate runners do not protect against:** root on the box (trusted), and agents
inside ONE runner (they share its user; the read rails are defence in depth, the runner
is the boundary).

## Moving a machine to several runners (operator steps — NOT automatic)

A box built before it ran several runners keeps running as it is: one `canopy-runner.service` as
`ubuntu`, its runner id, bindings, wraps and its agents' `~/.<slug>/.env` files. New
runner code does NOT move its secrets off disk on its own: that is on by default only
for a runner set up by `--install-runners`. To opt an older box in (canopy CLI >= 0.2.736), add a
systemd drop-in — `systemctl edit canopy-runner` → `Environment=RUNNER_SECRET_FILES=0`
(runner.env is rewritten on every start, so not there) — and restart; the `secrets`
health check says which path it is on. Each agent's legacy files are removed only after
one of its off-disk turns has succeeded. A box paired by invitation never falls back to
a PAT.

Moving it to several runners is a deliberate step, **only with Jonathan's approval**, and it
keeps the runner: the runner of the box's own workspace is named as the box, so
canopy-web re-attaches the SAME runner (by machine and name) and — only under the
approved invitation `up.sh --in-place` makes, never on a role proof alone — replaces
its aws-iam key row in place with the runner's software key — the runner id, its bindings
and every zero-knowledge wrap survive, because its KMS ECDH key is the same key, now
served by the broker (tests: `tests/test_machine_pairing.py::test_the_live_single_runner_box_is_adopted_keeping_its_id_bindings_and_wraps`,
`runner/ec2/tests/test_runners_on_one_machine.py::test_the_live_box_adopts_as_its_default_runner`).

In place, no instance change (recommended for the live box):

```bash
# 0. the deploy carrying this code is live, and the box has auto-updated to it
#    (runner page: code current). Wait for an idle moment: in-flight turns are stopped.
# 1. the runners' keys and ONE invitation listing every runner (the runner stack is not touched):
./up.sh --name cloud-ec2-2 --runners tree:dimagi,tree:marshellis,agent:eva --in-place
#    → approve the printed link with your passkey
# 2. on the box (SSM, as root) — the command up.sh printed:
sudo RUNNERS=tree:dimagi,tree:marshellis,agent:eva python3 /opt/canopy-runner/cloud_runner.py --install-runners
#    stops canopy-runner.service, writes box.env from runner.env's NON-secret keys,
#    replaces the old canopy-fetch-env (it re-wrote the PAT/Claude token/gog password),
#    and starts canopy-runners, the broker, canopy-pair and one canopy-runner@<id> each.
# 3. watch: journalctl -u canopy-pair -u 'canopy-runner@*' -f
#    the box-named runner re-attaches (same runner id); the new runners pair. Set up
#    this way, every runner is off disk by default (no RUNNER_SECRET_FILES=0 needed); a drop-in
#    with RUNNER_SECRET_FILES=0 on the old canopy-runner.service no longer applies.
# 4. ./wire.sh --name cloud-ec2-2 --instance marshellis --standby --agents <…>   (each new runner)
# 5. grant each zero-knowledge agent's vault key to the runner that now runs it.
```

What does not carry over to the runner's new home: the `ubuntu` user's local Claude
transcripts (canopy-web keeps every recorded turn; an old CLI session is not resumed
locally), its full Claude login (tokens come from the runner page), and its agent
clones (the runner re-clones). Later, `./up.sh --name cloud-ec2-2 --runners … --allow-instance-change`
syncs the stack's `Runners` (a stop/start; cloud-init does not re-run).

A new box runs several runners from its first boot: `./up.sh --name <box> --workspace <ws> --runners …`.

## Updating the runner code — it updates itself

**Ship a `cloud_runner.py` change by merging it and deploying canopy-web.** Within
30 minutes the box installs it and restarts itself. Same rules as the laptop
runner, and the same reasons — see
`docs/superpowers/specs/2026-07-30-cloud-runner-auto-update-design.md`.

How it works: `canopy-runner-update.timer` runs `update_runner.sh` every 30
minutes as the service user. It asks the control plane for **its own row's
`expected_code_sha`** — the sha of `runner/ec2/{cloud_runner.py,bootstrap_agents.sh}`
in the DEPLOYED image, so already through CI, the merge queue and a deploy — and
compares it to `/opt/canopy-runner/build-info.json`, the stamp left by whoever
installed the running bytes. If they differ and no turn is in flight
(`/opt/canopy-runner/in-flight`, rewritten by the daemon every heartbeat), it
installs `git show <that sha>:runner/ec2/cloud_runner.py` from the `/opt/canopy-web`
clone, re-stamps, and restarts the service.

Four things worth knowing:

- **It never tracks `origin/main`.** Installing main would run code nothing has
  deployed and leave `code_sha != expected_code_sha` permanently — the staleness
  banner would fire forever on exactly the boxes updating correctly.
- **A stale busy-marker is not "busy".** A marker older than 120s means the daemon
  stopped writing it (stopped, wedged, crash-looping) — the case auto-update exists
  to rescue, so it never blocks.
- **It only ever GETs.** A heartbeat from the updater would stamp the runner ONLINE
  and forge liveness for a daemon that may be dead.
- **It is a separate systemd unit**, because a runner that crash-loops could never
  update itself.

Watch it: `journalctl -u canopy-runner-update -f`. Ask without changing anything:
`canopy-runner-update --check` → `current|stale|busy|unknown`.

### Secrets Manager is now a first-boot seed
`canopy-fetch-env` installs the seed only when there is no
`cloud_runner.py` on disk. Past that the updater owns the file — re-fetching the
secret on every start would silently revert every update at the next restart. So
**`./up.sh` no longer ships runner code to a running box.** Two deliberate
overrides, both on the box:

```bash
sudo -u ubuntu canopy-runner-update --ref my-branch  # run a branch, deliberately
sudo -u ubuntu canopy-runner-update --from-secret    # go back to the published bytes
```

`up.sh` still publishes the seed (it is what a FRESH instance boots from), stamped
in its manifest with the same path-scoped sha the deploy computes (`cloud_runner.py`,
`bootstrap_agents.sh`, `update_runner.sh` — a test pins the lists equal). It refuses
to publish when it cannot resolve that sha, or when those files have uncommitted
changes (`--allow-dirty` overrides): an unstamped box answers `unknown` forever,
which looks exactly like a box that is up to date.

### The cloud-init gap (still real for everything else)
**CloudFormation applies a UserData change to an existing instance as a stop/start
— it does NOT re-run cloud-init**, which only runs `write_files`/`runcmd` on an
instance's *first* boot. So the units, `canopy-fetch-env`, and the
`canopy-runner-update` shim are frozen on a running box. Changing any of them means
a new instance — the blue/green path above, or a recycle:

```bash
./down.sh --keep-runner        # keep the row (and its routes) for wire.sh to copy
./up.sh && ./wire.sh --drill   # fresh instance, fresh cloud-init; re-wire it
```
`bootstrap_agents.sh` and `update_runner.sh` itself don't have this gap — both are
read from the canopy-web clone (see "Bootstrap" above; the shim in UserData is a
dozen lines that just `git show`s the real updater out of `origin/main`).

### Retrofitting a LIVE box instead of recycling it
A recycle destroys the instance's EBS volume, and that volume is not empty: it holds
the five agent clones, the gog keyring with all five gmail tokens, and every
provisioned `~/.<slug>/.env`. `bootstrap_agents.sh` rebuilds all of it idempotently
from 1Password, so a recycle is recoverable — but it also mints a NEW runner id,
which is why `wire.sh` has to re-swap every agent's assignments afterwards.

If you would rather keep that state, the only things missing from a running box are
the cloud-init bits (the shim, the sudoers rule, the two units) and the stamp — the
updater itself comes from git. Over SSM:

```bash
# 1. stamp what the box is currently running (from a canopy-web checkout):
git log -1 --format='{"sha": "%H", "committed_at": %ct, "ref": "retrofit"}' \
  -- runner/ec2/cloud_runner.py runner/ec2/bootstrap_agents.sh
#    → write that JSON to /opt/canopy-runner/build-info.json (chown ubuntu:ubuntu)
# 2. copy the four write_files blocks out of runner.cfn.yaml onto the box:
#    /usr/local/bin/canopy-runner-update (0755), /etc/sudoers.d/canopy-runner-update
#    (0440), and the canopy-runner-update .service + .timer
# 3. systemctl daemon-reload && systemctl enable --now canopy-runner-update.timer
# 4. canopy-runner-update --check   # expect: current (or stale, if a deploy is ahead)
```

Get step 1 right or skip it entirely — a WRONG sha is worse than none. An absent
stamp reads `unknown` and does nothing; a stamp claiming a sha this box is not
running would either suppress a real update or trigger a needless one.

## Runner fleets: capacity that scales itself

A fleet is an Auto Scaling group of interchangeable runners that canopy-web grows and
shrinks with the work waiting (`docs/architecture/runners.md` § Runner fleets; spec
`docs/superpowers/specs/2026-10-24-runner-fleets-design.md`). Members keep nothing the
fleet needs:
- their identity is their instance, proven to AWS;
- their vault key is the fleet's KMS key;
- their Claude credentials are the fleet's credential set;
- a member that goes away has its chats moved to another.

```bash
aws sso login --profile labs
./fleet.sh up --name dimagi --workspace dimagi --instance-type m7i.xlarge --min 1 --max 4 --warm 1
#   → approve: <link>   one passkey touch, once for the whole fleet
# once its first member has joined: open that member's Credentials tab,
#   Make its key (one passkey touch), then + Token for each Claude account
# route work to it: add any one member to the workspace runner order / agents' runners —
#   every member, and every member it adds later, is routed the same (the fleet's routing)
# or read a list your other runners already share (Credentials tab → Make a credential set from this list):
#   ./fleet.sh up --name dimagi --workspace dimagi --credential-set "jj credentials"
./fleet.sh up --name dimagi --max 6     # later: update the stack and the bounds; asks nothing
./fleet.sh down --name dimagi           # retire, empty the group (chats move), delete both stacks
```

- **Order on a new fleet.** `up` creates the stack with an EMPTY group, registers the fleet and waits for your approval, and only then sizes the group: an instance reports ready (the launch hook the stack waits on) only after it has joined, which needs the approved fleet.
- **What `up` builds.** Stack `canopy-fleet-<name>` (`fleet.cfn.yaml`). `fleet_template.py` renders it with `runner.cfn.yaml`'s own cloud-init, so a member is built exactly as a single box is. The stack holds:
  - the role, which trusts EC2 only;
  - a launch template;
  - the group, with new members protected from scale-in and a `canopy-ready` launch lifecycle hook;
  - a warm pool (`Stopped`, `ReuseOnScaleIn`);
  - `ScalerPolicy`, which lets canopy-web's task role set this group's desired capacity and protection, nothing else.

  The KMS ECDH key is a separate stack, `canopy-fleet-<name>-ecdh` (`ecdh-kms.cfn.yaml`). `up` also publishes the first-boot seed, as `up.sh` does.
- **Warm pool or from scratch.** A warm-pool instance has already bootstrapped and stopped. It comes into service in a minute or two, and a member that is scaled in goes back there with its clones and caches. `--warm 0` builds every member from scratch, which takes as long as its first bootstrap (up to ~40 min). Either way, the lifecycle hook holds the instance until `cloud_runner.py --fleet-ready` (root, every minute) sees that every runner has bootstrapped this boot. So nothing is stopped half-installed or put in service early.
- **Turns per member** come from the instance type's RAM (one per 1.5 GiB) and are given to canopy-web, so the scaler knows what one more member absorbs.
- **Knobs** (canopy-web settings): `RUNNER_WAKE_AFTER_S` (60), `RUNNER_IDLE_STOP_S` (1800), `RUNNER_FAILOVER_AFTER_S` (180), `RUNNER_SCALER_TICK_S` (30; 0 = off).
- A member is reached through SSM (`aws ssm start-session --target <instance>`). Its security group has no ingress.

## Notes
- **Ephemeral by design.** A box's Claude logins are its credential bundle on
  canopy-web (the runner page), staged into the process at start and replacing the
  Secrets Manager fallback that cloud-init writes to `/opt/canopy-runner/runner.env`
  (chmod 600); `down.sh` removes the box. A login changed on the runner page is picked
  up when the box next stages its bundle (its next start, or when its cascade runs out).
- **Runner identity:** the runner pairs on first boot and caches its id in
  `~/.canopy-cloud-runner.json`; a restart reuses the same runner row. A stack
  recycle (`down.sh --keep-runner && up.sh`) is a fresh instance, so it pairs a NEW
  row — `wire.sh` is what finds it and retires the old one.
