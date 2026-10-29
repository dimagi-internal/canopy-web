# Runners: routing, schedules, flags and the credentials a turn gets

How a queued `Turn` reaches a box: which workspaces a runner serves, which agents
it may hold, the ordered runner lists and rules that pick the box (and the turn's
mode), host-required runner flags, scheduled turns, the GitHub identity a turn is
handed, and the runner's own readiness. Who may *edit* each of these is in
[`access.md`](access.md) (→ "Runner roles", "Turn mode"); the routes are in
[`api-surface.md`](api-surface.md) (→ Harness, Agents). Code: `apps/harness/claim.py`
(the claim), `apps/harness/routing.py` (every routing predicate — the claim, turn
status, the unclaimable report and the topology all ask the same functions, so
they cannot disagree).

## Two ways an agent runs

1. **Use it yourself in Claude Code — no runner needed.** A person with the canopy
   plugin runs `canopy agent use <slug>` once on their computer: it installs the
   agent's plugin, enrols this computer's key so it can read the agent's secrets
   from canopy vault, and records their sessions as **local turns** on the agent's
   page. Who may: the agent's owner and admins, a `full:` rule, workspace editors —
   whom `access.decide` gives the whole agent (`apps/agents/credentials/licenses.py`). The people
   doing it are listed under the agent's Settings → How it runs and → People → Local
   use. A computer without the runner app gets its own key for reading the agent's
   vault (a `computer` runner made when it registers, which is no fleet runner).
   Detail: [`access.md`](access.md) → "Local use of an agent".
2. **Let it run on its own — needs a runner.** Either your own Mac running the
   runner app (set **Private**, it does only your work), or a fleet cloud box.
   Schedules, mail and dispatches start these turns; the rest of this doc is how
   one reaches a box. Schedules fire from canopy-web itself (see "Scheduled
   turns"), so no laptop has to be awake for them.

An agent used only the first way is complete without a runner; the setup checklist
says so (`apps/agents/setup.py`, the `runner` step).

### Private runners

**Private** is a setting on a runner app (`PUT /api/harness/runners/{id}/private`,
owner only): the box takes no fleet work — no routed turn, no workspace order, no
source rule (`Runner.directed_only`) — only what its owner pins or binds to it, plus
the owner's own Claude Code sessions on that computer (local turns). Those sessions
prove themselves with the box's device key, which may read an agent's secrets while
its owner holds an active licence. Internally the computer is a `PrivateRunner`
registration linked to the `Runner` (`PrivateRunner.runner`); the UI calls these
"your computers (used in Claude Code)", not a separate kind of runner. A computer
with no runner app is linked instead to a `computer` runner made for it, which only
holds its device keys: it never claims or heartbeats and is left out of every fleet
list, routing picker and health report. See [`access.md`](access.md) → "Local use
of an agent".

### Allowlisted agents, and directed-only by a person's word

**Allowlist agents** is a setting on a runner (a switch, then the agents to tick;
`GET`/`PUT /api/harness/runners/{id}/only-agents`, owner or runner admin;
`Runner.only_agents`): on, the box runs only the ticked agents' turns and chats — no
other agent's, no repo turn, no agentless chat — however they reach it, a pin included.
Off (an empty list) lifts it. It never adds work: an agent still reaches the box only
through its runner list or the order it follows. The choices are the agents in the
workspaces the box serves that the caller can see. It is the person-set counterpart of
a box declaring it runs one agent and is asked in the same places (`routing.serves_refusal`,
`turn_serves_refusal`, `runner_target_q`), so the claim, turn status, the unclaimable
report, the vault and what the box prepares all agree.

**A runner's name** is what people see, changed on its Settings tab (`PUT /api/harness/runners/{id}/name`,
owner or runner admin). The box keeps the name it paired as (`Runner.paired_as`); pair-by-role (one runner or several on a machine)
match on either, so a replaced instance still re-attaches to a renamed runner.

**Directed only** is a runner setting (`Runner.directed`; the runner page,
`PUT /api/harness/runners/{id}/directed`, owner or runner admin), never something the
box declares. A test box (`up.sh --test`) pairs with it on: its invitation carries
`directed`, so it is directed from the moment it pairs. A private runner is always
directed (`Runner.directed_only`). A sensitive-data box is the usual case for both: allowlist its sensitive-data agents and leave it
off directed-only, which would leave their routed turns with no taker.

## Which workspaces a runner serves (the tenant gate)

`routing.runner_tenant_slugs(runner)` is THE answer, and every runner-scoped
predicate calls it — claim and the schedule list (`_runner_schedule_qs`) both, so
"every schedule this runner may fire produces a turn it may claim" holds by
construction (`tests/test_claim_schedule_parity.py`). It is the INTERSECTION of:

- **the OWNER's workspaces where they hold `AGENT_WORK`** (editor and above) —
  `Runner.owner`, server-assigned at pairing from `request.user`. **Never
  `Runner.workspace`**: that FK records where the box *lives*; one box serves a
  fleet spanning workspaces, so scoping by it leaves other workspaces' turns
  QUEUED forever. A NULL owner fails closed (empty set).
- **the workspaces the box is ENROLLED in, plus every workspace below them**
  (`RunnerEnrollment`, `routing.covered_slugs`). Pairing enrolls only the
  workspace paired into; joining a workspace never enrolls existing boxes; leaving
  one drops the owner's boxes' enrollment there (`apps/workspaces/departure.py`); a
  division created later is covered; a separate root never is — which keeps a family
  workspace's box off Dimagi's work and Dimagi's boxes off the family's, though one
  person owns both. Managed by the owner or a runner admin at
  `/api/harness/runners/{id}/workspaces` (`tests/test_runner_enrollment.py`).

**A cloud runner serves one tree.** `enroll_runner` refuses (422) a `kind=cloud`
runner's enrollment whose root differs from its existing enrollments' root
(`runners._other_tree`; divisions of its root are fine). A cloud runner has its
own Unix user on its machine (below), so a machine that serves two trees runs two runners. A laptop
(`emdash`, `remote`) may span trees: it has one desktop user, so nothing on it
separates them, and `RunnerOut.roots` / `spans_trees` let `/supervisor` say so on
its row. `roots` lists only the trees the caller is a member of.

## Several runners on one machine

A cloud machine can run several runners, each serving a workspace tree, or a single
agent given a runner of its own. The machine declares what each serves in
`capabilities.serves` — `{"kind": "tree", "root": "dimagi"}` or
`{"kind": "agent", "slug": "eva"}` (`Runner.serves()`). Runners on one machine share
`Runner.machine_id`, the canonical IAM
role ARN the box proved: set by `runners.set_runner_machine` when an aws-iam device
key is approved and by pairing by role, never from a role the box merely names.

`routing.serves_refusal(runner, slug)` — part of `cannot_serve` — refuses:

- **"<runner> is the runner for agent S"**: an agent's runner asked about any agent but S —
  and, for a whole turn (`turn_serves_refusal`), any turn that does not act as S: a
  repo turn, an agentless chat. Its `runner_target_q` targets only S's turns and chats;
- **"S has its own runner on this machine (<runner>)"**: any other live runner with the
  same `machine_id` as the runner for agent S.

Both are restrictions a box declares on itself. Unlike the rest of `cannot_serve`,
neither a pin nor a session binding gets past it: `claim_next_turn` and the
coverage check (`_refined_allows`, so `turn_reach` agrees) ask it above both (a
pinned or bound turn would otherwise hand the box the agent's secrets through
`device_keys.runner_serves_agent`), `unclaimable_queued_turns` reports such a turn
with that reason, `runner_routes_agent` and `agents_routed_to`
ask it, `inherited_orders` drops such a runner for that agent, and the routing
writes (`apps/agents/api_routing.py::_runners_for_routing`) refuse it with 422 and the reason.
The topology lists a machine's runners together (`machine`, `serves` on each runner row).
`prime_machine_runners` loads the machine siblings once per claim and per report.
Tests: `tests/test_runners_on_one_machine.py`.

### Setting up the runners on a machine (`Runners`)

- **Runners.** The stack parameter `Runners` (`tree:dimagi,tree:marshellis,agent:eva`;
  empty = one runner for `tree:<RunnerWorkspace>`) lists them; `cloud_runner.parse_runners`
  names them. The runner for the box's own workspace tree keeps the **box's** name (so a
  box moved to several runners keeps its runner); every other runner is `<box>-<id>`. Each
  is a Unix user `cu-<id>` (home 0700), a `canopy-runner@<id>.service` (`User=cu-%i`,
  `NoNewPrivileges`, `PrivateTmp`), its own `/etc/canopy-runner/<id>.env`, state file,
  in-flight marker, clones, Claude logins and transcripts. The runner code and the
  updater's clone are root's and read-only to the runners.
