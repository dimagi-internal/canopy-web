# Cloud capabilities: leased, ephemeral machines for agents (design)

**Status:** direction set in conversation 2026-10-14 (Jonathan): "launch a new machine
just for the time we need it", and "canopy-web lets agents specify and use cloud
capabilities like these". This spec awaits review.
**First capability:** `android-emulator`, used by ACE's device walks (Phase 6, `/ace:qa-deep`).
It replaces ace-web's `/api/mobile` runner.

## Intent

An agent turn running on any runner, including the KVM-less cloud runner, can say
"I need an Android emulator" and get **its own** machine. The machine boots from a baked
image, is driven through canopy-web, and is **terminated** when the turn is done with it.
The mechanism is generic: a capability is any workload that needs hardware or an image a
runner doesn't have, such as an emulator, an EMOD model box, or a GPU later.

**Success:**
- Two ACE runs on `cloud-ec2-2` walk Phase 6 at the same time on two separate emulators.
- Neither run has ace-web, an instance id or AWS credentials in its path.
- Five minutes after both turns finish, no tagged instance is running.

**Not in v1:**
- warm pools (an ASG warm pool of stopped instances is the latency lever if boot time hurts);
- GPU capabilities;
- interactive streaming (VNC or port-forward);
- per-workspace AWS accounts;
- Fargate-shaped capabilities (no KVM; labs' synthetic `RunTask` path covers those already);
- billing / chargeback beyond an audit row per lease;
- moving labs' EMOD runner onto this (it is a candidate, not a v1 consumer).

## Today, and why not keep it

ace-web's `/api/mobile` drives **one shared, long-lived EC2 instance**
(`ACE_MOBILE_INSTANCE_ID`, pinned in `ace-web.cfn.yaml`).
- **Lifecycle:** started and stopped, never terminated, over SSM.
- **Locking:** a Redis lock covers **one recipe**, not a run. Destructive ops (`restart-runner`,
  `states/select`, `clear-app-data`, snapshot load, APK install) don't check the lock at all,
  so two runs corrupt each other.
- **Deploys:** recipe jobs are threads in ace-web's web process; a deploy kills them, and the
  lock then sits held for up to 30 minutes.
- **Rebakes:** `rebake.sh` replaces the instance outside CloudFormation and rewrites the pinned
  id through a PR.
- **Cloud runner:** `cloud-ec2-2` can't reach it at all (no PAT, ace#2905).
- **Android logic placement:** the Android-specific shell is ~1,800 lines of `controller.py`
  inside a web app.

`canopy_sdk.ondemand` (0.7.x) is the extracted, generic form of that controller:
`OnDemandInstance` (start / stop / SSM run), `Lease`, `JobStore`, and the
`ondemand-capability.cfn.yaml` stack with a `NestedVirtualization` switch. Labs' EMOD
runner uses it. It keeps the same one-fixed-instance shape, so it inherits the
sharing problem.

**Why not labs' "tasks":** labs' synthetic jobs launch a Fargate `RunTask` per job: own
compute, deploy-proof, live tagged-count cap, idempotent launch, self-cleaning. That is
the lifecycle we want. Fargate has no KVM, so an emulator can't run there. **We take
labs' task lifecycle and apply it to EC2:** `RunInstances` from a launch template per
lease, `TerminateInstances` on release.

**Why not a pool of stop/start instances:** each pooled instance is long-lived mutable
state that someone must reset between users. That reset is the "restore the
precondition" step ACE keeps getting wrong. A fresh instance from the image *is* the
precondition. Boot cost is roughly the same as today's start-from-stopped (~230 s to
EC2 status-ok, measured on m8i.xlarge, plus emulator cold boot). A first-boot volume
reads lazily from the snapshot; fast snapshot restore is the fix if that shows up.

## Shape: three layers

```
 agent turn (any runner) ──HTTPS──▶ canopy-web  apps/capabilities  (broker; framework tier)
   ace-mobile MCP                    ├─ Provider / Grant / Lease rows
   = lease client                    ├─ EC2 RunInstances / TerminateInstances  ┐ via SDK
                                     ├─ SSM SendCommand / GetCommandInvocation ├ EphemeralInstance
                                     └─ S3 presign (inputs in, artifacts out)  ┘
                                              │ SSM (no inbound ports)
                                              ▼
                                   per-lease EC2 from the capability image
                                   └─ domain CLI baked in (e.g. `ace-mobile run-recipe …`)
```

