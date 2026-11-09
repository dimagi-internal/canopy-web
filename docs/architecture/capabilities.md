# Cloud capabilities — the agent author's contract

A **capability** is a machine an agent leases from canopy-web for exactly as long as it needs it: it asks for
its `android-emulator`, gets a fresh EC2 instance booted from the agent's own image, drives it, releases it,
and the instance is terminated. A capability of kind `pool` leases a **named slot** instead — an exclusive
checkout of one of a few fixed things (a test account, a seat), with no machine and no AWS call (§6). Code:
`apps/capabilities` (framework tier), `canopy_sdk.ondemand.EphemeralInstance`,
`canopy_runtime.schema.CapabilityManifest`. Start reading at `apps/capabilities/api.py` (module docstring: who
may call what), then `apps/capabilities/services.py`.

**"Capability" means five things in canopy.** This page is about (c).

| | Meaning | Where |
|---|---|---|
| (a) | **Read mode or a host site**: a confined profile an agent offers, stored as an entry of `Agent.interface`'s `capabilities` and stamped on `Turn.capability` | `apps/agents/published_profiles.py`, `access-agent-roles-and-access-list.md` |
| (b) | **Workspace permission**: a named right a workspace role holds (`perms.can(user, ws, perms.X)`) | `apps/workspaces/permissions.py`, `roles.md` |
| (c) | **Cloud capability**: a leased machine or named pool slot | `apps/capabilities`, this page |
| (d) | **`Runner.capabilities`**: the JSON a box reports about itself | `apps/harness/models/runner.py`, `runners.md` |
| (e) | **`Storyboard.capability`**: the level a share link grants | `apps/storyboards/models.py` |

**Who owns what.** canopy-web owns the generic half and nothing domain-specific: the manifest schema, the
`ec2-ephemeral` and `pool` kinds, the grant, the lease lifecycle, caps, audit and the sweep. The agent's repo
owns everything else: `capability.yaml`, the CloudFormation stack, the image and its bake, the boot hook and
the CLI on the machine.

**Whose AWS permissions.** canopy-web's ECS task role grants it nothing for capabilities. Every capability
call is one the agent's stack grants that role (`BrokerPolicy`, attached through the stack's
`BrokerRoleName`), scoped to that agent's tagged instances, its exec document, its bucket and its stack. The
role is canopy-web's own `canopy-web-task-role` (`deploy.md` § canopy-web's own task role), so no other labs
service gets the grant: pass `BrokerRoleName=canopy-web-task-role`. All AWS calls sit behind one seam,
`apps/capabilities/aws.py`.

## 1. Declare it in your repo

One `capability.yaml` per capability, in the agent's own repo (its path is the repo's choice). The fields, defaults, ranges and the per-kind rules are
`canopy_runtime.schema.CapabilityManifest` (`packages/canopy_runtime/canopy_runtime/schema.py`); unknown keys
are an error. A param with no `default` is required at acquire.

