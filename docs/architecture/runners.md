# Runners: how an agent runs, the tenant gate and what a box may hold

How an agent runs, which workspaces a runner serves, setting up a machine's runners,
opted-in projects and which agents a box may hold. The rest of the runner story is split by
subject: [runner-routing.md](runner-routing.md) (the ordered runner lists and rules that pick the box,
the CLI, the claim, turn mode), [runner-flags-and-schedules.md](runner-flags-and-schedules.md)
(host-required runner flags, scheduled turns), [runner-credentials.md](runner-credentials.md) (the
identity and credentials a turn is handed, keyless sign-in, the runner software) and
[runner-fleets.md](runner-fleets.md) (fleets, credential sets, readiness). Who may *edit* each of these is in
[`access.md`](access.md) (→ "Runner roles", "Turn mode"); the routes are in
[`api-surface.md`](api-surface.md) (→ Harness, Agents). Code: `apps/harness/claim.py`
(the claim) and `apps/harness/claim_reach.py` (its read-only explanations: `turn_reach`,
`unclaimable_queued_turns`), `apps/harness/routing.py` (every routing predicate, re-exported
from `routing_orders`, `routing_machine`, `routing_serving` and `routing_projects` — the claim,
turn status, the unclaimable report and the topology all ask the same functions, so they
cannot disagree).

## Two ways an agent runs

1. **Use it yourself in Claude Code — no runner needed.** A person with the canopy
   plugin runs `canopy agent use <slug>` once on their computer: it installs the
   agent's plugin, enrols this computer's key so it can read the agent's secrets
   from canopy vault, and records their sessions as **direct turns** on the agent's
   page. Who may: the agent's owner and agent admins and anyone with an editor line on its
   access list — whom `turn_context.resolve_turn_context` gives the whole agent (`apps/agents/credentials/licenses.py`). The people
   doing it are listed under the agent's Settings → Overview → Where it runs and → Roles → Direct
   use. A computer without the runner app gets its own key for reading the agent's
   vault (a `computer` runner made when it registers, which is no fleet runner).
   Detail: [`access-direct-use-and-admission.md`](access-direct-use-and-admission.md) → "Direct use of an agent".
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
the owner's own Claude Code sessions on that computer (direct turns). Those sessions
prove themselves with the box's device key, which may read an agent's secrets while
its owner holds an active licence. Internally the computer is a `PrivateRunner`
registration linked to the `Runner` (`PrivateRunner.runner`); the UI calls these
"your computers (used in Claude Code)", not a separate kind of runner. A computer
with no runner app is linked instead to a `computer` runner made for it, which only
holds its device keys: it never claims or heartbeats and is left out of every fleet
list, routing picker and health report. See [`access-direct-use-and-admission.md`](access-direct-use-and-admission.md) → "Direct use
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
  one drops the owner's boxes' enrollment there (`apps/harness/departure.py`, run by `apps/workspaces/departure.py`); a
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
  canopy's `gog_home`). The inbox poller, full turns and a confined turn's mail path
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

### The job broker (`canopy-job-broker`)

A runner runs under `NoNewPrivileges`, so it can't become `canopy-job` to run a plain job, and
loosening that would loosen every model turn. `runner/ec2/job_broker.py` (root,
`/run/canopy-jobs/sock`, uid from `SO_PEERCRED`, like the other two) does only `status`, `export`,
`write_token`, `start`, `stop` and `cleanup`; no request can name a path, a user or a property.
`start` is one sandboxed `systemd-run` unit. `cloud_runner.py --setup-runners` copies it and `job_self_check.sh` to
`/opt/canopy-runner/job-broker/` and runs `job_broker.py --install`. What it isolates, and what
it doesn't, is in `runner/ec2/README.md` § Plain jobs. How a job is written, set up on a schedule and
read by its author is in [runner-flags-and-schedules.md](runner-flags-and-schedules.md) § Plain jobs; the
rollout, and the self check that must print PASS on a real box first, in [deploy.md](deploy.md).

## Opted-in projects (`ProjectOptIn`)