1. **SDK: `canopy_sdk.ondemand.EphemeralInstance`.** This sits next to `OnDemandInstance`.
   - `launch(lease_id, tags)` calls `RunInstances(LaunchTemplate, ClientToken=lease_id)`.
     The client token makes a retried acquire idempotent: same instance, never two.
   - `describe()`
   - `run_async(argv, timeout_s) -> command_id`
   - `invocation(command_id)`
   - `terminate()`
   - `list_tagged(capability)`, for the sweeper and the live-count cap.

   It shares `ssm.py` and `errors.py`. `OnDemandInstance` stays as it is for EMOD.
2. **canopy-web: `apps/capabilities/`, a FRAMEWORK app.** It is a generic substrate, agent-agnostic,
   listed in `FRAMEWORK` in `tests/test_architecture_boundary.py`. It owns the registry,
   grants, leases, the HTTP/MCP surface, the sweeper and the AWS calls. It imports
   `harness` and `agents`. Harness never imports it, except one sweep call on the claim tick.
3. **Capability image + domain CLI, owned by the domain.** This is the decision on the
   Android-tooling fork.
   - All Android knowledge moves **into the image**, as a CLI with JSON in and out (see
     § Android provider).
   - The broker only knows "run this argv, here are input files, collect artifacts".
   - The image source lives in the **domain repo**. For Android that is
     `dimagi-internal/ace` `infra/android-emulator/`, moved from ace-web `infra/mobile-ami/`,
     because Maestro recipes, CommCare APK versions and the test-user flow are ACE's.
   - canopy-web holds no Android code.

## Data model (`apps/capabilities/models.py`)

- **`CapabilityProvider`**
  - `slug` (`android-emulator`), `display_name`, `region`.
  - Stack outputs: `launch_template_id`, `artifacts_bucket`, `instance_role_arn`.
  - `ready_timeout_s` (default 900), `max_lease_s` (default 4 h), `idle_release_s` (default 15 min),
    `max_concurrent` (default 4).
  - `enabled`.
  - Registered by a fleet admin (`PUT /capabilities/providers/{slug}`) from the stack's outputs.
  - The AMI is **not** a field: the launch template resolves it from an SSM parameter
    (`/canopy/capabilities/<slug>/ami`). A rebake writes that parameter and touches no code
    or config. That kills `rebake.sh`'s PR-to-repin step.
- **`CapabilityGrant`**: `(workspace, agent | null, provider, max_concurrent, max_lease_s)`.
  - Granted by a workspace admin. `agent=null` grants every agent in the workspace.
  - Lower limits than the provider's win.
  - No grant means acquire returns 403 with the grant to ask for.
- **`CapabilityLease`**
  - `id` (uuid; also the EC2 `ClientToken` and the `canopy:lease` tag), `provider`, `workspace`, `agent`.
  - Holder: `turn` (nullable FK) **or** `user` (PAT holder, for interactive laptop sessions).
  - `params` (JSON: e.g. `{"state": "cc-2.63.0"}`, passed to the image's boot hook as user-data).
  - `status`, `instance_id`.
  - Timestamps: `requested_at`, `ready_at`, `released_at`, `expires_at` (hard cap = acquire + `max_lease_s`),
    `heartbeat_at`.
  - `failure` (text), `exec_count`.
  - An audit `events.record` is written at every transition.

**States:** `queued → provisioning → ready → releasing → released`. The failure exits are
`failed` (with `failure`) and `expired`. A lease never goes back from `ready` to `provisioning`:
a sick machine is released and a new lease acquired. That is the restore-don't-adapt rule.

## Lifecycle

- **Acquire** (`POST …/leases {provider, params}`)
  1. Check the grant.
  2. Count live leases for this provider, workspace and agent against the caps. Live means
     `queued`, `provisioning` or `ready`; the authoritative instance count comes from
     `list_tagged`, not the DB alone, like labs' `at_capacity()`.
  3. Under the cap: `launch()` → `provisioning`.
  4. Over the cap: the lease is `queued` (FIFO by `requested_at`).

  The call returns at once with `{lease_id, status, position?}`. There is no long-poll
  HTTP request; ace-web's ten-minute synchronous `ensure-running` is a design we drop.
- **Ready.** The image's boot hook makes the capability ready (e.g. emulator booted, APK
  installed). It then writes `s3://<bucket>/leases/<lease_id>/ready.json`
  (`{"ok": true, "timings": …, "diagnostics": …}`), or `ready.json` with `ok:false` and a
  reason. `GET …/leases/{id}` checks for that object (a `HeadObject`) while the lease is
  `provisioning`.
  - Readiness needs no SSM round-trip and no worker.
  - Past `ready_timeout_s`, the lease becomes `failed` and the instance is terminated.
  - The VM learns its lease id from user-data. An instance role can't see its own instance
    tags as principal tags, so per-lease write scoping uses `${aws:userid}`
    (`<role-id>:<instance-id>`). The instance writes only `vm/${aws:userid}/*`, and the broker
    maps instance → lease. Expressed in `leases/<id>/` terms below for readability; the real
    keys carry the instance id.
