# Runners: how an agent runs, the tenant gate and what a box may hold

How an agent runs, which workspaces a runner serves, setting up a machine's runners,
opted-in projects and which agents a box may hold. The rest of the runner story is split by
subject: [runner-routing.md](runner-routing.md) (the ordered runner lists and rules that pick the box,
the CLI, the claim, turn mode), [runner-flags-and-schedules.md](runner-flags-and-schedules.md)
(host-required runner flags, scheduled turns), [runner-credentials.md](runner-credentials.md) (the
identity and credentials a turn is handed, keyless sign-in, the runner software) and
[runner-fleets.md](runner-fleets.md) (fleets, credential sets, readiness). Who may *edit* each of these is in
[`access.md`](access.md) (→ "Runner roles", "Turn mode"); the routes are in
[`api-runners.md`](api-runners.md), [`api-turns-and-routing.md`](api-turns-and-routing.md) and [`api-agents.md`](api-agents.md).

Start reading at `apps/harness/claim.py` (the claim), `apps/harness/claim_reach.py` (its read-only
explanations: `turn_reach`, `unclaimable_queued_turns`) and `apps/harness/routing.py` (every routing
predicate, re-exported from `routing_orders`, `routing_machine`, `routing_serving` and
`routing_projects`). The claim, turn status, the unclaimable report and the topology all ask those
same functions, so they cannot disagree.

## Two ways an agent runs

1. **Use it yourself in Claude Code, no runner.** `canopy agent use <slug>` installs the agent's
   plugin, enrols this computer's key so it can read the agent's secrets, and records the person's
   sessions as **direct turns**. Who may: `turn_context.resolve_turn_context` gives the whole agent
   to its owner, admins and anyone with an editor line (`apps/agents/credentials/licenses.py`).
   A computer without the runner app gets a `computer` runner made for it that only holds its device
   keys: it never claims or heartbeats and is left out of every fleet list, routing picker and health
   report. Detail: [`access-direct-use-and-admission.md`](access-direct-use-and-admission.md) → "Direct use of an agent".
2. **Let it run on its own, on a runner.** A laptop running the runner app or a fleet cloud box.
   Schedules, mail and dispatches start these turns; schedules fire from canopy-web itself, so no
   laptop has to be awake.

An agent used only the first way is complete without a runner (`apps/agents/setup.py`, the `runner` step).

### Private runners

A **Private** runner (`Runner.private`, owner only) takes no fleet work, only what its owner pins or
binds to it, plus the owner's direct turns on that computer (`Runner.directed_only`). Those sessions prove
themselves with the box's device key, which may read an agent's secrets while its owner holds an
active licence. The computer is a `PrivateRunner` linked to the `Runner` (`PrivateRunner.runner`).

### Allowlisted agents, and directed-only by a person's word

- **Allowlist agents** (`Runner.only_agents`): non-empty, the box runs only those agents' turns and
  chats, however they reach it, a pin included. It never adds work; an agent still reaches the box
  only through its runner list or the order it follows. It is the person-set twin of a box declaring
  it runs one agent, and is asked in the same places (`routing.serves_refusal`,
  `turn_serves_refusal`, `runner_target_q`) so the claim, turn status, the unclaimable report, the
  vault and what the box prepares agree.
- **Directed only** (`Runner.directed`) is a runner admin's setting, never something the box declares;
  a private runner is always directed (`Runner.directed_only`). A test box (`up.sh --test`) pairs with it
  on. For a sensitive-data box, allowlist its agents and leave directed-only off: directed-only would
  leave their routed turns with no taker.
- A runner's name is changeable; the box keeps the name it paired as (`Runner.paired_as`), and pair-by-role
  matches on either, so a replaced instance re-attaches to a renamed runner.

## Which workspaces a runner serves (the tenant gate)

`routing.runner_tenant_slugs(runner)` (`apps/harness/routing_orders.py`) is THE answer, and every
runner-scoped predicate calls it: the claim and the schedule list (`api._runner_schedule_qs`) both, so
"every schedule this runner may fire produces a turn it may claim" holds by construction
(`tests/test_claim_schedule_parity.py`). It is the intersection of:

- **the OWNER's workspaces where they hold `AGENT_WORK`**. `Runner.owner` is server-assigned at
  pairing. **Never `Runner.workspace`**: that FK records where the box lives; one box serves a fleet
  spanning workspaces, and scoping by it leaves other workspaces' turns QUEUED forever. A NULL owner
  fails closed (empty set).
- **the workspaces the box is ENROLLED in plus every workspace below them** (`RunnerEnrollment`,
  `routing.covered_slugs`). Pairing enrolls only the workspace paired into; joining a workspace
  enrolls nothing; leaving one drops the owner's boxes' enrollment there
  (`apps/harness/departure.py`). A separate root is never covered, so a family workspace's box stays
  off Dimagi's work though one person owns both (`tests/test_runner_enrollment.py`).

**A cloud runner serves one tree.** `runners.enroll_runner` refuses (422) a `kind=cloud` runner's
enrollment in a different root (`runners._other_tree`): a cloud runner has its own Unix user, keys and
logins, so a machine serving two trees runs two runners. A laptop may span trees (one desktop
user); `RunnerOut.roots` / `spans_trees` say so, listing only trees the caller belongs to.

`Runner.capabilities` (agents, projects) is a caller-supplied routing HINT: intersected with the tenant
gate for project and session turns, never a substitute for it. Agent turns route by `RunnerAssignment`
and `runner_may_hold_agent`, tenant-gated through `routing.agent_tenant_q` (no NULL-workspace leg:
always AND it with `agent__isnull=False`); repo turns through `Turn.workspace`, session turns through
`chat_session.workspace`.

## Several runners on one machine

A cloud machine runs several runners, each serving a workspace tree or a single agent. The machine
declares what each serves in `capabilities.serves` (`Runner.serves()`; server-owned once set, a
capabilities write can neither replace nor clear it). Runners on one machine share `Runner.machine_id`,
the IAM role ARN the box proved: set by `runners.set_runner_machine` and by pairing by role, never from a
role the box merely names.

`routing.serves_refusal(runner, slug)` (part of `cannot_serve`; `turn_serves_refusal` for a whole turn)
refuses an agent's runner for any other agent, any repo turn and any agentless chat, and refuses any other
live runner on that `machine_id` for that agent. Both are restrictions a box declares on itself, so
neither a pin nor a session binding gets past it: a pinned or bound turn would otherwise hand the box the
agent's secrets through `device_keys.runner_serves_agent`. The same refusal is asked by `claim_next_turn`,
the coverage check (`claim_reach._refined_allows`), `unclaimable_queued_turns`, `runner_routes_agent`,
`agents_routed_to`, `inherited_orders` and the routing writes
(`apps/agents/api_routing.py::_runners_for_routing`, 422 with the reason).
`prime_machine_runners` loads the siblings once per claim and per report.
A runner's token reaches only itself and the agents it serves: `tests/test_runner_serves.py`,
`tests/test_runners_on_one_machine.py`.

### Setting up the runners on a machine (`Runners`)

The stack parameter `Runners` lists them (`cloud_runner.parse_runners`); `up.sh --runners` and
`runner/ec2/README.md` cover the operator side. What the code does not make obvious:

- **A runner cannot reach the instance role.** Root owns the units (`cloud_runner.py --install-runners`);
  an iptables rule rejects IMDS for every uid but root's. A runner signs in with a software key in its
  home (DPoP via `canopy vault runner-session`), never a PAT. Its ECDH key is a KMS key used only through
  `runner/ec2/kms_broker.py`, which maps the caller's `SO_PEERCRED` uid to that runner's key and nothing
  else. `box.env` holds the stack's parameters and no secret.
- **Pairing, once per machine.** One invitation lists every runner (`RunnerInvitation.runners`); one
  `pair-by-role` call with the instance-role proof makes one runner per entry in one transaction
  (`apps/harness/machine_pairing.py`). The creator must still have `agent.work` in every runner's
  workspace, or nothing is made.
