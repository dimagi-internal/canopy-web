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
   whom `access.decide` gives the whole agent (`apps/agents/licenses.py`). The people
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
(`runners._other_tree`; divisions of its root are fine). A cloud runner is one
isolation unit (below), so a box that serves two trees runs two runners. A laptop
(`emdash`, `remote`) may span trees: it has one desktop user, so nothing on it
separates them, and `RunnerOut.roots` / `spans_trees` let `/supervisor` say so on
its row. `roots` lists only the trees the caller is a member of.

## Isolation units: one runner per unit on a cloud box

A cloud box runs one runner per **isolation unit**: a workspace tree, or a single
agent given a unit of its own. The box declares it in `capabilities.unit` —
`{"kind": "tree", "root": "dimagi"}` or `{"kind": "agent", "slug": "eva"}`
(`Runner.unit()`). Runners on one box share `Runner.machine_id`, the canonical IAM
role ARN the box proved: set by `runners.set_runner_machine` when an aws-iam device
key is approved and by pairing by role, never from a role the box merely names.

`routing.unit_refusal(runner, slug)` — part of `cannot_serve` — refuses:

- **"<runner> is the agent unit for S"**: an agent unit asked about any agent but S —
  and, for a whole turn (`turn_unit_refusal`), any turn that does not act as S: a
  repo turn, an agentless chat. Its `runner_target_q` targets only S's turns and chats;
- **"S has its own unit on this machine (<runner>)"**: any other live runner with the
  same `machine_id` as the agent unit for S.

Both are restrictions a box declares on itself. Unlike the rest of `cannot_serve`,
neither a pin nor a session binding gets past it: `claim_next_turn` and the
coverage check (`_refined_allows`, so `turn_reach` agrees) ask it above both (a
pinned or bound turn would otherwise hand the box the agent's secrets through
`device_keys.runner_serves_agent`), `unclaimable_queued_turns` reports such a turn
with the unit's reason, `runner_routes_agent` and `agents_routed_to`
ask it, `inherited_orders` drops such a runner for that agent, and the routing
writes (`agents/api.py::_runners_for_routing`) refuse it with 422 and the reason.
The topology lists a box's units together (`machine`, `unit` on each runner row).
`prime_machine_units` loads the machine siblings once per claim and per report.
Tests: `tests/test_runner_units.py`.

### Units on the box (`RunnerUnits`)

- **Units.** The stack parameter `RunnerUnits` (`tree:dimagi,tree:marshellis,agent:eva`;
  empty = one unit `tree:<RunnerWorkspace>`) lists them; `cloud_runner.parse_units` names
  them. The tree unit of the box's own workspace keeps the **box's** name (so a box moved
  to units keeps its runner); every other unit is `<box>-<unit>`. Each is a Unix user
  `cu-<unit>` (home 0700), a `canopy-runner@<unit>.service` (`User=cu-%i`,
  `NoNewPrivileges`, `PrivateTmp`), its own `/etc/canopy-runner/<unit>.env`, state file,
  in-flight marker, clones, Claude logins and transcripts. The runner code and the
  updater's clone are root's and read-only to units.