- **Heartbeat.** Every `GET` or `exec` on the lease bumps `heartbeat_at`. A lease with no
  heartbeat for `idle_release_s` is released, which covers a client that crashed.
- **Release** happens on any of:
  - an explicit `DELETE …/leases/{id}`;
  - the holder turn reaching a terminal status (subscriber on `harness.signals.turn_status_changed`);
  - heartbeat idle;
  - `expires_at`.

  It calls `terminate()`, then `released`. Queued leases are promoted on every release and
  every sweep.
- **Sweep.** `capabilities.sweep()` runs on harness's claim tick (next to
  `sweep_expired_leases()`, `apps/harness/claim.py:544`; runners poll every ~15 s, so it is
  never idle while any runner lives). It does three things:
  - expires and idles leases;
  - promotes the queue;
  - **terminates any instance tagged `canopy:capability` whose `canopy:lease` is not a live
    lease** (orphans from a crash between `RunInstances` and the DB write).

  The sweep is the cost guard. Nothing else has to be correct for spend to stop.
- **Machine-side backstop**, for when canopy-web itself is down:
  - the launch template sets `InstanceInitiatedShutdownBehavior: terminate`;
  - the image runs a max-lifetime timer (`max_lease_s` + 10 min, read from user-data) that halts;
  - the SDK template's CPU-idle alarm takes the **terminate** action in ephemeral mode.

## Exec and files: stateless, deploy-safe

- **Exec.** `POST …/leases/{id}/exec {argv, timeout_s}` calls `SendCommand` with
  `OutputS3BucketName/KeyPrefix = leases/<id>/exec/` and returns `{exec_id: <CommandId>}`.
  `GET …/leases/{id}/exec/{exec_id}` calls `GetCommandInvocation` and returns
  `{status, exit_code, stdout_tail, stdout_url, stderr_url}` (full output from S3, presigned).
  - **SSM is the job store.** There is no web-process thread, no Redis job and no worker, so a
    canopy-web deploy mid-exec loses nothing. This is the specific failure ace-web has today.
  - `argv` is passed as a JSON array and shell-quoted by the broker. Raw shell is not accepted.
- **Inputs.** `POST …/leases/{id}/uploads {names[]}` returns presigned PUT URLs under
  `leases/<id>/in/`. The client uploads, then names the S3 keys in `argv`. This replaces
  base64 recipe and palette blobs inside SSM parameters.
- **Artifacts.** The CLI writes to `leases/<id>/out/<exec_id>/`. `GET …/leases/{id}/artifacts?prefix=`
  lists the objects with presigned GETs (1 h TTL). The bucket expires `leases/` objects after 7 days.
- **Size and time limits.** SSM caps a command at 48 h and inline output at 24 KB; we always
  use the S3 output. `timeout_s` is capped at the lease's remaining life.

## Auth surfaces

There are two holder kinds and one implementation. Both become MCP tools automatically
(`apps/mcp/api_tools.py`).
- **Turn-held** (cloud runner, any runner): `/runners/{rid}/turns/{tid}/capabilities/leases[...]`.
  This is the trust boundary of `turn_github_token` (`apps/harness/api.py:646`): only a turn
  this runner is executing right now, re-checked with `runner_may_hold_agent`. The lease
  dies with the turn.
