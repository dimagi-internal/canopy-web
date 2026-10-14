# Cloud capabilities: agents declare, canopy-web allows and leases (design)

**Status:** approved in conversation 2026-10-14 (Jonathan). The direction:
- "launch a new machine just for the time we need it";
- "canopy-web will be the place where the agent expresses and claims capabilities";
- "all the specifics belong to the agent (or a shared repo it's pointing at)";
- "its whole infra as code should be documented, not one-off created".

**First capability:** ACE's `android-emulator`, used for Phase 6 device walks and
`/ace:qa-deep`. It replaces ace-web's `/api/mobile`. ace-web keeps no knowledge of
capabilities.

## Intent

An agent turn on any runner, including the KVM-less cloud runner, can say "I need my
`android-emulator`" and get **its own** machine. The machine boots from the agent's
image, is driven through canopy-web, and is **terminated** when the agent is done.

**The split:**

| | canopy-web (generic, owns nothing domain-specific) | the agent's repo (or a shared repo it points at) |
|---|---|---|
| What a capability *is* | the manifest schema + the `ec2-ephemeral` kind contract | `capability.yaml`: name, stack, params, limits, exec allowlist |
| Infra | nothing new: it calls AWS with the permissions the agent's stack grants it | the CloudFormation stack (an instance of canopy's shared template), the image build, the deploy and bake scripts, the README |
| Software on the machine | nothing | the boot hook and the domain CLI (e.g. `ace-mobile`) |
| Policy | **allow** (workspace-admin grant), caps, audit, sweep | requests within its grant |

**Success:**
- Two ACE runs on `cloud-ec2-2` walk Phase 6 at the same time on two separate emulators,
  with no ace-web and no instance id in their path.
- Five minutes after both finish, no tagged instance is running.
- Recreating everything from an empty account is a documented sequence of scripts in the
  ACE repo.

**Not in v1:**
- warm pools;
- GPU;
- interactive streaming;
- per-workspace AWS accounts;
- capability kinds other than `ec2-ephemeral`;
- canopy-web fetching manifests from git itself (the agent publishes them);
- chargeback beyond audit events;
- moving labs' EMOD runner over.

## Why this shape (decided, kept short)

- **Ephemeral EC2, not Fargate tasks:** labs' synthetic-job lifecycle is right (own compute
  per job, deploy-proof, idempotent launch, cap = live tagged count, self-cleaning), but
  Fargate has no KVM. We apply that lifecycle to EC2: `RunInstances` from a launch
  template per lease, `TerminateInstances` on release.
- **Not a stop/start pool:** ace-web's single shared start/stop instance is long-lived
  mutable state that the next user inherits. Its per-recipe lock and unguarded destructive
  ops let concurrent runs corrupt each other, and its jobs die with the web process on
  deploy. A fresh instance from the image *is* the precondition.
- **Boot cost** is about the same as start-from-stopped: ~230 s to EC2 status-ok on
  m8i.xlarge, plus the emulator cold boot.

## The contract

### 1. Declaring: in the agent's repo

`runtime.yaml` (canopy_runtime schema) gains:

```yaml
capabilities:
  - manifest: infra/capabilities/android-emulator/capability.yaml   # path in this repo
```

`capability.yaml` (schema `canopy_runtime.schema.CapabilityManifest`, version 1):

```yaml
version: 1
name: android-emulator            # [a-z0-9-]{3,40}; unique per agent
kind: ec2-ephemeral               # the only kind v1 brokers
description: Android 14 emulator with CommCare + Maestro, driven by the ace-mobile CLI.
aws:
  region: us-east-1
  stack: canopy-cap-ace-android-emulator   # canopy-web reads this stack's Outputs
limits:                           # ceilings; a grant may only lower them
  max_concurrent: 4
  max_lease_s: 14400
  idle_release_s: 900
  ready_timeout_s: 900
params:                           # what an acquire may pass; delivered to the VM as user-data
  state: {type: string, default: cc-2.63.0, enum: [cc-2.62.0, cc-2.63.0]}
exec:
  allow: [ace-mobile]             # argv[0] allowlist; anything else is refused by canopy-web
```

**Publishing** is part of the agent's deploy, not a hand step.
`canopy-runtime publish-capabilities --spec runtime.yaml --agent <slug>` loads every manifest,
validates it, and sends `PUT /api/agents/{slug}/capabilities` with the list. That route is for
the agent's admins (the same gate as the runtime-registry upsert). It is idempotent, and a
capability missing from the list is retired (its live leases are released).

### 2. The `ec2-ephemeral` kind: what the agent's stack must provide

The stack is an instance of canopy's shared template,
`sdk/python/src/canopy_sdk/ondemand/assets/ondemand-capability.cfn.yaml` with `Mode: ephemeral`.
That template is the "shared repo it points at". An agent may write its own instead, as long
as the stack exposes the same contract.

