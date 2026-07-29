# The runner is an installed package, not a checkout

**Date:** 2026-07-28
**Status:** design → shipped
**Supersedes:** the branch half of `feat(supervisor): loud alert when a runner is on a non-main branch` (#306) — the alert stays, but it stops being the primary staleness signal.

## The problem

The laptop runner daemon executes from a **working checkout**. Its launchd job puts
`~/emdash-projects/canopy-web/packages/canopy_runner` (and, in the hand-patched live copy,
two sibling package dirs) on `PYTHONPATH`. That checkout is also the one a human — or an
agent turn — uses interactively. Anything that runs `git checkout` in it moves the code
the daemon runs.

This has happened at least three times. #306's answer was to *report* the branch on the
heartbeat and shout in `/supervisor` when it isn't `main`, deliberately warn-only ("a legit
dev-branch runner shouldn't be bricked"). That was the right call for a detector, but it
treats the symptom: the daemon is still one `git checkout` away from executing whatever
happens to be on disk.

Reviewing the seam for this change turned up that the coupling has already cost more than
the branch incidents:

**A1 — the committed launchd plist cannot start the runner.** `packages/canopy_runner/launchd/com.canopy.runner.plist`
sets `PYTHONPATH` to the runner package alone, but `main.py` imports `canopy_transcript` at
module scope and `schedules.py` imports `canopy_cron`. Proved by running it:

```
ModuleNotFoundError: No module named 'canopy_transcript'
```

The plist that actually runs (`~/Library/LaunchAgents/com.canopy.runner.plist`, mtime
2026-07-27) was hand-edited to a three-directory `PYTHONPATH`. The repo's provisioning
artifact is drift; a box provisioned from it gets a runner that dies on import.

**A2 — the menubar's "Take one turn" has been silently broken by A1.** `packages/canopy-runner-menubar/Sources/main.swift:26`
hardcodes the **repo** plist path and re-reads its `ProgramArguments` + env to run
`--drain-one` "so this can't drift from how the daemon runs" (main.swift:339). It reads the
broken copy, and the launch is fire-and-forget (`try? proc.run()`), so the failure surfaces
nowhere.

**A3 — the daemon's dependencies are undeclared and interpreter-fragile.** `croniter` and
`websocket-client` are `pip install --user` artifacts under
`~/Library/Python/3.14/lib/python/site-packages`, resolved by whichever `python3` the
plist's `PATH` finds (Homebrew's, currently 3.14). A `brew upgrade python` retires that
directory: schedules stop firing (croniter) and the wake listener silently degrades to
polling, because `wake.py` imports `websocket` lazily with a poll-only fallback. Nothing
declares this, and nothing would report it.

**A4/A5/A6/A7** — smaller, all downstream of "nobody ever installs this": `canopy_runner` is
the only in-repo package with no `[tool.hatch.build.targets.wheel]` block and nothing
asserting the CDP sidecar ships in the wheel; there is no `__version__`, so a running runner
cannot say what it is; `[tool.uv.sources]` marks both path deps `editable = true`, which is
right for `uv run` and a hazard for a copying install; and the sidecar's Node deps are a
manual `cd canopy_runner/cdp && npm install` at a path that moves under packaging.

## The shape

Install the runner. The daemon's code becomes a **snapshot in a tool venv** that no `git`
operation can reach, and updating is one command.

```bash
packages/canopy_runner/scripts/install-runner.sh          # origin/main
packages/canopy_runner/scripts/install-runner.sh <ref>    # anything else, deliberately
```

### Install from a snapshot of the ref, never the working tree

The script `git archive`s the requested ref into a temp dir, builds three wheels there, and
installs from those:

```
git -C $REPO fetch origin
git -C $REPO archive origin/main | tar -x -C $tmp
uv build --wheel -o $tmp/dist $tmp/packages/{canopy_cron,canopy_transcript,canopy_runner}
uv tool install --force --find-links $tmp/dist "canopy-runner[realtime]==$version"
```

Three properties follow, and each is load-bearing:

- **Building from `git archive`, not from `$REPO` itself**, means a dirty or branch-switched
  working tree cannot be installed by accident. Installing a branch stays possible — you
  pass the ref — but it is now an act, not a slip.
- **Installing from built wheels, not from the source tree**, sidesteps `[tool.uv.sources]`
  entirely. A wheel's metadata records `Requires-Dist: canopy-cron`; the `editable = true`
  path source is build-time-only and never reaches the tool venv. If it did, the venv would
  hold a `.pth` pointing into `$tmp`, which the script deletes on exit — an install that
  works until the next import.
- **`--find-links $tmp/dist`** is what lets the resolver satisfy `canopy-cron` /
  `canopy-transcript` (which exist on no index) while still reaching PyPI for
  `croniter` and the `realtime` extra's `websocket-client`. A3 dies here: the deps are
  declared, resolved, and pinned to the tool venv's own interpreter.

### The launchd plist is generated, not committed as a literal

The committed file becomes `com.canopy.runner.plist.template` with `__CANOPY_RUNNER_BIN__`
and `__HOME__` placeholders; the script renders it to `~/Library/LaunchAgents/` and
kickstarts the job. `ProgramArguments` becomes the installed console script
(`[project.scripts] canopy-runner`), and **`PYTHONPATH` disappears** — there is no source
tree to point at. A1 cannot recur, because the artifact that starts the runner no longer
names a directory whose contents it doesn't control.

