# Observed runner projects — a runner reports the repos it has, nobody declares them

**Date:** 2026-07-28
**Status:** accepted
**Supersedes:** the hand-declared half of `Runner.capabilities["projects"]`

## The problem

`capabilities.projects` is the allowlist `claim_next_turn` matches project turns
against (`Q(project__in=runner.project_names())`). It is typed once by a human at
pairing, and nothing keeps it true. It went stale in the only way that matters:
silently, and in the direction that stops work.

On 2026-07-28 a turn was dispatched at repo `canopy` under Hal's identity. No
runner declared `canopy` — the fleet declared ten repos, and the plugin repo,
one of the most-worked repos in the fleet, was not among them. `enqueue_turn`
accepted it (201; `Turn.project` is a free string that need not match a
registered `Project`), nothing could claim it, and it sat QUEUED until the
stuck-turn banner caught it 90 minutes later.

The declared list was not a curated safe-list that `canopy` had been deliberately
kept out of. It was the residue of which repos someone had dispatched at before.
Measured the same day: emdash held **21** projects; the runner declared **10**,
every one of them also in emdash. The declaration was a strict, arbitrary subset
of the truth.

### Why the guard didn't save it

`canopy project dispatch` preflights for exactly this. It was disabled at the
call site (`--no-preflight`) because it had cried wolf ten minutes earlier: the
fleet list was scoped `paired_by == caller`, so an agent identity saw zero
runners and the preflight concluded BLOCKED for a repo a runner was demonstrably
serving. That visibility bug is fixed separately (canopy-web #509). This spec is
about the other half: even a correct preflight is only as good as the list it
reads, and that list was maintained by hand.

## The insight

To the runner, an agent slug and a repo name are the same thing:

```python
target = turn.get("agent_slug") or turn.get("project") or ""   # execute.py
```

Both name an **emdash project**. So `capabilities.agents` and
`capabilities.projects` were always two spellings of one fact — *which emdash
projects does this box have?* — and agent routing has already moved off its
spelling onto `RunnerAssignment` (spec 2026-07-24). `capabilities.projects` is
the last consumer of "declare what you can drive."

And that fact is **observable**. The laptop runner already opens emdash's SQLite
DB and already reads its `projects` table (`emdash.READ_SCHEMA`,
`cdp_control.list_tasks` → `{tasks, projects}`). It has the true answer in hand
on every tick and throws it away.

This is the same distinction the harness already draws elsewhere. `live_status`
serves what we can *observe* about a runner, never what it last *claimed*
(`heartbeat()` writes ONLINE and nothing demotes it, so the stored column lies).
Session liveness is polled for the same reason. "Which repos can this box drive"
belongs in that family: a fact about the box, not a policy about it.

## Design

**`capabilities["projects"]` becomes runner-reported, never hand-typed.**

`HeartbeatIn` gains `projects: list[str] | None = None`, and the
absent-vs-empty distinction is load-bearing:

| sent | means | server |
|---|---|---|
| `["ace", "canopy", …]` | this is what I can drive | replace `capabilities["projects"]` |
| `[]` | I genuinely have none (a fresh box) | replace with empty |
| *field absent* | I could not tell this tick | **leave the stored list alone** |

Nothing about routing changes. `claim_next_turn` still matches
`Q(project__in=runner.project_names())`; `unclaimable_queued_turns` still
composes from the same helper. Only the writer changed.

### Who computes what

The contract is "report what you can drive." How, is the runner's own business:

- **laptop (`kind=emdash`)** — `SELECT name FROM projects` against emdash's DB.
  An added query on a DB it already opens, not an added dependency.
- **cloud** — unchanged in substance. It has no emdash; it reports its
  `RUNNER_PROJECTS` env, exactly the list it serves today.

A wildcard ("cloud can clone anything, so it can drive anything") was considered
and **rejected**: repos do not exist on the cloud box with any confidence, and
making every repo appear dispatchable there would trade a silent queue for a
confident lie. Cloud project work is set up deliberately, not inferred.

### The empty-report trap

If the runner cannot read emdash, it must **omit** the field — never send `[]`.

This is not a hypothetical. `replace_reported_sessions` carries the same rule and
the scar that produced it: reporting an empty list clears server state, and
"swallowing the error is what let a schema drift blank the supervisor with
nothing in the log." Here the blast radius is bigger — an empty projects report
makes **every** repo turn on that runner unclaimable, fleet-wide, until the next
good tick. `emdash.EmdashReadError` already distinguishes "the DB isn't there"
(a legitimate no-emdash box → genuinely `[]`) from "the DB is there and could not
be READ" (→ omit).

The same rule makes the rollout free: a runner on old code sends no field, keeps
its stored list, and nothing breaks before it updates.

### What this retires

`PATCH /api/harness/runners/{id}` with `projects` becomes a ghost edit — the next
heartbeat overwrites it seconds later. It should fail loudly (422) with a pointer
to the real fix ("this runner reports its projects; open the repo in emdash"),
rather than accept a write that silently evaporates. `agents` and `sessions` keep
working on that route.

Downstream, in the canopy repo (separate change, separate repo): with a
trustworthy list, `canopy project dispatch --declare` and `--no-preflight` both
lose their reason to exist.

## Scope of the widening

Observing means the laptop reports all 21 emdash projects rather than 10 — it
gains `ace-web`, `brain`, `canopy`, `commcare-hq`, `commcare-ios`,
`connect-prelogin`, `connect-search`, `game-generator`, `sam-e2e-parity`,
`sam-local-dev`, `scout`.

This is a deliberate accepted widening, not an oversight. The tenant gate is
unchanged and remains the real boundary: only a member of the runner's pairer's
workspace can enqueue a project turn at all. What the hand-typed list was
providing was not security but accident — and its accidental omissions cost more
than its accidental inclusions do.

## Deliberately out of scope

- **`capabilities.agents`.** Now provably redundant: agent slugs are emdash
  projects too, so the reported list already contains them, and `RunnerAssignment`
  has routed agent turns since spec 2026-07-24. It routes nothing today. Deleting
  it is a separate change with its own blast radius.
- **Whether project turns should get an ordered routing authority** like
  `RunnerAssignment`. Ranking only matters when several boxes can serve the same
  repo AND the choice between them carries meaning. That is the agent problem
  (which account has tokens), not yet the repo problem.

## Testing

**Runner** — a populated DB reports its names; an unreadable DB omits the field
(asserted as *absent*, not empty — the distinction is the whole safety property);
a missing DB reports `[]`.

**Server** — a heartbeat carrying the field replaces the stored list; a heartbeat
without it leaves the stored list untouched; `PATCH` of `projects` 422s.

**End to end** — a turn at a repo emdash has is claimed; a turn at a repo nothing
has stays QUEUED and appears in `/turns/unclaimable` with a reason that is now
true rather than merely stale.
