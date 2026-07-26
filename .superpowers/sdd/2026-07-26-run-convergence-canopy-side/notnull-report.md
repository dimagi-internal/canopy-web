# `Agent.workspace` NOT NULL — killing the fail-open-tenancy bug class at the schema level

Branch: `fix/agent-workspace-not-null` (based on `origin/main` @ `5738354`)

**Verdict: NOT NULL is safely achievable and it shipped.** No legitimate reason
for an unhomed agent to exist was found, and after this change no code path can
create one. Details and the residual risks are below.

---

## 1. Why instance-fixing was losing

`Agent.workspace` was added nullable in `0006` purely so the `AddField` needed no
default on existing rows. `0007` homed every existing agent the next migration
later. The nullability was never reverted, and in the window since, **eight**
call sites grew a "NULL means allow" leg — the shape

```python
if agent.workspace_id and not wsvc.is_member(user, agent.workspace_id):
    raise NotFound
```

short-circuits to *ungated* on exactly the row that declares no tenant, and its
queryset twin

```python
Q(agent__workspace_id__in=slugs) | Q(agent__workspace_id__isnull=True)
```

is unconditionally true for any row whose `agent_id` is NULL as well, because the
traversal is a LEFT JOIN. Four sites had been fixed one at a time (PRs #378,
#421, #423) — each fix correct, none of them addressing why the next one kept
appearing. The nullable column is the generator.

## 2. Feasibility: is an unhomed agent ever legitimate?

No. Evidence gathered before changing anything:

- **Nothing in the running app creates one.** `Agent.objects.create` /
  `update_or_create` appears in exactly one non-test place:
  `apps.agents.services.upsert_agent`, reachable only from
  `apps.agents.api.upsert_agent`, which is session-authed. No management
  command, MCP tool, seeder, or signal creates an `Agent`.
- **The one creation path already homed every agent**, just a few lines *after*
  creating it — the row existed unhomed for the length of a request, and would
  stay unhomed forever if the caller 4xx'd in between (the explicit-workspace
  branch could do exactly that).
- **The stated reason was discharged years ago.** The help text said "Nullable
  for migration safety"; `0007` is that migration.
- **Production has zero unhomed agents**, asserted in the comments of all four
  prior fixes and re-confirmed by the gate-4 investigation.
- The only things depending on the nullability were **test fixtures** — the
  "pre-tenancy suite" of agents with `workspace=None` and runners with
  `paired_by=None`. That is a fail-open tenancy rule held in place by a fixture,
  which is not a reason to keep it.

## 3. What shipped

### Migration `agents/0013_agent_workspace_not_null`

`RunPython` backfill + `AlterField(null=False)`, in one migration, reversible.

The backfill resolves its target **from the data**, most-evidenced first, and
the full reasoning is in the migration's own docstring:

1. **the modal workspace among already-homed agents** — the tenant this
   deployment's agents demonstrably live in; a stray NULL is by construction a
   row that escaped `0007` or was hand-created after it, and its siblings' home
   is the answer the data gives. It is also a *narrowing*: an unhomed agent is
   today visible to every authenticated caller via the fail-open legs, and
   afterwards only to that workspace's members;