- **Root's parts** (`cloud_runner.py --install-runners`, run by cloud-init or by an operator
  moving a live box): `canopy-runners.service` (every boot: `--setup-runners` makes users,
  env files, each runner's 32-byte gog secret, `/run/canopy/<uid>`, `runners.json`, and the
  iptables rule that rejects IMDS for every uid but root's), `canopy-kms-broker.service`
  (`runner/ec2/kms_broker.py`), `canopy-pair.service` (`--pair-runners`) and the updater
  (as root). `box.env` holds the stack's parameters and **no secret**.
- **Identity.** A runner cannot reach the instance role. Its signing key is a software key
  in its home made by the canopy CLI (`canopy vault device --public-jwk --kind software`);
  it signs in with `canopy vault runner-session` (DPoP), never a PAT. Its ECDH key is a
  KMS key of its own in the `<stack>-ecdh` stack (`alias/canopy-runner-ecdh/<role>/<id>`;
  the box-named runner keeps the box's original key), used only through the broker, which
  maps the caller's `SO_PEERCRED` uid to that runner's key and nothing else.
- **Pairing, once per machine.** `up.sh --runners …` makes ONE invitation listing every runner
  (`RunnerInvitation.runners`, each with its KMS public key); the approve page lists them.
  `canopy-pair` collects every runner's public keys and makes ONE `pair-by-role` call with
  the instance-role proof (`apps/harness/machine_pairing.py`): one transaction, one runner
  per entry (owner = the creator, its workspace, `machine_id` = the role,
  `capabilities.serves` from the invitation) with an ACTIVE software key. The creator must
  still have `agent.work` in **every** runner's workspace, or nothing is made.
- **Re-attach and adoption.** A runner already paired this way — its active key a runner
  software key, its runner on this `machine_id` — re-attaches with the same role and
  runner name and no approval: its new signing key replaces its runner's key row **in
  place** (`_rekey`), so the runner id, bindings and every zero-knowledge wrap on that row
  are kept, and tokens bought with the old key end. **Anything else needs the approved
  invitation that lists the runner** — above all *adopting* a box paired before it ran
  several runners, whose key is the instance role itself: a role proof alone (which any
  process on a box without the IMDS rule can make) never re-keys a runner. Adoption finds
  the runner by `machine_id` (or its role's aws-iam key) and name, turns its aws-iam key
  into the runner's software key, and its KMS ECDH key — the same public key, now from the
  broker — keeps its approval and wraps (`up.sh --in-place` makes that invitation). Every
  re-key emits `runner.rekeyed` and mails/pushes the runner's approvers. A different ECDH
  key waits for a person and drops the stale wraps; a runner with no active key (revoked)
  is refused. `capabilities.serves` is server-owned once set: a capabilities write can
  neither replace nor clear it.
- **Runner key slots are pinned** (`up.sh`): each extra runner keeps the `-ecdh` slot
  (`Runner1..4`) it was given — a re-run without `--runners` keeps every slot, a reordered
  list keeps each runner where it is, new runners take free slots, and a slot is dropped
  only with `--remove-runner <id>`. Runner keys (and the box's own) are `DeletionPolicy: Retain`.
- A runner's token reaches only itself and the agents it serves:
  `tests/test_runner_serves.py`.

### Secrets off disk

- **On by default only on a runner with its own Unix user** (`CANOPY_RUNNER_INSTANCE`, or a box with
  `/etc/canopy-runner/runners.json`). A box built before that keeps its
  `~/.<slug>/.env` files until an operator opts it in with `RUNNER_SECRET_FILES=0`
  (a systemd drop-in); `RUNNER_SECRET_FILES=1` keeps the files anywhere. Also gated on
  the installed canopy CLI (>= 0.2.736, `cloud_runner.OFF_DISK_MIN_CLI`): an older CLI
  cannot read `CANOPY_AGENT_ENV_FILE`, so the files stay. The `secrets` health check
  says which path a box is on and why.
- A turn's env is resolved in memory (`canopy cred env --runner --print-json`, cached per
  agent for 5 minutes), written 0600 to `/run/canopy/<uid>/<turn>/env` (a box without
  per-runner users that opted in: `/dev/shm/canopy-<uid>`), and handed to the turn as
  `CANOPY_AGENT_ENV_FILE`; it is removed in the turn's `finally` and swept at start. An
  agent with nothing to resolve (no `.env.tpl`, not canopy-web-sourced — canopy's "n/a")
  has an empty env. A turn whose env has a source but cannot be resolved (nothing
  cached), or cannot be written, is not run: it fails with `[runner] could not resolve
  <slug>'s credentials: <why>` and no session, which canopy-web re-queues; the box then
  stops claiming that agent (the claim's `paused` list, REST and WS) for 1, 2, 5, then
  10 minutes, reset by the next good resolve, so the re-queue is not spent at once.
- `canopy agent prepare --runner` gets `CANOPY_TURN_ENV_ROOT` and writes no
  `~/.<slug>/.env`, `<clone>/.env` or `.gws-sa-key.json`; the runner removes the ones an
  older runner left — for an agent only after one of its off-disk turns has succeeded
  (`~/.canopy-runner/off-disk-ok/<slug>`).
- **gog.** Each agent has its own `GOG_HOME=~/.gog/<slug>` and keyring password
  `HKDF-SHA256(runner secret, "gog:"+slug)` (`cloud_runner.gog_env`, the same derivation as
  canopy's `gog_home`). The inbox poller, full turns and a confined turn's mail capability
  get only that agent's three variables — never another agent's, never the secret's path.
  A box without a runner secret keeps its box-wide `GOG_*`.

`capabilities` (agents, projects) is a caller-supplied routing HINT declared by
the box; it is intersected with the tenant gate for **project and session** turns,
never a substitute for it — agent turns route by `RunnerAssignment` (and
`runner_may_hold_agent`).
Agent turns are tenant-gated through `routing.agent_tenant_q` (no NULL-workspace
leg — always AND it with `agent__isnull=False`), repo turns through
`Turn.workspace`, session turns through `chat_session.workspace`.

### The AWS broker (`canopy-aws-broker`)

A runner's turn cannot reach the instance role (IMDS is root's) and must not: it reads
the runner seed secrets. For the one AWS access turns need routinely, labs telemetry,
`runner/ec2/aws_broker.py` (root, `/run/canopy-aws/sock`, uid from `SO_PEERCRED`,
like the KMS broker) assumes ONLY the roles it names (`labs-observe` ->
`labs-jj-observe-readonly`, connect-labs `infra/labs-observe-readonly.yml`: metrics,
alarms and resource metadata; log events, object bodies, secrets, task commands/env and
DB content are denied) and hands the runner one hour of keys in `credential_process`
shape. Each session carries `SourceIdentity=canopy-runner-<name>` (from the uid, not the
request) and tags `runner`, `agent`. The instance role's `assume-labs-observe` policy
(runner.cfn.yaml, fleet.cfn.yaml) and the role's own trust both have to allow it.

`cloud_runner.py --setup-runners` copies `aws_broker.py` and the `kms_broker.py` it
imports (same sha) into `/opt/canopy-runner/aws-broker/` and runs `aws_broker.py
--install` on every start of root's setup, so the updater delivers it the next time it
restarts root's setup. The canopy CLI reads it with `canopy aws credentials --role
labs-observe` (the AWS CLI's `credential_process`), falling back to canopy-web's
`POST /api/agents/{slug}/aws-credentials` (`apps/agents/api_aws.py`, canopy-web's task
role assuming the same role) off the box.

## Opted-in projects (`ProjectOptIn`)

Canopy is opt-in per repo, and the choice is part of the person's account
(`apps/harness/models.py::ProjectOptIn`, `GET|PUT /api/me/canopy-projects/`,
Settings → Canopy projects), never config on a box. A person picks **every repo**
(`everything`) or **only the repos they name** (`projects`). With no row the
answer is nothing. Every repo outside the choice is plain Claude Code.

- **One gate.** `Runner.project_names()` is what the box holds
  (`observed_projects()`, its own report) ∩ what its OWNER opted into
  (`Runner.opts_in`). Every repo-turn route (`runner_target_q`, the workspace
  order, project chats, a pinned repo turn) and the usage scan's `scope` read it.
  A runner with no owner opts into nothing.
- **The box reports only what canopy wants.** The heartbeat reply's
  `report_projects` (`session_sync.report_projects`: null = every repo, else the
  opted-in repos plus the box's agent repos) is what the runner filters its
  session report by, BEFORE reading any tail or dialog
  (`canopy_runner/sessions.py`, held in `server_status`). An older server sends
  none, and the box reports everything as before.
- **Mirroring.** `session_sync.session_home` files no new session from a repo
  outside the opt-in in the ~10s report sweep. `/streams` and `/backfills` stop
  listing such a session's transcript. A session mirrored before its repo left the
  opt-in stays, but nothing more of it ships. `usage.record` keeps the usage of
  sessions canopy has a record of and nothing else, from any repo.
- **Agents are always canopy.** An agent's own repo is checked against
  `observed_projects()` (`cannot_serve`, the inherited order), never the opt-in.
- **`everything`** also covers a task emdash could not place in a repo (blank
  project). A chosen list covers only the repos it names.
- Migration `harness.0104` gave every existing runner owner `everything`. A
  person who pairs a box after it starts at nothing until they choose, and the
  `canopy_projects_off` alert (`apps/harness/alerts_project_opt_in.py`, setup,
  owner only) tells them so on Home and on the runner's page.
- **The canopy plugin needs no gate.** Its Claude Code hooks act only inside a
  canopy turn, an agent session or a local turn (an envelope, `$CANOPY_AGENT`, a
  `cx-` profile, a local-turn file). In any other session they write local files
  only and send nothing to canopy-web.

## Adding a repo to a laptop (`RunnerProjectRequest`)

A laptop runs an agent in the emdash project named after its slug, and routing
skips a laptop without one (`routing.cannot_serve`, `InheritedOrder.missing_repo`).
A person can ask the laptop to make that project itself, from canopy-web:
**"Get it there…"** wherever a laptop is shown lacking an agent's repo (the agent's
Routing / default order, the fleet map's workspace order, adding a laptop to an
agent's runners) → *Add it there now*; the runner page's **Routing → Repos (emdash
projects)** (an agent's repo, or any repo URL); and **Add repo** on `/ide`.

- **The ask** (`apps/harness/runner_projects.py`) is one row per (runner, project
  name), re-armed by asking again. Its owner or a runner administrator only, and a
  laptop (`kind=emdash`) only — a cloud box clones its agents itself
  (`cloud_runner._ensure_agent_clone`). An agent's ask uses its `repo_url` under
  its slug. Name and URL are validated on both ends (they become a folder name and
  a `git clone` argument on the box).
- **Delivery is durable**: pending asks ride the REST heartbeat reply as
  `add_projects` (`[{id, name, repo_url, requested_at}]`); a `runner.wake` ring only
  makes the box beat sooner.
- **The box** (`canopy_runner/add_project.py`): already in emdash's DB → added.
  Else `git clone -- <url> <repos root>/<name>` in the background with the macOS
  account's own git credentials (`GIT_TERMINAL_PROMPT=0`; the root is
  `~/emdash/repositories`, else `~/emdash-projects`); a folder already there is
  used only when its `origin` is the same repo. Then, in the loop thread and only
  with CDP healthy, `cdp/emdash_control.mjs add-project {name, path}` drives
  emdash's own **Add Project → Pick Directory** dialog: the path is handed to the
  Directory field's `onPathChange` through its React fiber (the field is a button
  that opens a native folder picker CDP cannot drive), the name input is set to
  `name`, and Create is clicked once emdash's path inspection enables it. A dialog
  already open (`BUSY`) defers the step a minute. The runner then checks emdash's
  DB for the project — a dialog that closed is not a project — and reports
  `POST …/projects/{id}/result {ok, detail}`. It never writes emdash's DB.
- **Outcome**: `added` when the project shows in the box's heartbeat report
  (observation discharges a pending ask whoever made the project; a reported
  success counts only once it does), else `failed` with the box's reason. A
  failure marks that ask only, never the runner not-ready.
- A repo that is not an agent's also needs the owner's opt-in (above) before repo
  work routes to it.

## Which agents a box may hold

`apps/agents/services.py::runner_may_hold_agent(runner, agent)` — true only when
the runner's OWNER is one of the agent's admins (`Agent.is_admin`: the owner, a
workspace owner, or an `AgentAdmin`). A box running an agent holds its prompt,
its caller's token, its secrets and its owner's GitHub identity, so this gates
claim (agent turns AND chats with the agent), credential resolve, the per-turn
GitHub token (re-asked at issue, since an owner can be demoted mid-turn), pins
(which also need the pinner to be an agent admin or that runner's admin —
`access.may_pin_runner`), and every routing write (runner list, source rules, actor
routes). Don't let a runner-assignment row be the trust boundary: the editor tier
writes those rows. A teammate whose laptop should run an agent's work needs an admin
grant on that agent. An agent's own canopy login is not a holder.

A **sensitive-data box is dedicated**: one with a declared `sensitive_data` flag OR
reporting a `data_mode` (`runner_requirements.is_sensitive_data_box` — either alone, unlike
routing's `in_force`, which needs both) holds only an agent with a `data: sensitive`
capability, whoever owns it. So no other agent's full profile, secrets or GitHub identity
shares a box with a sensitive conversation. Guarded by
`tests/test_sensitive_data_box_is_dedicated.py`.

## Which agents a box runs — one rule

`routing.runner_routes_agent(runner, agent)` answers "could this box ever claim
a turn of this agent?" with the claim's own static gates and nothing else:

1. the box is not retired, and not directed-only (below);
2. the agent's workspace is in the box's tenant (`runner_tenant_slugs`: enrolled
   there AND its owner may run work there);
3. its owner may hold the agent (`runner_may_hold_agent`, above);
4. the agent routes to it: an ENABLED `RunnerAssignment` row (its default list
   or any source/actor rule), or the workspace order it follows.

Availability, the cascade, a turn's requirements and the box's own report
(below) decide WHICH of those boxes takes a given turn. They never decide whether
a box runs the agent at all — so a box that failed to set an agent up keeps it on
its roster and retries at its next bootstrap.

**A box never claims a turn of an agent it reports it cannot run**
(`routing.cannot_serve`). What it serves on its machine (above), and two self-reports: a laptop driving emdash whose reported
`projects` lacks the agent's slug (never-reported = unknown, not empty), and any
box naming the agent in `capabilities["unprepared"]` — the cloud runner's
heartbeat `agents_unprepared`, the agents its bootstrap or lazy prepare tried and
has no clone of. It is a FLOOR on the composed list in
`routing.assignment_rows_for` (`serves=`), applied after strict truncation like
`requires`: the box neither claims nor blocks a lower rank, a strict rule whose
every box cannot run it waits, and `unclaimable_queued_turns` reports such a turn
as `config`, quoting what each box said. A pin still lands on a box that reports
it cannot run the agent (a drill exists to find this out), but never on one whose
`serves_refusal` refuses it.
`agents_routed_to(runner)` is the same rule as a list. Three readers share it,
and `tests/test_runner_serves_agents.py` holds it to `claim_next_turn` over every
box and agent pair:

| Reader | Uses |
|---|---|
| **The vault**: a resolve's device proof (`device_keys.runner_serves_agent`) | the rule, OR the box holds a live turn of the agent (a pinned dispatch, a bound chat), OR it is the agent owner's own LAPTOP working by hand, OR a private runner whose owner holds an active licence. An owner's cloud box reads only what it runs. A `computer` runner (no runner app) is asked only: does its owner administer the agent, or hold an active licence for it. |
| **The box's refresh**: `GET /runners/{id}/agents`, read by `bootstrap_agents.sh` | exactly the rule. A box prepares the agents it runs, not every agent its token can list. An answer is final, an empty one included (a new box prepares nothing); only no answer (not paired, an error, not a list) falls back to the registry. |
| **Readiness and the fleet view** (`agents_routed_to`) | exactly the rule. |

A resolve refused because an APPROVED box does not run the agent is routing, not
a misused token. The 401 says `reason: not_routed`, and it is recorded as an
`agent.credentials.refused` event; the agent's owner is not emailed. A missing or
unapproved proof is still a `device-proof` alert (`reason: no_valid_proof`).
Don't widen a reader past the rule (counting disabled rows, skipping enrollment
or the hold check, walking the whole registry): a box then asks every 30 minutes
for the vault of an agent it never runs, and once that agent is zero-knowledge
every refusal emails its owner.

The refresh itself (`agent prepare --runner --credentials-only`)
asks only for what that pass uses: the gog token (if declared) and the two vault
keys. Each turn's claim refreshes the agent's full env, naming its turn.

## Which CLI runs a turn (`apps/harness/clis.py`)

A turn runs in an **agent CLI**: `claude-code` or `codex`. A CLI is not a surface:
`Runner.engine` says where a laptop opens a session (emdash, the Claude desktop app,
headless); `claude-desktop` and `emdash` run Claude Code only.

- **The vocabulary lives in one place.** `clis.CLIS`, labels in `clis.CHOICES`; every schema
  types a CLI as `clis.CliName` (or `CliOrDefault`), Slack's flags are `--<cli>` plus
  `--claude`, and the frontend's `lib/clis.ts` types its labels against the generated union,
  so a new CLI fails the frontend type check until it has a label.
- **An agent lists the CLIs it is set up for**, `Agent.clis` (default `["claude-code"]`),
  its default first. It is a definition field: its admins set it (`PATCH
  /api/agents/{slug}/clis`, or the upsert, where an editor's change is refused like a repo
  change); Settings → Where it runs → Agent CLIs. `clis.list_refusal` refuses — 422 — adding a
  CLI no runner serving the workspace has (`clis.available`: any non-retired box whose tenant
  covers it and that reports the CLI, signed in or not), and removing one a schedule still
  runs on. `Agent.cli_models` sets the model each CLI starts on (blank = the runner's
  own default), in the same Settings table; the claim sends the turn's as `cli_model`.
  Every path canopy starts a session on uses it: cloud `claude -p` (`--model`), the
  cloud ACP executor and a laptop's headless runtime (`ANTHROPIC_MODEL`, which Claude
  Code reads before its settings), and a laptop's desktop-app runtime (the worktree's
  `.claude/settings.local.json` `model`, and `--model` on the seed, over the box's own
  `desktop_model`). A session emdash opens uses emdash's own model; Settings says so.
- **A conversation, turn or schedule may choose one of them**: `cli` on
  `POST /api/canopy-sessions/` (stored as `Session.cli` from birth), on
  `POST /api/harness/turns/` (stored as `Turn.requested_cli`), and on a schedule
  (`AgentSchedule.cli`, carried onto each turn it fires as `requested_cli`). `clis.refusal`
  refuses — 422 — a CLI the agent is not set up for, one no runner serving its workspace has,
  any CLI but Claude Code for an agentless or repo turn, and Codex for a sensitive site's
  conversation. The new-chat menu and the schedule editor offer the choice only for an agent
  set up for more than one. In Slack the ask's first word chooses it — `@canopy hal --codex
  <ask>`, `/hal --claude <ask>` (`slack.services.cli_flag`) — for a new thread; a thread
  already on another CLI is refused, saying to start a new one. Email, the embedded widget and
  a confined fork never choose. Without a choice the claim picks from the agent's list.
- **A box reports what it has** on every heartbeat, `clis`:
  `{"codex": {"signed_in": true, "profiles": 3, "envelope": 2, "version": "…"}}`, stored as
  `capabilities.clis` after `clis.sanitize` (malformed values dropped; names this server does
  not know are kept apart as `capabilities.clis_unknown`, shown and never routed to). It is a
  level: a beat without it removes it. A box that reports none is read as Claude Code alone —
  signed in unless `Runner.signed_out`, confining at the box-wide `profiles`/`envelope`
  (`clis.reported`). `RunnerOut.agent_clis` is that reading, per CLI (`label`, `signed_in`,
  `confines`, `version`, `known`): the runner page shows it and works out nothing itself.
- **One rule picks**, `clis.choose(turn, runner)`: the CLIs the turn allows (`clis.allowed`:
  the conversation's `Session.cli` once it is held to one, else the turn's `requested_cli`,
  else its acting agent's list, else the default — and never a CLI the agent is no longer set
  up for), keep those the box has signed in, for a caller's turn (`Turn.capability`) those it
  confines (`clis.confines`: the box's `profiles`/`envelope` for that CLI), for a sensitive
  conversation only `SENSITIVE_CLIS` (Claude Code); the first left wins, `None` = this box does
  not take it. The claim, coverage (`_refined_allows`, which passes its `ignore_requirements`
  counterfactual through) and `turn_reach` all ask it, above the pin: a pin never puts a turn
  on a CLI the box lacks or a caller's turn on one that cannot confine it.
- **What was asked and what was picked are two fields.** The claim writes `Turn.cli` (and the
  `status` event's `cli`); the claimed turn carries `cli` and `cli_model`. Every requeue (a
  sessionless failure, a failed mid-turn delivery, a lost fleet member) clears `Turn.cli` and
  keeps `requested_cli`, so a box that failed to start a turn on one CLI never holds it there.
- **A conversation is held to a CLI once its history is in one**: `clis.pin_conversation`,
  first write wins, when a runner reports a session for it (`session_sync.record_session`,
  `stamp_turn_session`, `link_turn_session`) or its turn finishes `done` — never at the claim.
  A session a box reports with no turn is held to the box's only CLI (emdash: Claude Code).
  Once held: a fork runs on the same CLI; a transfer or placement onto a box without it is
  refused (`placement._placeable_runner`); and if its agent is no longer set up for it, a send
  is refused at every door (`clis.conversation_refusal`, 422; Slack says so in the thread).
- **Sign-in is per CLI.** A box claims nothing only when no CLI is signed in. Coverage asks
  `choose` without the sign-in (a signed-out box still covers, like an offline one);
  `turn_reach` adds it, so a Codex conversation on a box whose Codex login is gone reads
  `signed_out` with `reason` "no runner has Codex signed in" (`Reach.cli`: the CLI a box would
  run it on were it signed in). A turn only boxes without a usable CLI would take is reported
  unclaimable with `clis.blocker`'s reason, most specific first: the agent no longer runs the
  conversation's CLI; a sensitive conversation on an agent set up for no CLI that may hold it;
  a box that has the CLI but cannot confine a caller's turn on it; or none has it, with how to
  sign it in. That pass runs only when the CLI could be the cause (`claim._cli_may_block`).
- **Credentials name their CLI**, `RunnerLogin.cli`; each CLI keeps its own cascade, and the
  last-fallback refusal is per CLI. Codex has no setup-token (an OpenAI API key, vault secret
  `openai-api-key:<key>`, or a login). A box's credential bundle carries another CLI's
  entries only once the box reports that CLI — older code reads every entry as Claude's.
- **A local session** (`POST /api/harness/local-turns/`) is a person's own Claude Code
  session: its turn records `cli="claude-code"`.
- **A prompt's plugin command is parsed once** (`apps/harness/invocations.py`): a first
  line `/<plugin>:<skill> <args>` is the canonical text canopy-web stores, shows and
  builds (`invocations.command` — mail turns, dispatches; an interface's `entry` must
  parse), and the claimed turn carries it parsed as `invocation` (`plugin`, `skill`,
  `args`, `rest`: the lines after). A turn executor renders that in its CLI's terms —
  Claude Code types `prompt` as it is; a CLI that loads skills but not commands invokes
  the skill `plugin:skill` — rather than parsing Claude's syntax itself.
- **A conversation can change CLI only by a transfer** that names one
  (`docs/architecture/chat-sessions.md` § A transfer carries the work): a fresh start in
  the new CLI from the handoff, never a resume.

Spec: `docs/superpowers/specs/2026-10-26-multi-cli-agents-design.md`.

## The claim (`claim.claim_next_turn`)

In order, each a hard stop:

1. `runner.live_status != ONLINE` → nothing. This covers **pause** (`live_status`
   returns PAUSED); a pause outranks a pin. Then the fleet-wide `FleetHold`.
   Every pause — the UI, MCP, Ada, the box's own `~/.canopy/PAUSED` sentinel —
   goes through `services.pause`, which records `paused_by`, and, when the pause
   has no `unpause_at`, puts the queued turns only that box could take to the
   admission rules (`access.md` → reach rules). `turn_reach` reports a turn whose
   every taker is paused as `parked` (with `held_until` when one resumes on its
   own), not `offline`.
2. No sweeps: expired leases, stale occurrences, late slots and stale drills are
   closed on the off-request clock (below), not by the claim.
3. Candidate filter: pinned here, or (`runner_target_q` ∧ routing kind); a cloud
   runner never takes `local_only`. A **directed-only** box
   (`Runner.directed_only`) targets only the sessions
   bound to it, so it claims just its pins and its bound chats — no routed agent
   turn (even with an enabled assignment row), no repo turn, no unbound chat, no
   inherited order. Every routed list drops it (`assignment_rows_for`, the
   workspace order, `inherited_orders` → `InheritedOrder.directed`), so it neither
   claims nor counts as a better rank; it routes and prepares no agent. A person's
   setting (above), not the box's. `profile_q`: a confined (caller) turn only to a
   runner reporting `profiles >= PROFILES_VERSION` and `envelope >= ENVELOPE_VERSION`
   — not bypassable by a pin. Agents/sessions with an executing turn are excluded
   (`busy_agent_ids` / `busy_session_ids`; the `isnull=False` filters in them are
   load-bearing — a NULL in the IN-list excludes every row), except a follow-up that
   may ride into the running turn (`routing.may_ride`). Then the tenant gate.
4. Per candidate: runner flags satisfy the turn's requirements (above the pin);
   `runner_may_hold_agent` (above the pin); then, unless pinned here, the
   availability cascade for the agent / repo order / session agent — over a list
   with every box that reports it cannot run the agent removed (`cannot_serve`) —
   and, for an unbound project chat, `routing.project_session_allows` (below).
5. Turn mode decided and stamped (`turn_mode.for_turn`), an admission `auto` rule
   may raise a standing manual to auto, then admission rejects
   (`apps/admission`, see `access.md` → "Admission rules"), then the conditional
   update to CLAIMED inside its own `transaction.atomic()` (an `IntegrityError` from
   `one_executing_turn_per_agent` means another box won). The claim event records
   why (`_routing_basis`: pin / actor / source / default).

`claim.turn_reach` and `unclaimable_queued_turns` explain a stuck turn from the
same predicates; `blocking_turn` is the "queued behind" twin of the busy
exclusion (`tests/test_turn_status.py`).

**The box's own gate, before it asks.** A cloud runner skips `POST /claim` (and
the WS `claim`) while any of its own limits says no — none of which the server
can see: at its turn cap (`MAX_CONCURRENT_TURNS`, default one per
`TURN_MEMORY_BUDGET_KB` = 1.5 GiB of RAM), below the memory floor
(`MemAvailable < CLAIM_MIN_AVAILABLE_KB` = 1 GiB), its token refused (401), or
an unknown `RUNNER_DATA_MODE`. Both claim loops ask one function,
`cloud_runner.claim_gate`, and every heartbeat reports it as the **`claim`
health check**: `ok` "claiming · 1/4 turns in flight · 2.3 GiB free"; `warn`
"not claiming for 14m: at turn cap 4/4 (in flight: <ids>)" or "… 0.6 GiB free <
1.0 GiB floor". So a turn that `turn_reach` calls LIVE but nobody claims is read
off the runner page (or `get_runner`'s `health_checks.claim`), not journald.
The laptop runner has no such limit of its own: its skips (paused, emdash CDP
down, Claude signed out) already reach canopy-web as the pause, a degraded
heartbeat and `signed_out`.

## Periodic work: the off-request clock (`apps/common/ticks.py`)

A runner's heartbeat is liveness only (its report, lease renewal for the turns it
holds, the stops it owes, the update nudge), and a claim does no global sweep.
Everything periodic is a job registered with `ticks.register(name, fn, every_s)`
from its own app's `AppConfig.ready`, and run by ONE asyncio loop in every web
process's ASGI lifespan (`config/asgi.py` → `ticks.running()`, waking every
`TICK_BASE_S`, 5 s). Each due job takes a fleet-wide cache key for its interval,
so N web tasks run it about once; with the cache down a process runs it on its
own clock. A failing job is logged and the rest still run. So the jobs run with
no runner online and through a fleet hold. `manage.py run_ticks [--list]` runs
one pass by hand.

| Job | App | Every |
|---|---|---|
| `harness.queue_sweeps`: expired leases (and `revoke_stale` turn tokens), stale occurrence turns, late slots, stale drills — **skipped while `FleetHold` is on**, so a held slot is not closed as MISSED | harness (`sweeps.py`) | `HARNESS_QUEUE_SWEEP_TICK_S` (15 s) |
| `harness.wake_due_runners`: scheduled unpauses (the box reads it on its next beat) | harness | 15 s |
| `harness.schedule_clock` | harness | `CANOPY_SCHEDULE_TICK_S` (60 s) |
| `capabilities.sweep` | capabilities | `CAPABILITY_SWEEP_INTERVAL_S` (30 s) |
| `slack.channel_ingest` (+ check-in expiry) | slack | `SLACK_INGEST_TICK_S` (15 s) |
| `agents.pat_rotation` (+ stale-proposal expiry), `agents.zk_notify` | agents | `CANOPY_PAT_ROTATION_TICK_S` (900 s), 60 s |
| `push.due_session_pushes` | push | 15 s |
| `canopy_sessions.purge_expired_secrets` | canopy_sessions | 60 s |
| `retention.sweep` (hourly inside, off unless enforced) | retention | 60 s |
| `contacts.hcp_oauth_deliver` | contacts | 60 s |
| `admission.sweep_unrunnable`, `admission.expire_runner_notices` | admission | 60 s |

Tests set `TICK_BASE_S = 0` and call a job's function directly.

## An agent's runner list (`RunnerAssignment`)

`RunnerAssignment` is the routing authority for agent turns. The box's
`capabilities.agents` and `Agent.runner_preference` do not route agent turns.

- **Default order:** rows with `source=""`, `rank` 0 = first choice
  (`GET|PUT /api/agents/{slug}/runners`, wholesale replace).
- **Availability cascade:** the highest-ranked available (`is_available`: online +
  ready) runner claims; a lower rank claims when every better rank is unavailable,
  or once the turn has been queued `CASCADE_GRACE_SECONDS` (60) — so a
  heartbeating-but-wedged box cannot stall the queue.
- **`enabled`** is a toggle, not removal: a disabled row keeps its rank, stays
  drillable, but is filtered out in `load_assignment_rows` — it neither claims nor
  blocks a lower rank. An agent whose own default rows are all disabled has still
  chosen a list and does NOT fall back to the workspace order.
- **Rules** layer on top (`routing.assignment_rows_for`, pure): an **actor rule**
  `(agent, source, actor)` → a **source rule** `(agent, source, actor="")` → the
  default order. Each rule is a rank-ordered LIST of runners. `source` is the
  `Turn.origin` vocabulary; `actor` is a normalized address matched against
  `actors.actor_of(turn)` (the enqueuing member, else the initiating contact).
  A non-strict rule puts its runners first and the default order beneath; a
  **`strict`** rule truncates — "these runners or nothing", the turn waits, and the
  grace has nobody below to promote. A rule whose rows are all disabled
  disappears (its strictness with it). Ranks are renumbered over the composed list.
  `routing.labelled_assignment_rows` is the one implementation and also says which
  rung put each runner there; `GET /api/agents/{slug}/route-preview` reads it to
  answer "where would this go now" (what Auto means in the New chat picker).
  Rule writes: `replace_agent_runner_rules`, `set_agent_runner_rule`,
  `set_agent_actor_route` (`tests/test_source_rules.py`,
  `tests/test_claim_source_routing.py`, `tests/test_agent_runner_rules_api.py`).
- **Above the cascade:** `Turn.pinned_runner` hard-pins one turn (drills, chat
  "wait"/"continue", directed starts, `runner_id` on `POST /api/harness/turns/`) —
  bypasses assignments and kind, never the tenant gate, flags, may-hold,
  `profile_q`, pause or the one-executing-turn constraints. **Session stickiness:**
  a chat bound to a `RunnerBinding` claims only on that runner; if it is gone the
  turn waits for the person to place it (chat banner) — no automatic failover,
  because continuing elsewhere starts a fresh emdash session without the
  conversation's context. The one exception is a **fleet member**: members are
  interchangeable by design, so the fleet scaler moves a chat off a gone member
  (§ Runner fleets).
- **Edit rights:** `agent.work` on the agent plus administering every runner the
  write ADDS (`_runners_for_routing`); the added runner must also be able to hold
  the agent. `auto` on any rule is for agent admins (`access.md` → "Turn mode").
- The UI is one table, `frontend/src/components/agents/AgentRouting.tsx`, in
  evaluation order: rules on top, the default order last as "Everything else",
  whose mode is the agent's turn mode.

## The workspace default order (`WorkspaceRunnerOrder`)

A workspace's ordered runner list (`GET|PUT /api/workspaces/{slug}/runner-order`;
set needs `runners.route`, admin and above; every runner must serve the workspace
per `runner_tenant_slugs` or it is a 422). It does two things:

- **An agent with no default order of its own FOLLOWS it** — the nearest
  workspace up the tree with an enabled order — live, never copied, and after the
  agent's own source/actor rules. `routing.inherited_orders` computes it and
  `load_assignment_rows` folds it into `defaults`, so every routing reader sees it
  without knowing (`tests/test_agent_inherits_workspace_order.py`). For each agent
  it drops a laptop (`Runner.EMDASH`) that does not report the agent's repo among
  its emdash projects (an agent's project is its slug; cloud runners are never
  dropped for this) and a box that may not hold the agent; both are reported
  (`InheritedOrder.missing_repo` / `cannot_hold`) so a screen can say why. Replacing
  a cloud box is therefore one edit to the workspace order; `runner/ec2/wire.sh`
  swaps it there. `GET /api/agents/{slug}/default-order` shows what an agent follows.
- **Repo turns** (a project dispatch: no agent, no session) in that workspace
  route by it (`repo_order_for`, `_repo_order_allows`): same cascade and grace,
  ranked only among listed runners that declare the repo and meet the turn's
  requirements. A workspace with no enabled rows: any serving runner that declares
  the repo (`tests/test_workspace_runner_order.py`).

### Project chats

A project chat is a chat session with no agent, only a repo (`Session.project`).
Its turns, while the session is **unbound**, follow the rules a repo turn
follows (`routing.project_session_allows`, `tests/test_project_chat_routing.py`):

1. the box declares the repo (`Runner.project_names()`; a chat with no repo has
   nothing to match here);
2. if the session's workspace has an enabled order, the order decides — the same
   cascade and grace, among listed boxes that pass rule 1 (`order_for`);
3. with no order, only a box the turn's sender (`enqueued_by`) administers
   (`harness.runners.can_administer_runner`). A turn with no person behind it gets
   no box this way, so it needs an order.

A bound session still claims only on its holder, and a pin still bypasses 1–3.
The claim asks `project_session_allows`; `_refined_allows` (so `turn_reach`, the
unclaimable report and the topology) asks `project_session_covers`, the same
predicates without the clock. A turn no box will take reads
`project_session_waits_why`: "no runner order in <ws>, and <person> administers no
box with <repo>". Choosing a box for a project chat — `runner_id` at creation, a
placement on send, `/place`, a move — needs you to administer it
(`canopy_sessions.placement.may_place`, the one placement rule).

The topology screens and `GET /api/workspaces/{slug}/runner-topology`
(`apps/harness/topology.py`) are computed from these same predicates. A
directed-only box is drawn as `directed` (never serving), not as a route.

## Turn mode per rule

`RunnerAssignment.turn_mode` (`""` = defer, `manual`, `auto`; rule-level, written to
every row of the rule) rides the routing ladder: actor rule → source rule →
`Agent.turn_mode`, under the dispatch request and the editor tier
(`apps/harness/turn_mode.py`; full ladder in `access.md` → "Turn mode"). Decided at
CLAIM, stamped on `Turn.turn_mode` / `turn_mode_basis` so a rule edited mid-turn
does not flip work under way, and delivered in the caller envelope (`turn_mode`),
which `canopy agent mode --caller` prefers over the agent-wide switch. **An `auto`
on a rule naming a person applies only to a VERIFIED message**; otherwise the turn
runs manual — a forged `From:` may pick a box, never drive an unreviewed agent
(`tests/test_turn_mode_rules.py`). Spec:
`docs/superpowers/specs/2026-09-23-per-rule-turn-mode-design.md`.

## Runner flags and host requirements (sensitive-data runners)

A **sensitive-data runner** (flag `sensitive_data`) is a box set up for a host's
sensitive data. It is not "zero data retention" itself: it runs Claude only on a ZDR API
key, runs every turn under a locked harness, limits egress to an allowlist of hosts
(`runner/ec2/README.md` § Egress lock), keeps nothing after a turn, and holds only agents
that handle sensitive data. The box runs in that mode with `RUNNER_DATA_MODE=sensitive_data`
and reports it on every heartbeat; any other non-empty value takes no turn. The report
is believed only from a **cloud** runner's beat signed in with its own device key
(`RunnerAccessToken`, `runner_protocol.data_mode_trusted`); a laptop's, or any beat sent
with a person's token, is recorded as no data mode. So the declaration (a person) and the
report (the box's stack) never come from the same hand. `tests/test_data_mode_trust.py`.

Nothing of a sensitive-data box's transcripts reaches canopy-web except a confined
turn's sealed turn-end rows. Its usage report keeps no title, a session canopy has no
record of is neither kept nor listed as missing (`events.usage`), and sending one up,
asking a backfill (`transcript_read.backfill_refused`), resetting one, filing a
reported session (`session_sync.session_home`) or posting `session-backfill` is refused,
with 409 on the routes. `tests/test_sensitive_box_uploads.py`.

On the box (`runner/ec2/README.md` § Sensitive-data mode): every turn's read tools are
scoped to its own working directory, its caller envelope and its OWN sessions' tool output
(the settings are rewritten as each session starts, so a fork never reads its source's),
and every transcript, the kept-conversation records and the other per-turn state under
`~/.canopy` are denied (`sensitive_data_settings`; a confined session elsewhere gets the
same denies from `native_permissions`, and a read tool with no `read_paths` reaches only
its worktree). Every other session's directory and every other turn's caller envelope are
denied by PATTERN — every name but the turn's own (`native_permissions.complement_globs`),
never a snapshot — as are `/proc` and browser profiles.

**A caller's words never reach a confined prompt with a live `@`.** Claude Code expands
`@/path` into the file's contents before the model runs, with no tool call: only deny
rules stop it, not the allow list or `profile_guard`. Every confined turn, cloud and
laptop, turns each `@` that could start a mention into `＠` (Claude Code's own escape;
`caller.neutralise_mentions`, cloud `_neutralise_mentions`); an email address keeps its
`@`, and server-side attribution folds `＠` back (`transcript_ingest._squash`).

A sensitive conversation's kept session is forgotten when it idles, closes, the box's
ask is refused (401/403/404, a retired box) or the box shuts down — after its turns drain,
and no turn finishing then keeps one; the stop handler only sets flags (`_finish_stop`).
The sweep never deletes a record a finishing turn rewrote meanwhile. A resumed turn ships
the records after what canopy-web says it holds (the claim's `transcript_held`,
`session_sync.turn_end_held`), so a failed earlier ship is sent again — chunked, with
retries, and its live tail ticks once more as it stops.
`runner/ec2/tests/test_sensitive_data_box.py`.

- A **host** requires flags of its visitors' conversations with
  `CANOPY_HOST["RUNNER_REQUIREMENTS"] = ["sensitive_data"]` (SDK `dimagi-canopy`); the SDK signs
  them into the visitor assertion (`canopy_runner_requirements`), and canopy stamps
  them on the session's `metadata["runner_requirements"]` — server-owned,
  union-only, never lifted by a later token that omits them. A host grant carries
  its own requirements too (`runner_requirements.requirements_of_grant`).
- A **runner admin** declares a box's flags (`RunnerFlag`,
  `PUT /api/harness/runners/{id}/flags`, audited with `declared_by`). canopy cannot
  verify a flag — it records who vouched. Nothing a runner reports can set one.
- `apps/harness/runner_requirements.py` is the one reader. The claim, placement,
  the unclaimable report, turn status (`requires`) and the `site_call` gateway
  (`apps/host_grants/host_gateway.py`) all refuse a runner missing a flag — above pins,
  and with no fallback: a requirement is a floor applied to the composed list after
  strict truncation, so a turn nothing can serve says so and waits. Guarded end to
  end by `tests/test_sdk_round_trip.py` and `tests/test_runner_requirements*.py`.
  Spec: `docs/superpowers/specs/2026-09-30-zdr-runners-design.md`.

## A turn on a site's page withholds the agent's own login there

A turn whose conversation is held on a Connected site's page that its agent serves
carries `page_site: {site, withheld}` in its claim (`interface.page_turn`; the
names come from `own_login:` in the agent's interface). There the agent reaches the
site only through canopy's gateway, as the person who sent (`access.md` § Acting as
the person on a connected site), so:

- **Cloud runner** (`runner/ec2/cloud_runner.py`, `page_site`): the withheld names
  are dropped from the turn's environment (`_agent_env`) and its per-turn env file
  (`write_turn_env`, `CANOPY_AGENT_ENV_FILE`), and `CANOPY_PAGE_SITE=<site>` is set —
  confined turns too. The box still holds the values for the agent's other turns
  (the in-memory resolve is per agent), so the boundary is the turn's own process.
- **Laptop runner** (`runner/canopy_runner/canopy_runner/page_site.py`): emdash, not
  the runner, starts the session, so it writes `~/.canopy/page/task/<task>.json` for
  the agent's MCP headers helper (found from `EMDASH_TASK_NAME`). The agent's own
  `.env` stays on that disk: on a laptop the helper and the caller envelope's `site`
  block are the boundary, not the environment.
- An agent whose secrets come from 1Password still gets its own
  `OP_SERVICE_ACCOUNT_TOKEN` in a full turn, which can read the withheld value from
  its vault; keep the site login in canopy-web's vault for the withholding to bind.

## Scheduled turns

The scheduler is a producer of turns, not an execution engine (no celery/beat).
`AgentSchedule` (`apps/harness/models.py`) holds the cron server-side, and
**canopy-web fires it itself**: `apps/harness/schedule_clock.py`, a job on the
off-request clock (`apps/common/ticks.py`, in the web process's ASGI lifespan), evaluates every
enabled schedule each `CANOPY_SCHEDULE_TICK_S` (60 s; a cache key keeps N web tasks
to about one tick) and calls `schedule_turns.fire_schedule` for each due slot. So a
workspace whose agents run only on cloud boxes, or whose laptop is shut, still fires
its schedules. A laptop runner still evaluates them too
(`runner/canopy_runner/canopy_runner/schedules.py`, `POST …/schedules/{id}/fire`);
both reach the same slot key, so whichever fires second collapses onto the first's
turn. Firing only queues a turn — which box runs it is decided on the claim.

A schedule is an agent **trigger** of kind `cron` (access.md § Triggers); the server
clock, the runner's sync and fire, and the week calendar read `kind=cron` only. Its
turn is accountable to whoever set it up: a slot whose creator has since lost the
right to it (`triggers.refusal`) is not fired — it is consumed (`last_slot`), logged
(`harness.triggers` / `trigger.refused`), answered 409 to a runner, and the
`trigger_stopped` alert stays open until it is fixed.

- **Anchor on `fire_after`, never `last_slot`.** `schedule_turns.fire_after_of`
  (`last_slot or created_at`, pinned just before a one-off's instant; served to
  runners as `ScheduleOut.fire_after`) is what the server clock and the runner pass
  to `due_slot(after=...)`; `last_slot` is NULL
  until the first fire, and an unbounded backward look fires a slot that predates
  the schedule.
- **Concurrent fires are safe:** the slot-derived `idempotency_key`
  (`sched:<id>:<slot>`) collapses duplicates inside `enqueue_turn`. Firing a new
  slot supersedes the schedule's still-open occurrences as MISSED — you only owe the
  newest.
- **No occurrence table:** the `Turn` is the occurrence;
  `latest_occurrence_turn` selects by `origin_ref__schedule_id`, scheduled or
  "Run now", any status.
- **One-offs** (`run_once_at`) fire only their instant and disable themselves in the
  same transaction (`OneOffSlotMismatch` otherwise).
- **Grace release:** an EXECUTING occurrence held past `grace_minutes` (anchored on
  `claimed_at` — held time, not owed time) is finished MISSED, because an abandoned
  session holds `one_executing_turn_per_agent` forever (its heartbeat keeps renewing
  the lease). Runs on the off-request queue sweeps (every 15 s), not the fire tick
  (a weekly fire tick could never honour a short grace).
- **Late slots:** a queued slot turn not started within `LATE_SLOT_WINDOW_MINUTES`
  (30) of its slot is skipped as MISSED with no nag; `always_run` and one-offs are
  exempt, manual runs untouched.
- **Nag:** a grace-released occurrence raises a `review` ask on a task
  (`_raise_schedule_nag`, only when the schedule's `notify` includes `inbox`);
  approving re-runs the prompt, and a later DONE occurrence closes open nags
  (`resolve_schedule_nags`). It reaches **Waiting on you** as an ordinary ask.
- An occurrence runs as `system` with the schedule's creator as the accountable
  person (`turn_mode._accountable_cap`, `access.md` → "The rule").

Specs: `docs/superpowers/specs/2026-07-15-agent-scheduled-turns-design.md`,
`2026-07-21-supervisor-inbox-items-only-design.md`.

## The GitHub identity a turn gets

**An agent acts on GitHub with a delegated credential — a token its OWNER or one of its
ADMINS gave it from their own account — never a box credential.** Boxes hold no GitHub
token of their own. The token is a vault entry of the agent's (`github-token`,
`account: "delegated"`), sealed only to the machines that run the agent
(`apps/agents/credentials/accounts.py`, `delegations.py`; spec
`2026-10-26-delegated-credentials-in-the-vault-design.md`).

- The owner or an admin pastes a fine-grained PAT on the agent's Settings → Credentials →
  GitHub (canopy pre-fills GitHub's creation form, `delegations.create_url`; GitHub has
  no API to mint one). Its repository selection IS the agent's scope. It is refused
  unless it can push and open a pull request on the agent's repo (`check_repo`: probes
  that GitHub answers 422 for a branch/commit that cannot exist, 403/404 for no
  permission — nothing is created), and (409 `no_machine_can_open`) while no machine
  that runs the agent has an approved device key. Then canopy-web seals it to those
  machines (and to the person's own vault key) and keeps nothing it can read. Giving one
  replaces the one in force.
- In force (`accounts.serves`): while its person is one of the agent's admins (the owner
  counts; `is_admin` fails closed on someone who left the workspace). Never on a date:
  GitHub's own expiry is shown, and GitHub refuses it.
- Per turn, `POST /api/harness/runners/{rid}/turns/{tid}/github-token`: only for a turn
  that runner is executing, re-checking `runner_may_hold_agent`; the principal is
  resolved server-side (`delegations.turn_agent`); **409 with the reason and no shared
  fallback** when absent or no longer in force; always refused for a confined (caller)
  turn. It returns the git identity (noreply email), `requested_by` for a
  `Requested-by:` trailer, and `delegated: "github-token"` with the `agent` whose vault
  holds it — no token. The cloud runner opens it from its OWN resolve of that agent
  (`delegated` field, opened by the canopy CLI with the box's key;
  `cloud_runner._delegated_value`). A token still held the old way (`AgentDelegation`,
  until `manage.py move_delegations_to_vault --apply`) comes in `token`, as before.
- **Checked by the boxes.** canopy-web cannot open it, so a box that does probes it at
  most every six hours (`cloud_runner.probe_github_token`) and reports
  `POST /api/harness/runners/{rid}/delegated-checks`; a failed probe turns `github.<slug>`
  red with "replace it". `github-readiness` reads what the boxes found.
- **A new machine** gets it from one that holds it: the keeper (`canopy vault zk keep`)
  sees it in `GET …/vault-keeper` (`delegated`), checks the newcomer's endorsement, and
  re-seals it (`PUT …/vault-keeper/{slug}/delegated/{name}`).
- **Cloning a new agent.** The box-wide prepare (`bootstrap_agents.sh`, `canopy agent
  prepare --runner`) therefore has no GitHub token: an agent whose private repo is not
  on the box yet is cloned at its FIRST turn, with that turn's token
  (`cloud_runner._ensure_agent_clone` → `_clone_agent_repo`, using the `repo` the
  github-token answer names), and prepare then runs with that turn's GitHub env (the
  canopy CLI's `git` falls back to `GH_TOKEN`), so its private plugin repos install. The
  first turn of each agent after every box-wide bootstrap prepares it again the same
  way (`_PREPARED_WITH_GITHUB`), so private plugins stay as fresh as the box-wide pass
  used to keep them. A clone the box-wide pass could not make is not reported
  unprepared — that would stop canopy-web sending the turn that clones it; a failed
  in-turn clone is.
- **Agentless repo turns and project chats** (`Turn.project`, or the chat
  session's `project`, = repo name) run as no one on GitHub and are
  refused the same way, unless the repo has an `agents.RepoIdentity` row
  (`repo_slug` unique → agent; edited in Django admin, listed as
  `identity_for_repos` on `GET /api/agents/{slug}/github`), in which case the turn
  runs as that agent under the same rule.
- **canopy-web's MCP sign-in is per turn.** Claude Code remembers a 401 from an
  MCP server in `mcp-needs-auth-cache.json` and skips it for ~15 minutes in every
  session sharing that file — on a box, every login's copy is a symlink to the
  one in `~/.claude`. The cloud runner removes the canopy-web entry before each
  turn (`_clear_canopy_needs_auth`), so one session with no credential cannot
  take canopy's tools away from the turns after it.
- Readiness: a box calls `GET /api/harness/runners/{rid}/github-readiness` at boot
  (checked live per agent it serves), and the readiness drill covers it — a missing
  or expired token is a red health check, not a mid-turn 403.

Spec: `docs/superpowers/specs/2026-09-24-agent-github-delegation-design.md`
(`tests/test_agent_delegations.py`). Salesforce for agents: a workspace vault holding `salesforce` (Eva's chrome-sales credential), attached to each agent that uses it; boxes stage it from the resolved values.

### The canopy login a turn gets (turn-scoped agent token)

A runner turn acts as its agent with a login canopy-web issues for THAT turn, in place
of the agent's stored, never-expiring `canopy-pat` (phase 1: clients prefer it and fall
back to the stored PAT; phase 2 removes the PAT).

- `POST /api/harness/runners/{rid}/turns/{tid}/agent-token`
  (`apps/harness/api_turn_agent_token.py`, `turn_agent_tokens.py`): only with runner
  `rid`'s own runner token (a PAT is 403), for a turn it is executing, re-checking
  `runner_may_hold_agent`; 409 for a local turn, a confined (caller) turn, or an
  agent with no active `Agent.user`. Mints a `PersonalToken` for `Agent.user` (12 h),
  recorded as a `TurnAgentToken`; logged as an `AgentCredentialRead`
  (`turn-agent-token`, `via=turn`) and an `agent.canopy_token.issued` event.
- Revoked when the turn stops executing on that runner: `finish_turn` (every exit,
  requeues included), every terminal transition (`harness.closing.close_turn`: the
  lease sweep, a stop, an admission refusal, a moved turn), and `revoke_stale` on
  every queue-sweep tick.
- **Session-scoped on a laptop.** An emdash turn finishes as soon as its Claude session
  exists, and the person goes on driving the session. So on an `emdash` runner a turn
  whose session is still active and bound to that runner under the turn's session key
  gets a login tied to the SESSION (`TurnAgentToken.session`; `via=session` in the read
  log), held while that stays true and revoked by `revoke_stale` when the session leaves
  the runner or is archived. `finish_turn` leaves it alone. `?scope=session` asks for that
  login only (404 with no live session), for a client that outlives the turn. A turn's
  session is its `chat_session`, or the chat it names in `origin_ref.chat_session_id` (a
  laptop chat turn records no session key), else the binding its session key names; it must
  be the turn's agent's session. Never for a cloud or `computer` runner. Every action it takes is tied to
  `turn`, the turn that opened the session.
- Cloud runner: `CANOPY_WEB_PAT` in the turn env + `~/.canopy/agent-token/turn/<tid>.token`
  for the MCP headers helper. Laptop: the canopy CLI asks on demand with the box's
  runner token and the session's turn id, for session scope; the MCP headers helper asks
  through `canopy agent session-token`.

Spec: `docs/superpowers/specs/2026-10-21-turn-scoped-agent-token-design.md`
(`tests/test_turn_agent_token.py`).

### The per-user GitHub App (`apps/github_connect/github_app.py`)

A public GitHub App (`canopy-agents`) authorized per user (user-to-server flow).
It is canopy-web's OWN server-side GitHub access as a person — skill history
(`apps/agents/skill_history.py`, as the agent's owner) and the installation list
call `access_token_for(user)`; no route hands it to a runner.

- `GitHubConnection` stores the refresh token and current 8-hour access token,
  Fernet-encrypted. The access token is reused until `ACCESS_TOKEN_MARGIN` (5 min)
  before expiry. **The refresh token rotates**, so a refresh runs under
  `select_for_update` with a re-read after the lock — two concurrent refreshes
  spending one refresh token kill the grant (`refresh_failed_at` → "reconnect").
- **No `Administration` permission**: repo creation needs it, a user token cannot
  be down-scoped, and it would let agents delete repos. The user creates a blank
  repo; canopy only pushes.
- **Authorizing is not installing:** `/login/oauth/authorize` has no repository
  picker, so Connect points at `/apps/{slug}/installations/new`
  (`install_url`). `connected` alone is not enough state; an empty installation list
  means access to nothing.
- Browser legs: bare views `/auth/github/{start,callback}/` (`config/urls.py`); JSON:
  `/api/tokens/github{,/installations}`.
- Config: `GITHUB_APP_CLIENT_ID` and `GITHUB_APP_SLUG` are public (plain env);
  only `GITHUB_APP_CLIENT_SECRET` is a Secrets Manager entry. Unconfigured is a real
  state — `/settings` says so and everything else works (`is_configured`).

Spec: `docs/superpowers/specs/2026-09-12-github-backed-agent-creation-design.md`
(`tests/test_github_connect.py`).

## A cloud runner's Claude credentials (one ordered list)

A cloud runner holds **one ordered list** of Claude credentials (`RunnerLogin`),
any number of each kind, in any order a person sets on its credentials page. The
box uses the first that works and moves down the list when one hits its usage cap
or stops working. Three kinds:

- **Login** — a full `claude auth login --claudeai`, signed in through the browser
  relay (`RunnerMint` with `slot: box` and the entry's `login_key`; URL out, code
  back). It **stays on the box** — its refresh token rotates, so a copy anywhere
  else would break both. canopy-web holds only the entry and what the box last
  reported about it (`signed_in`, `account`). The only kind that carries claude.ai
  connectors: a setup-token is inference-only (`[claudeai-mcp] Missing
  user:mcp_servers scope`).
- **Token** — a `claude setup-token`, made on the box through the same relay
  (`slot: token`; `login_key` names the token to replace, "" adds one). Good for
  a year with nothing to renew. The box reads the token off a replayed screen
  (`render_screen`: the CLI redraws only changed cells, so stripping its cursor
  moves loses characters), probes it (a token Claude refuses is a failed sign-in,
  with Claude's words), names its entry (`POST …/mint/entry {vault_key}`), seals
  it into the runner's vault (`canopy vault runner-seal`) and only then reports it
  (`…/mint/result {vault_key}`) — done only if the vault holds it; any failure is a
  failed sign-in saying why, and an entry made for it goes again.
- **API key** — sealed into the runner's vault in the browser (`POST …/vault/entries`,
  then `PUT /api/vaults/{id}/zk/values`). Billed per use: stepping onto one raises
  the coalesced `claude_api_key_fallback` alert.

**Values live only in the runner's vault** (`docs/architecture/vault.md` § A
runner's own vault): canopy-web keeps the list — names, kinds, order — and never a
token or key. The bundle names each value's secret (`cascade[].vault_secret`); the
box opens them with its own device key (`canopy vault runner-credentials`), keeps
what it opened until the bundle changes or `VAULT_REREAD_SECONDS` (1 h) passes, and
says how it went in its `claude.vault` check. Nothing takes a plaintext value (no
paste route, no fixed-slot write, no token in a sign-in result), so a runner needs a
vault with a key before it can hold a token or API key; a login needs none.

A token or key cannot say whose it is, so a person names it (`name`). The box
reads the list as `cascade` on `GET …/credential`, with `logins` (the login
entries) and `vault_open`. The status view's `has_claude_token`,
`has_claude_token_secondary`, `has_claude_api_key` read the first two tokens and the
first API key whose value the vault holds.

- **On the box**, each login has its own config directory: `default` is the CLI's
  own (`~/.claude`, state in `~/.claude.json`); every other login is
  `~/.claude-logins/<key>`, holding its own `.credentials.json` and `.claude.json`
  with everything else symlinked to `~/.claude` — so transcripts (`projects/`),
  plugins and settings stay in one place whichever login a turn ran on. Selecting
  a login sets `CLAUDE_CONFIG_DIR` (unset for `default`) and clears both auth env
  vars; concurrent turns each keep the directory they started with. A login is
  in the cascade only when its directory holds a login with the
  `user:mcp_servers` scope.
- **Add** a login (`POST …/logins`) appends an entry and starts its sign-in; it
  reads "not signed in yet" until the box reports otherwise. **Add** a token by
  signing it in, an API key by sealing it (above); **rename** one
  (`PATCH …/credentials/{key}`, `name` only — a `value` is refused).
  **Remove** any entry (`DELETE …/credentials/{key}`); on its next bundle read
  (≤5 min) the box drops it, deleting a login's directory (for `default`, only
  its `.credentials.json`). **Reorder** (`PUT …/credentials/order`) takes every
  key, first to last.
- **The box reports** (`POST …/logins/report`) which listed logins are signed in
  and as which account, whenever that changes. A default login the list has never
  held (signed in before the list existed, or by hand) is reported with
  `adopt_default` and joins the list first; `~/.claude-logins/.default-adopted`
  then records that a missing `default` means removed.

**A token or key keeps the box alive.** A login renews itself, and a renewal can
fail; a box with nothing else then stops. So removing
the **last** token or API key is refused (422; `LAST_FALLBACK_REFUSAL`): add
another first. A box with no usable credential at all waits in
`fetch_and_stage_credential`, heartbeating as online but not ready with a note
that says to sign in, and serves the browser sign-in from there — it does not
read "stale".

The runner reports a `claude.login` health check (ok = signed in on the box as
the first login's account; absent when the list has no login). Any mix of
logins, tokens and API keys, one or several, is a healthy box: `claude.credentials`
fails only when the list is empty, and nothing warns about how many there are. Tests:
`runner/ec2/tests/test_box_login.py`, `tests/test_runner_logins.py`,
`tests/test_runner_mint.py`, `tests/test_runner_credential.py`.

**Nothing copies between boxes.** `wire.sh` gives a new box its own vault (or, with
`--vault`, attaches it to one it shares an isolation boundary with) and prints its
Credentials tab, where its owner makes the key and signs it in; it never holds a
token. A cloud box with no signed-in full login says so on its page.

### The credential probe: "ready" means a credential answered

A stored value is not a working one. So the box asks each credential one short
question with no tools (`cloud_runner._probe_credential`: `claude -p "Reply with
OK" --output-format json --max-turns 1` in a scratch dir, with that credential's
environment built as a copy — never `os.environ`, turns run concurrently; 90 s
timeout) and classifies the answer the way a turn's failure is (below): the
result's `api_error_status` first (401 = `dead`), then the `_is_usage_cap` /
`_is_auth_required` / `_is_out_of_credit` text matchers: `ok`, `capped` (+ the reset
when readable), `dead`, `no_credit` (the account has no credit), or `unknown` (timeout, crash, unreadable). `CLAUDE_PROBE=0` turns probing off
(every probe answers `unknown`).

- **When:** after `fetch_and_stage_credential` stages a bundle, synchronously,
  then reported at once in a heartbeat (the next one is otherwise after the
  bootstrap); after a re-read changes the bundle, only the new or changed entries,
  in the background — and every entry when the bundle was re-written unchanged
  (its `updated_at` moved); after a login is signed in again or the default login
  is adopted (its value, a config dir, does not change). A re-probe keeps the
  entry's old check in the heartbeat until the new answer replaces it — the
  `runner_credential` alert reads a missing check as fixed. For a capped entry once
  its reset has passed and a dead one every `DEAD_REPROBE_SECONDS` (10 min) — the
  main loop's poll tick (`no_credit` like dead, so a topped-up account unpauses the box); and when a turn's own failure is ambiguous (below).
  Background requests queue on one prober thread. Each check's detail ends
  `· probed <ISO>`. Never on a plain heartbeat — probes spend usage. A turn's own outcome
  updates the same state for free (a turn that ran marks its credential `ok`; a
  classified cap or dead marks it so).
- **Health:** one check per credential, `claude.probe.<label>` (`box-login`,
  `box-login:<key>`, `token:<key>`, `api-key:<key>`): ok / warn "at its
  usage cap — resets <ISO>" / fail "not signed in — someone has to sign it in
  again" / fail "out of credit — someone has to add credit to its account" / warn
  "could not check: …". The runner page colours each token and login
  dot by it (`RunnerCredentials.tsx` `probeLabel`, pinned to
  the box's labels by `tests/test_probe_labels_match.py`); with no check the dot
  says only that a value is set.
- **Readiness:** the heartbeat sends `ready=False` with a note naming each
  credential's state (and, when all are capped, the earliest reset) only when every
  staged credential was probed and none is `ok`, `unknown`, or capped with its
  reset passed. A hung probe is `unknown`, so it never keeps a box from serving.
  The WS beat carries `ready`/`ready_note` like the REST beat.
- **Self-pause:** `ready` alone does not stop claims (it feeds only
  `Runner.is_available`), so the box also pauses itself (`POST …/pause`, note
  prefixed `[box] `): until the earliest reset when every credential is capped,
  open-ended when any is dead. The usage-cap park after a turn uses the same
  prefix. It lifts its own pause (`POST …/unpause`) once a probe answers `ok` —
  only a pause whose note carries the prefix, read off the heartbeat reply
  (`paused`/`paused_note`, on both the REST reply and the WS ack). It never
  pauses over, or lifts, a person's pause. A beat with no usable reply leaves
  the pause alone. After anyone changes the pause (a person's unpause, the
  scheduled wake) the box pauses again only on probes taken after that change.
- **A `claude -p` turn's failure is read from the stream, not its text**
  (`cloud_runner._credential_fault`): an assistant message's `error`
  (`authentication_failed`, `oauth_org_not_allowed`, `account_on_hold` → dead;
  `rate_limit` → capped; `billing_error` → no credit), a `system/api_retry` with a dead
  category, a result with `api_error_status: 401`, and a `rate_limit_event` whose
  `status` is `rejected` (capped, its `resetsAt` the exact reset). Dead fails over
  and raises `claude_auth_required`, like a signed-out login. **No credit** (an API
  key whose account is empty, "credit balance is too low") is not a cap — nothing
  resets it: the box fails over like a cap, raises `claude_credit_exhausted` at once,
  and when every credential is spent pauses itself (`[box] out of credit`, retried on
  `CAP_PAUSE_FALLBACK_SECONDS`, lifted by a probe that answers). The text matchers
  decide only when the stream named nothing (ACP, an older CLI): an invalid
  setup-token's "API Error: 401 OAuth access token is invalid" matches none of
  them. An assistant message the stream marked as a credential error is held back
  like a cap sentence. Tests: `runner/ec2/tests/test_structured_credential_errors.py`.
- **An ACP turn is read from its transcript.** The adapter can end a turn the API
  refused with a clean stop and the error as plain text, and refuses a dead token and
  a capped one with the same bare `auth_required`. Claude Code writes the refusal into
  the session's transcript as an `isApiErrorMessage` record with its `error` category,
  so after an ACP turn the runner reads the last record written since the turn began
  (`_transcript_fault`): an API error there is the turn's fault — dead, capped or no
  credit, exactly as under `claude -p` — and fails the turn. Tests:
  `runner/ec2/tests/test_out_of_credit_and_acp_faults.py`.
- **A bare ACP refusal with no transcript is probed.** Over ACP a capped token and a signed-out one
  both fail with `runner error (acp): authentication required`. When nothing
  earlier in the turn capped, the box probes that credential: capped → it is a cap
  (reset from the probe, the probe's line in the turn note, the out-of-usage park
  and alert); dead or inconclusive → not signed in, as before. The CLI's own
  sentences (`_AUTH_REQUIRED_MARKERS`) still win without a probe.

**The alerts reach a person.** The box posts `claude_auth_required`,
`claude_usage_exhausted`, `claude_credit_exhausted` and `claude_api_key_fallback` to
`POST /api/events/` with
its runner token; with no workspace pinned they file in `Runner.workspace`, and
each also mails and Web-Pushes the runner's approvers (owner + runner admins)
once per (runner, alert) per UTC day, linking to the runner on `/supervisor`
(`apps/harness/credential_notify.py`). The exception: a signed-out credential on
a box whose health reports a signed-out `claude.probe.<slot>` check is the
`runner_credential` alert instead (`apps/harness/alerts_runner_credential.py`; so is
an out-of-credit one, "… is out of credit") —
titled with the token's name or the login's account (never the slot), notified
once to the owner and runner admins, resolved when the probe passes — and its
`claude_auth_required` (or `claude_credit_exhausted`) is not also mailed. A laptop (no probe checks) or an
inconclusive probe is still mailed. Tests:
`runner/ec2/tests/test_credential_probe.py`, `runner/ec2/tests/test_auth_required.py`,
`tests/test_credential_notify.py`, `tests/test_alerts_runner_credential.py`,
`tests/test_realtime_runner_consumer.py`.

## How a runner signs in (keyless)

- A paired box signs in **as itself**: it trades a fresh proof of its APPROVED device key (an EC2 box's instance role; a laptop's Secure Enclave/TPM key) for an hour-long `RunnerAccessToken` at `POST /api/runner-sessions/`, renews it before it runs out, and uses it for REST and its `ws/runner/<id>/` socket. Nothing long-lived lives on the box, and nothing in Settings → Tokens can cut a runner off.
- **One protocol, two channels.** The `ws/runner/<id>/` socket's `heartbeat` and `finish` frames are read and applied by the same functions as `POST …/runners/{id}/heartbeat` and `POST …/turns/{id}/finish` (`apps/harness/runner_protocol.py`, over `HeartbeatIn` / `TurnFinishIn`): a WS beat carries `degraded`/`note`, `host`, `projects` and `mailboxes_readable` with the REST rules (absent leaves a list alone; a malformed field costs that field, never the beat), and a WS finish takes only `done`/`failed`/`cancelled` and records `session_key` (a refused frame acks `ok: false` with an `error`). Who may act as a runner is `runner_protocol.acting_runner_q` — its owner; a runner with no owner connects for nobody and is in nobody's `/supervisor` snapshot.
- The token opens the runner protocol only (`apps/harness/runner_auth.py`) and authenticates as `Runner.owner`, so every owner-scoped runner gate is unchanged. Revoking the device key (Settings → Security) stops the box at once — the socket re-checks its credential on every frame (`channels_auth.still_signed_in`).
- **A laptop installed from a download pairs in the browser and holds no PAT** (`apps/harness/api_runner_pairings.py`, `runner/canopy_runner/canopy_runner/browser_pair.py`). The box makes its device key, then `POST /api/harness/runner-pairings/` with the public key and a DPoP proof of it, the loopback port it listens on, its hostname, account and whether emdash is present; it shows the returned code and opens `/pair/<code>`. The person signs in, checks the code, picks a workspace where they hold `agent.work` and a name, and approves behind a passkey step-up (a token is refused). The approval's one-time secret comes back only as a redirect to `http://127.0.0.1:<port>/paired` on the machine whose browser approved, so an approve link sent to someone else gives the sender nothing. The box claims (`…/{id}/claim`, DPoP of the same key plus the secret) and gets a `Runner` (`kind=emdash`, runtime `claude-cli` when there is no emdash — and on Windows unless emdash is already serving its DevTools port, since the "Emdash CDP" launcher that reopens it with one is macOS-only; `Runner.platform` keeps the OS it paired from, so its remedy names the fix that exists there) whose device key is already ACTIVE, ECDH key approved with it; the approver must still hold `agent.work` there. A key canopy-web already has ACTIVE on a live runner re-attaches instead; any other known key, an expired, declined or used pairing, or a wrong secret or proof is one uniform 401. Pairings last 15 minutes, one live per key. `runner.json` then has no token, and `~/.canopy/device-key/status.json` is written as canopy's CLI writes it so `canopy vault` shares the key.
- A developer laptop (`install-runner.sh`) pairs with its person's PAT (`CANOPY_TOKEN`), which also registers its key. A **cloud box pairs by invitation and holds no PAT** (below). A box paired by invitation, and every runner with its own Unix user on a machine, has **no PAT fallback**: without a token it reports itself unauthenticated. Only a cloud box from an older stack that still has `CANOPY_TOKEN` (and never paired by invitation) falls back to it while no approved key can get a token (`runner/ec2/cloud_runner.py::_bearer`). No turn inherits the box's credential: an agent turn acts with its own login, and an agentless repo turn (anyone's, once routed here) reaches canopy only through its asker's read-only caller token (`cloud_runner._agent_env` strips `CANOPY_TOKEN` from both).
- **Pairing a cloud box by invitation** (`apps/harness/api_runner_invitations.py`). `runner/ec2/up.sh`, with the operator's own workbench token, creates a `RunnerInvitation` once the stack's instance role exists: `{name, workspace, role_arn, ecdh_public_jwk}` (the `<stack>-ecdh` KMS public key), in a workspace where the operator has `agent.work`. It lasts an hour, and one live invitation exists per (role, name): your own new one replaces yours, and someone else's live one is never replaced (409). The approve link is printed, mailed and pushed to the creator. **Only the creator approves it**, from a browser behind a passkey step-up (`stepup.require_recent_step_up`); a token gets 403, so neither `up.sh` nor the box can approve its own pairing. The box (no state file, no `CANOPY_TOKEN`) calls `POST /api/harness/runners/pair-by-role` with an instance-role proof bound to that URL (`aws_iam_proof.verify`): waiting → 202; approved → one transaction creates the `Runner` (owner = creator, `workspace` = the invitation's, enrolled there by `Runner.save`), its `aws-iam` `RunnerDeviceKey` already ACTIVE (and the invitation's ECDH key approved with it), and marks the invitation consumed. The creator must still have `agent.work` there at that moment. Every other answer (no invitation, expired, used, another name, another ECDH key, a bad or replayed proof) is one uniform 401 that creates nothing. The box then signs in at `POST /api/runner-sessions/` as any keyless box does.
- **A replaced instance re-attaches.** The role is the box's identity (`jkt` = sha256 of the role ARN), and `up.sh --allow-instance-change` keeps the stack's role. So `pair-by-role` from a role with an ACTIVE key on a non-retired runner of the same name returns 200 with that runner's id; no invitation is needed or used, and `wire.sh` has no predecessor to retire. A role whose runner was retired needs a new invitation from that runner's owner; the old key row is replaced, so `jkt` stays unique. A key for the role on another owner's runner (live, revoked or retired) is never erased, and a pending or revoked key on a live runner is never replaced either: a revocation is undone by its owner, not by re-inviting. Each is the uniform 401.
- A machine running several runners pairs every runner in one call (`runners` on the invitation and on `pair-by-role`; see "Setting up the runners on a machine" above).
- **A fleet member joins with no invitation of its own.** When the proven role is a fleet's, `pair-by-role` is `fleets.join`: the fleet's one passkey approval plus AWS's word that the instance is the fleet group's (§ Runner fleets). Its key is role + instance, so sign-in (`aws_iam_proof.verify_caller` → `known_key`) tells members apart.
- Cloud runner: `runner/ec2/cloud_runner.py::_bearer` signs its instance-role proof itself; a runner with its own Unix user runs `canopy vault runner-session` with its own software key. Laptop runner: `runner/canopy_runner/canopy_runner/runner_token.py::RunnerCredential` runs `canopy vault runner-session` when the canopy CLI is installed, else signs the proof itself (`device_proof.py`, the same key files and formats as canopy's `orchestrator/device_key.py`: `se.key` through `canopy-device-key`, the TPM key `canopy-device-key`, or `sw.pem`), shared by its REST client and wake socket. Both renew 10 min before expiry, retry a 401 once with a fresh token, and fall back to the PAT only while no approved key can get one.

## The runner software

A runner is an installed package, never a checkout, and updates itself to what is deployed.

**Laptop install from a download.** `/get-started` and the topology page offer the installers (`GET /api/harness/runner-releases/downloads`, `/download/runner/<platform>`: a signed-in person gets a 302 to a 15-minute presigned S3 URL). macOS (`macos-arm64`, `macos-x86_64`): a DMG holding `Canopy Runner.app`, which copies itself to `~/Applications` and runs `canopy-runner setup` from `Contents/Resources/runtime` — browser pairing when there is no `runner.json`, `~/.canopy/bin/canopy-device-key` linked into the bundle, and the `com.canopy.runner`, `.updater` and `.menubar` LaunchAgents pointing at the app's version-free path. Windows (`windows-x86_64`): an Inno Setup installer that never asks for admin, installs `%LOCALAPPDATA%\Programs\Canopy Runner\versions\<sha12>` behind a `current` junction and runs the same `setup`, which registers `\Canopy\canopy-runner` (at logon, `pythonw.exe … run --log-file`) and `\Canopy\canopy-runner-updater` for the current user. Each bundle carries its own CPython (python-build-standalone) with the runner wheels, Node.js, the CDP sidecar's `node_modules` and the ACP adapter, and on macOS the menu-bar app and `canopy-device-key`; nothing is fetched at install. The runner finds its bundle from `sys.executable` (`bundle.py`) and puts the bundle's node and adapter first. Build: `.github/workflows/runner-release.yml` and `runner/packaging/` (README lists the signing secrets). Every runner-source sha that reaches main is built, and on main uploaded to `runner-releases/<sha>/` in the attachments bucket with `manifest.json` written last; `signed`/`notarized` in the manifest say what actually happened. The upload needs `UploadRunnerReleasesOnly` on the deploy role (`deploy/aws/deploy_roles.py`, applied by hand). Spec: `docs/superpowers/specs/2026-10-19-runner-installers-design.md`.

**The two installs share one runner.** The downloaded app and `install-runner.sh` use the same `runner.json`, device key and job labels; whichever installed last runs it, and switching never re-pairs. `setup` says when it takes over from a developer install; `install-runner.sh` replaces a released app in `~/Applications` and a `canopy-device-key` link into it. `~/.canopy/hold-updates` holds a box on what it runs, for both updaters. Testing paths (second macOS account, unreleased workflow artifacts): `runner/packaging/README.md`.

**Developer install.** `runner/canopy_runner/scripts/install-runner.sh` installs and updates: it `git archive`s a ref, builds and `uv tool install`s three wheels, provisions the CDP sidecar's node deps, and renders + loads a generated launchd plist. The daemon runs from a tool venv no checkout can change. Re-running restarts the daemon (not mid-turn). `--ref <branch>` installs another ref; `--no-launchd` skips the daemon; `--workspace <ws>` pairs into a workspace. Spec: `docs/superpowers/specs/2026-07-28-runner-as-installed-package-design.md`.

**Auto-update (both kinds).** A separate job — `com.canopy.runner.updater` (launchd, every 30 min; `--no-auto-update` to skip) on a laptop, `runner/ec2/update_runner.sh` (systemd timer) on a cloud box — installs the **deployed** `expected_code_sha` for that runner kind, never `origin/main`; defers while a turn is in flight (`~/.canopy/in-flight`); and is read-only against the control plane (`canopy-runner update-check` → `current|stale|busy|unknown`, never a heartbeat). A heartbeat reporting a differing sha gets a `runner.update_available` frame that kicks the updater, so the timer is only the fallback. The canopy plugin + CLI is a second version a box runs, fixed by a re-bootstrap rather than the updater: a box reporting a canopy CLI behind the canopy release gets `refresh_pending` on its heartbeat reply (`apps/harness/canopy_release.py`), and its `canopy.cli` check warns against that release, not only against its own clone. A downloaded install's updater is `canopy-runner self-update --if-stale`: it asks `GET /api/harness/runner-releases/current?platform=` with its own runner token (the route serves the release for the deployed `RUNNER_CODE_SHA`; none built = 404 = `unknown`), downloads the update artifact, checks its sha256 (and on macOS the code signature's team against the running app's), then swaps `~/Applications/Canopy Runner.app` (macOS) or runs the installer with `/UPDATE` and flips `current` (Windows), re-runs `setup --no-pair` and restarts the runner. Laptop log: `~/.canopy/updater.log`; bootout the updater before testing a branch on a box. The cloud box installs via `git show <sha>:runner/ec2/cloud_runner.py` from its on-box clone; Secrets Manager is a first-boot seed only; override with `canopy-runner-update --ref <ref>`. Spec: `docs/superpowers/specs/2026-07-30-cloud-runner-auto-update-design.md`.

**Disk no runner owns.** A runner sweeps only its own scratch, but its `disk` check measures the whole volume. So every tick of the cloud updater (root on a machine running several runners) also handles what no runner owns:
- it ages `/tmp` out after 3 days, whoever owns the files (`/etc/tmpfiles.d/tmp.conf`);
- it deletes the regenerable caches of the user from before several runners (`LEGACY_USER`, when that user is not a runner's and runs nothing);
- at ≥ 80% it records the largest of `/home/*` and `/tmp` in `/opt/canopy-runner/disk-usage.json`, which the `disk` check's detail then names (`runner/ec2/update_runner.sh`, `cloud_runner._disk_largest`).

**Code provenance.** `code_sha` is the last commit touching that runner kind's own source paths, not repo HEAD — laptop: the runner package and its scripts, the menu-bar app, the device key, brand assets and the in-repo `canopy_cron`/`canopy_transcript` packages; cloud: `cloud_runner.py`, `bootstrap_agents.sh`, `update_runner.sh`, `kms_broker.py`, `canopy_transcript` and `canopy_acp` (both lists in `.github/workflows/deploy-labs.yml`). The server holds `RUNNER_CODE_SHA` / `RUNNER_CLOUD_CODE_SHA` (computed by the deploy workflow, `fetch-depth: 0`); `RunnerOut.expected_code_sha` serves the one matching the row's `kind`, and `/supervisor` alerts only when both sides are non-empty and differ (empty = unknown).

**The cloud runner** (`runner/ec2/cloud_runner.py`) polls claim every 15s and drains the queue, executes via `claude -p` (or ACP with `RUNNER_EXECUTOR=acp`, default off; see the executors below), declares repos via `RUNNER_PROJECTS`, and runs the stream/backfill contract on a 3s clock. A cloud box learns of an agent only by bootstrapping, so registering one (or changing its repo) requests a refresh on the cloud boxes that will take its turns (`apps/agents/setup.py::request_setup`); until that box posts the agent's bootstrap report, the agent's setup checklist shows its runner step as waiting. And if a turn for an agent the box has no clone of arrives first, the box bootstraps before running it (`cloud_runner._ensure_agent_clone`, once per agent per `LAZY_BOOTSTRAP_RETRY`) rather than running it in a bare scratch dir. `bootstrap_agents.sh` does the box's half (tooling, gog's file keyring, the canopy plugin + CLI) and then runs `canopy agent prepare --runner` per agent (clones `dimagi-internal/<slug>` into `/opt/agents`, env, plugins, Gmail, a readiness report); it runs from `main()` after the credential is staged and is not `set -e`, so one agent's failure does not stop the rest. Spec: `docs/superpowers/specs/2026-07-25-cloud-agent-bootstrap-design.md`.

### How the cloud runner drives a CLI (turn executors)

The CLI is *which* agent program runs a turn (`claude-code`, `codex`, `apps/harness/clis.py`); the **executor** is *how* the cloud runner drives it. Each executor is a `TurnExecutor` subclass in `cloud_runner.py`, registered in `_EXECUTORS` by its `RUNNER_EXECUTOR` value (runner.cfn.yaml `RunnerExecutor`), and `_executor()` is the only code that turns that setting into behaviour (a branch on the name fails `tests/test_turn_executors.py`):

| Method | Does |
|---|---|
| `run_turn(prompt, turn_id, emit, *, cwd, agent_slug, resume_session_id)` → `(ok, text, cli_session_id)` | starts or resumes the turn and runs it to its end: emits live `assistant` / `tool_start` / `tool_end` events, calls `_announce_session` once it knows the CLI's session id, ends early on `stop_requested`, and leaves a usage cap's wording in `text` (exact reset in `_CAP_RESET_HINT`) so `execute_prompt` fails over down the credential cascade |
| `live(turn_id)` | the handle a stop reaches (a process, a protocol connection), or None |
| `interrupt(turn_id, handle, *, escalate, waited)` | a person's stop: polite first, escalated once ignored for `STOP_ESCALATE_SECONDS`; `request_stop` asks every executor, since one may hand a turn to another |
| `prepare()` | bootstrap installs (the ACP adapter) |
| `check_packages()` | its `packages.*` health check |
| `carries_settings` | whether it can run a locked settings document; a sensitive-data box runs only an executor that can |

Today: `cli` (`ClaudePrintExecutor`: `claude -p` + stream-json, `run_claude`) and `acp` (`AcpExecutor`: canopy_acp over `claude-agent-acp`, `run_acp`, which runs the turn as `claude -p` when canopy_acp did not import); both drive `claude-code`. A new way of driving a CLI — Codex's `codex app-server`, an Agent SDK executor — is a new subclass, its protocol driver a package off the runner's clone imported lazily as `canopy_acp` is. The classes live in `cloud_runner.py` itself because the box runs that file alone (seeded whole, replaced whole by `update_runner.sh`).

What the claim says about the turn's CLI reaches it: a turn claimed for a CLI the box's executor does not drive (`cli`) is finished `failed` with `cli_mismatch` before anything runs (`_cli_refusal`; no `cli` = Claude Code), and the model its agent names (`cli_model`, shaped like a model name or ignored) is `--model` on `claude -p` (`_turn_model`, `_TURN_ENV.model`) and `ANTHROPIC_MODEL` for the ACP adapter's Claude Code (`_model_env`).

Not on the interface, because both executors share them as `claude-code`'s: the session store (`_resume_target_exists`, `_ship_transcript_rows`, `_TurnTail` read `~/.claude/projects`), forks, the credential cascade and its probe, and reading a cap or a sign-out from a turn's text. A second CLI brings its own of each.

## Runner fleets (`apps/harness/fleets.py`)

Cloud capacity grows with a **fleet**: one EC2 Auto Scaling group of interchangeable
cloud runners, whose size canopy-web sets from the work waiting for it. Spec:
`docs/superpowers/specs/2026-10-24-runner-fleets-design.md`. Building one: `runner/ec2/fleet.sh`
(README § Runner fleets).

- **One approval, then AWS vouches.** A fleet (`RunnerFleet`) is approved once, by its
  creator, from a browser behind a passkey: its `RunnerInvitation` on the usual approve
  page, used up by the approval. No token can approve it. After that, an instance
  joins on its own at `pair-by-role` when STS says it signs as
  `assumed-role/<the fleet's role>/<i-…>` and EC2 says that instance is pending or
  running with the reserved tag `aws:autoscaling:groupName = <the fleet's group>`. The
  role trusts only EC2, so the session name is an instance id EC2 set, and no caller
  can set an `aws:` tag. The fleet must be active and its owner must still have
  `agent.work` there. Any refusal is the uniform 401.
- **A member is its own runner.** Its name is `<fleet>-<instance id>`. Its device key
  is role + instance (`aws_iam_proof.member_jkt`; sign-in reads the instance from
  `verify_caller`). Its `machine_id` is `<role>/<instance>`. One member cannot sign in
  as another, and the role on its own signs in as nobody. A fleet's role is never one
  box's identity: an invitation, a fleet and a device-key enrollment each refuse a role
  the other uses.
- **One KMS key per fleet, so new members need no person.** Every member's ECDH key is
  the fleet's KMS key (`ecdh-kms.cfn.yaml`), which only the role can derive with. The
  first member is granted the credential set's vault key by its owner (Make its key /
  grant, once per fleet). From then on canopy-web keeps the fleet's copy
  (`RunnerFleet.vault_wraps`, refreshed each tick) and gives it to each new member
  (`_give_wraps`). The copy is ciphertext for the same public key; a wrap binds vault,
  key version and the ECDH key thumbprint, so it opens on any member and canopy-web
  opens none of it.
- **A member is routed as its fleet** (`fleets.sync_routing`). Agents' runner lists and
  workspaces' runner orders name runners, and a member the scaler adds is a new runner,
  so the fleet keeps its routing (`RunnerFleet.routing`): every row its members hold. A
  member joins with those rows (a live member's, else the fleet's copy), and each tick
  first carries a change made to one member's rows (added to an agent, taken off one,
  switched off) to every member. Route a fleet by routing any one member. A member whose
  instance left the group is retired with its rows deleted, as the retire route does;
  the fleet's copy outlives every member.
- **A credential set is the fleet's one list** (`RunnerCredentialSet`, § Credential
  sets below). `fleet.sh up --credential-set <name>` makes the fleet read an existing
  set; otherwise it gets a new one.
- **The scaler** (`tick`, the off-request clock, `RUNNER_SCALER_TICK_S` 30 s, 0 = off).
  Each tick, per fleet:
  1. Read the group and its warm pool. `Runner.power` follows each instance's state:
     `InService` → `on`, `Pending` → `waking`, `Warmed:*` → `stopped`. A member whose
     instance left the group is retired, and a turn it was running is queued again as
     itself (`requeue_lost`, at most twice).
  2. **Fail over** before measuring demand. A chat bound to a member that is gone
     (retired, or unreachable and out of service or silent `RUNNER_FAILOVER_AFTER_S`,
     3 min) with a turn waiting moves to the live member with the most room
     (`send.move_queued_turns`, as the fleet's owner). A member that still answers is
     never moved off, because its handoff would still be packing.
  3. **Grow** for turns the fleet could claim (the claim's `_coverage` plus the tenant
     gate) that have waited past `RUNNER_WAKE_AFTER_S` (60 s) and are not behind their
     agent's or session's current turn. It adds `ceil(starving / member_turns)` members,
     less what members coming up already absorb, up to `max_members`.
  4. **Shrink** by one member per tick, never while anything waits, never below
     `min_members`. A member idle `RUNNER_IDLE_STOP_S` (30 min) goes `draining`; **the
     claim refuses a member that is not `on`**. Next tick, if it is still idle, its
     scale-in protection is lifted and the desired capacity lowered by one, so AWS
     scales in that one instance, back into the warm pool. Every member is otherwise
     protected, so AWS never picks a busy one.
  5. Each size change is a `runner.fleet.scaled` fact.

  A retired fleet is still ticked while it has members: they are retired and their
  chats move as their instances go.
- **Readers.** A member in the warm pool or coming up, not paused or signed out, of an
  active fleet, is `Runner.wakeable`: `turn_reach` counts it as a LIVE taker and the
  unclaimable report as reachable.
- **Keep `min_members` ≥ 1**: the Gmail doorbell and inbox pollers run on a box (every
  member polls; the server dedupes mail turns per thread). **Capacity per member** is
  `MAX_CONCURRENT_TURNS` = RAM / 1.5 GiB (an m7i.xlarge runs 10); `fleet.sh` tells
  canopy-web the same number (`member_turns`).
- **Out of scope:** sensitive-data boxes and machines running several runners are single named
  boxes (`up.sh`). A fleet member is one runner for one workspace tree.


## Credential sets (`apps/harness/credential_sets.py`)

A **credential set** (`RunnerCredentialSet`) is one ordered list of Claude tokens and API
keys that several cloud runners read as their own (`runners.list_owner`): every member
of a fleet, and any other cloud runner of the set's owner that joins it. An edit from
any of their Credentials tabs (add, rename, reorder, remove) edits the set, and every
reader's stamp moves. Tokens and API keys only: a full login is refused (409), because
its refresh token rotates and cannot be shared. Usage caps are per Claude account, so
N runners on one token share one cap.

- **The list is shared across workspace trees; the values are not.** A vault never
  serves two isolation boundaries (`docs/architecture/vault.md` § A runner's own
  vault), so a set keeps **one vault per workspace tree** (`RunnerCredentialSet.vaults`,
  matched by the tree root of the vault's workspace, `credential_sets.vault_in_tree`).
  The set's first runner in a tree brings its own vault or gets a new one named for the
  set; every later runner in that tree is attached to it. A runner on a set is served
  by that vault only. Each tree's vault holds its own copy of each value, sealed by a
  person of that vault in their browser ("Add its key") or by one of its boxes after a
  sign-in ("Get a token", which fills the existing entry). An entry with no value in a
  runner's tree is one that runner skips; its Credentials tab says how many it lacks,
  and how each other tree's vault stands.
- **Coming onto a set.** A fleet member joins its fleet's set as it pairs. `POST
  /api/harness/runner-credential-sets/` `{runner, name}` shares a runner's own list:
  its entries and vault become the set's, so nothing it reads changes (409 while the
  list holds a login). `PUT …/runner-credential-sets/runners/{id}` `{credential_set}`
  makes a runner read a set (409 while it has entries of its own, or for a fleet
  member; 422 `vault_crosses_isolation` when its tree's vault already serves runners of
  another boundary, such as an agent's runner beside a tree's runner), or `null` to read its own
  list again. The set's vault then stops serving it, and its copy of the key is handed
  out to nobody (`workspace_vaults.device_may_hold`).
- **Making a set is separate from choosing who reads it.** The Credentials tab offers
  "Make a credential set from this list" (the POST), then shows the set's list, the
  runners reading it, and an "Add a runner…" picker of the owner's other cloud runners
  (`credential_sets.candidates`: each with why it may not join yet, and whether it is in
  a tree the set has no vault for). Adding one is the PUT on that runner.
- **One owner.** A set, and every runner on it, have the same owner. Only that person
  puts a runner on a set, and only they change a set's list (`api_runners._list_admin_or_404`,
  403): a runner administrator administers one runner, and the list is every runner's on
  the set. They still read it.
- **Removing an entry** deletes its secret from every tree's vault of the set, unless a
  runner outside the set that a vault also serves still lists it.
## Runner readiness (the not-ready marker)

A failed turn writes `~/.canopy/not-ready` (`runner/canopy_runner/canopy_runner/readiness.py`,
`mark_failed`); `readiness.compute` reports it on the heartbeat as `ready=False`, a
not-ready runner is not `Runner.is_available`, and lower ranks in every cascade
take its work. `mark_ok` clears it only after a turn executes, so the marker **expires after
`MARKER_TTL_SECONDS` (15 min)** — otherwise one transient failure exiles a box that
keeps heartbeating ONLINE. Expiry is retry-with-backoff: a genuinely broken box
fails its next turn and re-marks itself. The CDP probe gates independently
(`compute`), so expiry cannot mask emdash being down.

**Signed out is the one not-ready note that stops claiming.** A laptop turn finishes
at delivery, so without this a box with no Claude login would stay ready and answer
every message with the CLI's "Not logged in". The hook listener reads the transcript
tail on `Stop`/`Notification` (`canopy_transcript.signed_out`, the API-error record
only — never agent text): on that record the box writes the marker with
`canopy_transcript.SIGNED_OUT_NOTE`, raises `claude_auth_required` once
(`hooks.report_signed_out`, mailed by `credential_notify`), and skips its claim; on
the next real reply from any session on the box it clears it (`note_signed_in`). The
server reads the note as `Runner.signed_out`: `claim_next_turn` refuses the box
(above the pin), `turn_reach` returns `signed_out` for a turn only signed-out boxes
could take, admission rules treat it as offline, and the session list carries
`runner_signed_out` for `/ide`. Other not-ready notes (no Claude desktop CLI, which
falls back to emdash) still claim.
