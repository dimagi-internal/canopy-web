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
  workspace paired into; joining a workspace never enrolls existing boxes; a
  separate root is never covered. Managed by the owner or a runner admin at
  `/api/harness/runners/{id}/workspaces` (`tests/test_runner_enrollment.py`).

`capabilities` (agents, projects) is a caller-supplied routing HINT declared by
the box; it is intersected with the tenant gate, never a substitute for it.
Agent turns are tenant-gated through `routing.agent_tenant_q` (no NULL-workspace
leg — always AND it with `agent__isnull=False`), repo turns through
`Turn.workspace`, session turns through `chat_session.workspace`.

## Which agents a box may hold

`apps/agents/services.py::runner_may_hold_agent(runner, agent)` — true only when
the runner's OWNER is one of the agent's admins (`Agent.is_admin`: the owner, a
workspace owner, or an `AgentAdmin`). A box running an agent holds its prompt,
its caller's token, its secrets and its owner's GitHub identity, so this gates
claim (agent turns AND chats with the agent), credential resolve, the per-turn
GitHub token (re-asked at issue, since an owner can be demoted mid-turn), pins,
and every routing write. A teammate whose laptop should run an agent's work needs
an admin grant on that agent. An agent's own canopy login is not a holder.

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
   runner never takes `local_only`. `profile_q`: a confined (caller) turn only to a
   runner reporting `profiles >= PROFILES_VERSION` and `envelope >= ENVELOPE_VERSION`
   — not bypassable by a pin. Agents/sessions with an executing turn are excluded
   (`busy_agent_ids` / `busy_session_ids`; the `isnull=False` filters in them are
   load-bearing — a NULL in the IN-list excludes every row), except a follow-up that
   may ride into the running turn (`routing.may_ride`). Then the tenant gate.
4. Per candidate: runner flags satisfy the turn's requirements (above the pin);
   `runner_may_hold_agent` (above the pin); then, unless pinned here, the
   availability cascade for the agent / repo order / session agent.
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

The topology screens and `GET /api/workspaces/{slug}/runner-topology`
(`apps/harness/topology.py`) are computed from these same predicates.

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
`AgentSchedule` (`apps/harness/models.py`) holds the cron server-side; the RUNNER
evaluates it (`runner/canopy_runner/canopy_runner/schedules.py`) and POSTs a due
slot; `schedule_turns.fire_schedule` enqueues it.

- **Anchor on `fire_after`, never `last_slot`.** `ScheduleOut.fire_after`
  (`schedule_services`: `last_slot or created_at`, pinned just before a one-off's
  instant) is what the runner passes to `due_slot(after=...)`; `last_slot` is NULL
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

**An agent acts on GitHub as its OWNER, through a delegation — never a box
credential.** Boxes hold no GitHub token. `AgentDelegation` (person, agent,
service) is the third credential kind beside the agent's own vault and the
workspace's shared vault: a person lending their identity to one agent
(`apps/agents/delegations.py`).

- The owner pastes a fine-grained PAT on the agent's Settings → Credentials →
  GitHub (canopy pre-fills GitHub's creation form, `delegations.create_url`; GitHub
  has no API to mint one). Its repository selection IS the agent's scope. Only the
  agent's owner may set it, and it is refused unless it can push and open a pull
  request on the agent's repo (`check_repo`: probes that GitHub answers 422 for a
  branch/commit that cannot exist, 403/404 for no permission — nothing is created).
- In force only while that person OWNS the agent and is a member of its workspace
  (`delegation_for`).
- Issued per turn by `POST /api/harness/runners/{rid}/turns/{tid}/github-token`:
  only for a turn that runner is executing, re-checking `runner_may_hold_agent`;
  the principal is resolved server-side (`delegations.turn_agent`); **409 with the
  reason and no shared fallback** when absent or expired; always refused for a
  confined (caller) turn. Returns the token, the owner's git identity (noreply
  email) and `requested_by` for a `Requested-by:` trailer — the caller is recorded,
  never the principal. Stopping bad writes is the agent's own gating, not the token
  layer's. Each issue is an `agent.github.issued` event.
- **Agentless repo turns** (`Turn.project` = repo name) run as no one and are
  refused the same way, unless the repo has an `agents.RepoIdentity` row
  (`repo_slug` unique → agent; edited in Django admin, listed as
  `identity_for_repos` on `GET /api/agents/{slug}/github`), in which case the turn
  runs as that agent under the same rule.