The menubar app's `plistPath` moves to `~/Library/LaunchAgents/com.canopy.runner.plist`
(A2) — the copy that is actually loaded. Its "reuse the daemon's own invocation" intent is
preserved and, for the first time, true.

### The sidecar provisions itself

`cdp/emdash_control.mjs` stays inside the package (it is code; it should be versioned with
the Python that calls it), and ships in the wheel via an explicit
`[tool.hatch.build.targets.wheel]` block plus a test that unzips a built wheel and asserts
the `.mjs` and its `package.json` are present (A4).

Its `node_modules` cannot ship — `playwright-core` is a Node dependency, and site-packages
is replaced wholesale on every reinstall. So `cdp_control` grows
`ensure_sidecar_deps()`: if `node_modules/playwright-core` is missing next to the sidecar,
run `npm install --omit=dev` there once, before spawning. `canopy-runner install-sidecar`
does the same thing eagerly so the install script pays that cost instead of the first turn.
A failure raises `CDPError` naming the manual command, exactly as the missing-`node` case
already does.

ESM is why the deps live *next to* the sidecar rather than in a stable
`~/.canopy/cdp-deps`: `NODE_PATH` is consulted for CommonJS resolution only, and this
sidecar is `type: module`. Node resolves its bare `playwright-core` import by walking up
from the `.mjs` file, so the deps must be on that path.

## The signal changes: from "which branch" to "is this the current runner"

Once installed, `_code_branch()`'s `git rev-parse` finds no repository, returns `""`, and
the #306 banner can never fire again. That is correct — an installed runner has no branch
to be wrong about — but it would leave the real question unanswered. The failure mode
survives packaging in a new form: **a runner that was installed and then never updated.**

So the runner reports two new facts on the heartbeat, and the server holds the third:

| Field | Meaning |
|---|---|
| `Runner.code_version` | The runner package's `__version__` — human-readable, shown in the UI. |
| `Runner.code_sha` | **The sha of the last commit that touched `packages/canopy_runner/canopy_runner/`** — not the repo HEAD. |
| `settings.RUNNER_CODE_SHA` | The same quantity, computed at image build time and injected as an env var. |

The alert is `both non-empty and different`. Comparing *the same quantity* on both sides is
the whole design:

- **Not repo HEAD.** A source-mode runner's HEAD moves on every commit to canopy-web;
  comparing that against anything would alert on a frontend CSS change. `git log -1
  --format=%H -- <runner source dir>` is the same number on both sides and moves only when
  the runner's own code moves.
- **Not a version number a human bumps.** A signal that depends on someone remembering to
  bump `__version__` is decorative on the day they forget — which is the day it matters.
  The sha is computed, so there is nothing to forget. `code_version` is kept for
  legibility, not for the comparison.
- **Both provenances compute it identically.** In source mode the runner runs the `git log`
  itself; in installed mode the install script bakes the answer into `canopy_runner/_build_info.py`
  before building the wheel. A source runner therefore gets "your checkout is behind" for
  free, on the same code path.
- **Fail-safe on either side missing.** Empty sha (cloud runner, a shallow clone, a dev
  box, git absent) means no alert. A staleness alert that fires on incomplete information
  is worse than none, and this repo has paid for `NULL`-means-*something* predicates
  before.

The deploy workflow gains `fetch-depth: 0` (a depth-1 checkout makes `git log -- <path>`
answer "" or lie) and passes `RUNNER_CODE_SHA` as a build arg. The `ENV` line sits late in
the Dockerfile so a runner-only change doesn't bust the layer cache above it.

### Why the branch alert stays

A source-mode runner is still legitimate, still possible, and still exactly as dangerous as
it was in #306. The two alerts answer different questions — *"is this checkout on the wrong
branch"* and *"is this runner running the current code"* — and after this change a given
runner can only really trip one of them. They merge into one presentational helper
(`runnerCodeAlerts`) emitting a discriminated union, so the supervisor renders one stack of
banners rather than two components that each independently decide what "unreachable" means.
The `unreachable` → **Retire** affordance from #380 applies to both: a quiet runner cannot
heartbeat its way out of either state.

## What this does not do

- **The cloud runner is untouched.** It reports no sha, so it never alerts. It is a
  different program (`deploy/ec2-runner/cloud_runner.py`) with its own deployment; giving it
  `canopy-runner`'s version would be comparing two unrelated artifacts. The field is there
  when it wants it.
- **It does not re-provision the live laptop.** The first install is a one-time bootstrap
  and it restarts the daemon; running it is a human act, not something a PR does. Until it
  runs, the box keeps working exactly as it does today (source mode, hand-patched plist),
  and the branch alert keeps covering it.
- **It does not make `uv run` in the package worse.** `[tool.uv.sources]` keeps
  `editable = true` for development; the install path never consults it.

## Verification

The claim "an installed runner cannot be moved by `git checkout`" is only worth what its
proof is worth, so the suite includes an actual install:

1. Build all three wheels; unzip the runner wheel and assert `cdp/emdash_control.mjs` +
   `cdp/package.json` are inside it (A4).
2. `uv tool install` into an env-isolated `UV_TOOL_DIR`/`UV_TOOL_BIN_DIR` — never the
   developer's real tool dir — and run `canopy-runner --version`.
3. Assert the installed console script resolves outside any git repository, and that
   `_build_info.SHA` survived the build.

Plus the ordinary gates: `canopy_runner`'s suite, the backend suite (heartbeat persists and
serves the new fields; the alert helper's parity), the frontend unit tests for the merged
alert helper, and a regenerated `frontend/src/api/generated.ts`.
