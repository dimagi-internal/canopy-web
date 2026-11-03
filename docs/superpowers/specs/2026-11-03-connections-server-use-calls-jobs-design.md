# Connections canopy-web can use, calls from screens, and plain jobs

**Status:** design 2026-11-03, direction approved by Jonathan the same day. It amends `2026-10-31-connections-zero-knowledge-design.md` (the anti-goal "canopy-web never refreshes" now applies only while a connection's switch is off) and replaces the transport of §3 in `2026-10-28-projects-as-products-design.md` (allowlist, declarations and click-gated writes stay).

## Intent

Jonathan: connections must be able to run "async when needed, and not necessarily in an LLM, for example something might just run on the Salesforce connection." Projects and apps must be able to use them. Sending every call through a runner "seems pretty convoluted"; instead canopy-web itself can be a vault member with its key in KMS, "and that can be removed if you want to do all the calls out to a runner."

He also asked for two things this design must keep: signing in ends on "a clear connection succeeded screen", and connecting as himself and Eva having her own account are separate, both supported.

## Goals

1. **A per-connection switch: "canopy-web can use this."** On: canopy-web can open, refresh and call with it, no runner and no person present. Off: exactly today's guarantee.
2. **Defaults (Jonathan, 2026-11-03):** on for Salesforce, off for Google. Each owner can change it per connection.
3. **Sign-in ends on a success screen.** With the switch on, the connection works for screens the moment sign-in finishes, with no passkey.
4. **Screens and apps call providers through canopy-web**, within an allowlist, as declared, with writes needing a click.
5. **Plain scheduled jobs:** code from a repo, on a cloud machine, no model, using connections.

## Non-goals

- Replacing the sealing or the runner path. Agent turns, the laptop CLI, grants, statements and keepers stay.
- A separate key-holding service. It could later stop token theft from a hacked server; the switch would not change.
- Running project code on canopy-web. Rule kept from the projects spec.
- Google calls through canopy-web, which need publisher consent for mail and calendar (roadmap P4). Until then Google has no switch in the UI.
- The Chrome extension as a caller. It needs a client-scoped OAuth token that canopy doesn't have yet (`/oauth/register` is public and only one scope exists). That stays with roadmap P6 (`client:<project>`), then S4, and uses this build's allowlist and token path.
- Sealing a connection for someone with no vault key. Connecting still needs one (`no_vault_key`), set up once, so "no passkey" means no passkey at connect time.
- Jobs started by inbound mail, and jobs on laptops.

## The security trade, stated in the product

| If someone gets… | Switch off | Switch on |
|---|---|---|
| A database dump or backup | Nothing | Nothing: the key never leaves KMS |
| Control of the running server | Can trigger runs, can't take the token | **Can have KMS open the token and keep using it after being evicted, until the owner disconnects** |

KMS logs every use in CloudTrail, and removing the task role's grant cuts canopy-web off at once. Turning the switch off stops future use by an honest server; it does not retract a copy already stolen, which takes a disconnect. A hacked server cannot turn the switch on for an off connection: it isn't a recipient, and machines reseal only as the owner's signed statement allows.

The settings screen says this in one plain sentence beside the switch.

## Part 1: the switch

### The key

- A new stack, `deploy/aws/canopy-web-connections-key.cfn.yaml`: a KMS key, `ECC_NIST_P256`, `KEY_AGREEMENT`, alias `alias/canopy-web-connections`, modelled on `runner/ec2/ecdh-kms.cfn.yaml`. Its policy gives `canopy-web-task-role` only `kms:DeriveSharedSecret` and `kms:GetPublicKey`. Deployed by hand with a labs admin login, as the role stack is, and only with Jonathan's approval.
- `apps/connections/server_key.py`: `available()`, `public_jwk()`, `thumbprint()`, `derive(peer_jwk) -> bytes`. Setting `CONNECTIONS_SERVER_KMS_KEY`; unset means `available()` is false and every connection behaves as switched off, so the code can merge before the key exists. Tests use a software key behind the same interface, reachable only from test settings.
- `deploy/aws/canopy-web-task-role.cfn.yaml`'s inventory comment and `tests/test_task_role_stack.py` gain the new call.

### On the wire

- canopy-web is a `device` recipient whose target is the key's thumbprint. No new recipient kind.
- The statement gains an **optional** key, `canopy_web: <thumbprint>`. It is present only while the switch is on, so a statement with the switch off is byte-identical to today's and older CLIs keep working.
- Order of change: `docs/vault/zk-v1/connections_v1_reference.py` and vectors, then the CLI (`wire._STATEMENT_KEYS`, `trust._device_allowed`, which also admits `jkt == statement.canopy_web`), then canopy-web and the browser.
- An older CLI refuses any statement carrying the new key, on cloud boxes and laptops alike. So canopy-web accepts `canopy_web` in a share only once the setting `CONNECTIONS_SERVER_STATEMENTS` is on, turned on after the CLI release has reached the cloud boxes and `CONNECTIONS_ZK_MIN_CLI` is raised to it. Until then, a connection with the switch on can't also be shared with machines, and the screen says so. A laptop on an older CLI gets the CLI's refusal with an added sentence telling it to update; that's accepted.
- Machines accept an envelope written by canopy-web only under a statement naming it. Before any statement exists, no machine reads the connection at all.

### States

| | No statement yet | Statement exists |
|---|---|---|
| **On** | Sealed to owner + canopy-web at connect; row `unshared`, `server_use=true` | Statement has `canopy_web`; recipients include it |
| **Off** | Sealed to owner only | Statement omits it; pruned from both envelopes |

`recipients()` stays the one rule: it adds canopy-web when `server_use` is true and, on an `active` row, only if the statement names the current thumbprint.

**Reconnecting** (`_upsert_connection` keeps the old statement but sets `unshared` and clears the sigs): `server_use` keeps the owner's choice and is never reset to the provider's default. canopy-web can use the new token at once if it was on. Machines stop until the owner shares again, as today. While a row is `unshared`, its old statement decides nothing.

### Connecting

The callback seals to the owner, plus canopy-web when `server_use` is true (on a new row, `Provider.server_use_default and server_key.available()`), then redirects to a new **connected** screen, `/w/<ws>/connections/<id>/connected`. This replaces the redirect to Share. It needs no vault unlock (the label is plain metadata) and shows one of:

- **Person, switch on:** "Connected as X. Project screens and apps you use can use it now." Secondary links: "Also let agents or my computers use it" (the Share screen) and the switch.
- **Agent, switch on:** "Connected Eva as X. Screens can use it now. One step left for Eva's own runs:" and a "Turn on for Eva's machines" button that opens Share. The passkey is unavoidable here: machines trust only a signed statement.
- **Switch off (Google, or no key):** "Connected as X. One step left: choose who can use it" with a button to Share.

The Share screen's "a first share has to reach someone" rule counts canopy-web as someone.

### Turning it on and off

- **Off, no statement:** `POST /api/connections/{id}/server-use {on: false}`. canopy-web prunes itself from both envelopes with `zk_seal.keep_only` and `keep_only_sig`. No passkey: giving up access is always easy.
- **Off, statement exists, or on in any state:** through the Share screen: the browser signs a new statement and reseals (passkey). A switch that needs this says so and goes there.
- `sharing.py` accepts `canopy_web` only if it equals the current thumbprint and the signer is the owner, and sets `server_use` from it.
- In-memory token caches key on the envelope digest, so a change takes effect at once in new reads and within the cache's TTL everywhere.

### canopy-web using a token

`apps/connections/server_tokens.py`, `access_token(conn) -> (token, instance_url)`:

1. A process-memory cache, TTL 5 minutes at most and never past the token's expiry, keyed by connection id and access envelope digest.
2. Else open `access_sealed` through `server_key.derive` (a new `zk_seal.open_for` taking a derive function; check it against the zk-v1 vectors). Use it when it has at least 60 s left.
3. Else take the same refresh lease machines take (`tokens.py`, holder `canopy-web`), refresh, and write back under the same compare-and-swap. If a machine holds the lease, wait up to 30 s for its write, as machines do.
4. Write-back seals to everyone `recipients()` names and carries writer MACs by canopy-web's key, one KMS derive per recipient. The plaintext keeps the contract's exact fields, including `statement_floor` and `owner_key`, read from what was opened.
5. `invalid_grant` marks `needs_reconnect`, as machines do.

Refresh and revoke move back into canopy-web's `Provider` for this path; the CLI keeps its own. A test pins that both agree on the token URL rules and on `rotates_refresh_token`. The browser accepts canopy-web's writes only while the row is `unshared`, or under a statement naming it.

**Disconnecting:** a `revoking` row canopy-web can open is revoked by it at once, then the usual tick retries, so disconnecting no longer waits for a machine.

### Audit

New kinds in `services.AUDITED`: `server_use_on`, `server_use_off`, `server_refreshed`. No token in any event, log or response; the existing secret-scan test extends to `server_tokens` and the call route.

## Part 2: calls from screens and apps

`POST /api/connections/{provider}/request {method, path, query?, body?, surface, as?, operation_id?}` → the provider's status and JSON body. `surface` (view and revision, or app) is required, and the route refuses a PAT or OAuth token: only a signed-in person's session. An agent's own turn uses its token file, not this route.

**Whose connection.** Always one from the surface's own workspace.

- `as: "viewer"` (default): the caller's own connection, and no way to name another person's. Several accounts for one provider in that workspace → 409 `ambiguous`, with a sentence saying which to disconnect.
- `as: "shared"`: an agent's connection (Eva's) that its admin has shared with this project or app, for any member who can open that screen. Every viewer acts as Eva's Salesforce user, and the audit line records both. A new model `ConnectionSurfaceShare(connection, kind: project|app, ref, approved_reads, approved_writes, approved_by)`; agent-owned connections only, so a person's own account is never used by someone else. **The admin approves the screen's declared reads and writes when sharing. A revision that asks for more stops working on `as: "shared"` until the admin approves again**, because a view's author isn't necessarily someone who may act as Eva. Managed from Eva's Accounts tab.
- No usable connection, or its switch is off → 409 `not_connected` or `server_use_off`, with a sentence and a link saying what to do.