- **User-held** (Jon's laptop session using the cloud emulator): `/capabilities/leases[...]`
  with a PAT. The lease is bound to the user and the agent named in the request, and it
  needs a grant for that agent in a workspace the user belongs to. It dies on heartbeat idle.
- The route gate is a new `capabilities` protocol in `apps/api/route_gates.py`.
- Fleet-admin-only: provider registration and `force-release`.

## Declaration: what an agent says it needs

- An agent declares the capabilities it may use in its runtime: `runtime.yaml`
  `capabilities: [android-emulator]`, mirrored to `Agent.runtime_capabilities` and served by
  `get_agent_runtime` (`apps/agents/api.py:741`).
- The declaration is **permission and display, not routing.** Every runner can reach the
  broker, so there is nothing to filter claims on.
  - Acquire for an undeclared capability is refused (422, "declare it in runtime.yaml").
  - Acquire for a declared but ungranted one is refused (403, naming the grant).
- `agent_topology` / `runner_topology` show each agent's declared capabilities, grant state
  and live leases (count, age, spend-hours). `CapabilityGrant` is the admin's lever.
- This is deliberately **not** a `RUNNER_FLAGS` value. Flags attest a property of a box,
  such as ZDR; a capability is something you rent.
- **Runner-local dependencies are a different thing.** `adb`, Chromium and similar belong to
  the runner image or `runtime.yaml` `preflight`. ace#2905 (Chromium on `cloud-ec2-2`) is
  fixed there, not here.

## Android provider (`android-emulator`)

- **Stack.** An instance of the SDK template in `Mode: ephemeral` (new parameter):
  - the launch template only, no `Instance` resource;
  - `NestedVirtualization: true` (set by `CpuOptions` in the launch template; the SDK
    template already supports that switch);
  - `m8i.xlarge`, 30 GB gp3, IMDSv2, an egress-only security group (443/53);
  - the AMI from the SSM parameter.

  The consumer policy, attached to canopy-web's task role:
  - `ec2:RunInstances` only from this launch template, with request tags
    `canopy:capability=android-emulator`;
  - `ec2:TerminateInstances` and `ssm:SendCommand` conditioned on that tag;
  - `iam:PassRole` for the instance role;
  - S3 on the bucket (the broker reads `vm/*`, writes `leases/*/in/`, presigns both).
- **Image** (Packer, from ace-web `infra/mobile-ami/`, moved to ACE `infra/android-emulator/`).
  It carries over: API 34 AVD, Maestro, CommCare APKs per state, the runner unit.
  - **Boot hook:** read `params.state` from user-data, cold-boot the AVD with `-wipe-data`,
    install that state's APK, write `ready.json`. The ready-marker logic carries over from
    `ace-emulator-launch`; the target becomes S3 instead of `/run`.
  - **CLI `ace-mobile`**, one subcommand per operation `/api/mobile` serves today, ported
    from `controller.py`'s shell builders:
    - `run-recipe --recipe s3://… [--palette s3://…] --out s3://…`
    - `register-test-user`
    - `ui-dump`
    - `screenshot`
    - `install-apk --apk s3://…|https://…`
    - `clear-app-data`
    - `repair-driver`
    - `install-driver`
    - `snapshot save|load`
    - `diagnose`

    Each prints one JSON result on stdout and writes artifacts to `--out`.
  - **Two new subcommands become possible** because the CLI runs beside the emulator:
    - `set-location`, which removes `CLOUD_MOCK_LOCATION_UNSUPPORTED`;
    - `record start|stop` (screenrecord), which gives cloud-recording parity.
  - **The CLI is tested in the ACE repo** against a recorded fixture.
  - The CLI version is stamped into `ready.json`, so a client can refuse an image older
    than it needs.
- **No lock anywhere.** One lease means one machine and one holder. Within a lease, ACE's
  existing session lock (`mcp/mobile/session-lock.ts`) still serialises the turn's own calls.

## ACE client

- **`LeaseBackend` replaces `CloudBackend`'s transport.** The `MobileBackend` interface is
  unchanged, so no skill changes.
  - `ensureAvdRunning(state)` acquires (or reuses this session's live lease) and polls to `ready`.
  - Every op becomes an `exec` of the matching CLI subcommand plus the artifact download.
    The S3-download and screenshot-dir code in `cloud.ts` carries over.
  - `stopAvd` releases. The MCP process releases on exit (best-effort; heartbeat idle is
    the real guarantee).
  - "Restore the precondition" becomes literal: a heal that would cold-boot the AVD
    releases and re-acquires.
- **Holder selection.** If the session is a canopy turn (`CANOPY_TURN_ID` and runner
  credentials present), use the turn-held routes. Otherwise use a user PAT
  (`~/.claude/canopy/workbench-token`).
- **Backend selection becomes automatic:**
  - `ACE_MOBILE_BACKEND` if set;
  - else `local` when a provisioned AVD exists;
  - else `lease`.

  The ppid-keyed toggle file stays as a manual override.
- **Phase-scoped lease.** The orchestrator holds one lease across a whole device phase (Phase 6
  smoke + capture). Heartbeats are implicit in its calls, and it releases at the phase boundary.
- `bin/ace-doctor` gains a `capability_lease` probe: grant present, provider enabled, a
  dry-run acquire refused only for cap.

## Rollout

Each step is one PR and leaves something working.

0. **ACE: the current path works for one run at a time.** ace#2907 (routes and query-param
   contract, plus its fixture test) is already open. Also put a PAT on `cloud-ec2-2` (ace#2905).
1. **SDK:**
   - `EphemeralInstance`;
   - `Mode: ephemeral` in `ondemand-capability.cfn.yaml` (launch template only, terminate
     behaviour, tag-conditioned consumer policy, lease-scoped instance-role S3);
   - the AMI-from-SSM-parameter launch template;
   - tests with stubbed boto3.

   Release `dimagi-canopy` 0.8.0.
2. **canopy-web `apps/capabilities`:**
   - models and migrations;
   - provider, grant and lease routes (turn-held and user-held);
   - the exec, upload and artifact routes;
   - the sweep on the claim tick and the `turn_status_changed` release;
   - the topology fields;
   - `docs/architecture/capabilities.md`.

   Tests: lifecycle state machine, cap and queue, orphan sweep, deploy-safety (exec status
   read back from a fresh process), auth boundaries (another runner's turn → 404,
   undeclared → 422, ungranted → 403).
3. **ACE `infra/android-emulator/`:**
   - the image moves over from ace-web;
   - the `ace-mobile` CLI and boot hook;
   - stack deploy (`canopy-cap-android-emulator`);
   - the first bake writes the SSM parameter;
   - provider registered.

   Live check: acquire, run `diagnose`, release, then confirm the instance is terminated.
4. **ACE `LeaseBackend`, automatic backend selection, the doctor probe.**
   - Live check: one smoke recipe on `cloud-ec2-2`, then two concurrent turns.
   - The ace-web path stays selectable as `ACE_MOBILE_BACKEND=cloud-legacy` for one release.
5. **Retire ace-web `/api/mobile`:**
   - delete `apps/mobile`, `infra/mobile-ami` and `deploy/aws/ace-mobile.cfn.yaml`;
   - terminate `i-0c447ed51374a4871` once its retained resources are confirmed unused;
   - remove the ace-web task-role mobile policy.

**Later, not v1:** move labs' EMOD runner from `OnDemandInstance` to a provider (it gains
concurrency and loses its pinned id); warm pool; GPU providers.

## Open questions

- **Account and role.** canopy-web, ace-web and labs share the `labs-jj` account and
  `labs-jj-ecs-task-role`. The consumer policy attaches there as ace-web's does today.
  Confirm before step 1 that canopy-web's task role is that role (its CFN takes
  `TaskRoleArn` as a parameter).
- **Boot budget.** A fresh launch with the emulator cold-boot is expected at ~5–7 min;
  measure it in step 3 before deciding on a warm pool.
- **Cap defaults.** 4 concurrent, a 4 h max lease and a 15 min idle release are guesses
  sized to a Phase 6 walk. Revisit after the first week of `capability.lease.*` events.
