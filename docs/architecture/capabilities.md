# Cloud capabilities — the agent author's contract

A **capability** is a machine an agent leases from canopy-web for exactly as long as it
needs it: "give me my `android-emulator`", get a fresh EC2 instance booted from the
agent's own image, drive it, release it, and it is terminated. A capability of kind
`pool` leases a **named slot** instead — an exclusive checkout of one of a few fixed
things (a test account, a seat), with no machine and no AWS call (§6). Design record:
`docs/superpowers/specs/2026-10-14-cloud-capabilities-design.md`. Code: `apps/capabilities`
(framework tier), `canopy_sdk.ondemand.EphemeralInstance`,
`canopy_runtime.schema.CapabilityManifest`.

**Who owns what.** canopy-web owns the generic half and nothing domain-specific: the
manifest schema, the `ec2-ephemeral` and `pool` kinds, the grant, the lease lifecycle, caps, audit
and the sweep. The agent's repo (or a shared repo it points at) owns everything else:
`capability.yaml`, the CloudFormation stack, the image and its bake, the boot hook and
the CLI on the machine.

**Whose AWS permissions.** canopy-web's ECS task role grants it nothing for
capabilities. Every capability call is one the agent's stack grants that role —
`BrokerPolicy`, attached through the stack's `BrokerRoleName` — scoped to that agent's
tagged instances, its exec document, its bucket and its stack. That role is canopy-web's
own `canopy-web-task-role` (since 2026-10-15; `deploy.md` § canopy-web's own task role),
so no other labs service gets the grant. Pass `BrokerRoleName=canopy-web-task-role`.

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
kind: ec2-ephemeral               # or `pool` (§6)
description: Android 14 emulator with CommCare + Maestro, driven by the ace-mobile CLI.
aws:
  region: us-east-1
  stack: canopy-cap-ace-android-emulator   # canopy-web reads this stack's Outputs
limits:                           # ceilings; a grant may only lower them
  max_concurrent: 4               # 1-50 (default 1)
  max_lease_s: 14400              # 60-172800 (default 3600); the machine halts at this age
  idle_release_s: 900             # released after this long with no lease call or running exec (default 900)
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
stack meets §2 — including, on a 0.9.0+ stack, that every `exec.allow` entry is on the
stack's `ExecAllow` and that `max_lease_s` is within its `MaxInstanceLifetimeSeconds`.
Idempotent. A capability missing from the list is **retired** and its live leases
released. The caller must be the agent's owner or an admin, or the agent's own login (its
PAT).

The Outputs are re-read by the sweep about every 15 minutes, after a lease misses its
ready deadline, and on `POST /api/agents/{slug}/capabilities/{name}/refresh` (run it right
after a stack update). A re-read that fails the contract keeps the old Outputs and records
`capability.outputs.unreadable`.

## 2. The `ec2-ephemeral` stack

Deploy canopy's shared template with `Mode=ephemeral`
(`canopy_sdk.ondemand.assets.asset_path("ondemand-capability.cfn.yaml")`, pinned to a
`dimagi-canopy-v<version>` tag), or write your own that exposes the same contract.

```bash
aws cloudformation deploy --stack-name canopy-cap-<agent>-<capability> \
  --template-file ondemand-capability.cfn.yaml --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides Mode=ephemeral Capability=<capability> Owner=<agent-slug> \
    OwnerTag=<who-to-ask> InstanceType=m8i.xlarge VpcId=vpc-… SubnetId=subnet-… \
    NestedVirtualization=true ExecAllow=<cli>[,<cli>…] BrokerRoleName=<canopy-web's task role>
```

| Ephemeral parameter | |
|---|---|
| `Mode` | `ephemeral` (default `persistent`, the stopped-when-idle single instance) |
| `Capability` | the manifest's `name` |
| `Owner` | the agent's slug; tags instances `canopy:owner` and names the AMI parameter |
| `OwnerTag` | the human-facing `owner` tag |
| `BrokerRoleName` | the broker's IAM role: canopy-web's own task role, `canopy-web-task-role` (since 2026-10-15; `deploy.md`) |
| `ExecAllow` | **required** (0.9.0+): the manifest's `exec.allow`, comma-separated; the exec document runs nothing else |
| `MaxInstanceLifetimeSeconds` | 0.9.0+, default 16200 (4 h + 30 min): the backstop terminates any of the capability's instances older than this. Keep it above `max_lease_s` |
| `RootVolumeIops`, `RootVolumeThroughput`, `RootVolumeInitializationRate` | 0.9.0+, default empty (= unset; `0` too): gp3 IOPS (3000–80000), gp3 throughput MiB/s (125–2000), and the EBS volume initialization rate MiB/s (100–300) at which a fresh root volume pulls the AMI snapshot instead of hydrating lazily. Every lease starts from a cold volume, so this is the boot-time lever: `6000` / `500` / `300` took the Android emulator's `boot_completed` from ~50 s to ~37 s |
| `InstanceType`, `VpcId`, `SubnetId`, `NestedVirtualization`, `RootVolumeGb`, `UserData` | as for persistent |
| `AmiId`, `ConsumerTaskRoleName`, `IdleStopMinutes` | persistent only; leave empty |

What the stack must provide:

- **Outputs:** `LaunchTemplateId`, `ArtifactsBucketName`, `InstanceRoleArn`,
  `Capability` — and `Owner` (must equal the agent's slug) and `InstanceRoleId` (names
  each instance's S3 prefix). A publish names any that are missing. A 0.9.0+ stack adds
  `LeaseObjectScope` (`instance`), `ExecDocumentName`, `ExecAllow`,
  `MaxInstanceLifetimeSeconds` and `BackstopFunctionName`; without them the broker uses the
  pre-0.9.0 contract (`leases/<id>/` objects, `AWS-RunShellScript`).
- **Launch template:** the broker launches its `$Latest` version (SDK 0.9.0+), so a stack
  update — new volume settings, instance type, user data — applies from the next lease.
  Only the stack writes versions; `BrokerPolicy` cannot.
- **Tags:** instances are tagged `canopy:capability=<name>` and `canopy:owner=<agent>`
  (the launch template sets them; canopy-web also sends them, plus `canopy:lease=<id>` and
  `canopy:broker=<deployment>` — `CAPABILITY_BROKER_ID`, by default the host of
  `CANOPY_IDENTITY_BASE_URL`). A broker counts and kills only instances carrying its own
  `canopy:broker` tag (or none: launched before the tag existed), so two canopy-webs can
  share a stack.
- **AMI pointer:** the launch template's image is `resolve:ssm:/canopy/capabilities/<agent>/<name>/ami`,
  resolved by EC2 at every launch (as the RunInstances caller, so `BrokerPolicy` may read
  that one parameter). A rebake writes it; nothing is re-pinned. Write it before the first
  deploy.
- **Self-termination:** `InstanceInitiatedShutdownBehavior: terminate`. There is no
  CloudWatch idle alarm (an alarm names one instance, and these do not exist at deploy).
- **The backstop** (0.9.0+): `BackstopFunction`, a stack-owned Lambda on an EventBridge
  `rate(5 minutes)` rule, terminates every instance tagged with this capability and owner
  whose launch time is older than `MaxInstanceLifetimeSeconds`. Its role can describe
  instances, terminate only those two tags' instances, and write its own log group. It
  works with canopy-web down, deleted or wrong.
- **The exec document** (0.9.0+): `canopy-cap-<owner>-<capability>-exec`, the only document
  `BrokerPolicy` may send. It takes `Argv` (base64 of the argv, each argument
  NUL-terminated), `LeaseId`, `LeaseIn`, `LeaseOut` and `ExecutionTimeout`, every one
  pattern-restricted so no value can carry shell syntax; it runs the argv only if argv[0]
  is exactly one of `ExecAllow` (else exit 126). Updated in place (`NewVersion`); the broker
  sends `$LATEST`.
- **Broker permissions** (`BrokerPolicy` on `BrokerRoleName`), and nothing else:
  `ec2:RunInstances` from this launch template only, the instance only with both request
  tags (extra tags allowed); `ec2:CreateTags` at launch only; `ec2:TerminateInstances` and
  `ssm:SendCommand` on instances carrying both tags (plus the exec document — on older
  stacks `AWS-RunShellScript`); `ec2:DescribeInstances`/`DescribeInstanceStatus` and
  `ssm:GetCommandInvocation` (read-only, no resource scoping exists); `ssm:GetParameter(s)`
  on the AMI parameter; `iam:PassRole` on the instance role to EC2; S3 on the stack's
  bucket; `cloudformation:DescribeStacks` on the stack.
- **Instance role** (0.9.0+): read and write only `vm/${aws:userid}/*` — its own prefix (an
  instance cannot read its own tags, so the scope is the session,
  `<InstanceRoleId>:<instance-id>`); the SSM agent's own calls (registration, Run Command,
  Session Manager, associations, inventory) and an explicit **Deny on
  `ssm:GetParameter*`**. Not `AmazonSSMManagedInstanceCore`: its `ssm:GetParameter(s)` on `*`
  would let a lease machine decrypt every SecureString in the account. Objects under
  `leases/` and `vm/` expire after 7 days.

**Where a lease's objects live.** On a 0.9.0+ stack under the machine's own prefix,
`vm/<InstanceRoleId>:<instance-id>/lease/<lease-id>/` — `in/` (uploads), `out/`
(artifacts), `exec/` (SSM's command output). On an older stack, `leases/<lease-id>/`. A
lease keeps the layout it started with (`CapabilityLease.s3_base`) even if its stack is
updated mid-lease. **Update a stack to 0.9.0 when no lease is live**: a running old-layout
lease loses its `leases/` grants the moment the stack updates.

**The image must:**

1. read user-data JSON `{"lease_id", "params", "max_lifetime_s", "bucket"}` from IMDS;
2. when ready, write `s3://<bucket>/vm/<aws:userid>/ready.json` — the prefix is
   `aws sts get-caller-identity --query UserId --output text`, i.e.
   `<InstanceRoleId>:<instance-id>` — containing `{"ok": true|false, "reason"?, "timings"?, "cli_version"?}`.
   `ok: false` fails the lease at once (and terminates the machine);
3. halt at `max_lifetime_s` (a halt terminates);
4. put every `exec.allow` command on `PATH` for SSM's root shell, and have `bash` and
   `base64` (any Ubuntu image does);
5. read and write S3 only through the URIs it is given (argv, `CANOPY_LEASE_IN`,
   `CANOPY_LEASE_OUT`) — never compute a lease path itself.

An exec runs with `CANOPY_LEASE_ID`, `CANOPY_LEASE_IN` and `CANOPY_LEASE_OUT` (the lease's
`in/` and `out/` prefixes) exported. Write artifacts under `CANOPY_LEASE_OUT`; read staged
inputs from the `s3_uri`s the uploads route returned.

## 3. Allow it (a workspace admin)

On the web: the agent's **Capabilities** page,
`https://canopy.dimagi.com/w/<workspace>/agents/<slug>/capabilities` — Grant (optionally
lowering the limits), Limits, Revoke; members without `integrations` see it read-only.
Through the API:

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
| `GET /api/capabilities/leases/{id}?wait_s=0..20` | → lease. **The heartbeat**; while provisioning it reads `ready.json`. `wait_s` long-polls (below) |
| `DELETE /api/capabilities/leases/{id}` | → lease, `released`; the machine is terminated. Idempotent |
| `POST /api/capabilities/leases/{id}/exec` | `{argv[], timeout_s=600}` → `{lease_id, exec_id, timeout_s}`. 422 unless `argv[0]` is on `exec.allow`; 409 unless ready. `timeout_s` is capped at the time the lease has left |
| `GET /api/capabilities/leases/{id}/exec/{exec_id}?wait_s=0..20` | → `{exec_id, status, done, exit_code, stdout_tail, stderr_tail, stdout_url, stderr_url}` |
| `POST /api/capabilities/leases/{id}/uploads` | `{names[]}` → `{lease_id, uploads: [{name, put_url, s3_uri}], expires_in_s}`. 409 for an ended lease, and (0.9.0+ stack) while the machine is not launched yet (`instance_id` null) |
| `GET /api/capabilities/leases/{id}/artifacts?prefix=` | → `{lease_id, prefix, artifacts: [{path, size, url}], expires_in_s}` |
| `GET /api/agents/{slug}/capabilities` | → `{agent, capabilities: [{name, kind, description, region, stack, manifest, stack_outputs, published_at, grant, limits, live_leases}], retired}` |

A **lease** is `{lease_id, agent, capability, status, ready, position, instance_id,
params, turn_id, created_at, provisioning_at, ready_at, ended_at, expires_at,
last_heartbeat_at, idle_release_s, reason, ready_info, slot, out_prefix, slot_data}` (`slot`: a
`pool` lease's slot, else null; `slot_data`: a ready pool lease's slot data, for its
acquirer only — § 6 "Slot data" — else null). `out_prefix` is the
lease's `out/` as an `s3://` URI, the same value as `CANOPY_LEASE_OUT`, for a CLI that needs
`--out` as a literal (argv is never shell-expanded, so `$CANOPY_LEASE_OUT` is not
expanded). On a 0.9.0+ stack it names the machine, so it is `""` until `instance_id` is set
(normally already in the acquire's response). Use the URIs the broker returns; never
build them.

- **States:** `queued → provisioning → ready → released`; failure exits `failed`
  (launch refused, not ready in time, `ok: false`, the machine died) and `expired`
  (`max_lease_s`). A `ready` lease never returns to `provisioning`: release a sick
  machine and acquire another.
- **Heartbeat:** any lease call counts, and so does a running exec — until it is done or
  its `timeout_s` passes, an exec keeps its lease from idling out even if nobody polls.
  Otherwise poll at least every `idle_release_s`, including while queued.
- **Long-poll:** `wait_s` (0-20; more is 422) holds a GET until the state changes: a lease
  returns as soon as it leaves the status it had on arrival (queued → provisioning,
  provisioning → ready or failed), an exec as soon as it is done; otherwise after
  `wait_s`. The server re-checks about once a second (ready.json in S3, or
  GetCommandInvocation) and never calls DescribeInstances in the loop. Keep your HTTP
  timeout above `wait_s` + 10 s.
- **Exec** `status` is SSM's own (`Pending`, `InProgress`, `Delayed`, `Success`,
  `Cancelled`, `TimedOut`, `Failed`, `Cancelling`); `exit_code` and the presigned
  `stdout_url`/`stderr_url` appear once `done`. `stdout_tail` is the last 4000
  characters. SSM is the job store, so an exec survives a canopy-web deploy. Exit 126 with
  "not on this capability's exec allow list" means the stack's `ExecAllow` and the
  manifest disagree.
- **Presigned URLs** (uploads, artifacts, exec streams) work for up to 1 h — never longer
  than the credentials that signed them; `expires_in_s` says how long.
- **Turn binding:** pass `turn_id` (the cloud runner exports `CANOPY_TURN_ID` to every
  turn) and the lease is released when that turn ends.
- **Caps:** at most `max_concurrent` machines per agent capability, counting holding
  leases plus any of this broker's tagged instances no lease accounts for (except one whose
  lease has just ended). Over the cap, leases queue FIFO and are promoted as machines free
  up.

**Operating it** (workspace-scoped; all also MCP tools):

| Route | |
|---|---|
| `GET /api/capabilities/leases?agent=&capability=&status=&since=&limit=` | → `{leases: [lease], truncated}`, newest first; `status` is one status, a comma list, or `live`. Member, or the agent's own login. A read: no heartbeat |
| `GET /api/capabilities/spend?since=&until=&agent=&capability=` | → `{since, until, rows: [{agent, capability, leases, machine_seconds, live}], total_machine_seconds}`: machine time from `provisioning_at` to `ended_at` (or now), clipped to the window (default the last 7 days) |
| `POST /api/capabilities/leases/{id}/force-release` | `{reason?}` → lease. Ends a live lease and terminates its machine **even if the lease already ended**. `integrations` or the agent's admins |
| `POST /api/agents/{slug}/capabilities/{name}/terminate-orphans` | → `{agent, capability, orphans, gone}`: the sweep's machine pass, now. `integrations` or the agent's admins; 502 if EC2 cannot be listed |
| `POST /api/agents/{slug}/capabilities/{name}/refresh` | → the capability: re-read the stack's Outputs (422 naming what fails the contract). Agent admins or the agent's own login |
| `manage.py capability_sweep` | run the sweep once in an ECS Exec shell; prints the counts |

Every transition is an event (`source=capabilities`, `kind=capability.lease.<status>`;
also `capability.lease.launched`, `.launch_retry` (once per distinct error),
`.force_terminated`, `capability.exec.sent` — `argv0` and `argc` only, never the arguments,
which can carry paths and credentials — `capability.orphans.terminated`,
`capability.published`, `capability.retired`, `capability.outputs.refreshed` /
`.unreadable`, and the sweep's own `capability.sweep.failed` / `.error` / `.unreachable`,
coalesced so a persistent failure is one row with a count), readable at
`GET /api/events/?source=capabilities`.

## 5. Cost guards (each independent of the ones before it)

1. Explicit release.
2. The bound turn ending (`turn_events_appended` with a terminal status), or no lease call
   and no running exec for `idle_release_s`.
3. **The sweep.** Off-request, on its own clock: every web process runs it from an asyncio
   task in the ASGI lifespan (`apps/capabilities/scheduler.py`, every
   `CAPABILITY_SWEEP_INTERVAL_S`, default 30 s). A cache key makes it at most once per 25 s
   **across the fleet**; if the cache (Redis) fails, each process falls back to its own
   clock rather than skipping. A runner's claim (`apps/harness/claim.py`) triggers it too,
   above every early return, but only when no sweep has finished for 90 s — so a fleet hold
   or no runner online stops nothing, and normally no AWS call rides a claim. It never
   raises; a failure is an error event. It: expires leases past `expires_at`; releases idle
   ones and ones whose turn ended; finishes provisioning checks and re-launches a
   provisioning lease that has no instance (same ClientToken); adopts an instance whose id
   never reached the DB; ends a ready lease whose machine is gone; re-reads stale stack
   Outputs; terminates every one of this broker's tagged instances with no live lease — for
   **every** published capability (and any retired in the last 3 days), not only ones with
   recent leases; and promotes the queue.
4. **Deleting what a lease belongs to** (its agent, or its capability row) terminates the
   lease's machine first (`pre_delete`). A workspace cannot be deleted under an agent.
5. Machine-side: halt at `max_lifetime_s`, shutdown = terminate.
6. Stack-side: the backstop Lambda (§2), every 5 minutes. 5 and 6 work while canopy-web is
   down.

## 6. The `pool` kind: an exclusive checkout of a named slot

A `pool` capability brokers **one of a small fixed set of named things per lease** — no
machine, no stack, no image, no AWS call anywhere (publish, acquire, release, sweep). Use
it when two holders of the same thing at once break each other: ACE's Connect test
workers, where a second device signing in as the same worker locks the first out.

```yaml
version: 1
name: test-worker
kind: pool
description: Connect test workers, one device session each.
slots: [worker-1, worker-2, worker-3, worker-4]   # 1-50, unique, [a-z0-9-]{3,40}; handed out in this order
limits: {max_lease_s: 14400, idle_release_s: 900}
```

- **Manifest:** `slots` required; `aws`, `exec` and `params` are refused (the broker owns
  the one acquire param, `slot`); `ready_timeout_s` is ignored. Concurrency is the slot
  count: `limits.max_concurrent` defaults to `len(slots)` and, like a grant, may only lower
  it. An `ec2-ephemeral` manifest may not carry `slots`.
- **Publish:** the same `canopy-runtime publish-capabilities` / `PUT
  /api/agents/{slug}/capabilities`; no stack is read (`stack`, `region` and
  `stack_outputs` are empty). Changing `slots` never strands a live lease: one holding a
  removed slot keeps it until it ends, and that slot is never handed out again; a queued
  lease that asked for a removed slot fails. Changing a capability's kind ends its live
  leases first, as the kind they were (an ec2-ephemeral machine is terminated).
- **Acquire:** `POST /api/capabilities/leases {agent, capability, params?, turn_id?}`;
  `params` takes only an optional `{"slot": "<one of slots>"}` (anything else is 422).
  Under the capability's row lock, a free slot — one no live lease of this capability
  holds — makes the lease **`ready` at once** (`queued → ready`; it never provisions):
  `slot`, `params.slot` and `ready_info` (`{"slot": …}`) name it, `expires_at` is
  `max_lease_s` from now, and `instance_id`/`out_prefix` stay empty. The requested slot,
  else the first free one in declared order. With none free (or the requested one held)
  the lease is `queued` with `position`.
- **Promotion:** releasing (or expiring, idling, force-releasing, a turn ending, revoking
  the grant) frees the slot and hands it on **in the same transaction**: oldest queued
  first; a lease waiting for one particular slot gets exactly that slot and does not hold
  up the leases behind it. The sweep promotes pools too (no AWS work for them), so a
  promotion lost to a crash waits at most one sweep.
- **Exclusivity is in the database:** a conditional unique constraint
  (`cap_lease_one_live_per_slot`: one `(capability, slot)` among live leases), not only the
  row lock.
- **Everything else is the ec2-ephemeral contract:** grants, heartbeat and long-poll
  (`wait_s` returns when a queued lease gets its slot), list (`slot` on every lease shows
  who holds which), idle release, `max_lease_s` expiry, turn binding, events
  (`capability.lease.ready` names the slot) and spend (slot-seconds from `ready_at`).
- **No machine:** exec, exec status, uploads and artifacts on a pool lease are 422, and so
  are `…/refresh` and `…/terminate-orphans` on a pool capability.

### Slot data: what each slot is

A pool slot is a fixed thing — ACE's `worker-1` is one Connect test worker — and the
holder needs its details. canopy-web keeps them next to the lease, one row per slot
(`CapabilitySlotData`, unique on capability + slot): a `public` map (str → str: a phone, a
name) and a `secret` map (a PIN, a backup code) stored only as Fernet ciphertext of a JSON
object (`apps/common/encryption.py`, the same keys as `AgentCredential`). Every slot holds
its own values; there is no default or shared value across slots.

| Route | Gate | Body → response |
|---|---|---|
| `PUT /api/agents/{slug}/capabilities/{name}/slots/{slot}` | workspace admin (`integrations`), or the agent's own login | `{"public": {k: v\|null}?, "secret": {k: v\|null}?}` → the masked slot |
| `GET /api/agents/{slug}/capabilities/{name}/slots` | member, or the agent's own login | → `{agent, capability, slots: [masked slot]}`, one per declared slot, in order |
| `DELETE /api/agents/{slug}/capabilities/{name}/slots/{slot}` | as PUT | → 204, idempotent |

A **masked slot** is `{slot, declared, public, secret_keys, updated_at, updated_by, held,
lease_id}`: public values, the NAMES of the secret keys, and whether (and which) live lease
holds it. MCP tools: `set_capability_slot_data`, `list_capability_slot_data`,
`delete_capability_slot_data`.

- **Writes are per key.** A string writes the key, `null` deletes it, a key left out is
  kept, a section left out is untouched. Values are non-blank strings (≤ 4096); keys match
  `[A-Za-z0-9_.-]{1,64}`, at most 50 per section; a key is public or secret, never both
  (send `null` in one section and the value in the other to move it). A slot left with no
  keys has no row. 422 unless the capability is a `pool` that declares `slot` (and is not
  retired); a refusal names the key, never the value.
- **Republishing keeps the data.** It is keyed by the capability row and the slot name, not
  the manifest. Data for a slot the manifest no longer declares is kept but never
  delivered; DELETE removes it (PUT refuses it).
- **Delivery — the only door a secret value leaves by.** The acquire response and `GET
  /api/capabilities/leases/{id}` (and its long-poll, and the `get_capability_lease` tool)
  carry `slot_data: {"public": {…}, "secret": {…}}`, decrypted, when ALL hold: the caller is
  the lease's `requested_by` (the login that acquired it — an agent's PAT gets it for the
  leases it acquired), the lease is a pool lease in `ready`, its slot is still declared, and
  the slot has data. Otherwise `slot_data` is `null` — on every list, on release, once the
  lease is terminal, and for every other caller, workspace admins and owners included.
- **Every delivery of secret values is an event**, `capability.slot.secret_delivered`
  (lease id, slot, user, secret key names). Writes are `capability.slot.data_set` /
  `.data_deleted` with key names.
- **Never in a log.** No event, error, `__str__`/`repr`, MCP call-log row or settings-audit
  row carries a secret value: the audit stores the request body with `secret` redacted and
  a masked before/after (`public`, `secret_keys`, and `secret_keys_written` — so a rotated
  PIN is still a recorded change). Not a digest either: a PIN's hash is the PIN.