- **Root's parts** (`cloud_runner.py --install-units`, run by cloud-init or by an operator
  moving a live box): `canopy-units.service` (every boot: `--setup-units` makes users,
  env files, each unit's 32-byte gog secret, `/run/canopy/<uid>`, `units.json`, and the
  iptables rule that rejects IMDS for every uid but root's), `canopy-kms-broker.service`
  (`runner/ec2/kms_broker.py`), `canopy-pair.service` (`--pair-units`) and the updater
  (as root). `box.env` holds the stack's parameters and **no secret**.
- **Identity.** A unit cannot reach the instance role. Its signing key is a software key
  in its home made by the canopy CLI (`canopy vault device --public-jwk --kind software`);
  it signs in with `canopy vault runner-session` (DPoP), never a PAT. Its ECDH key is a
  KMS key of its own in the `<stack>-ecdh` stack (`alias/canopy-runner-ecdh/<role>/<unit>`;
  the box-named unit keeps the box's original key), used only through the broker, which
  maps the caller's `SO_PEERCRED` uid to that unit's key and nothing else.
- **Pairing, once per box.** `up.sh --units …` makes ONE invitation listing every unit
  (`RunnerInvitation.units`, each with its KMS public key); the approve page lists them.
  `canopy-pair` collects every unit's public keys and makes ONE `pair-by-role` call with
  the instance-role proof (`apps/harness/unit_pairing.py`): one transaction, one runner
  per unit (owner = the creator, its workspace, `machine_id` = the role,
  `capabilities.unit` from the invitation) with an ACTIVE software key. The creator must
  still have `agent.work` in **every** unit's workspace, or nothing is made.
- **Re-attach and adoption.** A unit that is already a unit — its active key a unit
  software key, its runner on this `machine_id` — re-attaches with the same role and
  unit name and no approval: its new signing key replaces its runner's key row **in
  place** (`_rekey`), so the runner id, bindings and every zero-knowledge wrap on that row
  are kept, and tokens bought with the old key end. **Anything else needs the approved
  invitation that lists the unit** — above all *adopting* a box paired before units,
  whose key is the instance role itself: a role proof alone (which any process on a box
  without the IMDS rule can make) never re-keys a runner. Adoption finds the runner by
  `machine_id` (or its role's aws-iam key) and name, turns its aws-iam key into the
  unit's software key, and its KMS ECDH key — the same public key, now from the broker —
  keeps its approval and wraps (`up.sh --in-place` makes that invitation). Every re-key
  emits `runner.rekeyed` and mails/pushes the runner's approvers. A different ECDH key
  waits for a person and drops the stale wraps; a unit with no active key (revoked) is
  refused. `capabilities.unit` is server-owned once set: a capabilities write can neither
  replace nor clear it.
- **Unit key slots are pinned** (`up.sh`): each extra unit keeps the `-ecdh` slot
  (`Unit1..4`) it was given — a re-run without `--units` keeps every slot, a reordered
  list keeps each unit where it is, new units take free slots, and a slot is dropped only
  with `--remove-unit <id>`. Unit keys (and the box's own) are `DeletionPolicy: Retain`.
- A unit's token reaches only its own runner and the agents that runner serves:
  `tests/test_runner_units_isolation.py`.

### Secrets off disk

- **On by default on an isolation unit only** (`CANOPY_UNIT`, or a box with
  `/etc/canopy-runner/units.json`). A box built before units keeps its
  `~/.<slug>/.env` files until an operator opts it in with `RUNNER_SECRET_FILES=0`
  (a systemd drop-in); `RUNNER_SECRET_FILES=1` keeps the files anywhere. Also gated on
  the installed canopy CLI (>= 0.2.736, `cloud_runner.OFF_DISK_MIN_CLI`): an older CLI
  cannot read `CANOPY_AGENT_ENV_FILE`, so the files stay. The `secrets` health check
  says which path a box is on and why.
- A turn's env is resolved in memory (`canopy cred env --runner --print-json`, cached per
  agent for 5 minutes), written 0600 to `/run/canopy/<uid>/<turn>/env` (a box without
  units that opted in: `/dev/shm/canopy-<uid>`), and handed to the turn as
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
  `HKDF-SHA256(unit secret, "gog:"+slug)` (`cloud_runner.gog_env`, the same derivation as
  canopy's `gog_home`). The inbox poller, full turns and a confined turn's mail capability
  get only that agent's three variables — never another agent's, never the secret's path.
  A box without a unit secret keeps its box-wide `GOG_*`.

`capabilities` (agents, projects) is a caller-supplied routing HINT declared by
the box; it is intersected with the tenant gate for **project and session** turns,
never a substitute for it — agent turns route by `RunnerAssignment` (and
`runner_may_hold_agent`).
Agent turns are tenant-gated through `routing.agent_tenant_q` (no NULL-workspace
leg — always AND it with `agent__isnull=False`), repo turns through
`Turn.workspace`, session turns through `chat_session.workspace`.

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
(`routing.cannot_serve`). Its isolation unit (above), and two self-reports: a laptop driving emdash whose reported
`projects` lacks the agent's slug (never-reported = unknown, not empty), and any
box naming the agent in `capabilities["unprepared"]` — the cloud runner's
heartbeat `agents_unprepared`, the agents its bootstrap or lazy prepare tried and
has no clone of. It is a FLOOR on the composed list in
`routing.assignment_rows_for` (`serves=`), applied after strict truncation like
`requires`: the box neither claims nor blocks a lower rank, a strict rule whose
every box cannot run it waits, and `unclaimable_queued_turns` reports such a turn
as `config`, quoting what each box said. A pin still lands on a box that reports
it cannot run the agent (a drill exists to find this out), but never on one whose
unit refuses it.
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
2. Lazy sweeps: expired leases, `schedule_turns.release_stale_occurrence_turns_all`,
   `skip_late_scheduled_turns` (see Scheduled turns).
3. Candidate filter: pinned here, or (`runner_target_q` ∧ routing kind); a cloud
   runner never takes `local_only`. A **directed-only** box
   (`capabilities.directed`, `Runner.directed_only`) targets only the sessions
   bound to it, so it claims just its pins and its bound chats — no routed agent
   turn (even with an enabled assignment row), no repo turn, no unbound chat, no
   inherited order. Every routed list drops it (`assignment_rows_for`, the
   workspace order, `inherited_orders` → `InheritedOrder.directed`), so it neither
   claims nor counts as a better rank; it routes and prepares no agent. Box-declared
   (`RUNNER_DIRECTED=1`) and trusted as such: it can only make a box take less. `profile_q`: a confined (caller) turn only to a
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
  conversation's context.
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
(`canopy_sessions.services.may_place`, the one placement rule).

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

## Runner flags and host requirements (ZDR)

- A **host** requires flags of its visitors' conversations with
  `CANOPY_HOST["RUNNER_REQUIREMENTS"] = ["zdr"]` (SDK `dimagi-canopy`); the SDK signs
  them into the visitor assertion (`canopy_runner_requirements`), and canopy stamps
  them on the session's `metadata["runner_requirements"]` — server-owned,
  union-only, never lifted by a later token that omits them. A host grant carries
  its own requirements too (`runner_requirements.requirements_of_grant`).
- A **runner admin** declares a box's flags (`RunnerFlag`,
  `PUT /api/harness/runners/{id}/flags`, audited with `declared_by`). canopy cannot
  verify a flag — it records who vouched. Nothing a runner reports can set one.
- `apps/harness/runner_requirements.py` is the one reader. The claim, placement,
  the unclaimable report, turn status (`requires`) and the `site_call` gateway
  (`apps/tokens/host_gateway.py`) all refuse a runner missing a flag — above pins,
  and with no fallback: a requirement is a floor applied to the composed list after
  strict truncation, so a turn nothing can serve says so and waits. Guarded end to
  end by `tests/test_sdk_round_trip.py` and `tests/test_runner_requirements*.py`.
  Spec: `docs/superpowers/specs/2026-09-30-zdr-runners-design.md`.

## Scheduled turns

The scheduler is a producer of turns, not an execution engine (no celery/beat).
`AgentSchedule` (`apps/harness/models.py`) holds the cron server-side, and
**canopy-web fires it itself**: `apps/harness/schedule_clock.py`, an asyncio loop in
the web process's ASGI lifespan (like `pat_rotation_scheduler`), evaluates every
enabled schedule each `CANOPY_SCHEDULE_TICK_S` (60 s; a cache key keeps N web tasks
to about one tick) and calls `schedule_turns.fire_schedule` for each due slot. So a
workspace whose agents run only on cloud boxes, or whose laptop is shut, still fires
its schedules. A laptop runner still evaluates them too
(`runner/canopy_runner/canopy_runner/schedules.py`, `POST …/schedules/{id}/fire`);
both reach the same slot key, so whichever fires second collapses onto the first's
turn. Firing only queues a turn — which box runs it is decided on the claim.

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
  the lease). Runs lazily on the CLAIM tick, not the fire tick (a weekly fire tick
  could never honour a short grace).
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

**An agent acts on GitHub as its OWNER (or, while the owner has lent none, one of
its ADMINS), through a delegation — never a box credential.** Boxes hold no GitHub token. `AgentDelegation` (person, agent,
service) is the third credential kind beside the agent's own vault and the
workspace's shared vault: a person lending their identity to one agent
(`apps/agents/delegations.py`).

- The owner or an admin pastes a fine-grained PAT on the agent's Settings → Credentials →
  GitHub (canopy pre-fills GitHub's creation form, `delegations.create_url`; GitHub
  has no API to mint one). Its repository selection IS the agent's scope. The
  agent's owner or an admin (`Agent.is_admin`) may set it, and it is refused unless it can push and open a pull
  request on the agent's repo (`check_repo`: probes that GitHub answers 422 for a
  branch/commit that cannot exist, 403/404 for no permission — nothing is created).
- In force (`delegation_for`): the owner's, while they are a member of its
  workspace; else the most recently saved one whose person is still an admin. The
  owner's always wins. Salesforce delegations stay owner-only.
- Issued per turn by `POST /api/harness/runners/{rid}/turns/{tid}/github-token`:
  only for a turn that runner is executing, re-checking `runner_may_hold_agent`;
  the principal is resolved server-side (`delegations.turn_agent`); **409 with the
  reason and no shared fallback** when absent or expired; always refused for a
  confined (caller) turn. Returns the token, the lender's git identity (noreply
  email) and `requested_by` for a `Requested-by:` trailer — the caller is recorded,
  never the principal. Stopping bad writes is the agent's own gating, not the token
  layer's. Each issue is an `agent.github.issued` event.
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
(`tests/test_agent_delegations.py`). Salesforce uses the same model with an agent
as lender (`delegations.set_salesforce`).

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
  every sweep tick.