Publish as part of the agent's deploy: `canopy-runtime publish-capabilities --dir infra/capabilities --agent
<slug>` (`--help` lists the flags). `--dir` takes every `<dir>/*/capability.yaml` and refuses a directory with
none, rather than retire everything. It sends the whole list in one `PUT /api/agents/{slug}/capabilities`
(`api.put_agent_capabilities`). canopy-web validates again with the same model, reads each ec2-ephemeral
stack's Outputs, and refuses the publish (422, naming what is wrong) unless every stack meets the contract
(§2), is the agent's own (its `Owner` Output), lists every `exec.allow` entry on its `ExecAllow`, and allows
`max_lease_s` within its `MaxInstanceLifetimeSeconds` (`services._resolve_outputs`). Nothing is written unless
every manifest passes. Publishing is idempotent. A capability missing from the list is **retired** and its
live leases released; a changed `kind` ends the live leases first, as the kind they were
(`services.publish`). The caller must be the agent's owner or an admin, or the agent's own login.

The Outputs are re-read by the sweep, after a lease misses its ready deadline, and on
`POST /api/agents/{slug}/capabilities/{name}/refresh` (run it right after a stack update). A re-read that fails
the contract keeps the old Outputs and records `capability.outputs.unreadable`
(`services.refresh_outputs`).

## 2. The `ec2-ephemeral` stack

Deploy canopy's shared template with `Mode=ephemeral`
(`canopy_sdk.ondemand.assets.asset_path("ondemand-capability.cfn.yaml")`, pinned to a `dimagi-canopy-v<version>`
tag), or write your own that exposes the same contract. The template's header
(`sdk/python/src/canopy_sdk/ondemand/assets/ondemand-capability.cfn.yaml`) states the contract and its
parameters; the Outputs canopy-web requires are `aws.REQUIRED_OUTPUTS`, `CHECKED_OUTPUTS` and
`OPTIONAL_OUTPUTS`. Things the template cannot say for you:

- **`Owner` must equal the agent's slug**, and the stack must be that agent's own.
- **Broker tags.** canopy-web tags instances `canopy:lease` and `canopy:broker` (`services.broker_id`, which
  `CAPABILITY_BROKER_ID` overrides). A broker counts and kills only instances carrying its own `canopy:broker`
  tag (or none), so two canopy-webs can share a stack (`services._ours`).
- **The AMI pointer** is the SSM parameter `/canopy/capabilities/<agent>/<name>/ami`, resolved by EC2 at every
  launch. A rebake writes it; nothing is re-pinned. Write it before the first deploy.
- **The launch template's `$Latest`** is what the broker launches, so a stack update applies from the next
  lease.
- **Update a stack to the per-instance layout (`LeaseObjectScope`) when no lease is live**: a running
  old-layout lease loses its `leases/` grants the moment the stack updates. A lease keeps the layout it
  started with (`CapabilityLease.s3_base`).
- **Cost backstops live in the stack** (self-terminating shutdown behaviour, the backstop Lambda) so they work
  while canopy-web is down. `MaxInstanceLifetimeSeconds` must stay above `max_lease_s`.
- **The instance role must not read SSM parameters**: `AmazonSSMManagedInstanceCore` would let a lease machine
  decrypt every SecureString in the account, which is why the template carries an explicit Deny on
  `ssm:GetParameter*`.

**The image must** (canopy-web cannot check it; `services._launch`, `services.check_ready`):

1. read user-data JSON `{"lease_id", "params", "max_lifetime_s", "bucket"}` from IMDS;
2. when ready, write `s3://<bucket>/vm/<aws:userid>/ready.json` — the prefix is
   `aws sts get-caller-identity --query UserId --output text` (`<InstanceRoleId>:<instance-id>`) — containing
   `{"ok": true|false, "reason"?, "timings"?, "cli_version"?}`; `ok: false` fails the lease at once and
   terminates the machine;
3. halt at `max_lifetime_s` (a halt terminates);
4. put every `exec.allow` command on `PATH` for SSM's root shell, and have `bash` and `base64`;
5. read and write S3 only through the URIs it is given (argv, `CANOPY_LEASE_IN`, `CANOPY_LEASE_OUT`) — never
   compute a lease path itself.

An exec runs with `CANOPY_LEASE_ID`, `CANOPY_LEASE_IN` and `CANOPY_LEASE_OUT` exported. Write artifacts under
`CANOPY_LEASE_OUT`; read staged inputs from the `s3_uri`s the uploads route returned.

## 3. Allow it (a workspace admin)

Without a grant an acquire is 403, and the message names the call (`services.acquire`). A grant is
`PUT /api/capabilities/grants` (`api.set_capability_grant`; `integrations`: admin or owner of the agent's
workspace), and the agent's **Capabilities** page does the same. A grant may only lower the manifest's limits
(`services.effective_limits`); revoking it releases its live leases. Grants and publishes are settings,
recorded in the settings audit.

## 4. Lease and use it

The caller is the agent's own login, or an editor+ of the agent's workspace acting for it
(`api._agent_to_lease_for`). Routes, bodies and refusals: `apps/capabilities/api.py` and
`apps/capabilities/schemas.py`; every route is also an MCP tool. Use the URIs the broker returns
(`out_prefix`, `s3_uri`); never build them. `out_prefix` names the machine, so it is `""` until `instance_id`
is set.

- **States:** `queued → provisioning → ready → released`; failure exits `failed` (launch refused, not ready in
  time, `ok: false`, the machine died) and `expired` (`max_lease_s`). A `ready` lease never returns to
  `provisioning`: release a sick machine and acquire another.
- **Heartbeat:** any lease call counts, and so does a running exec until it is done or its `timeout_s` passes
  (`services.last_activity`). Otherwise poll at least every `idle_release_s`, including while queued.
- **Long-poll:** `wait_s` on the lease and exec GETs (`services.MAX_WAIT_S`; more is 422) holds the request
  until the state changes. Keep your HTTP timeout above `wait_s` + 10 s.
- **Exec** runs only an `argv[0]` on the manifest's `exec.allow` (422 otherwise) and only on a `ready` lease
  (409). SSM is the job store, so an exec survives a canopy-web deploy. Exit 126 with "not on this capability's
  exec allow list" means the stack's `ExecAllow` and the manifest disagree.
- **Presigned URLs** (uploads, artifacts, exec streams) last at most `services.PRESIGN_TTL_S`, never longer
  than the credentials that signed them.
- **Turn binding:** pass `turn_id` (the cloud runner exports `CANOPY_TURN_ID` to every turn) and the lease is
  released when that turn ends (`apps/capabilities/signals.py`).
- **Caps:** at most `max_concurrent` machines per agent capability, counting holding leases plus any of this
  broker's tagged instances no lease accounts for (except one whose lease has already ended). Over the cap,
  leases queue FIFO and are promoted as machines free up (`services._holding_count`, `services.promote`).
- **Operating it** (list, spend, force-release, terminate-orphans, `manage.py capability_sweep`): same files.
  Force-release ends a lease and terminates its machine **even if the lease already ended**.
- **Events:** every transition is an event with `source=capabilities`, `kind=capability.lease.<status>` and
  siblings (`services._event`, `services._cap_event`), readable at `GET /api/events/?source=capabilities`.
  `capability.exec.sent` carries `argv0` and `argc` only, never the arguments, which can carry paths and
  credentials and are readable by every member of the workspace.

## 5. Cost guards (each independent of the ones before it)

1. Explicit release.
2. The bound turn ending, or no lease call and no running exec for `idle_release_s`.
3. **The sweep** (`services.sweep`). Off-request: every web process runs it as a job on the shared clock
   (`apps/common/ticks.py`, registered by `apps/capabilities/scheduler.py`; `runner-routing.md` § Periodic
   work). A cache key makes it run at most once per `services.SWEEP_EVERY_S` **across the fleet**; if the cache
   fails, each process falls back to its own clock rather than skipping. A runner's claim (`apps/harness/claim.py`)
   triggers it too, above every early return, but only when no sweep has finished for `SWEEP_STALE_S`, so a
   fleet hold or no runner online stops nothing and normally no AWS call rides a claim. It never raises; a
   failure is an error event. It terminates every one of this broker's tagged instances with no live lease for
   **every** published capability (and any retired within `RETIRED_SWEEP_DAYS`), not only ones with recent
   leases.
4. **Deleting what a lease belongs to** (its agent, or its capability row) terminates the lease's machine
   first (`apps/capabilities/signals.py`, `pre_delete`). A workspace cannot be deleted under an agent.
5. Machine-side: halt at `max_lifetime_s`, shutdown = terminate.
6. Stack-side: the backstop Lambda (§2). 5 and 6 work while canopy-web is down.

## 6. The `pool` kind: an exclusive checkout of a named slot

A `pool` capability brokers **one of a small fixed set of named things per lease** — no machine, no stack, no
image, no AWS call anywhere (publish, acquire, release, sweep). Use it when two holders of the same thing at
once break each other: a second device signing in as the same test worker locks the first out. The manifest
shape is `CapabilityManifest` (`slots` required; `aws`, `exec` and `params` refused; the broker owns the one
acquire param, `slot`).

- **Acquire:** under the capability's row lock, a free slot — one no live lease of this capability holds —
  makes the lease **`ready` at once** (`queued → ready`; it never provisions). The requested slot, else the
  first free one in declared order; with none free (or the requested one held) the lease is `queued`
  (`services._assign_slots`).
- **Changing `slots` never strands a live lease:** one holding a removed slot keeps it until it ends, and that
  slot is never handed out again; a queued lease that asked for a removed slot fails.
- **Promotion:** releasing (or expiring, idling, force-releasing, a turn ending, revoking the grant) frees the
  slot and hands it on **in the same transaction**: oldest queued first; a lease waiting for one particular
  slot gets exactly that slot and does not hold up the leases behind it. The sweep promotes pools too, so a
  promotion lost to a crash waits at most one sweep.
- **Exclusivity is in the database:** the conditional unique constraint `cap_lease_one_live_per_slot`
  (`apps/capabilities/models.py`), not only the row lock.
- **No machine:** exec, uploads and artifacts on a pool lease are 422, and so are `…/refresh` and
  `…/terminate-orphans` on a pool capability (`services._no_machine`). Everything else is the ec2-ephemeral
  contract.

### Slot data: what each slot is

A pool slot is a fixed thing (one Connect test worker) and the holder needs its details. canopy-web keeps them
beside the lease, one row per slot (`CapabilitySlotData`, unique on capability + slot): a `public` map and a
`secret` map. Read `apps/capabilities/slot_data.py`'s module docstring first; it states the security rules,
and `tests/` pins them. In short, none of these may be relaxed:

- **Zero-knowledge.** A secret value is stored only **sealed** (`zks1`, `apps/agents/credentials/zk_seal.py`)
  to the agent's readers at the moment of the write (`zk_seal.recipients_for`), under the name
  `capability-slot:<capability>/<slot>/<key>`. canopy-web holds no key that opens it. A plaintext value is
  sealed in the request and never stored or logged in clear; 409 when nobody can read the agent's secrets yet.
  Key names are stored in clear.
- **Delivered to exactly one caller, still sealed** (`slot_data.deliver`): the user who acquired the lease
  (`requested_by`), while it is `ready`, for the slot it holds, while the manifest still declares that slot.
  Everyone else gets `slot_data: null` — on every list, on release, once terminal, workspace admins and owners
  included. The holder opens it on its machine with `canopy vault open-slot-data`. Each delivery of secret
  values is an event, `capability.slot.secret_delivered`, with key names.
- **Writes** are for a workspace admin (`integrations`) or the agent's own login; a browser also needs a fresh
  passkey step-up (`api.set_capability_slot_data`). The listing is masked: public values, the NAMES of secret
  keys, never a secret value, to anyone.
- **It follows the agent's readers.** A reader gained later (a machine approved, a vault member with a vault
  key) gets the value from a box that holds it: the keeper (`canopy vault zk keep`) re-seals through
  `slot_data.keeper_work` / `slot_data.reseal`, and canopy-web refuses a recipient that is not a reader now, a
  re-seal that drops a reader that held it, a box that held no copy, and a stale write. A reader the agent
  loses is dropped from every value's envelope by `slot_data.prune` (a tick, `scheduler.SLOT_PRUNE_EVERY_S`).
- **Republishing keeps the data**, keyed by capability row and slot name. Data for a slot the manifest no
  longer declares is kept but never delivered; DELETE removes it.
- **Never in a log.** No event, error, `__str__`/`repr`, MCP call-log row or settings-audit row carries a
  secret value: the audit stores the request body with `secret` redacted and a masked before/after
  (`secret_keys_written` records that a PIN rotated). Not a digest either: a PIN's hash is the PIN.
