# Runners: flags, host requirements and scheduled turns

Host-required runner flags (sensitive-data runners), why a turn on a site's page withholds the agent's own login there, and scheduled turns.

**See also:** [runners.md](runners.md), [runner-routing.md](runner-routing.md), [runner-credentials.md](runner-credentials.md), [runner-fleets.md](runner-fleets.md).

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

A turn whose conversation is held on a host site's page that its agent serves
carries `page_site: {site, withheld}` in its claim (`interface.page_turn`; the
names come from the host site's `own_login`). There the agent reaches the
site only through canopy's gateway, as the person who sent (`access-confinement-and-act-as.md` § Acting as
the person on a host site), so:

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
- Keep the site login in the agent's Canopy vault for the withholding to bind: a value
  kept anywhere else is outside it.

## Scheduled turns

The scheduler is a producer of turns, not an execution engine (no celery/beat).
`AgentSchedule` (`apps/harness/models/schedule.py`) holds the cron server-side, and
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
  session would occupy a runner forever (its heartbeat keeps renewing the lease). Runs on the off-request queue sweeps (every 15 s), not the fire tick
  (a weekly fire tick could never honour a short grace).
- **Late slots:** a queued slot turn not started within `LATE_SLOT_WINDOW_MINUTES`
  (30) of its slot is skipped as MISSED with no nag; `always_run` and one-offs are
  exempt, manual runs untouched.
- **Nag:** a grace-released occurrence raises a `review` ask on a task
  (`_raise_schedule_nag`, only when the schedule's `notify` includes `inbox`);
  approving re-runs the prompt, and a later DONE occurrence closes open nags
  (`resolve_schedule_nags`). It reaches **Waiting on you** as an ordinary ask.
- **A schedule can be a job** (`job`, `job_repo`): a command from a repo's `main` run with no model
  on a cloud box. Its fire is the same, its claim and its run are not, and its nag has no re-run
  (approving would start a model turn): see § Plain jobs.
- An occurrence runs as `system` with the schedule's creator as the accountable
  person (`turn_mode._accountable_cap`, `access.md` → "The rule").

Specs: `docs/superpowers/specs/2026-07-15-agent-scheduled-turns-design.md`,
`2026-07-21-supervisor-inbox-items-only-design.md`.

## Plain jobs

A schedule can run a **job** instead of a prompt: a command from a repo's `main`, on a cloud
machine, with no model, using the agent's connections. Set it in **Triggers** (New trigger →
**Run a job (no AI)** → *Which repo*, *Which job*); only an admin of the agent can set or change one
(anyone else sees the job read-only: "Only an admin of {agent} can change a job."). It fires on its
cron, or on **Run now**, as any schedule does, and runs on `routing = cloud_only`; `grace_minutes`
is at least 125 so a 120-minute job is not released as missed while it runs.

**The file.** canopy-web says only the job's name, the agent and the repo's name (a bare
`[A-Za-z0-9._-]` name, no `/`); the box builds the address from its own `REPO_URL_TEMPLATE`, so it
reaches only the org's repos, and **the command always comes from that repo's `main`**. A failed
fetch fails the job: it never runs stale code.

```yaml
# .canopy/jobs.yaml
version: 1
jobs:
  sync-leads:                      # [a-z0-9][a-z0-9_-]{0,63}
    run: [python3, scripts/sync_leads.py]   # an argv list: no shell, no $VARIABLE expansion
    timeout_minutes: 15            # 1 to 120, default 15
```

Unknown keys are an error. The job runs from the root of the checkout (an export of `main`, so no
`.git` and no git credentials).

