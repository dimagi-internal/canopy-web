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
- `kms_broker.py` — the root-owned broker that derives with each isolation unit's own KMS ECDH key, for that unit's uid only (stdlib-only; see "Isolation units").
- `_lib.sh` — what the three lifecycle scripts must agree on: the runner-name → stack mapping and the seed's secret names.
- `seed.py` — builds the first-boot seed (manifest + ≤60 KB parts, sha256-checked on the box).
- `prune_caches.py` — bootstrap step 4b: drops plugin-cache versions nothing has installed, and npm/uv download caches.
- `update_runner.sh` — the auto-updater: installs the DEPLOYED runner sha when this box is behind and idle. Run on a 30-minute systemd timer via a thin `/usr/local/bin/canopy-runner-update` shim, and read from the canopy-web clone so it ships by deploy like everything else.
- `bootstrap_agents.sh` — idempotent bootstrap of the BOX (tooling, gog's keyring, the canopy plugin + CLI, Claude Code itself, caches); each agent is prepared by canopy's `canopy agent prepare --runner`. Runs ON the box, from the runner's own `canopy-web` clone (`RUNNER_SRC_DIR`) — not baked into the template. See "Bootstrap" below.
- `secrets.sh` — put/update this runner's secrets in Secrets Manager (values read from a file/stdin, never shell history).
- `up.sh` — preflight every one-time prerequisite, publish the seed, `cloudformation deploy` (updates via a reviewed change set); pulls the private key from SSM for SSH; invites a new box to pair (prints the approve link) and waits for it.
- `wire.sh` — operator-side: stage the fresh runner's credential bundle, wait for the box to prove one credential works, retire its predecessor, swap agent assignments onto it, optionally drill it. `--standby` instead adds it as a DISABLED row and touches nothing else; `--test` stages credentials, waits for the proof and stops. Run this after `up.sh`.
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
                                  # directed-only: RunnerDirected=1, RunnerSessions=1, RunnerAgents='',
                                  # RunnerProjects=--projects (default canopy-web), RunnerWorkspace=canopy
./wire.sh --name cloud-ec2-fork --test
                                  # discover it, stage its credentials, wait for one to answer
                                  # (--probe-minutes), stop: no retire, no assignment, rule or
                                  # workspace-order change, no --agents. Prints the chat URL.
./down.sh --name cloud-ec2-fork
```
A directed-only box (`capabilities.directed`) claims only turns pinned to it and
chats bound to it — never routed agent work, a repo turn, an unbound chat or an
inherited workspace order, even where a list names it. It stays session-capable,
so the chat "Run on" picker offers it ("directed only"): start a chat on it and the
chat binds to it. `--test` refuses the live name and needs `--workspace`;
`--workspace <slug>` (→ `RunnerWorkspace`) also works on any other `up.sh` run.

**Tear down** (it bills hourly): `./down.sh [--name X]`. It deletes the stack, then
retires the runner row — only a row that has STOPPED heartbeating, so a same-name
replacement that is still alive is never touched — then checks no agent's list or
rules still name it. Secrets stay (they are shared by every runner stack).

**Which Claude credentials a new box gets.** `wire.sh` copies the long-lived tokens and
API key (with their labels) from a runner, in this order: `--credential-from
<name|id>`; else the same-name predecessor (a rebuild keeps its own bundle); else the
live cloud box — not retired, online, ready, not paused, latest heartbeat; else
Secrets Manager's `claude-oauth-token`. It prints which source it used and why, and
what it copied, by name. That is the design: the fleet's tokens live on the runner
page and travel box to box; the Secrets Manager token is a rarely-used build fallback
for a fleet with no live runner, so it being old or at its usage cap is expected.

**Full logins never travel.** Their refresh token rotates, so a copy breaks both
boxes. `wire.sh` prints each signed-in full login on the source it did not copy
("… — full logins stay on the box they were made on") and the new box's credentials
link (`/supervisor?tab=runners&runner=<id>`), where "+ Add a login" signs one in.

**A credential has to work before anything routes to it.** After staging, the box
probes each credential with one short prompt (`claude.probe.<label>` health checks,
see `docs/architecture/runners.md`). `wire.sh` waits up to `--probe-minutes` (default
5; `0` skips) for one to be `ok`, prints every probe, and when none is, exits
3 with the "+ Add a login" link before it retires, swaps or adds anything. Only
probes taken after the staging moment count (the bundle's `updated_at`; each
check's detail ends `· probed <ISO>`), so a result left from an earlier staging
cannot pass. A running box (`--runner-id`) notices a re-staged bundle on its next
re-read (≤5 min) before it probes, so give it `--probe-minutes 10`.

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
| `canopy/cloud-runner/claude-oauth-token` (build fallback — must exist, may be old) | `up.sh` | set once: `claude setup-token` as the subscription account, `./secrets.sh claude <file>`; read only when no live runner has logins to copy |
| a live Claude login on some cloud runner (what new boxes copy) | `wire.sh` (prints its source) | `/supervisor` → Runners → the box → Claude login (a human `claude setup-token` per subscription) |
| `canopy/cloud-runner/gog-keyring-password` | `up.sh` | any strong password, `./secrets.sh gog <file>` |
| operator token `~/.claude/canopy/workbench-token` | `wire.sh`, `down.sh` | `/canopy:canopy-web-pat-mint` |
| per agent: its vault + service-account key | `wire.sh` (refuses in `--standby`) | `canopy agent vault --slug <slug> --workspace <ws>` (the setup checklist's Vault step) |
| per agent: GitHub delegation (owner's token) | `wire.sh` | agent Settings → Credentials → GitHub (the owner) |
| per workspace: shared vault key | `wire.sh` | `/w/<ws>/settings/secrets` (workspace owner) |

## Where secrets live, and which key reads them

Two levels, each with its OWN 1Password service-account key, both **stored in
canopy-web** (encrypted) and handed to a box per agent at bootstrap. There is no
third level and no fallback: a level that is not registered is *not configured*,
and the box says so instead of reaching for another credential.

| level | vault | key stored on | holds | registered with |
|---|---|---|---|---|
| **tenant** (workspace) | e.g. `Canopy-Shared` | `Workspace.shared_op_vault` + `shared_op_sa_token_enc` | what every agent in the tenant shares: the `canopy` / `canopy-web` gog OAuth **clients** | `PUT /api/workspaces/<ws>/shared-vault` |
| **agent** | e.g. `Agent-Hal` | `Agent.op_vault` + `op_sa_token_enc` | that agent's own secrets: its `.env.tpl` values (`CANOPY_WEB_PAT`, mailbox config…), its gmail token (`gog-token`) | `PUT /api/agents/<slug>/vault` |

The split is the blast radius: an agent's key reads that agent's vault **and
nothing else**, so a compromise is bounded to one agent. That is also why the
shared gog clients need the tenant key — using an agent key against the shared
vault is the 2026-09-07 outage (a good gmail token with no client id+secret
beside it).

**What the box holds:** no 1Password key of its own for agent work. Every `op`
call names the key it is made with — the agent's for an agent vault, the
tenant's for the shared vault — so "which identity read this secret" always has
an answer. Until 2026-09-22 an unregistered agent silently fell back to a
derived `Agent-<Slug>` name and the box-wide key staged by `wire.sh`, which made
four unconfigured agents look provisioned while running on secrets months old.

**Unregistered now looks like this**, in the runner's log and in
`GET /api/agents/<slug>/readiness` (`env_ok: false` plus the reason):

```
WARN: hal: NOT registered in canopy-web — no agent vault/key. Register it: PUT /api/agents/hal/vault
WARN: hal: no vault registered in canopy-web — keeping any existing ~/.hal/.env, which may be stale
```

**Register both in the UI** — that is what it is for:

| level | where |
|---|---|
| tenant | **`/w/<ws>/settings/secrets`** — the workspace's shared vault + its service account |
| agent | the agent's **Settings → Credentials** (`/w/<ws>/agents/<slug>/settings#credentials`) — its own vault + its own service account; also **Connect Google mailbox** and **GitHub** |
| the box itself | **`/supervisor` → Runners → the box** — its Claude login (no 1Password key and no GitHub token; it is handed the two above, and each turn its agent owner's GitHub token, per agent) |

The key is a 1Password service account scoped to that one vault, minted in
1Password, then handed to canopy-web — it is never typed on the box. Name it
`canopy-web-<slug>`. `PUT /api/agents/<slug>/vault` takes the agent's owner or an
admin (`_agent_for_admin`), by browser or PAT; the shared vault
(`PUT /api/workspaces/<ws>/shared-vault`) is workspace-owner only. Never pass the
key through an agent session's tool arguments or a file it can read — that lands
it in the transcript. The one path is canopy's `canopy agent vault --slug <slug>`,
which creates the vault, mints the agent's `canopy-pat` and its read-only key, pipes
each from `op` / the API straight to where it lives and never prints one (the raw
alternatives are deny-railed in the fleet gating baseline).

Then press **Refresh** on the runner page; the next bootstrap materializes it.

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
| `disk` | root volume ≥ 80% (warn) / ≥ 90% (fail) — Refresh (bootstrap prunes caches) or grow `VolumeSize` (see "Disk") |
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
  `canopy-web` and stores the token in canopy-web.
- **Terminal:** `gog login <mailbox> --client canopy --services gmail,drive,docs,sheets,forms,appscript`
  on a laptop, then `gog auth tokens export` and `op` it into that agent's
  `Agent-<Name>/gog-token` item, field `credential`.

`bootstrap_agents.sh` leaves a live mailbox alone; otherwise it imports the NEWER of
the two by the token's `created_at`. So after re-minting, press **Refresh** on the
runner (or wait for its next bootstrap) and read `GET /api/agents/<slug>/readiness`.
The imported file is never left on disk (`shred -u`, falling back to `rm -f`).

## Secrets (in Secrets Manager, under `canopy/cloud-runner/`)
- `canopy/cloud-runner/canopy-pat` — **no longer needed by a new box.** A new stack sets `CanopyTokenSecret=''`, so the box holds no PAT and pairs by invitation (below); `up.sh` neither checks nor reads it. A stack made before that keeps its `CanopyTokenSecret` on update, and its box (already paired, keyless) does not need the PAT either; the boot script tolerates a named secret that is gone. So the secret can be deleted once no stack reads it — a **human AWS step after the deploy** (`aws --profile labs secretsmanager delete-secret --secret-id canopy/cloud-runner/canopy-pat`), not done by any script.
- `canopy/cloud-runner/claude-oauth-token` — the Claude **build fallback**: a long-lived, non-rotating `claude setup-token` (`CLAUDE_CODE_OAUTH_TOKEN`). Must exist (`up.sh` checks), but is intentionally rarely used: a box runs on its credential bundle from the runner page, which `wire.sh` copies from a live runner, and this is read (by `wire.sh`, on your machine — never by the box) only when there is none — so an old or capped value here is expected.
- `canopy/cloud-runner/gog-keyring-password` — read only by a box built before isolation units. A box of units derives each agent's keyring password from its unit's own secret; `up.sh` no longer requires this one. **Do not copy ace-web's live OAuth blob** — its refresh tokens rotate on every use, so a second consumer gets invalidated (verified: it 401s / can't refresh).
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
`--test` (→ `RunnerDirected=1`, `RunnerSessions=1`, `RunnerAgents=''`, `RunnerProjects` from
`--projects`),
`--volume-size` (→ `VolumeSize`, default **40** GiB — 20 filled up), `--instance-type`.
Anything else: `EXTRA_PARAMS='Key=Val Key=Val' ./up.sh`:
`InstanceType` (t3.medium), `CanopyBaseUrl`, `RunnerProjects`, `RunnerAgents`,
`AgentSlugs` (`ace,ada,echo,eva,hal` — which agents `bootstrap_agents.sh`
provisions; distinct from `RunnerAgents`, which is which agents this runner may
CLAIM turns for), `RunnerName`, `VolumeSize`, `RunnerUnits` (`--units`; '' = one unit,
see "Isolation units"). The template reads no PAT, Claude token or gog password secret:
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
  keeps each plugin's installed version(s) plus the newest two others and deletes
  the rest (nothing at all if `installed_plugins.json` is unreadable), cleans npm's
  cache past 500 MB, and runs `uv cache prune`;
- the heartbeat health carries a **`disk`** check — warn ≥ 80%, fail ≥ 90% — so the
  runner page says so before turns start failing on writes.

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

## Isolation units: one box, many tenants (`RunnerUnits`)

Spec: `docs/superpowers/specs/2026-10-19-runner-isolation-units-design.md`; the
canopy-web half is `docs/architecture/runners.md` § "Units on the box".

A box built from this template runs one **isolation unit** per entry of `RunnerUnits`
(`./up.sh --units tree:dimagi,tree:marshellis,agent:eva`; empty = one unit
`tree:<RunnerWorkspace>`). Each unit is:

| | where |
|---|---|
| a Unix user and home (0700) | `cu-<unit>`, `/home/cu-<unit>` (`cu-dimagi`, `cu-agent-eva`) |
| a runner process and `Runner` row | `canopy-runner@<unit>.service`; named `<box>` for the box's own workspace, else `<box>-<unit>` |
| its env (no PAT, no Claude token, no gog password) | `/etc/canopy-runner/<unit>.env` (0600, the unit's) |
| its gog secret (each agent's keyring password is derived from it) | `/etc/canopy-runner/<unit>.gog-secret` (32 bytes, 0400) |
| its per-turn secrets (tmpfs, gone when the turn ends) | `/run/canopy/<uid>/<turn>/env` |
| its signing key (software, made by the canopy CLI) | `~/.canopy/device-key/` in its home |
| its zero-knowledge ECDH key | KMS, `<stack>-ecdh` — the box's original key for the box-named unit, `alias/canopy-runner-ecdh/<role>/<unit>` for the others (four slots) |

Root's parts, all written by `cloud_runner.py --install-units`: `canopy-units.service`
(every boot: users, env files, secrets, and the iptables rule that rejects IMDS for
every uid but root's), `canopy-kms-broker.service` (the only thing that uses a unit's
KMS key, for that unit's uid only), `canopy-pair.service` (pairs every unit in ONE
`pair-by-role` call), and the auto-updater (root: one install, every
`canopy-runner@*` restarted, deferred while any unit has a turn in flight).
`/etc/canopy-runner/box.env` holds the stack's parameters and no secret.

**Pairing takes one passkey touch for the whole box.** `up.sh --units …` makes one
invitation that lists every unit (the approve page shows them all); `canopy-pair`
pairs them all once you approve. A replaced instance (same role, same unit names)
re-attaches every unit with no approval, keeping each runner id and every vault wrap;
each re-key is recorded (`runner.rekeyed`) and mailed to the runner's approvers.
Adopting a box paired before units always takes an approved invitation. Each unit
keeps its `-ecdh` key slot across re-runs; `./up.sh … --remove-unit <id>` is the only
way to drop one, and the key is retained in KMS either way. A NEW unit's zero-knowledge agents need their vault key granted to it once (the
existing "Grant pending" flow on the agent's page).

**Wiring.** The box's own unit: `./wire.sh --name <box>`. Each other unit:
`./wire.sh --name <box> --unit <unit> [--standby --agents …]`. Claude tokens are
copied from the box's own unit by default (`--credential-from` to choose); a full
login cannot be copied (its refresh token rotates) — sign one in on that unit's
runner page if you want it. Nothing on a unit depends on a full login.

**What a unit does not protect against:** root on the box (trusted), and agents
inside ONE unit (they share its user; the read rails are defence in depth, the unit
is the boundary).

## Moving a box to units (operator steps — NOT automatic)

A box built before units keeps running as it is: one `canopy-runner.service` as
`ubuntu`, its runner id, bindings and wraps. New runner code changes it only in two
ways: its turns' secrets move to tmpfs once its canopy CLI is >= 0.2.736 (the
`secrets` health check says which path it is on; to keep the files, a systemd drop-in
— `systemctl edit canopy-runner` → `Environment=RUNNER_SECRET_FILES=1` — since
runner.env is rewritten on every start), and a box paired by invitation never falls
back to a PAT.

Moving it to units is a deliberate step, **only with Jonathan's approval**, and it
keeps the runner: the unit of the box's own workspace is named as the box, so
canopy-web re-attaches the SAME runner (by machine and name) and — only under the
approved invitation `up.sh --in-place` makes, never on a role proof alone — replaces
its aws-iam key row in place with the unit's software key — the runner id, its bindings
and every zero-knowledge wrap survive, because its KMS ECDH key is the same key, now
served by the broker (tests: `tests/test_runner_units_pairing.py::test_the_live_single_unit_box_is_adopted_keeping_its_id_bindings_and_wraps`,
`runner/ec2/tests/test_isolation_units.py::test_the_live_box_adopts_as_its_default_unit`).

In place, no instance change (recommended for the live box):

```bash
# 0. the deploy carrying this code is live, and the box has auto-updated to it
#    (runner page: code current). Wait for an idle moment: in-flight turns are stopped.
# 1. the unit keys and ONE invitation listing every unit (the runner stack is not touched):
./up.sh --name cloud-ec2-2 --units tree:dimagi,tree:marshellis,agent:eva --in-place
#    → approve the printed link with your passkey
# 2. on the box (SSM, as root) — the command up.sh printed:
sudo RUNNER_UNITS=tree:dimagi,tree:marshellis,agent:eva python3 /opt/canopy-runner/cloud_runner.py --install-units
#    stops canopy-runner.service, writes box.env from runner.env's NON-secret keys,
#    replaces the old canopy-fetch-env (it re-wrote the PAT/Claude token/gog password),
#    and starts canopy-units, the broker, canopy-pair and one canopy-runner@<unit> each.
# 3. watch: journalctl -u canopy-pair -u 'canopy-runner@*' -f
#    the box-named unit re-attaches (same runner id); the new units pair.
# 4. ./wire.sh --name cloud-ec2-2 --unit marshellis --standby --agents <…>   (each new unit)
# 5. grant each zero-knowledge agent's vault key to the unit that now runs it.
```

What does not carry over to the unit's new home: the `ubuntu` user's local Claude
transcripts (canopy-web keeps every recorded turn; an old CLI session is not resumed
locally), its full Claude login (tokens come from the runner page), and its agent
clones (the unit re-clones). Later, `./up.sh --name cloud-ec2-2 --units … --allow-instance-change`
syncs the stack's `RunnerUnits` (a stop/start; cloud-init does not re-run).

A new box takes units from its first boot: `./up.sh --name <box> --workspace <ws> --units …`.

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