- Cloud runner: `CANOPY_WEB_PAT` in the turn env + `~/.canopy/agent-token/turn/<tid>.token`
  for the MCP headers helper. Laptop: the canopy CLI asks on demand with the box's
  runner token and the session's turn id.

Spec: `docs/superpowers/specs/2026-10-21-turn-scoped-agent-token-design.md`
(`tests/test_turn_agent_token.py`).

### The per-user GitHub App (`apps/tokens/github_app.py`)

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

## A cloud runner's Claude login (setup-token vs. full login)

A cloud runner's bundle holds `claude setup-token` tokens (`subscription-1`,
`subscription-2`) and an API key, tried in that order. A setup-token is
**inference-only**: the CLI never loads claude.ai connectors (Claude Docs and the
rest) for it — `[claudeai-mcp] Missing user:mcp_servers scope`. So a turn on one
cannot read a Claude Doc however the doc is shared.

A full `claude auth login --claudeai` fixes that, and a box holds **a list of
them** (`RunnerLogin`), tried in order before any backup token. Each is signed in
through the browser relay (`RunnerMint` with `slot: box` and the login's
`login_key`; URL out, code back) and **stays on the box** — its refresh token
rotates, so a copy anywhere else would break both. canopy-web holds only the list:
which logins, in what order, and what the box last reported about each
(`signed_in`, `account`).