**The six environment variables** are all it gets (`env -i`; no model key, no MCP token, no runner
token, nothing of the runner's):

| | |
|---|---|
| `PATH` | the box's system path |
| `HOME` | the job's own folder, writable |
| `LANG` | `C.UTF-8` |
| `CANOPY_AGENT_SLUG` | the agent the job runs as |
| `CANOPY_TURN_ID` | this run's turn (its page on canopy-web) |
| `CANOPY_JOB_TOKENS` | a folder of the agent's connection tokens, one `<provider>.json` each |

**Reading a connection.** Each connection the agent can use is copied to
`$CANOPY_JOB_TOKENS/<provider>.json` (0600, the job's own copy, refreshed about every minute and
removed when the job ends). The file is the one the agent's turns read: `access_token`,
`instance_url`, `expires_at` (epoch seconds) and `account_label`. It holds an ACCESS token, never the
refresh token. For Salesforce:

```python
import json, os, urllib.parse, urllib.request

with open(os.path.join(os.environ["CANOPY_JOB_TOKENS"], "salesforce.json")) as fh:
    sf = json.load(fh)          # re-read it in a long job: it is refreshed under you

query = urllib.parse.urlencode({"q": "SELECT Id, Name FROM Lead LIMIT 5"})
req = urllib.request.Request(
    f"{sf['instance_url']}/services/data/v62.0/query?{query}",
    headers={"Authorization": f"Bearer {sf['access_token']}"},
)
print(json.load(urllib.request.urlopen(req))["records"])
```

An agent with no such connection has no file; a job should say so plainly and exit non-zero.

**Output and ending.** What the job prints streams to its turn as `status` events
(`{source: "job", text}`), and the turn's page shows it as a plain log, refreshed while the job runs, with the exit and the last
lines under it (`result_note`); there is no chat and no transcript. Output is scrubbed of the
exact token values and capped at 1 MB. A non-zero exit is `FAILED` and is **never retried** (a job
may have written); so is a timeout, which kills everything of its user. A failed, lost or
missed job raises a review task on **Waiting on you**, with the last lines it printed if it failed
and canopy's reason otherwise. None of them starts a debugger turn. It has no
Start now (that would start a model turn): the task offers **Run now**, and a later good run
closes it.

**What a job can't reach.** Jobs can reach the public internet, but not private addresses inside
our network. Not the instance metadata address (so no AWS role), not `localhost` or the box's own
addresses, not any private range (the VPC included), not a Unix socket (the brokers'
included), not another runner's or agent's files, tokens or `.env`, and not the runner's login. No
`/proc/meminfo` either. It sees only its own folder. The sandbox and its limits are in
`runner/ec2/README.md` § Plain jobs.

**Dependencies are the job's to install.** The box carries Python and the usual system tools, not a
repo's packages. The job's own script installs what it needs into its `HOME` (for example
`pip install --user -r requirements.txt` before it imports them) or vendors them in the repo;
download time counts against `timeout_minutes`, and nothing persists between runs.

**One job per machine at a time.** Jobs share one Unix user, so a box never starts a second beside
the first, whichever of its runners would claim it: canopy-web compares machines (`machine_id`;
each fleet member is its own, and a runner with none answers for itself). The box backs this up: in
the rare race where two runners claim at once, the second fails with "Another job is running on
this machine." and touches nothing.

**How long a job waits.** A slot waits in the queue ("Waiting for a cloud machine that can run
jobs." on its schedule) for **up to 30 minutes** past its slot, for a box that takes jobs to be
free. Then it is skipped as `MISSED` with the reason "No cloud machine that can run jobs took it in
time.", which its row shows and a review task on **Waiting on you** repeats (one open review per
schedule, however many slots an outage takes; if the schedule
notifies the inbox); the next slot fires as usual. An `always_run` schedule doesn't skip, and waits
until its next slot replaces it; so does a slot whose next one comes within the 30 minutes. A
one-off, or a **Run now**, has no next slot and waits until a box takes it.

**Run now while the job runs** is refused (409, "This job is still running."): nothing is queued
and nothing is recorded. A new SLOT in that time is recorded as `MISSED`, with no review, and the
running job carries on.

**Boxes that don't take jobs.** A **sensitive-data** runner never reports `jobs`, and nor does a
box with **any egress-locked runner**, from any of its runners, so a job never fails on its first
network call. Nor does a box that predates jobs or lacks the `canopy-job` user and its broker
(`--setup-runners` makes them). A runner asks again every few minutes, so a change to any of these
takes effect within 5 minutes with no restart. If no box can take a job, the turn is listed as a
queued turn no runner can claim, with that reason, and the schedule says "Waiting for a cloud
machine that can run jobs." for as long as it waits (§ How long a job waits, above).
