# Runbook: prove a phone Continue lands in a real emdash session

The final link of the mobile loop types into real emdash, so it is human-gated — not a CI
test. This proves it with a **single** turn, **without** restarting the fleet daemon.

The automated layers cover everything up to this point:
- `tests/test_mobile_loop_e2e.py` (L1) proves the dispatch → claim → execute software chain
  with a recording fake emdash.
- `scripts/qa/smoke_mobile_loop.py` (L2) proves the live server loop (report → list →
  dispatch → resolve-to-reuse) end to end over the API.

This runbook (L3) closes the one remaining gap: the physical CDP drive into a real session.

## Preconditions

- **Daemon code is current.** The runner is an INSTALLED package, not a checkout (#512) —
  a `git pull` in `~/emdash-projects/canopy-web` no longer changes what the daemon runs.
  Update it with the installer, which builds a snapshot of `origin/main`:
  ```
  runner/canopy_runner/scripts/install-runner.sh
  ```
  `RunnerOut.code_sha` vs `expected_code_sha` on the Runners tab tells you whether a given
  box is behind.
- **emdash is running with the CDP port open** (`--remote-debugging-port=9222`, the runner's
  `cdp_port`).
- **The target project is OPEN IN EMDASH on that box.** There is nothing to declare: the
  runner reports `capabilities["projects"]` from emdash's own projects table on every
  heartbeat, and `PATCH .../runners/<id>` **422s** a hand-written `projects` (it would be
  overwritten seconds later). Open the repo as a project in emdash and it becomes routable
  within a tick. Confirm with:
  ```
  canopy project runners <repo>          # who would claim it now
  ```
  On a CLOUD runner, which has no emdash, the equivalent is its `RUNNER_PROJECTS` env.
- **Use a SCRATCH emdash task**, not a real work session, for the first run. Note its exact
  task name (what shows in the emdash sidebar). The `thread_key` is `emdash:<that-name>`.

## Steps

1. **The session shows on the phone.** Once the updated daemon ticks it reports its open
   sessions automatically; they appear on Supervisor → **Sessions**. (Or report on demand
   by letting the daemon take one tick.)

2. **Dispatch ONE Continue** into the scratch session — from the phone (Sessions → type a
   prompt → Continue), or with the helper:
   ```
   CANOPY_PAT=<raw> CANOPY_URL=https://labs.connect.dimagi.com/canopy \
     uv run python scripts/qa/dispatch_one_continue.py \
       --project canopy-web --workspace dimagi \
       --thread emdash:<scratch-task-name> --prompt "QA: add a one-line comment to the top of README"
   ```
   It prints the turn id and the exact `--drain-one` command to run next.

3. **Take exactly that one turn.** The global pause sentinel does NOT block `--drain-one`,
   so the rest of the fleet stays off:
   ```
   python -m canopy_runner.main --drain-one --config ~/.canopy/runner.json
   ```
   Expected output: `reused:<turn-id>` (the runner opened the existing session and sent the
   prompt). `created:<turn-id>:<task>` means it spawned a fresh session instead — check that
   the `thread_key` matched a reported session.

4. **Confirm** the prompt appears in the scratch emdash session and the model acts on it.
   That is the whole loop, proven physically.

## Rollback

- The turn is one-shot; nothing recurring was started. If you dispatched but decide not to
  run it: `POST /api/harness/turns/<id>/cancel`.
- To stop the runner claiming repo turns again, you can no longer edit its capabilities —
  the list is reported, so it would come straight back on the next heartbeat. Close the
  project in emdash (it stops being reported), or pause the runner entirely via the
  menu-bar app / the pause sentinel, which stops it claiming anything.
