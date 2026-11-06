# Runners: routing, the claim and turn mode

How a queued `Turn` finds a box: the one rule for which agents a box runs, which CLI runs a turn, the claim (`claim.claim_next_turn`), the off-request clock (`apps/common/ticks.py`), an agent's runner list, the workspace default order, project chats and turn mode per rule. Code: `apps/harness/claim.py`, `apps/harness/routing.py`, `apps/harness/clis.py`.

**See also:** [runners.md](runners.md) (how an agent runs, the tenant gate, opt-in projects, which agents a box may hold), [runner-flags-and-schedules.md](runner-flags-and-schedules.md), [runner-credentials.md](runner-credentials.md), [runner-fleets.md](runner-fleets.md).

## Which agents a box runs — one rule

`routing.runner_routes_agent(runner, agent)` answers "could this box ever claim
a turn of this agent?" with the claim's own static gates and nothing else:

1. the box is not retired, and not directed-only (below);
2. the agent's workspace is in the box's tenant (`runner_tenant_slugs`: enrolled
   there AND its owner may run work there);
3. its owner may hold the agent (`runner_may_hold_agent`; [runners.md](runners.md) "Which agents a box may hold");
4. the agent routes to it: an ENABLED `RunnerAssignment` row (its default list
   or any source/actor rule), or the workspace order it follows.

Availability, the cascade, a turn's requirements and the box's own report
(below) decide WHICH of those boxes takes a given turn. They never decide whether
a box runs the agent at all — so a box that failed to set an agent up keeps it on
its roster and retries at its next bootstrap.

**A box never claims a turn of an agent it reports it cannot run**
(`routing.cannot_serve`). What it serves on its machine ([runners.md](runners.md)), and two self-reports: a laptop driving emdash whose reported
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
  up for), keep those the box has signed in, for a caller's confined turn (stored `Turn.capability`) those it
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
  is refused at every send path (`clis.conversation_refusal`, 422; Slack says so in the thread).
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
- **A direct session** (`POST /api/harness/local-turns/`) is a person's own Claude Code
  session: its turn records `cli="claude-code"`.
- **A prompt's plugin command is parsed once** (`apps/harness/invocations.py`): a first
  line `/<plugin>:<skill> <args>` is the canonical text canopy-web stores, shows and
  builds (`invocations.command` — mail turns, dispatches; a profile's `entry` must
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
   setting ([runners.md](runners.md) "Allowlisted agents, and directed-only"), not the box's. `profile_q`: a confined (caller) turn only to a
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
5. **The Turn Context is asked again**, from today's facts (`turn_context.at_claim`, with the enqueue's own question
   kept in `origin_ref.queued_for`; access.md § Two moments). Refused: the turn is closed `cancelled`, never claimed,
   its note the refusal plus `refusals.CHANGED_WHILE_QUEUED`. Otherwise the new stamp is written to `Turn.capability`
   before the payload is built, and a box that cannot confine the new answer passes it over.
6. Turn mode decided and stamped (`turn_mode.for_turn`), an admission `auto` rule
   may raise a standing manual to auto, then admission rejects
   (`apps/admission`, see `access-direct-use-and-admission.md` → "Admission rules"), then the conditional
   update to CLAIMED inside its own `transaction.atomic()` (an `IntegrityError` from
   `one_executing_turn_per_session` means another box won that conversation). **An agent
   is not a lane:** any number of one agent's turns may be executing at once, each on
   its own worktree; only a CONVERSATION serializes (one long turn never holds up a dispatch for the same agent). The claim event records
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

1. the box offers the repo in the session's workspace (`Runner.offers_project_in`:
   held, opted in, assigned there; [runners.md](runners.md) § Opted-in projects; a
   chat with no repo has nothing to match here);
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
`Agent.turn_mode`, under the dispatch request and the `manual_only` cap of an editor line
(`apps/harness/turn_mode.py`; full ladder in `access.md` → "Turn mode"). Decided at
CLAIM, stamped on `Turn.turn_mode` / `turn_mode_basis` so a rule edited mid-turn
does not flip work under way, and delivered in the caller envelope (`turn_mode`),
which `canopy agent mode --caller` prefers over the agent-wide switch. **An `auto`
on a rule naming a person applies only to a VERIFIED message**; otherwise the turn
runs manual — a forged `From:` may pick a box, never drive an unreviewed agent
(`tests/test_turn_mode_rules.py`). Spec:
`docs/superpowers/specs/2026-09-23-per-rule-turn-mode-design.md`.
