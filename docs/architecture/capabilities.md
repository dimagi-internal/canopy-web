# Cloud capabilities — the agent author's contract

A **capability** is a machine an agent leases from canopy-web for exactly as long as it
needs it: "give me my `android-emulator`", get a fresh EC2 instance booted from the
agent's own image, drive it, release it, and it is terminated. Design record:
`docs/superpowers/specs/2026-10-14-cloud-capabilities-design.md`. Code: `apps/capabilities`
(framework tier), `canopy_sdk.ondemand.EphemeralInstance`,
`canopy_runtime.schema.CapabilityManifest`.

**Who owns what.** canopy-web owns the generic half and nothing domain-specific: the
manifest schema, the `ec2-ephemeral` kind, the grant, the lease lifecycle, caps, audit
and the sweep. The agent's repo (or a shared repo it points at) owns everything else:
`capability.yaml`, the CloudFormation stack, the image and its bake, the boot hook and
the CLI on the machine. canopy-web holds no AWS permission of its own for any of this —
it acts only with what the agent's stack grants its task role.

## 1. Declare it in your repo

`runtime.yaml`:

```yaml
capabilities:
  - manifest: infra/capabilities/android-emulator/capability.yaml   # relative to runtime.yaml
```

`capability.yaml` (`CapabilityManifest`, version 1; unknown keys are an error):

```yaml
version: 1
name: android-emulator            # [a-z0-9-]{3,40}, unique per agent (the shared template caps it at 31)
kind: ec2-ephemeral               # the only kind v1 brokers
description: Android 14 emulator with CommCare + Maestro, driven by the ace-mobile CLI.
aws:
  region: us-east-1
  stack: canopy-cap-ace-android-emulator   # canopy-web reads this stack's Outputs
limits:                           # ceilings; a grant may only lower them
  max_concurrent: 4               # 1-50 (default 1)
  max_lease_s: 14400              # 60-172800 (default 3600); the machine halts at this age
  idle_release_s: 900             # released after this long with no lease call (default 900)
  ready_timeout_s: 900            # failed if not ready this long after launch (default 900)
params:                           # what an acquire may pass; types: string|integer|number|boolean
  state: {type: string, default: cc-2.63.0, enum: [cc-2.62.0, cc-2.63.0]}
exec:
  allow: [ace-mobile]             # argv[0] allowlist: bare names on the machine's PATH
```

A param with no `default` is required at acquire.

**Publish as part of your deploy:**

```bash
canopy-runtime publish-capabilities --spec runtime.yaml --agent <slug> \
  [--base-url https://canopy.dimagi.com] [--token $CANOPY_TOKEN]
```

It validates every manifest and sends the whole list in one
`PUT /api/agents/{slug}/capabilities`. canopy-web validates again (same model), reads
each stack's Outputs, and refuses the publish (422, naming what is wrong) unless every
stack meets §2. Idempotent. A capability missing from the list is **retired** and its
live leases released. The caller must be the agent's owner or an admin, or the agent's
own login (its PAT).

## 2. The `ec2-ephemeral` stack

Deploy canopy's shared template with `Mode=ephemeral`
(`canopy_sdk.ondemand.assets.asset_path("ondemand-capability.cfn.yaml")`, pinned to a
`dimagi-canopy-v<version>` tag), or write your own that exposes the same contract.

```bash
aws cloudformation deploy --stack-name canopy-cap-<agent>-<capability> \
  --template-file ondemand-capability.cfn.yaml --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides Mode=ephemeral Capability=<capability> Owner=<agent-slug> \
    OwnerTag=<who-to-ask> InstanceType=m8i.xlarge VpcId=vpc-… SubnetId=subnet-… \
    NestedVirtualization=true BrokerRoleName=labs-jj-ecs-task-role
```