- Readiness: a box calls `GET /api/harness/runners/{rid}/github-readiness` at boot
  (checked live per agent it serves), and the readiness drill covers it — a missing
  or expired token is a red health check, not a mid-turn 403.

Spec: `docs/superpowers/specs/2026-09-24-agent-github-delegation-design.md`
(`tests/test_agent_delegations.py`). Salesforce uses the same model with an agent
as lender (`delegations.set_salesforce`).

### The per-user GitHub App (`apps/tokens/github_app.py`)

A public GitHub App (`canopy-agents`) authorized per user (user-to-server flow).
It is canopy-web's OWN server-side GitHub access as a person — skill history
(`apps/agents/skill_history.py`) and plugin archives (`apps/plugins/services.py`,
after the delegation) call `access_token_for(user)`; no route hands it to a runner.

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

## The runner software

A runner is an installed package, never a checkout, and updates itself to what is deployed.

**Laptop install.** `runner/canopy_runner/scripts/install-runner.sh` installs and updates: it `git archive`s a ref, builds and `uv tool install`s three wheels, provisions the CDP sidecar's node deps, and renders + loads a generated launchd plist. The daemon runs from a tool venv no checkout can change. Re-running restarts the daemon (not mid-turn). `--ref <branch>` installs another ref; `--no-launchd` skips the daemon; `--workspace <ws>` pairs into a workspace. Spec: `docs/superpowers/specs/2026-07-28-runner-as-installed-package-design.md`.

**Auto-update (both kinds).** A separate job — `com.canopy.runner.updater` (launchd, every 30 min; `--no-auto-update` to skip) on a laptop, `runner/ec2/update_runner.sh` (systemd timer) on a cloud box — installs the **deployed** `expected_code_sha` for that runner kind, never `origin/main`; defers while a turn is in flight (`~/.canopy/in-flight`); and is read-only against the control plane (`canopy-runner update-check` → `current|stale|busy|unknown`, never a heartbeat). A heartbeat reporting a differing sha gets a `runner.update_available` frame that kicks the updater, so the timer is only the fallback. Laptop log: `~/.canopy/updater.log`; bootout the updater before testing a branch on a box. The cloud box installs via `git show <sha>:runner/ec2/cloud_runner.py` from its on-box clone; Secrets Manager is a first-boot seed only; override with `canopy-runner-update --ref <ref>`. Spec: `docs/superpowers/specs/2026-07-30-cloud-runner-auto-update-design.md`.

**Code provenance.** `code_sha` is the last commit touching that runner kind's own source (`runner/canopy_runner/…` or `runner/ec2/`), not repo HEAD. The server holds `RUNNER_CODE_SHA` / `RUNNER_CLOUD_CODE_SHA` (computed by the deploy workflow, `fetch-depth: 0`); `RunnerOut.expected_code_sha` serves the one matching the row's `kind`, and `/supervisor` alerts only when both sides are non-empty and differ (empty = unknown).

**The cloud runner** (`runner/ec2/cloud_runner.py`) polls claim every 15s and drains the queue, executes via `claude -p` (or ACP with `RUNNER_EXECUTOR=acp`, default off), declares repos via `RUNNER_PROJECTS`, and runs the stream/backfill contract on a 3s clock. `bootstrap_agents.sh` provisions the agent fleet (clones each `dimagi-internal/<slug>` into `/opt/agents`, installs the canopy plugin, `op inject`s env, maps gog OAuth clients); it runs from `main()` after the credential is staged and is not `set -e`, so one agent's failure does not stop the rest. Spec: `docs/superpowers/specs/2026-07-25-cloud-agent-bootstrap-design.md`.

## Runner readiness (the not-ready marker)

A failed turn writes `~/.canopy/not-ready` (`runner/canopy_runner/canopy_runner/readiness.py`,
`mark_failed`); `readiness.compute` reports it on the heartbeat as `ready=False`, a
not-ready runner is not `Runner.is_available`, and lower ranks in every cascade
take its work. `mark_ok` clears it only after a turn executes, so the marker **expires after
`MARKER_TTL_SECONDS` (15 min)** — otherwise one transient failure exiles a box that
keeps heartbeating ONLINE. Expiry is retry-with-backoff: a genuinely broken box
fails its next turn and re-marks itself. The CDP probe gates independently
(`compute`), so expiry cannot mask emdash being down.