Canopy is opt-in per repo, and the choice is part of the person's account
(`apps/harness/models/runner.py::ProjectOptIn`, `GET|PUT /api/me/canopy-projects/`,
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
- **Each repo belongs to workspaces.** `ProjectOptIn.workspaces` is `{repo: [slug, …]}`,
  set per repo on the same screen, and covers the workspaces below each slug. A repo
  never assigned belongs to the home workspace of the box holding it (no home: where
  it is enrolled), so enrolling a box in a second tree carries no repo there.
  `Runner.offers_project_in(repo, workspace)` is the question for repo work (held,
  opted in, assigned): the workspace order, project chats, an unordered repo turn and
  a pinned repo turn ask it, and a chosen box asks `project_assigned_to`
  (`canopy_sessions.placement`). A chat already bound to a box stays there. It does
  not touch mirroring or usage, which are the owner's own, or an agent's repo.
- **What a person sees of a box's repos** is `runner_repos.annotate_for_caller`, read
  once for the fleet list. `RunnerOut.repo_workspaces` is `{repo: the workspaces you
  can start a repo chat in}`: assigned there, and the workspace's order lists the box
  or you administer it. New chat offers exactly that, so `capabilities["projects"]`
  is never a list of what someone can start. To someone who does not administer the
  box, `capabilities["projects"]` and `GET /runners/{id}/projects` show only the repos
  it offers in their workspaces, plus their workspaces' agents' repos.
- **`everything`** also covers a task emdash could not place in a repo (blank
  project). A chosen list covers only the repos it names.
- Migration `harness.0104` gave every existing runner owner `everything`. A
  person who pairs a box after it starts at nothing until they choose, and the
  `canopy_projects_off` alert (`apps/harness/alerts/project_opt_in.py`, setup,
  owner only) tells them so on Home and on the runner's page.
- **The canopy plugin needs no gate.** Its Claude Code hooks act only inside a
  canopy turn, an agent session or a direct turn (an envelope, `$CANOPY_AGENT`, a
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

## An agent's state document

What an agent keeps on its box is lost when it moves to another. `AgentStateDocument` is one JSON document per agent, a backup on canopy-web that the agent writes after each change and reads only when its local copy is missing (`apps/harness/agent_state.py`, routes in `api-surface.md`). The box's copy stays the working one. It lives in a framework app, so it is named for what it is and canopy-web never looks inside it; it is not an analytics input and no fact is recorded about it. Rules:

- **Write:** the agent's own login, inside a running, unconfined turn of that agent (`turn_asker.serving_turn`). A person's own full turn may write, so their request to be forgotten reaches the backup.
- **Read:** the agent's owner and admins (`Agent.is_admin`, which includes a workspace owner); and that same login only when its turn answers to nobody or to one of them (`turn_asker.standing`, the rule of `access.review_grantors`), because a read returns everyone's records. A colleague's turn can write and cannot read. Everyone else gets the 404 of a missing agent.
- **Refused always:** a stored token of the agent's login, a session-scoped login on a laptop, a finished or confined turn, a caller token. So an agent on a laptop runner has no backup.
- **Version:** every write names the version it read; a stale one gets 409 and changes nothing. The agent reconciles and writes again. The cap is 1 MiB of compact JSON.

MCP serves these like any route, and the call log records argument names, never the document. An agent keeping people's records should deny-rail the tools for its model, so only its script reads the document (the coach's `config/gating.json`).

## Which agents a box may hold

`apps/agents/holding.py::runner_may_hold_agent(runner, agent)` (re-exported as `agents.services.runner_may_hold_agent`) — true only when
the runner's OWNER is one of the agent's admins (`Agent.is_admin`: the owner, a
workspace owner, or an `AgentAdmin`). A box running an agent holds its prompt,
its caller's token, its secrets and its owner's GitHub identity, so this gates
claim (agent turns AND chats with the agent), credential resolve, the per-turn
GitHub token (re-asked at issue, since an owner can be demoted mid-turn), pins
(which also need the pinner to be an agent admin or that runner's admin —
`access.may_pin_runner`), and every routing write (runner list, source rules, actor
routes). Don't let a runner-assignment row be the trust boundary: a workspace editor
writes those rows. A teammate whose laptop should run an agent's work needs an admin
grant on that agent. An agent's own canopy login is not a holder.

A **sensitive-data box is dedicated**: one with a declared `sensitive_data` flag OR
reporting a `data_mode` (`runner_requirements.is_sensitive_data_box` — either alone, unlike
routing's `in_force`, which needs both) holds only an agent with a `data: sensitive`
host site, whoever owns it. So no other agent's full profile, secrets or GitHub identity
shares a box with a sensitive conversation. Guarded by
`tests/test_sensitive_data_box_is_dedicated.py`.
