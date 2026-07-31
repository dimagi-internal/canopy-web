# The cloud runner updates itself too

**Date:** 2026-07-30
**Status:** designed
**Supersedes for the cloud box:** `runner/ec2/README.md` § "Updating the runner code"
**Sibling:** `docs/superpowers/specs/2026-07-28-runner-as-installed-package-design.md`
(the laptop half — this spec is that spec applied to the other runner)

## The problem

The laptop runner auto-updates: a launchd timer asks the control plane which sha
was DEPLOYED, and installs exactly that when the box is behind and idle. Shipping
laptop runner code is therefore `merge to main` + a deploy.

The cloud runner has none of it. Its bytes reach the box through Secrets Manager
(`canopy/cloud-runner/runner-code`), published by an operator running `up.sh` from
a laptop with AWS credentials, and re-installed by `canopy-fetch-env` on every
service start. So shipping cloud runner code is: find the operator, find their AWS
session, `./up.sh && systemctl restart canopy-runner`. Nothing on the box knows
what it *should* be running, and nothing on the server knows what it *is* running —
`Runner.code_sha` is empty for every cloud box, and `codeProvenance.ts` documents
that silence as intended ("the cloud runner is a separate program that reports
none").

The result is a box that drifts silently and only ever in one direction. Measured
2026-07-30: the stack was created 2026-07-26 and never updated, while
`runner/ec2/` had moved since — nobody had noticed, because there is no surface on
which that could have shown up.

## What "expected" means for a cloud box

The same thing it means for a laptop, computed over a different path.

`deploy-labs.yml` already resolves `RUNNER_CODE_SHA` as
`git log -1 --format=%H -- runner/canopy_runner/canopy_runner`. This adds
`RUNNER_CLOUD_CODE_SHA` over **`runner/ec2/cloud_runner.py runner/ec2/bootstrap_agents.sh`**,
with `RUNNER_CLOUD_CODE_COMMITTED_AT` as the `%ct` of that same commit — a sha is an
identity and cannot say "behind" on its own, which is the whole reason the laptop
carries both.

Both files, not just the runner: `bootstrap_agents.sh` is re-run on every service
start, but nothing ever *triggers* a start. A bootstrap fix therefore ships today
only when a human happens to restart the box, which is the same absent event this
spec exists to supply.

`RunnerOut.expected_code_sha` resolves per `obj.kind` — `cloud` gets the cloud
pair, everything else keeps today's. That is the entire server surface: the
supervisor's existing banner, its ordering, and its unknown-means-silent rule all
carry over untouched.

### The heartbeat that erases it

`services.heartbeat()` assigns `code_sha` / `code_branch` / `code_version` /
`code_committed_at` **unconditionally**, and the cloud runner's primary heartbeat
is a WebSocket frame whose consumer (`apps/realtime/consumers.py::_heartbeat`)
calls that function with no provenance at all. So provenance sent on the REST
paths would be wiped by the next WS beat 20 seconds later, and the field would
read empty forever while looking like it had simply never been reported.

This is the bug class `provenance.py`'s own docstring records — six call sites,
four of which silently reset the field — so the fix is the same shape: the
consumer passes provenance through, and the cloud runner stamps it in ONE place
(`_heartbeat_body()`) used by all four of its heartbeat call sites (idle REST,
lease renewal, pairing, WS beat).

The laptop is unaffected: it uses that socket only as a wake listener and never
sends a `heartbeat` action over it.

## What the box knows about itself

**`/opt/canopy-runner/build-info.json`** — `{sha, committed_at, installed_at, ref}`,
written by whoever installed the bytes. There is no git history at
`/opt/canopy-runner`, so the running code cannot derive its own provenance the way
a source-mode laptop runner can; it has to be stamped, exactly as
`install-runner.sh` stamps `_build_info.py`.

Missing, unreadable, or malformed means **UNKNOWN**, and unknown does nothing and
says nothing. This repo has paid for empty-means-something predicates before.

**`/opt/canopy-runner/in-flight`** — `{count, at}`, rewritten every heartbeat tick,
byte-identical to `update.mark_busy`'s file so the two runners' markers stay
interchangeable. Including the rule that matters most: a marker older than 120s is
not "busy", it is a daemon that has stopped running its loop — which is precisely
the case auto-update exists to rescue, so it must never block the update.

## The updater

`runner/ec2/update_runner.sh`, installed to `/usr/local/bin/canopy-runner-update`
and driven by a `canopy-runner-update.timer` every 30 minutes.

**A separate systemd unit, not a thread in the runner.** A runner that
crash-loops is exactly when auto-update matters, and a self-updating process
cannot rescue itself: it never heartbeats, never learns it is behind, and stays
bricked until a human notices. An independent timer means shipping a fix is
enough. (Same reasoning, verbatim, as the laptop's separate launchd job.)

It runs as root, so it can write `/opt` and restart the unit with no sudo dance.

**Read-only against the control plane.** It asks `GET /api/harness/runners/` for
its own row's `expected_code_sha` and must NEVER heartbeat — a heartbeat from this
second process would stamp the runner ONLINE and forge liveness for a daemon that
may be dead.

The decision is `update.py` transliterated:

| verdict | meaning | action |
|---|---|---|
| `current` | stamp == expected | nothing |
| `stale` | differ, nothing in flight | install `expected` |
| `busy` | differ, a turn is in flight | nothing; try in 30 min |
| `unknown` | either side empty / unreachable | nothing |

Every verdict exits 0. This runs on a timer, and a non-zero exit for the ordinary
case fills the journal with false failures and trains everyone to ignore it.

**Install pins to the sha, never `origin/main`.** `git -C /opt/canopy-web` fetches
that commit (the clone is `--depth 1`, so fetch it by sha with a deepening
fallback), then `git show <sha>:runner/ec2/cloud_runner.py`. Installing main
instead would run code nothing has deployed AND leave `code_sha != expected`
permanently, so the staleness banner would fire forever on exactly the boxes
auto-updating correctly.

Reading the file out of git rather than checking the clone out also means an agent
turn that leaves `/opt/canopy-web` on a branch cannot change what gets installed —
the coupling that bit the laptop three times.

Before anything is moved into place the candidate is `python3 -m py_compile`d:
systemd's `ExecStart` points straight at this file, so a truncated or garbage copy
is a box that will not boot. Then stamp, then `systemctl restart canopy-runner`.

**Escape hatches**, both deliberate acts: `--ref <ref>` installs something else
(a branch, for debugging a box), `--from-secret` restores the Secrets Manager
bytes. `--check` prints the verdict and changes nothing.

## Secrets Manager becomes a first-boot seed

`canopy-fetch-env` runs on every service start and rewrites
`/opt/canopy-runner/cloud_runner.py` from the secret. Left alone it would revert
every auto-update at the next restart, so it changes to install the secret's bytes
only when that file is **absent**. Its `runner.env` handling is untouched.

`up.sh` also publishes the seed's PROVENANCE, so a fresh box's first update check
is a real answer rather than `unknown`. That goes in a second Secrets Manager entry
(`canopy/cloud-runner/runner-code-sha`) and deliberately NOT in a CloudFormation
parameter: a parameter is interpolated into UserData, CFN applies a UserData change
to a running instance as a **stop/start**, and this value changes on every runner
commit — so a parameter would bounce the live box, new public IP and all, every
time anyone ran `up.sh`. If the operator's clone cannot resolve the sha, `up.sh`
says so — a seed with no stamp means that box never auto-updates, and silence there
is indistinguishable from the feature working.

**Consequence, stated plainly:** `./up.sh` is no longer how runner code reaches a
running box. Code ships by merge + deploy, like the laptop's; the on-box override
is `canopy-runner-update --ref` / `--from-secret`.

## Rollout

`canopy-fetch-env`, the systemd units and the updater script all live in
cloud-init's `write_files`, which only runs on an instance's FIRST boot. So this
ships with a stack recycle:

```bash
cd runner/ec2 && ./down.sh && ./up.sh && ./wire.sh --drill
```

The order matters in one respect only: the deploy carrying `RUNNER_CLOUD_CODE_SHA`
should land first, or the fresh box's first checks read `unknown` (harmless, and
self-corrects at the next deploy).

## Testing

- `runner/ec2/tests/` — stdlib-only, run in CI from its own dir in a bare env
  (the only thing proving `cloud_runner.py` has not grown a dependency on
  canopy-web's environment): stamp reading incl. missing/garbage, the in-flight
  marker, and that every heartbeat call site carries provenance.
- A pytest-driven bash test for `update_runner.sh` with fake `curl` / `git` /
  `systemctl` on PATH, the way `test_install_job_self_protection.py` drives the
  laptop installer — the verdict logic and "which commands ran against what" is
  invisible to any amount of reading the script.
- Django: a `cloud` row serves the cloud expected sha while an `emdash` row serves
  the laptop one, and a WS heartbeat no longer erases reported provenance.

## Out of scope

- `packages/canopy_transcript` and `canopy_acp` are exposed onto `sys.path` from
  the clone at main tip on every start, not at the deployed sha. Pre-existing, and
  a different question (they are libraries, not the executable); noted so the
  inconsistency is on the record rather than discovered later.
- Giving the box write access to its own code secret. The update source is git
  precisely so a runner cannot rewrite the channel it boots from.