2. **the sole workspace**, if the deployment has exactly one (a dev DB whose
   tenant isn't called `dimagi`) — no other candidate exists, so there is
   nothing to get wrong;
3. **the default `dimagi` workspace**, created exactly as `0007` creates it —
   reached only with several workspaces and not one homed agent, i.e. no
   evidence at all. Not an invented default: it is the target `0007` already
   picked for every agent in this deployment's history;
4. **no users at all → raise** with an actionable message. A `Workspace` needs a
   `created_by`, so this is genuinely unresolvable; failing loudly beats an
   opaque NOT NULL violation two lines later. Unreachable in practice (creating
   an agent requires being logged in).

Deliberately **not** done: granting anyone membership of the target workspace.
`0007` did that because it was making a pre-tenancy world tenanted for the first
time; here the target already has members, and adding more would be a privilege
escalation dressed as a data fix.

**Reversibility.** The `AlterField` reverse restores `null=True`; the data step's
reverse is a no-op (we do not record which rows we touched, and a row *with* a
tenant is strictly safer than one without). Reversing therefore leaves the column
nullable and every agent still homed — the same state a re-run would find, so
forward/back/forward is idempotent. Verified by actually doing it (§5).

### The creation path

`services.upsert_agent(data, *, workspace)` — `workspace` is now **required and
keyword-only**, applied via `create_defaults` so it homes on CREATE and never
moves an existing agent between tenants on a re-register (the plugin re-upserts
on every sync). The view resolves the tenant *before* the row is written, so
there is no window in which an unhomed agent exists.

### `isnull` legs removed — 8 sites

| # | Site | What it gated | Named in the task? |
|---|------|----------------|--------------------|
| 1 | `apps/harness/services.py::claim_next_turn` | agent-turn claim routing | yes |
| 2 | `apps/harness/api.py::_runner_schedule_qs` | which schedules a runner may sync/fire | yes |
| 3 | `apps/harness/services.py::unclaimable_queued_turns` | `GET /api/harness/turns/unclaimable` — leaked `prompt`/`target` of any unhomed agent's queued turns to any authenticated caller | yes |
| 4 | `apps/harness/schedule_services.py::week_schedules` | `GET /api/agents/schedules/week` — leaked schedule name, prompt, cron, timezone | yes |
| 5 | `apps/harness/api_schedules.py::_visible_workspace_ids` | the `\| {None}` that fed #4 | yes |
| 6 | `apps/realtime/snapshot.py::supervisor_snapshot` | **found here** — pushed an unhomed agent's slug + open-item count to every connected supervisor WebSocket regardless of tenant | no |
| 7 | `apps/harness/schedule_services.py::_resolve_agent` | **found here** — `if agent.workspace_id and not is_member(...)`, gating the whole schedule CRUD (list/create/update/delete/run-now) for **both** REST and MCP | no |
| 8 | `apps/agent_runs/api.py::_get_agent_or_404` | **found here on `main`** — same fail-open shape, gating the entire run read model (labels, `session_link`, artifacts, verdict rationales, decision reasoning) and its writes (gate, verdict, fork) | no |

Sites 6–8 were not in the task's list of four. Site 8 is the one the gate-4
report fixed on a *separate* branch (`fix/agent-runs-fail-closed`), which is not
merged into `main`; the flip applied here is byte-identical to that branch's, so
if both land the conflict is trivial (or a clean no-op).

**Left alone, deliberately** — same `isnull` spelling, different (nullable by
design) column, all out of this task's scope:
`Runner.workspace` (`_runner_visibility_q`, `snapshot.py:42`),
`Turn.workspace` on project turns, `Project.workspace`, `Review.workspace`,
`Walkthrough.workspace`, `Issue.workspace`. Those product-root FKs are the same
latent bug class one model over and are worth their own pass; nothing here
depends on them.

### The dangerous pair: proving claim ⇄ schedule agreement

The brief is right that this is the recurring defect, so it was fixed at two
levels rather than one.

**Structurally.** Both predicates now call the same two functions in
`apps/harness/services.py`:

- `runner_tenant_slugs(runner)` — the workspaces of the human who paired the
  runner, never the `Runner.workspace` FK. NULL `paired_by` fails closed via an
  empty set. This *replaced* `_runner_schedule_qs`'s separate `.none()` branch:
  two mechanisms for one rule is precisely how things drift, so there is now one.
- `agent_tenant_q(ws_slugs)` — the tenancy `Q`, with no NULL escape hatch.

They are no longer two hand-written predicates that happen to match; there is one
predicate with two callers. Note the task's warning was heeded in both
directions: **nothing** was "simplified" toward the `Runner.workspace` FK.

**Behaviourally** — `tests/test_claim_schedule_parity.py`, 5 tests. The fixture
is the production shape that broke: one runner, a pairer belonging to two
workspaces, a third workspace belonging to a stranger, and the runner's own
`workspace` FK pointing at only *one* of the pairer's two. The central test
asserts **set equality** of "agents whose schedules this runner may fire" and
"agents whose turns this runner may claim", so a change that widens *or* narrows
either side alone fails whichever direction it moves. Claimability is measured by
draining (claim → mark terminal → claim again), because
`one_executing_turn_per_agent` means a single call would only ever prove the
first match.

Structural agreement is checked too (`test_the_two_predicates_are_the_same_object`),
but as the cheap early warning — the behavioural tests are the gate, because
"they call the same helper" is a property of today's code and the invariant has
to outlive it.

**Proved it fails on drift.** Reintroducing the exact outage —
`_runner_schedule_qs` scoped to `{runner.workspace_id}` — was run as a probe:
3 of the 5 tests fail, including the second-workspace test that names the
4-of-5-agents-stopped incident. Reverted immediately after.

## 4. Test fallout

`uv run pytest` → **1555 passed, 1 skipped** (baseline on `main`: 1554 passed,
1 skipped). `uv run ruff check . --select F --ignore F403,F405` → clean.

Two kinds of fallout:

**(a) Fixtures that minted unhomed agents (~25 files).** Rather than copy a
workspace-making snippet into each — the copies are how they drifted in the first
place — the helpers live in `apps/workspaces/testing.py` (`a_user`,
`a_workspace`, `a_member`), with a root `conftest.py` exposing a
`default_workspace` fixture that delegates to the same code. Test-support module
in the app that owns the tenancy concept, imported by tests only.

A second-order effect worth naming: many suites also paired runners with
`paired_by=None`, which only ever worked because *both* sides of the tenancy rule
had a NULL-means-allow leg. Those now pair with a real member — i.e. several
claim tests were previously passing for the wrong reason and now exercise the
gate they claim to.

**(b) Six tests that asserted "an unhomed agent is invisible on surface X".**
These cannot be written any more, and were always the weaker claim. They are
replaced by `tests/test_agent_workspace_not_null.py` (5 tests: the row cannot be
created, cannot be un-homed after the fact, the service refuses to create one
without a tenant, upsert homes on create but never moves, and the schedule
traversal can never land on NULL). Each affected file keeps a comment saying what
moved and where, and its **cross-tenant** test — the half with something left to
prove — stays. Two were upgraded rather than deleted:
`tests/test_realtime_groups.py::test_user_cannot_read_workspaceless_turn` now
uses a *project* turn (whose own workspace FK is genuinely nullable), so the
fail-closed branch it pins is still exercised, and
`test_week_schedules_none_in_set_includes_unhomed_agents` became
`test_week_schedules_never_matches_on_a_null_workspace` — a stray `None` in the
set must now match nothing rather than everything.

`apps/agents/tests/test_workspace_not_null_migration.py` (7 tests) covers the
backfill's target resolution — each rung of the ladder, the deterministic
slug tie-break, the raise, and that the production path creates nothing as a
side effect of finding nothing wrong.

## 5. Migration verified end to end

Against a file-backed SQLite DB, not just the in-memory test run:

1. `migrate` from scratch → applies clean;
2. `migrate agents 0012` → **reverses** clean;
3. seed the pre-`0013` world: 2 workspaces (`connect`, `dimagi`), 2 agents homed
   in `connect`, 2 unhomed strays;
4. `migrate agents` → strays land in **`connect`** — the modal homed workspace,
   *not* the `dimagi` default that also existed, i.e. the data-derived rule
   actually fired rather than the fallback;
5. `PRAGMA table_info` confirms `workspace_id` NOT NULL;
6. reverse again with data present, re-apply → idempotent, as documented.

## 6. Other checks

- **OpenAPI unchanged.** Dumped the schema before and after; byte-identical, so
  `frontend/src/api/generated.ts` needs no regen and CI's freshness check will
  pass.
- `AgentOut.workspace` is left as `str | None` on purpose. Tightening it to `str`
  would be more truthful but changes the response contract and forces a frontend
  regen for zero functional gain; it is a reasonable follow-up, not part of this.
- Architecture-boundary test passes; `apps/workspaces/testing.py` imports only
  Django and its own app.
- `CLAUDE.md` updated: the NOT NULL invariant and the claim/schedule sharing are
  both now written down where the next agent will read them.

## 7. Residual risk I could not eliminate

- **Postgres was not available in this worktree**, so the migration was verified
  on SQLite. The operations are stock (`RunPython` + `AlterField`) and Django
  emits `ALTER TABLE ... ALTER COLUMN ... SET NOT NULL` on Postgres. That takes
  an `ACCESS EXCLUSIVE` lock and a full table scan; `agents_agent` holds a
  handful of rows, so it is instant — but it is a lock, and it is taken during
  the pre-cutover migration task.
- **A concurrent write racing the migration** could in principle insert an
  unhomed agent between the backfill and the `SET NOT NULL`, failing the deploy.
  Only `upsert_agent` can create an agent, both statements are in one transaction,
  and post-deploy that path always homes — but the window is not formally zero.
  Consequence is a failed migration, not corruption.
- **Site 8 overlaps an unmerged branch** (`fix/agent-runs-fail-closed`). Trivial
  conflict at worst; flagged rather than coordinated.
- **The same bug class is alive one model over.** `Project`, `Review`,
  `Walkthrough`, `Issue` and `Turn` all still carry a nullable `workspace` with
  `isnull=True`-means-allow legs (`apps/projects/api.py:129`,
  `apps/projects/services.py:47`, `apps/reviews/api.py:230`,
  `apps/walkthroughs/api.py:355`, `apps/issues/api.py:36`). Out of scope here and
  untouched, but they are the identical shape and will keep generating instances
  until they get the same treatment. `Runner.workspace` is the one that should
  *stay* nullable — that one is by design.