- **On the box**, each login has its own config directory: `default` is the CLI's
  own (`~/.claude`, state in `~/.claude.json`); every other login is
  `~/.claude-logins/<key>`, holding its own `.credentials.json` and `.claude.json`
  with everything else symlinked to `~/.claude` — so transcripts (`projects/`),
  plugins and settings stay in one place whichever login a turn ran on. Selecting
  a login sets `CLAUDE_CONFIG_DIR` (unset for `default`) and clears both auth env
  vars; concurrent turns each keep the directory they started with. A login is
  in the cascade only when its directory holds a login with the
  `user:mcp_servers` scope; a usage cap falls through to the next login, then the
  backup tokens.
- **Add** (`POST …/logins`) appends a row and starts its sign-in; the row reads
  "not signed in yet" until the box reports otherwise. **Remove**
  (`DELETE …/logins/{key}`) drops the row; on its next bundle read (≤5 min) the
  box deletes that login's directory (for `default`, only its `.credentials.json`).
  **Reorder** (`PUT …/logins/order`) takes every key, first to last.
- **The box reports** (`POST …/logins/report`) which listed logins are signed in
  and as which account, whenever that changes. A default login the list has never
  held (signed in before the list existed, or by hand) is reported with
  `adopt_default` and joins the list first; `~/.claude-logins/.default-adopted`
  then records that a missing `default` means removed.

**Long-lived tokens keep the box alive.** A full login renews itself, and a renewal
can fail; a box whose backup tokens have all been removed then stops. So:
- The page always offers **Create a long-lived token** (`POST …/mint` with `slot:
  primary|secondary` runs `claude setup-token` on the box; the token lands in that
  slot). Its buttons say "token", never "Sign in with Claude": offered side by
  side under the same words, an operator takes a token believing it is the full
  login.
- `POST …/credential` refuses (422) a write that would clear the **last** backup
  token; replace it first. The page disables that Remove.