**What a view's author can see.** A view declaring `as: "viewer"` reads each viewer's Salesforce data with no click, and its author can get at what it shows. That's the decision in the projects spec ("for CRM data inside one workspace that is acceptable"), and it includes views written by agents. Writes still need a click, and mail and calendar still wait for per-publisher consent (P4).

**Allowlist** (`apps/connections/calls.py`): the Salesforce list in projects spec §3, on method + path pattern. The host comes only from the opened plaintext's `instance_url` under the provider's host rules. Paths are normalised and refused with `..`, a scheme, or a host. Google is refused until P4.

**Declarations.** `DynamicView.connections`, snapshotted on `DynamicViewRevision` and validated on save, modelled on `agent_tasks` (`declared_tasks.py`); and a `connections` key in the workspace app manifest. Shape as projects spec §3, plus `as`. The bridge refuses anything undeclared; the server checks again against the revision.

**The bridge.** A new `connection` message in `frontend/src/dynamicViews/protocol.ts`, handled in `DynamicViewFrame.tsx` beside `read`, exposed in the frame as `canopy.connection.request(...)`, and available to app pages only on the `member` door. Public app pages never call.

**Writes need a click.** A click inside the frame, forwarded with the message, asks for an operation lease `POST /api/connections/operations`: `{operation_id, declared write set, expires 120 s, cap 500 writes}`. Copilot-proposed writes wait for the model gateway (P5).