- **Re-attach and adoption.** A runner already paired this way (its active key a runner software key, on
  this `machine_id`) re-attaches with the same role and name and no approval; its new key replaces the key
  row in place (`machine_pairing._rekey`), so runner id, bindings and zero-knowledge wraps are kept and
  tokens bought with the old key end. **Anything else needs the approved invitation that lists the
  runner**, above all adopting a box paired before it ran several runners: a role proof alone (which any
  process on a box without the IMDS rule can make) never re-keys a runner. A different ECDH key waits for a
  person and drops the stale wraps; a runner with no active key (revoked) is refused. Every re-key emits
  `runner.rekeyed` and notifies the runner's approvers.
- **Runner key slots are pinned** by `up.sh`, and runner keys are `DeletionPolicy: Retain`.

### Secrets off disk

- **On by default only for a runner with its own Unix user** (`cloud_runner.py` near `OFF_DISK_MIN_CLI`;
  `RUNNER_SECRET_FILES=0|1` overrides) and only with a new enough canopy CLI. The `secrets` health check
  says which path a box is on and why.
- A turn's env is resolved in memory, written 0600 to the turn's own tmpfs path and handed over as
  `CANOPY_AGENT_ENV_FILE`; it is removed in the turn's `finally` and swept at start. A turn whose env
  has a source but cannot be resolved is not run: it fails `[runner] could not resolve <slug>'s
  credentials`, canopy-web re-queues it, and the box stops claiming that agent for a growing interval
  (`CRED_BACKOFF_STEPS`, via the claim's `paused` list) so the re-queue is not spent at once.
- `canopy agent prepare --runner` writes no `.env` or key file (`CANOPY_TURN_ENV_ROOT`).
- **gog.** Each agent has its own `GOG_HOME` and keyring password derived from the runner secret
  (`cloud_runner.gog_env`, the same derivation as canopy's `gog_home`). The inbox poller, full turns and a
  confined turn's mail path get only that agent's variables, never another agent's.

### The AWS broker (`canopy-aws-broker`)

A runner's turn cannot reach the instance role (it reads the runner seed secrets). `runner/ec2/aws_broker.py`
(root, uid from `SO_PEERCRED`) assumes ONLY the read-only roles it names and hands the runner an hour of keys
in `credential_process` shape; its module docstring is the spec. **It never vends `labs-readwrite`**: the
agent a runner names is a claim, so that role comes only from canopy-web, which checks the agent and
`settings.CANOPY_AWS_ROLE_AGENTS` (`apps/agents/api_aws.py`). The canopy CLI asks `canopy aws credentials --role`.

### The job broker (`canopy-job-broker`)

A runner runs under `NoNewPrivileges`, so `runner/ec2/job_broker.py` (root, uid from `SO_PEERCRED`) starts a
plain job as `canopy-job` in a sandboxed unit; no request can name a path, a user or a property. What it
isolates is in its module docstring and `runner/ec2/README.md` § Plain jobs; how a job is written is in
[runner-flags-and-schedules.md](runner-flags-and-schedules.md) § Plain jobs; the self check that must print PASS
on a real box first is in [deploy.md](deploy.md).

## Opted-in projects (`ProjectOptIn`)

Canopy is opt-in per repo, and the choice is part of the person's account
(`apps/harness/models/runner.py::ProjectOptIn`, `GET|PUT /api/me/canopy-projects/`), never config on a box:
**every repo** (`everything`) or **only the repos named** (`projects`); with no row the answer is nothing.

- **One gate.** `Runner.project_names()` is what the box holds (`observed_projects()`, its own report) ∩
  what its OWNER opted into (`Runner.opts_in`). Every repo-turn route (`runner_target_q`, the workspace
  order, project chats, a pinned repo turn) and the usage scan's `scope` read it. A runner with no owner
  opts into nothing.
- **The box reports only what canopy wants**: the heartbeat reply's `report_projects`
  (`session_sync.report_projects`) is what the runner filters its session report by, before reading any
  tail or dialog. Mirroring follows: `session_sync.session_home` files no new session from a repo outside
  the opt-in, and a session mirrored before its repo left the opt-in stays but ships nothing more.
- **Agents are always canopy.** An agent's own repo is checked against `observed_projects()`
  (`cannot_serve`, the inherited order), never the opt-in.
- **Each repo belongs to workspaces.** `ProjectOptIn.workspaces` is `{repo: [slug, …]}` and covers the
  workspaces below each slug; a repo never assigned belongs to the home workspace of the box holding it, so
  enrolling a box in a second tree carries no repo there. `Runner.offers_project_in(repo, workspace)` is the
  question for repo work (held, opted in, assigned); a chosen box asks `project_assigned_to`
  (`canopy_sessions.placement`). It does not touch mirroring, usage, or an agent's repo.
- **What a person sees of a box's repos** is `runner_repos.annotate_for_caller`: to someone who does not
  administer the box, `capabilities["projects"]` and `GET /runners/{id}/projects` show only repos it offers in
  their workspaces, so `capabilities["projects"]` is never a list of what someone can start.
- A person who pairs a box starts at nothing until they choose; the `canopy_projects_off` alert
  (`apps/harness/alerts/project_opt_in.py`) tells them.
- **The canopy plugin needs no gate.** Its Claude Code hooks act only inside a canopy turn, an agent session
  or a direct turn; elsewhere they write local files only and send nothing to canopy-web.

## Adding a repo to a laptop (`RunnerProjectRequest`)

A laptop runs an agent in the emdash project named after its slug, and routing skips a laptop without one
(`routing.cannot_serve`, `InheritedOrder.missing_repo`). A person can ask the laptop to make that project
from canopy-web. The flow is in the module docstrings of `apps/harness/runner_projects.py` (the ask) and
`runner/canopy_runner/canopy_runner/add_project.py` (the box). Rules the code relies on:

- One row per (runner, project name), re-armed by asking again; the runner's owner or a runner admin only;
  laptops (`kind=emdash`) only, since a cloud box clones its agents itself (`cloud_runner._ensure_agent_clone`).
  Name and URL are validated on both ends (`NAME_RE`, `REPO_URL_RE`): they become a folder name and a
  `git clone` argument.
- Delivery is durable: pending asks ride the heartbeat reply as `add_projects`; `runner.wake` only makes the
  box beat sooner. The box never writes emdash's DB.
- The outcome is `added` when the project shows in the box's heartbeat report (observation discharges an ask
  whoever made the project), else `failed` with the box's reason; a failure never marks the runner not-ready.
- A repo that is not an agent's also needs the owner's opt-in before repo work routes to it.

## An agent's state document

`AgentStateDocument` is one JSON document per agent, a backup on canopy-web that the agent writes after each
change and reads only when its local copy is missing. canopy-web never looks inside it; it is not an analytics
input. Who may read and write, and the versioned write, are in the docstrings of `apps/harness/agent_state.py`
and `apps/harness/api_agent_state.py`. The security rules: a colleague's turn can write and cannot read (a
read returns everyone's records); a stored token of the agent's login, a session-scoped login on a laptop, a
finished or confined turn and a caller token are always refused; everyone else gets the 404 of a missing
agent. MCP serves it like any route and the call log records argument names, never the document; an agent
keeping people's records should deny-rail the tools for its model so only its script reads the document.

## Which agents a box may hold

`apps/agents/holding.py::runner_may_hold_agent(runner, agent)` (re-exported as
`agents.services.runner_may_hold_agent`) is true only when the runner's OWNER is one of the agent's admins
(`Agent.is_admin`: the owner, a workspace owner, or an `AgentAdmin`). A box running an agent holds its
prompt, its caller's token, its secrets and its owner's GitHub identity, so this gates the claim (agent turns
AND chats with the agent), credential resolve, the per-turn GitHub token (re-asked at issue, since an owner
can be demoted mid-turn), pins (which also need the pinner to be an agent admin or that runner's admin,
`access.may_pin_runner`), and every routing write (runner list, source rules, actor routes). Do not let a
runner-assignment row be the trust boundary: a workspace editor writes those rows. A teammate whose laptop
should run an agent's work needs an admin grant on that agent. An agent's own canopy login is not a holder.

A **sensitive-data box is dedicated**: one with a declared `sensitive_data` flag OR reporting a `data_mode`
(`runner_requirements.is_sensitive_data_box`, either alone, unlike `in_force`, which needs both) holds only an
agent with a `data: sensitive` host site, whoever owns it, so no other agent's full profile, secrets or GitHub
identity shares a box with a sensitive conversation. Guarded by `tests/test_sensitive_data_box_is_dedicated.py`.