- **Outputs:** `LaunchTemplateId`, `ArtifactsBucketName`, `InstanceRoleArn`, `Capability`.
- **Tagging:** the launch template tags instances `canopy:capability=<name>` and
  `canopy:owner=<agent>`.
- **AMI pointer:** the AMI is resolved from SSM parameter `/canopy/capabilities/<agent>/<name>/ami`.
  A rebake writes the parameter; there is never an instance id or AMI id to re-pin.
- **Self-termination:** `InstanceInitiatedShutdownBehavior: terminate`, plus an idle alarm whose
  action is **terminate**.
- **Broker permissions:** the stack grants the broker role (parameter `BrokerRoleName`, which
  for canopy-web is `labs-jj-ecs-task-role`):
  - `ec2:RunInstances` from this launch template only, with those request tags;
  - `ec2:TerminateInstances`, `ec2:DescribeInstances`, `ssm:SendCommand` and
    `ssm:GetCommandInvocation` on instances carrying the tag;
  - `iam:PassRole` on the instance role;
  - S3 on the bucket;
  - `cloudformation:DescribeStacks` on the stack.

  The agent's stack is what lets canopy-web act, and only on that agent's machines.
- **The image** must:
  - read user-data JSON `{"lease_id", "params", "max_lifetime_s", "bucket"}`;
  - when ready, write `s3://<bucket>/vm/<instance-id>/ready.json`
    (`{"ok": bool, "reason"?, "timings"?, "cli_version"?}`);
  - halt at `max_lifetime_s`;
  - put every allow-listed CLI on `PATH`.

  The instance role may only write `vm/${aws:userid}/*`. An instance role can't see its own
  tags, so the scoping is by instance id.

### 3. Allowing: canopy-web

- **`CapabilityGrant`:** `(workspace, agent, capability_name, max_concurrent?, max_lease_s?)`,
  set by a workspace admin. Without a grant, acquire returns 403 naming the grant to ask for.
  A grant's limits can only lower the manifest's.

### 4. Leasing and using: canopy-web API (PAT-authenticated; each route is also an MCP tool)

The caller is the agent's own account, or a member of the agent's workspace acting for it,
which covers Jon's laptop sessions.

| Route | Does |
|---|---|
| `POST /api/capabilities/leases` `{agent, capability, params?, turn_id?}` | Validate params against the manifest. Under the cap: `RunInstances` with `ClientToken=lease_id` (a retry can never launch twice) → `provisioning`. Over the cap: `queued`. Returns `{lease_id, status, position?}` immediately. |
| `GET /api/capabilities/leases/{id}` | Heartbeat. While provisioning, checks for `ready.json` (a `HeadObject`; no SSM, no worker). Returns `status`, `ready`, `instance_id`, `expires_at`. |
| `DELETE /api/capabilities/leases/{id}` | Terminate → `released`. |
| `POST /api/capabilities/leases/{id}/exec` `{argv[], timeout_s}` | `argv[0]` must be on the allowlist. `SendCommand` with the shell-quoted argv and output to `s3://…/leases/<id>/exec/`. Returns `{exec_id}` (the SSM CommandId). |
| `GET /api/capabilities/leases/{id}/exec/{exec_id}` | `GetCommandInvocation`, returning `{status, exit_code, stdout_tail, stdout_url, stderr_url}`. |
| `POST /api/capabilities/leases/{id}/uploads` `{names[]}` | Presigned PUT URLs under `leases/<id>/in/`, plus the `s3://` URIs to pass in argv. |
| `GET /api/capabilities/leases/{id}/artifacts?prefix=` | Presigned GETs for `leases/<id>/out/<prefix>…` (1 h TTL). |
| `GET /api/agents/{slug}/capabilities` | Declared capabilities, grant state, live leases. |

- **Lease states:** `queued → provisioning → ready → released`, with failure exits `failed`
  and `expired`.
  - A `ready` lease never returns to `provisioning`: a sick machine is released and a new
    one acquired (restore, don't adapt).
  - Every transition is an `events.record` row (`capability.lease.*`).
- **SSM is the job store.** There are no web-process threads and no Redis jobs, so a
  canopy-web deploy mid-exec loses nothing.
- **Turn binding:** with `turn_id` (the cloud runner exports `CANOPY_TURN_ID` to the turn's
  process), the lease is released when that turn reaches a terminal status
  (`harness.signals.turn_status_changed`).

### 5. Cost guard: four layers, any one of which suffices

1. Explicit release.
2. Turn terminal (with `turn_id`), or no heartbeat for `idle_release_s`.
3. **Sweep** on harness's claim tick, next to `sweep_expired_leases()` (runners poll every
   ~15 s). It:
   - expires leases past `expires_at` and idles stale ones;
   - promotes the FIFO queue;
   - terminates any `canopy:capability`-tagged instance with no live lease (orphans from a
     crash between `RunInstances` and the DB write).