| Ephemeral parameter | |
|---|---|
| `Mode` | `ephemeral` (default `persistent`, the stopped-when-idle single instance) |
| `Capability` | the manifest's `name` |
| `Owner` | the agent's slug; tags instances `canopy:owner` and names the AMI parameter |
| `OwnerTag` | the human-facing `owner` tag |
| `BrokerRoleName` | the broker's IAM role; for canopy-web, `labs-jj-ecs-task-role` |
| `InstanceType`, `VpcId`, `SubnetId`, `NestedVirtualization`, `RootVolumeGb`, `UserData` | as for persistent |
| `AmiId`, `ConsumerTaskRoleName`, `IdleStopMinutes` | persistent only; leave empty |

What the stack must provide:

- **Outputs:** `LaunchTemplateId`, `ArtifactsBucketName`, `InstanceRoleArn`,
  `Capability` — and `Owner` (must equal the agent's slug) and `InstanceRoleId` (names
  each instance's S3 prefix). A publish names any that are missing.
- **Tags:** instances are tagged `canopy:capability=<name>` and `canopy:owner=<agent>`
  (the launch template sets them; canopy-web also sends them, plus `canopy:lease=<id>`).
- **AMI pointer:** the launch template's image is `resolve:ssm:/canopy/capabilities/<agent>/<name>/ami`,
  resolved by EC2 at every launch. A rebake writes that parameter; nothing is re-pinned.
  Write it before the first lease (EC2 may also check it when the stack creates the
  launch template, so bake before the first deploy if that refuses).
- **Self-termination:** `InstanceInitiatedShutdownBehavior: terminate`. There is no
  CloudWatch idle alarm in ephemeral mode (an alarm names one instance, and these do not
  exist at deploy); the image's own halt (below) and canopy-web's sweep cover it.
- **Broker permissions** (`BrokerPolicy` on `BrokerRoleName`), and nothing else:
  `ec2:RunInstances` from this launch template only, the instance only with both request
  tags; `ec2:CreateTags` at launch only; `ec2:TerminateInstances` and `ssm:SendCommand`
  on instances carrying both tags (plus the `AWS-RunShellScript` document);
  `ec2:DescribeInstances`/`DescribeInstanceStatus` and `ssm:GetCommandInvocation`
  (read-only, no resource scoping exists); `iam:PassRole` on the instance role to EC2;
  S3 on the stack's bucket; `cloudformation:DescribeStacks` on the stack.
- **Instance role S3 scope:** write `vm/${aws:userid}/*` (its own prefix — an instance
  cannot read its own tags, so the scope is the session, `<InstanceRoleId>:<instance-id>`),
  read `leases/*/in/*`, write `leases/*/out/*` and `leases/*/exec/*` (SSM's command
  output). Objects under `leases/` and `vm/` expire after 7 days.

**The image must:**

1. read user-data JSON `{"lease_id", "params", "max_lifetime_s", "bucket"}` from IMDS;
2. when ready, write `s3://<bucket>/vm/<aws:userid>/ready.json` — the prefix is
   `aws sts get-caller-identity --query UserId --output text`, i.e.
   `<InstanceRoleId>:<instance-id>` — containing `{"ok": true|false, "reason"?, "timings"?, "cli_version"?}`.
   `ok: false` fails the lease at once (and terminates the machine);
3. halt at `max_lifetime_s` (a halt terminates);
4. put every `exec.allow` command on `PATH` for SSM's root shell.

An exec runs with `CANOPY_LEASE_ID`, `CANOPY_LEASE_IN` (`s3://<bucket>/leases/<id>/in/`)
and `CANOPY_LEASE_OUT` (`…/out/`) exported. Write artifacts under `CANOPY_LEASE_OUT`;
read staged inputs from the `s3_uri`s the uploads route returned.

## 3. Allow it (a workspace admin)

`PUT /api/capabilities/grants {"agent", "capability", "max_concurrent"?, "max_lease_s"?}`
(`integrations`: admin or owner of the agent's workspace). Without a grant an acquire
is 403 and the message names this call. A grant may only lower the manifest's limits;
revoking it (`DELETE /api/capabilities/grants/{id}`) releases its live leases. Grants
and publishes are settings, recorded in the settings audit.

## 4. Lease and use it

The caller is the agent's own login, or an editor+ of the agent's workspace acting for
it (a laptop session). PAT-authenticated; every route is also an MCP tool.

| Route | Body → response |
|---|---|
| `POST /api/capabilities/leases` | `{agent, capability, params?, turn_id?}` → lease. Returns at once: `provisioning` (launched with `ClientToken=lease_id`) or `queued` with `position` |
| `GET /api/capabilities/leases/{id}` | → lease. **The heartbeat**; while provisioning it reads `ready.json` |
| `DELETE /api/capabilities/leases/{id}` | → lease, `released`; the machine is terminated. Idempotent |
| `POST /api/capabilities/leases/{id}/exec` | `{argv[], timeout_s=600}` → `{lease_id, exec_id, timeout_s}`. 422 unless `argv[0]` is on `exec.allow`; 409 unless ready |
| `GET /api/capabilities/leases/{id}/exec/{exec_id}` | → `{exec_id, status, done, exit_code, stdout_tail, stderr_tail, stdout_url, stderr_url}` |
| `POST /api/capabilities/leases/{id}/uploads` | `{names[]}` → `{lease_id, uploads: [{name, put_url, s3_uri}], expires_in_s}` |
| `GET /api/capabilities/leases/{id}/artifacts?prefix=` | → `{lease_id, prefix, artifacts: [{path, size, url}], expires_in_s}` |
| `GET /api/agents/{slug}/capabilities` | → `{agent, capabilities: [{name, kind, description, region, stack, manifest, stack_outputs, published_at, grant, limits, live_leases}], retired}` |

A **lease** is `{lease_id, agent, capability, status, ready, position, instance_id,
params, turn_id, created_at, provisioning_at, ready_at, ended_at, expires_at,
last_heartbeat_at, idle_release_s, reason, ready_info}`.

- **States:** `queued → provisioning → ready → released`; failure exits `failed`
  (launch refused, not ready in time, `ok: false`, the machine died) and `expired`
  (`max_lease_s`). A `ready` lease never returns to `provisioning`: release a sick
  machine and acquire another.
- **Heartbeat:** any lease call counts. Poll at least every `idle_release_s`, including
  while queued.
- **Exec** `status` is SSM's own (`Pending`, `InProgress`, `Delayed`, `Success`,
  `Cancelled`, `TimedOut`, `Failed`, `Cancelling`); `exit_code` and the presigned
  `stdout_url`/`stderr_url` (1 h) appear once `done`. `stdout_tail` is the last 4000
  characters. SSM is the job store, so an exec survives a canopy-web deploy.
- **Turn binding:** pass `turn_id` (the cloud runner exports `CANOPY_TURN_ID` to every
  turn) and the lease is released when that turn ends.
- **Caps:** at most `max_concurrent` machines per agent capability, counting holding
  leases plus any tagged instance no lease accounts for. Over the cap, leases queue FIFO
  and are promoted as machines free up.

Every transition is an event (`source=capabilities`, `kind=capability.lease.<status>`;
also `capability.lease.launched`, `capability.exec.sent`, `capability.orphans.terminated`,
`capability.published`, `capability.retired`), readable at `GET /api/events/?source=capabilities`.

## 5. Cost guards (any one suffices)

1. Explicit release.
2. The bound turn ending (`turn_events_appended` with a terminal status), or no
   heartbeat for `idle_release_s`.
3. **The sweep**, on the harness claim tick (`apps/harness/claim.py`, at most every 30 s
   per process, never raising): expires leases past `expires_at`, releases idle ones and
   ones whose turn ended, finishes provisioning checks, re-launches a provisioning lease
   that has no instance (same ClientToken), adopts an instance whose id never reached the
   DB, ends a ready lease whose machine is gone, terminates every tagged instance with no
   live lease, and promotes the queue.
4. Machine-side: halt at `max_lifetime_s`, shutdown = terminate. These work while
   canopy-web is down.