**Limits.** 300 calls a minute per person per provider, 30 s timeout, 5 MB response cap, 429 through `TYPE_RATE_LIMIT`. Move `embedding/rate_limit._over` to `apps/common` and share it. A provider call must not hold a database transaction.

**Audit.** Every write is an audited event with viewer, connection, surface, revision and operation id. Reads are a log line.

## Part 3: plain jobs

**What a job is.** A named command in a repo's `.canopy/jobs.yaml`:

```yaml
version: 1
jobs:
  sync-leads:
    run: [python, scripts/sync_leads.py]
    timeout_minutes: 15
```

Unknown keys are an error. `run` is an argv list with no shell. Timeout is at most 120 minutes.

**Starting one.** `AgentSchedule.job` (a name, blank for ordinary turns) and `AgentSchedule.job_repo` (a repo name, chosen by the agent admin who sets it up, stored as the name itself and not a pointer to a project, so editing a project can't repoint it), set with cron or "Run now". `fire_schedule` and `run_schedule_now` copy both to new `Turn` fields and enqueue with a new routing value, `cloud_only`. `claim.py` gives a job turn only to a cloud runner that reports the `jobs` capability, so an older box can't treat it as an empty prompt.

**What canopy-web can say.** Only the job's name, the agent and a repo name. The box builds the repo's address from its own `REPO_URL_TEMPLATE`, so it only ever reaches `dimagi-internal`, and the command comes from that repo's `main`. A hacked canopy-web can start a job and pick among repos in the org, but it cannot supply code. It can still start an AI turn, the door accepted in the zero-knowledge spec; this design doesn't widen or close it.

**Running one**, in `runner/ec2/cloud_runner.py` at the `execute_prompt` seam in `_run_turn`:

- Fetch and check out `origin/main` of `job_repo`. If the fetch fails, the job fails: never run stale code.
- Read `.canopy/jobs.yaml` there. A name that isn't listed fails with a plain message.
- **A job runs as a separate Unix user, `canopy-job`**, not the runner's. The runner's user can read its own login, other agents' token files and `.env`s and reach the key broker, and `_child_safe_env` keeps credentials on purpose. So the job gets a new minimal environment (`PATH`, `HOME`, the agent slug, the turn id, `CANOPY_JOB_TOKENS`), no model key and no MCP token, and can read only its checkout and its own token directory. A box without that user doesn't report the `jobs` capability. `runner/ec2` image and bootstrap create the user and a narrow sudo rule for it.
- `_stage_connections` runs as usual and heartbeats keep tokens fresh. Each is also copied to `$CANOPY_JOB_TOKENS/<provider>.json` (a per-job directory, 0700, owned by `canopy-job`, same file format) and removed when the job ends.
- Stream stdout and stderr through the turn's events, and finish with the exit code and the last lines in `result_note`.
- Non-zero exit is `FAILED` and **never retried**: a job may have written. The sessionless-retry path must skip job turns. A timeout kills the process group and fails.

**Which connection.** The agent's, by the existing rule: its own, else one lent to it. So a job runs as Eva on Eva's account, or as Jonathan if he lent his account to that agent. Failed and missed occurrences use the schedule's existing alert and nag.

**Who can set one up.** An agent admin, as schedules are today, and only someone who could already merge to the repo can define a job.

Any project can have a job this way: its repo holds the code, and the agent that runs it supplies the account.

## Order of work

Three PRs, each merged and usable alone:

1. **Part 1:** reference and vectors, CLI, canopy-web, browser, connected screen, key stack. Needs Jonathan's approval to deploy the key and canopy-web.
2. **Part 2:** route, allowlist, declarations, bridge, leases, shares.
3. **Part 3:** `.canopy/jobs.yaml`, schedule and turn fields, claim gate, the `canopy-job` user, runner branch, UI for picking a job on a schedule.

Docs with each: `vault.md`, `api-surface.md`, `deploy.md`, `dynamic-views.md`, `runner-flags-and-schedules.md`. Update the canopy-salesforce roadmap after Part 2 lands.

## Left to the builder

Field and helper names beyond those given, the screens' layout within their stated wording, the event batching for job output, and the frame API's exact shape.