4. Machine-side: halt-at-`max_lifetime_s`, shutdown = terminate, and an idle alarm that
   terminates. These work while canopy-web itself is down.

## canopy-web implementation

- **`apps/capabilities/`**, a FRAMEWORK app (agent-agnostic substrate, added to `FRAMEWORK`
  in `tests/test_architecture_boundary.py`). It imports `agents`, `harness`, `events` and
  `workspaces`.
- **Models:**
  - `AgentCapability` (the published manifest, plus `stack_outputs` resolved at publish via
    `DescribeStacks`);
  - `CapabilityGrant`;
  - `CapabilityLease`.
- **SDK:** `canopy_sdk.ondemand.EphemeralInstance` (launch, describe, run_async, invocation,
  terminate, list_tagged), next to the unchanged `OnDemandInstance`. The template gains
  `Mode: ephemeral | persistent`; the default `persistent` keeps labs-emod as it is.
- **Schema:** `canopy_runtime.schema` gains `capabilities:` and `CapabilityManifest`, plus the
  `publish-capabilities` CLI.
- **Runner:** `runner/ec2/cloud_runner.py` exports `CANOPY_TURN_ID` to the turn's process.
- **Docs:** `docs/architecture/capabilities.md` (the contract above, for agent authors).

## ACE implementation (all specifics)

- **`infra/capabilities/android-emulator/`**, the whole IaC, documented:
  - `capability.yaml`;
  - `stack.params.json` (parameters for canopy's shared template, pinned to a `dimagi-canopy`
    tag);
  - `deploy.sh`: fetch the pinned template, `aws cloudformation deploy`, then
    `canopy-runtime publish-capabilities`;
  - `image/`: the Packer template and files moved from ace-web `infra/mobile-ami/`, plus the
    boot hook and the CLI;
  - `bake.sh`: Packer build, then write the SSM AMI parameter;
  - `README.md`: from an empty account to a live lease, step by step;
  - `teardown.sh`.
- **`ace-mobile` CLI**, baked into the image. It is the port of ace-web
  `apps/mobile/controller.py`'s shell builders. Each subcommand prints one JSON line and
  writes artifacts to `--out`:
  - `diagnose`
  - `run-recipe --recipe s3://… [--palette s3://…] [--env K=V…] --out s3://…`
  - `register-test-user`
  - `ui-dump`
  - `screenshot`
  - `install-apk`
  - `clear-app-data`
  - `repair-driver`
  - `install-driver`
  - `snapshot save|load`
  - `set-location`: new, and possible because the CLI runs beside the emulator.
- **`mcp/mobile/backends/lease.ts`:** the `MobileBackend` interface over the canopy-web lease
  API, so no skill changes.
  - Acquire on `ensureAvdRunning(state)`, reusing this session's live lease, then poll to ready.
  - Each op is an exec of the matching CLI subcommand, plus the artifact download.
  - `stopAvd` releases.
  - Auth is the agent's canopy-web PAT, with `turn_id` from `CANOPY_TURN_ID` when present.
- **Backend selection:** `ACE_MOBILE_BACKEND` if set (`local` | `lease` | `cloud`, where
  `cloud` is the legacy ace-web transport kept for one release). Otherwise `local` when a
  provisioned AVD exists, else `lease`.
- **`runtime.yaml`** declares the capability. `bin/ace-doctor` probes the declaration, the
  grant and the stack.

## Rollout

1. **canopy-web:** SDK + template mode + schema/CLI + `apps/capabilities` + runner env + docs.
2. **ACE:** `infra/capabilities/android-emulator/` + CLI + `lease.ts` + selection + doctor.
3. **Live:**
   1. `deploy.sh` (stack + publish);
   2. `bake.sh` (AMI → SSM parameter);
   3. grant `ace` in its workspace;
   4. acquire → `ace-mobile diagnose` → release → the instance is terminated;
   5. one Phase 6 smoke recipe on the lease backend;
   6. two concurrent leases.
4. **Retire ace-web `/api/mobile`:**
   - delete `apps/mobile`, `infra/mobile-ami`, `deploy/aws/ace-mobile.cfn.yaml` and the
     task-def env and policy;
   - delete the `ace-mobile` stack (its resources are `Retain`, so terminate the instance and
     empty and delete the bucket explicitly, after the new path is verified).

## Open questions

- **Boot budget:** the expected ~5–7 min is to be measured in Rollout 3. It decides whether a
  warm pool is ever needed.
- **Cap defaults:** these are guesses sized to a Phase 6 walk; revisit after a week of
  `capability.lease.*` events.