- A box with no usable credential at all waits in `fetch_and_stage_credential`,
  heartbeating as online but not ready with a note that says to sign in, and
  serves the browser sign-in from there — it does not read "stale".

The runner reports a `claude.login` health check (ok = signed in on the box as
the first login's account, warn = backup tokens only). Tests:
`runner/ec2/tests/test_box_login.py`, `tests/test_runner_logins.py`,
`tests/test_runner_mint.py`, `tests/test_runner_credential.py`.

**Full logins never copy.** `wire.sh` copies a source box's long-lived tokens and
API key (by name, printed), never its full logins, and prints each one it left
behind with the new box's credentials link. A cloud box with no signed-in full
login says so on its page: logins don't copy between runners, add one here.

### The credential probe: "ready" means a credential answered

A stored value is not a working one. So the box asks each credential one short
question with no tools (`cloud_runner._probe_credential`: `claude -p "Reply with
OK" --output-format json --max-turns 1` in a scratch dir, with that credential's
environment built as a copy — never `os.environ`, turns run concurrently; 90 s
timeout) and classifies the answer with the same `_is_usage_cap` /
`_is_auth_required` turns use: `ok`, `capped` (+ the reset when readable), `dead`,
or `unknown` (timeout, crash, unreadable). `CLAUDE_PROBE=0` turns probing off
(every probe answers `unknown`).

- **When:** after `fetch_and_stage_credential` stages a bundle, synchronously,
  then reported at once in a heartbeat (the next one is otherwise after the
  bootstrap); after a re-read changes the bundle, only the new or changed entries,
  in the background — and every entry when the bundle was re-written unchanged
  (its `updated_at` moved); after a login is signed in again or the default login
  is adopted (its value, a config dir, does not change); for a capped entry once
  its reset has passed and a dead one every `DEAD_REPROBE_SECONDS` (10 min) — the
  main loop's poll tick; and when a turn's own failure is ambiguous (below).
  Background requests queue on one prober thread. Each check's detail ends
  `· probed <ISO>`. Never on a plain heartbeat — probes spend usage. A turn's own outcome
  updates the same state for free (a turn that ran marks its credential `ok`; a
  classified cap or dead marks it so). A promoted subscription carries its probe.
- **Health:** one check per credential, `claude.probe.<label>` (`subscription-1`,
  `subscription-2`, `api-key`, `box-login`, `box-login:<key>`): ok / warn "at its
  usage cap — resets <ISO>" / fail "not signed in — someone has to sign it in
  again" / warn "could not check: …". The runner page colours each token and login
  dot by it (`RunnerCredentials.tsx` `PROBE_LABEL` / `loginProbeLabel`, pinned to
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
- **A bare ACP refusal is probed.** Over ACP a capped token and a signed-out one
  both fail with `runner error (acp): authentication required`. When nothing
  earlier in the turn capped, the box probes that credential: capped → it is a cap
  (reset from the probe, the probe's line in the turn note, the out-of-usage park
  and alert); dead or inconclusive → not signed in, as before. The CLI's own
  sentences (`_AUTH_REQUIRED_MARKERS`) still win without a probe.

**The alerts reach a person.** The box posts `claude_auth_required`,
`claude_usage_exhausted` and `claude_api_key_fallback` to `POST /api/events/` with
its runner token; with no workspace pinned they file in `Runner.workspace`, and
each also mails and Web-Pushes the runner's approvers (owner + runner admins)
once per (runner, alert) per UTC day, linking to the runner on `/supervisor`
(`apps/harness/credential_notify.py`). Tests:
`runner/ec2/tests/test_credential_probe.py`, `runner/ec2/tests/test_auth_required.py`,
`tests/test_credential_notify.py`, `tests/test_realtime_runner_consumer.py`.

## How a runner signs in (keyless)

- A paired box signs in **as itself**: it trades a fresh proof of its APPROVED device key (an EC2 box's instance role; a laptop's Secure Enclave/TPM key) for an hour-long `RunnerAccessToken` at `POST /api/runner-sessions/`, renews it before it runs out, and uses it for REST and its `ws/runner/<id>/` socket. Nothing long-lived lives on the box, and nothing in Settings → Tokens can cut a runner off.
- **One protocol, two channels.** The `ws/runner/<id>/` socket's `heartbeat` and `finish` frames are read and applied by the same functions as `POST …/runners/{id}/heartbeat` and `POST …/turns/{id}/finish` (`apps/harness/runner_protocol.py`, over `HeartbeatIn` / `TurnFinishIn`): a WS beat carries `degraded`/`note`, `host`, `projects` and `mailboxes_readable` with the REST rules (absent leaves a list alone; a malformed field costs that field, never the beat), and a WS finish takes only `done`/`failed`/`cancelled` and records `session_key` (a refused frame acks `ok: false` with an `error`). Who may act as a runner is `runner_protocol.acting_runner_q` — its owner; a runner with no owner connects for nobody and is in nobody's `/supervisor` snapshot.
- The token opens the runner protocol only (`apps/harness/runner_auth.py`) and authenticates as `Runner.owner`, so every owner-scoped runner gate is unchanged. Revoking the device key (Settings → Security) stops the box at once — the socket re-checks its credential on every frame (`channels_auth.still_signed_in`).
- **A laptop installed from a download pairs in the browser and holds no PAT** (`apps/harness/api_runner_pairings.py`, `runner/canopy_runner/canopy_runner/browser_pair.py`). The box makes its device key, then `POST /api/harness/runner-pairings/` with the public key and a DPoP proof of it, the loopback port it listens on, its hostname, account and whether emdash is present; it shows the returned code and opens `/pair/<code>`. The person signs in, checks the code, picks a workspace where they hold `agent.work` and a name, and approves behind a passkey step-up (a token is refused). The approval's one-time secret comes back only as a redirect to `http://127.0.0.1:<port>/paired` on the machine whose browser approved, so an approve link sent to someone else gives the sender nothing. The box claims (`…/{id}/claim`, DPoP of the same key plus the secret) and gets a `Runner` (`kind=emdash`, runtime `claude-cli` when there is no emdash) whose device key is already ACTIVE, ECDH key approved with it; the approver must still hold `agent.work` there. A key canopy-web already has ACTIVE on a live runner re-attaches instead; any other known key, an expired, declined or used pairing, or a wrong secret or proof is one uniform 401. Pairings last 15 minutes, one live per key. `runner.json` then has no token, and `~/.canopy/device-key/status.json` is written as canopy's CLI writes it so `canopy vault` shares the key.
- A developer laptop (`install-runner.sh`) pairs with its person's PAT (`CANOPY_TOKEN`), which also registers its key. A **cloud box pairs by invitation and holds no PAT** (below). A box paired by invitation, and every isolation unit, has **no PAT fallback**: without a token it reports itself unauthenticated. Only a cloud box from an older stack that still has `CANOPY_TOKEN` (and never paired by invitation) falls back to it while no approved key can get a token (`runner/ec2/cloud_runner.py::_bearer`). An agent turn never inherits the box's credential — it acts with its own `CANOPY_WEB_PAT`.
- **Pairing a cloud box by invitation** (`apps/harness/api_runner_invitations.py`). `runner/ec2/up.sh`, with the operator's own workbench token, creates a `RunnerInvitation` once the stack's instance role exists: `{name, workspace, role_arn, ecdh_public_jwk}` (the `<stack>-ecdh` KMS public key), in a workspace where the operator has `agent.work`. It lasts an hour, and one live invitation exists per (role, name): your own new one replaces yours, and someone else's live one is never replaced (409). The approve link is printed, mailed and pushed to the creator. **Only the creator approves it**, from a browser behind a passkey step-up (`stepup.require_recent_step_up`); a token gets 403, so neither `up.sh` nor the box can approve its own pairing. The box (no state file, no `CANOPY_TOKEN`) calls `POST /api/harness/runners/pair-by-role` with an instance-role proof bound to that URL (`aws_iam_proof.verify`): waiting → 202; approved → one transaction creates the `Runner` (owner = creator, `workspace` = the invitation's, enrolled there by `Runner.save`), its `aws-iam` `RunnerDeviceKey` already ACTIVE (and the invitation's ECDH key approved with it), and marks the invitation consumed. The creator must still have `agent.work` there at that moment. Every other answer (no invitation, expired, used, another name, another ECDH key, a bad or replayed proof) is one uniform 401 that creates nothing. The box then signs in at `POST /api/runner-sessions/` as any keyless box does.
- **A replaced instance re-attaches.** The role is the box's identity (`jkt` = sha256 of the role ARN), and `up.sh --allow-instance-change` keeps the stack's role. So `pair-by-role` from a role with an ACTIVE key on a non-retired runner of the same name returns 200 with that runner's id; no invitation is needed or used, and `wire.sh` has no predecessor to retire. A role whose runner was retired needs a new invitation from that runner's owner; the old key row is replaced, so `jkt` stays unique. A key for the role on another owner's runner (live, revoked or retired) is never erased, and a pending or revoked key on a live runner is never replaced either: a revocation is undone by its owner, not by re-inviting. Each is the uniform 401.
- A box of isolation units pairs every unit in one call (`units` on the invitation and on `pair-by-role`; see "Units on the box" above).
- Cloud runner: `runner/ec2/cloud_runner.py::_bearer` signs its instance-role proof itself; an isolation unit runs `canopy vault runner-session` with its own software key. Laptop runner: `runner/canopy_runner/canopy_runner/runner_token.py::RunnerCredential` runs `canopy vault runner-session` when the canopy CLI is installed, else signs the proof itself (`device_proof.py`, the same key files and formats as canopy's `orchestrator/device_key.py`: `se.key` through `canopy-device-key`, the TPM key `canopy-device-key`, or `sw.pem`), shared by its REST client and wake socket. Both renew 10 min before expiry, retry a 401 once with a fresh token, and fall back to the PAT only while no approved key can get one.

## The runner software

A runner is an installed package, never a checkout, and updates itself to what is deployed.

**Laptop install from a download.** `/get-started` and the topology page offer the installers (`GET /api/harness/runner-releases/downloads`, `/download/runner/<platform>`: a signed-in person gets a 302 to a 15-minute presigned S3 URL). macOS (`macos-arm64`, `macos-x86_64`): a DMG holding `Canopy Runner.app`, which copies itself to `~/Applications` and runs `canopy-runner setup` from `Contents/Resources/runtime` — browser pairing when there is no `runner.json`, `~/.canopy/bin/canopy-device-key` linked into the bundle, and the `com.canopy.runner`, `.updater` and `.menubar` LaunchAgents pointing at the app's version-free path. Windows (`windows-x86_64`): an Inno Setup installer that never asks for admin, installs `%LOCALAPPDATA%\Programs\Canopy Runner\versions\<sha12>` behind a `current` junction and runs the same `setup`, which registers `\Canopy\canopy-runner` (at logon, `pythonw.exe … run --log-file`) and `\Canopy\canopy-runner-updater` for the current user. Each bundle carries its own CPython (python-build-standalone) with the runner wheels, Node.js, the CDP sidecar's `node_modules` and the ACP adapter, and on macOS the menu-bar app and `canopy-device-key`; nothing is fetched at install. The runner finds its bundle from `sys.executable` (`bundle.py`) and puts the bundle's node and adapter first. Build: `.github/workflows/runner-release.yml` and `runner/packaging/` (README lists the signing secrets). Every runner-source sha that reaches main is built, and on main uploaded to `runner-releases/<sha>/` in the attachments bucket with `manifest.json` written last; `signed`/`notarized` in the manifest say what actually happened. The upload needs `UploadRunnerReleasesOnly` on the deploy role (`deploy/aws/deploy_roles.py`, applied by hand). Spec: `docs/superpowers/specs/2026-10-19-runner-installers-design.md`.

**The two installs share one runner.** The downloaded app and `install-runner.sh` use the same `runner.json`, device key and job labels; whichever installed last runs it, and switching never re-pairs. `setup` says when it takes over from a developer install; `install-runner.sh` replaces a released app in `~/Applications` and a `canopy-device-key` link into it. `~/.canopy/hold-updates` holds a box on what it runs, for both updaters. Testing paths (second macOS account, unreleased workflow artifacts): `runner/packaging/README.md`.

**Developer install.** `runner/canopy_runner/scripts/install-runner.sh` installs and updates: it `git archive`s a ref, builds and `uv tool install`s three wheels, provisions the CDP sidecar's node deps, and renders + loads a generated launchd plist. The daemon runs from a tool venv no checkout can change. Re-running restarts the daemon (not mid-turn). `--ref <branch>` installs another ref; `--no-launchd` skips the daemon; `--workspace <ws>` pairs into a workspace. Spec: `docs/superpowers/specs/2026-07-28-runner-as-installed-package-design.md`.

**Auto-update (both kinds).** A separate job — `com.canopy.runner.updater` (launchd, every 30 min; `--no-auto-update` to skip) on a laptop, `runner/ec2/update_runner.sh` (systemd timer) on a cloud box — installs the **deployed** `expected_code_sha` for that runner kind, never `origin/main`; defers while a turn is in flight (`~/.canopy/in-flight`); and is read-only against the control plane (`canopy-runner update-check` → `current|stale|busy|unknown`, never a heartbeat). A heartbeat reporting a differing sha gets a `runner.update_available` frame that kicks the updater, so the timer is only the fallback. A downloaded install's updater is `canopy-runner self-update --if-stale`: it asks `GET /api/harness/runner-releases/current?platform=` with its own runner token (the route serves the release for the deployed `RUNNER_CODE_SHA`; none built = 404 = `unknown`), downloads the update artifact, checks its sha256 (and on macOS the code signature's team against the running app's), then swaps `~/Applications/Canopy Runner.app` (macOS) or runs the installer with `/UPDATE` and flips `current` (Windows), re-runs `setup --no-pair` and restarts the runner. Laptop log: `~/.canopy/updater.log`; bootout the updater before testing a branch on a box. The cloud box installs via `git show <sha>:runner/ec2/cloud_runner.py` from its on-box clone; Secrets Manager is a first-boot seed only; override with `canopy-runner-update --ref <ref>`. Spec: `docs/superpowers/specs/2026-07-30-cloud-runner-auto-update-design.md`.

**Code provenance.** `code_sha` is the last commit touching that runner kind's own source paths, not repo HEAD — laptop: the runner package and its scripts, the menu-bar app, the device key, brand assets and the in-repo `canopy_cron`/`canopy_transcript` packages; cloud: `cloud_runner.py`, `bootstrap_agents.sh`, `update_runner.sh`, `kms_broker.py`, `canopy_transcript` and `canopy_acp` (both lists in `.github/workflows/deploy-labs.yml`). The server holds `RUNNER_CODE_SHA` / `RUNNER_CLOUD_CODE_SHA` (computed by the deploy workflow, `fetch-depth: 0`); `RunnerOut.expected_code_sha` serves the one matching the row's `kind`, and `/supervisor` alerts only when both sides are non-empty and differ (empty = unknown).

**The cloud runner** (`runner/ec2/cloud_runner.py`) polls claim every 15s and drains the queue, executes via `claude -p` (or ACP with `RUNNER_EXECUTOR=acp`, default off), declares repos via `RUNNER_PROJECTS`, and runs the stream/backfill contract on a 3s clock. A cloud box learns of an agent only by bootstrapping, so registering one (or changing its repo) requests a refresh on the cloud boxes that will take its turns (`apps/agents/setup.py::request_setup`); until that box posts the agent's bootstrap report, the agent's setup checklist shows its runner step as waiting. And if a turn for an agent the box has no clone of arrives first, the box bootstraps before running it (`cloud_runner._ensure_agent_clone`, once per agent per `LAZY_BOOTSTRAP_RETRY`) rather than running it in a bare scratch dir. `bootstrap_agents.sh` does the box's half (tooling, gog's file keyring, the canopy plugin + CLI) and then runs `canopy agent prepare --runner` per agent (clones `dimagi-internal/<slug>` into `/opt/agents`, env, plugins, Gmail, a readiness report); it runs from `main()` after the credential is staged and is not `set -e`, so one agent's failure does not stop the rest. Spec: `docs/superpowers/specs/2026-07-25-cloud-agent-bootstrap-design.md`.

## Runner readiness (the not-ready marker)

A failed turn writes `~/.canopy/not-ready` (`runner/canopy_runner/canopy_runner/readiness.py`,
`mark_failed`); `readiness.compute` reports it on the heartbeat as `ready=False`, a
not-ready runner is not `Runner.is_available`, and lower ranks in every cascade
take its work. `mark_ok` clears it only after a turn executes, so the marker **expires after
`MARKER_TTL_SECONDS` (15 min)** — otherwise one transient failure exiles a box that
keeps heartbeating ONLINE. Expiry is retry-with-backoff: a genuinely broken box
fails its next turn and re-marks itself. The CDP probe gates independently
(`compute`), so expiry cannot mask emdash being down.
