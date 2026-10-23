# Changelog — dimagi-canopy

The import name is `canopy_sdk` and does not change with the distribution name.

## 0.17.0 — 2026-10-23

**Breaking: real data → sensitive data in `site.json`.** A site's concept is "this agent
may work with my sensitive data" — the vocabulary canopy uses everywhere else (a sensitive
conversation, a capability that handles sensitive data, a sensitive-data runner). canopy
reads only the new names; a host must move in lockstep.

- Wire (`site.json`): `real_data` → `sensitive_data` (still `{"requires": {"handles_sensitive",
  "runner_flag"}}`), and `uses.real_data_agents` → `uses.sensitive_data_agents`.
- `canopy_sdk.site.site_document(..., real_data=...)` → `site_document(..., sensitive_data=...)`.
- `SiteNeeds.real_data_requires` → `SiteNeeds.sensitive_data_requires`;
  `SiteNeeds.uses_real_data_agents` → `SiteNeeds.uses_sensitive_data_agents`.
- Django: the `CANOPY_HOST["SITE_NEEDS"]` hook returns `{"sensitive_data": {...},
  "uses": {"agents": [...], "sensitive_data_agents": [...], ...}}`; `views.site_document`
  reads `needs["sensitive_data"]`.
- The `pages` part of `site.json` is informational for canopy: it shows it, but never
  enforces it or tells an agent. Agents declare only `sites:` and `data: sensitive`; the
  host enforces its own page scopes.

## 0.16.0 — 2026-10-23

A host is set up from what it already knows, names its canopy tenant directly, and can
say why canopy showed it nothing. Additive: nothing a 0.15 host calls changes shape.

- **`CANOPY_HOST["WORKSPACE"]`** (`HostConfig.workspace`): the canopy workspace slug the
  Connected site is registered in. When set, the SDK signs it as the new claim
  `canopy_workspace` (`contract.WORKSPACE_CLAIM`, `contract.parse_workspace_claim`) into
  every visitor assertion and site agents request, and canopy selects the site's
  registration by it — so deleting or moving the agent in `AGENT_SLUG` no longer leaves
  the site unresolvable. `AGENT_SLUG` keeps working (and keeps its other jobs); with both,
  canopy refuses a disagreement with `workspace_mismatch`. The site agents request sends
  the workspace alone when it is set.
- **`canopy_sdk.site_agents`**: `request_assertion(..., workspace="")` and
  `fetch_site_agents(..., workspace="")`. A refusal canopy answered is no longer
  `unreachable`: `SiteAgentsError.code` is canopy's own code (`unknown_issuer`,
  `ambiguous_issuer`, `workspace_mismatch`, `bad_signature`, `rate_limited`…, or `refused`
  when it named none) and `SiteAgentsError.status` its HTTP status; `unreachable` means
  canopy could not be reached. `SiteAgentsError.as_dict()` / `from_dict()`.
- **Links in the answer**: `SiteAgents.site_settings_url` (this site's page in canopy,
  the wire's `site.settings_url`) and `SiteAgent.problem_links` — `AgentProblem(text, url)`,
  each problem with the URL of the page that fixes it (wire `problem_links: [{text, url}]`,
  beside the unchanged `problems` sentences). Both empty from an older canopy.
- **`canopy_sdk.django.site_agents`**: `last_error() -> SiteAgentsError | None` — the last
  refresh's failure (`not_configured` at once for an unusable `CANOPY_HOST`), kept in the
  host's cache and cleared by the next success. `refresh()` is a host view's "re-read now":
  it bypasses the cache and the retry pause, raises `SiteAgentsError` (including
  `not_configured`, no longer `HostNotConfigured`), and records the failure. The cache key
  now includes the tenant selector (`WORKSPACE` / `AGENT_SLUG`), so changing it takes
  effect on the next read.
- **`canopy_sdk.site`**: an optional `site` block — `{"origins", "jwks_url", "issuer",
  "mcp_resource"}` — in `site_document(..., site=...)`, read back as `SiteNeeds.site`
  (`SiteFacts`, or None). `WELL_KNOWN_PATH = "/.well-known/canopy-site.json"`.
- **`canopy/site.json`** fills the `site` block from the host's settings: `ORIGINS`
  (default: the origin of `ISSUER`, else of the request), `JWKS_URL` (default: the SDK's
  `jwks.json` route), `ISSUER` and `RESOURCE` while the grant is on. canopy's Connected
  sites page adopts them on its owner's confirm ("Fill from the site"); mount
  `views.site_document` at `/.well-known/canopy-site.json` so the site's address alone
  finds it.
- `canopy_sdk.fetch.FetchError` carries `status` and `detail` when the server answered.

## 0.15.0 — 2026-10-22

**Breaking.** The sensitive-data vocabulary replaces ZDR / PHI (spec
`2026-10-22-sensitive-data-agents-design`). "ZDR" now names only the zero-data-retention
API key a sensitive-data runner uses; the runner is a sensitive-data runner, and the
capability handles sensitive data. No aliases: the old names and tokens are gone, and a
host must move with a canopy-web that speaks the new ones.

- The runner flag / runner requirement token `zdr` → `sensitive_data`
  (`contract.RUNNER_FLAGS == {"sensitive_data"}`); `runner_posture.data_mode` `zdr` →
  `sensitive_data`.
- `canopy_sdk.host.execution`: `ZDR` → `SENSITIVE_DATA`, `PHI` → `SENSITIVE`;
  `Execution.phi_clearance` → `Execution.sensitive_clearance(agents=, require=("sensitive_data",))`;
  the `declaration`'s `data` `phi` → `sensitive` (the interface setting `data: sensitive`).
- `canopy_sdk.site_agents`: `SiteAgent.phi_safe` → `handles_sensitive`; `AgentCapability.phi_safe`,
  `phi_set_by`, `phi_set_at` → `handles_sensitive`, `sensitive_set_by`, `sensitive_set_at`, and
  the same keys on the wire.
- `canopy_sdk.site`: `real_data.requires` keys `{"phi_safe", "runner_flag"}` →
  `{"handles_sensitive", "runner_flag"}`; `runner_flag` is `sensitive_data`.
- Hosts: `RUNNER_REQUIREMENTS` / `runner_requirements=["zdr"]` → `["sensitive_data"]`.

## 0.14.0 — 2026-10-22

The site agents API: canopy tells a connected site, signed, which agents it may show
and on what terms, and the site publishes what it needs back (connect-labs spec
`2026-10-22-canopy-site-agents-api-design` §1–2). No new secrets: the site asks with
its own signing key, canopy answers with its client (`Canopy-Execution`) key.

- **`canopy_sdk.site_agents`.** `fetch_site_agents(canopy_url, site_name, site_key, canopy_keys, *,
  client_id=None, agent="")` → `SiteAgents` (`version`, `site_name`, `tenant`, `runner_flags`,
  `agents`, `agent(slug)`, `stale`, `age`), with `SiteAgent` (`capabilities`,
  `phi_safe`, `flags_available`, `ready_with` / `ready_with_flag`, `ready`, `problems`)
  and `AgentCapability` (`phi_safe`, `phi_set_by`, `phi_set_at`); `SiteAgentsError(code)`
  otherwise. Also `request_assertion` and `verify_site_agents`, and canopy's `verify_request`,
  `sign_site_agents`, `content_version`. `typ` `canopy-site-agents-request+jwt` / `canopy-site-agents+jwt`.
- **`canopy_sdk.django.site_agents.current()`**: cached in the host's cache for 5 minutes,
  refreshed on a miss; a failed refresh serves the last good response marked `stale=True`
  for up to 24 hours (retried at most once a minute), then `None` — show no agent.
- **`canopy/site.json`** (`views.site_document`, in `canopy_sdk.django.urls`): the
  host's `PAGE_SCOPES` (titled by `PAGE_TITLES`) plus `SITE_NEEDS()` (`real_data`,
  `uses`, `titles`), signed with the host key, `typ` `canopy-site+jwt`.
  `canopy_sdk.site` holds both halves (`site_document`, `sign_site_document`,
  `verify_site_document` → `SiteNeeds`, `site_document_url`).
- `canopy_sdk.fetch.get_text` — the pinned, bounded GET for a compact JWS; a refusal's
  RFC 7807 `detail` is carried in the `FetchError`.

Needs a canopy-web that serves `GET /api/sites/agents`; until then `current()` is
`None`.

## 0.13.0 — 2026-10-22

`Canopy-Execution` no longer pins a commit: PHI-safe is an interface setting
(`data: phi`) and the agent runs its current code.

- `Execution.phi_clearance` drops the commit check (`sha_mismatch` is gone). The
  `declaration` claim is `{data, capability, declared_by, declared_at}` — who last
  published the interface holding the setting, and when.
- `Execution.agent_sha` stays, always `""` from a current canopy, so a host that logs
  it keeps working; it is no longer stated.

A host on 0.12 refuses every call with `sha_mismatch` once canopy stops stating a
commit: upgrade before relying on real data.

## 0.12.0 — 2026-10-22

`Canopy-Execution`: canopy vouches for each call an agent's turn makes to a host, and the
host can verify it (spec `2026-10-22-phi-agents-on-zdr-runners` §6).

- **Host:** `canopy_sdk.host.verify_execution(request, *, config, client_keys, replay_store)`
  → `Execution` (agent, `agent_sha`, the PHI-safe `declaration`, `runner` with its flags in
  force and who declared them, `runner_posture`, `requirements`, `turn`, `session`), or
  `ExecutionRefused(code)`. Checks canopy's client-key signature and `typ`, `iss`, `aud` (your
  MCP resource), a ≤ 60s life, a single-use `jti`, `cnf.jkt` against the DPoP key proved on the
  request (`presented_dpop_jkt` by default) and `ath` against its access token.
- `Execution.phi_clearance(agents=, require=("zdr",))` — `None` when real data may be released,
  else the refusal: `agent_not_allowed`, `undeclared`, `sha_mismatch`, `runner_flags`,
  `runner_posture`, `requirements`.
- **Django:** `canopy_sdk.django.execution.verify_execution(request)`, wired to `CANOPY_HOST`,
  the jti table and a cached key resolver.
- **canopy's half:** `consumer.execution_statement(...)`; `contract.EXECUTION_HEADER`,
  `EXECUTION_TYP`, `EXECUTION_MAX_LIFETIME`, `EXECUTION_REQUIRED_CLAIMS`.

Needs a canopy-web whose gateway sends the header; until then `verify_execution` refuses
with `no_statement`, so a host that requires it fails closed.

## 0.11.0 — 2026-10-22

A page chooses its own agent and runner requirements; the panel passes more options through.

- **Per-page agent.** `panel_context(request, ..., agent="labsdata", runner_requirements=["zdr"])`
  (and `{% canopy_panel agent=... runner_requirements=... %}`) signs the page's choice into its page
  token. The mint signs it into the visitor assertion as the new `canopy_agent` claim
  (`contract.AGENT_CLAIM`), and canopy binds the token — and the host grant it redeems — to that
  agent. `AGENT_SLUG` stays the default for every other page, unbound.
- **Per-page runner requirements** add to `RUNNER_REQUIREMENTS`; a page can raise the floor, never
  lower it. `sign_visitor_assertion(..., agent=, runner_requirements=)`,
  `arrival_payload(..., bind_agent=True, runner_requirements=)`.
- **Key mode:** `CANOPY_HOST["PAGE_AGENTS"] = {key: {"agent": ..., "runner_requirements": [...]}}`;
  `panel_context(agent=...)` raises there, since a browser-named page cannot be signed.
- `PageRegistry.page(value, user_id)` / `PageTokens.read(...)` return a `Page` (scopes, agent,
  runner requirements); `django.conf.page_for(value, user)`. `scopes_for` / `page_scopes` unchanged.
- **Panel options pass through:** `PANEL` (and a per-render `panel_context(..., panel={...})`, merged
  key by key) now carries `width`, `open`, `target` (inline mode), `expandable` and
  `initial_display` to the widget. Unknown `panel=` keys raise.

Needs a canopy-web that understands `canopy_agent` to bind; an older canopy ignores the claim and
serves the token unbound.

## 0.10.0 — 2026-10-15

Ships `canopy-runtime` 0.3.0 at this tag: the capability manifest schema gains `kind: pool`.

- **`kind: pool`** (canopy-web#1509): a capability that leases a NAMED SLOT instead of a machine.
  `slots` is required (1–50 unique names); `aws`, `exec` and `params` are refused. Concurrency is
  the slot count. The broker hands each lease one free slot (`params.slot`, `ready_info.slot`) or
  queues it; the database refuses two live leases on one slot. First user: ACE's `test-worker`
  pool of Connect test workers (dimagi-internal/ace#2955).
- `canopy-runtime publish-capabilities` omits null fields, so `ec2-ephemeral` manifests publish
  unchanged to a broker that predates pools.

## 0.9.0 — 2026-10-14

`canopy_sdk.ondemand`, ephemeral capabilities: the stack guards itself, independent of the broker.

- **The instance role no longer carries `AmazonSSMManagedInstanceCore`** in ephemeral mode. That
  policy grants `ssm:GetParameter(s)` on `*`, so with the default `aws/ssm` key any process on a
  lease machine could decrypt every SecureString parameter in the account. The role now carries
  exactly what the SSM agent needs (registration, Run Command, Session Manager, associations,
  inventory) and an explicit Deny on `ssm:GetParameter*`. Persistent mode is unchanged (the
  render-equality test against 0.7.2 still holds).
- **Per-machine object prefixes.** Each instance reads and writes only `vm/${aws:userid}/*`; a
  lease's `in/`, `out/` and `exec/` live under `vm/<InstanceRoleId>:<instance-id>/lease/<lease-id>/`.
  The `leases/*` grants are gone, so no machine can read another lease's inputs or overwrite its
  results. New output `LeaseObjectScope: instance` tells the broker.
- **An exec document** (`ExecDocument`, output `ExecDocumentName`), the only document
  `BrokerPolicy` may send (no more `AWS-RunShellScript`): it takes the argv base64-encoded, NUL
  per argument, and runs it only if argv[0] is on the new `ExecAllow` parameter. Every parameter
  is pattern-restricted, so no argument is ever script text. `ExecAllow` is required in
  ephemeral mode.
- **A lifetime backstop** (`BackstopFunction`): a stack-owned Lambda, every 5 minutes, terminates
  any instance tagged with the stack's capability and owner older than the new
  `MaxInstanceLifetimeSeconds` (default 16200 = 4 h + 30 min). Its role can describe instances and
  terminate only those. Outputs `MaxInstanceLifetimeSeconds`, `BackstopFunctionName`.
- `send_command(..., document_name=, parameters=, document_version=)` and
  `EphemeralInstance.run_document_async(...)`: send a named document with its own parameters.
- `TAG_BROKER` (`canopy:broker`): the broker's deployment id, which canopy-web now tags on every
  launch (`launch(tags=...)` already took extra tags); `BrokerPolicy` allows it.

- **Root-volume performance parameters** (both modes; empty or `0` = unset, which is the old
  behaviour): `RootVolumeIops` (gp3, 3000–80000), `RootVolumeThroughput` (gp3 MiB/s, 125–2000)
  and `RootVolumeInitializationRate` (EBS Provisioned Rate for Volume Initialization, MiB/s,
  100–300 — how fast a fresh volume pulls the AMI's snapshot blocks instead of hydrating them
  lazily on first read). An ephemeral machine starts from a cold volume on every lease; on the
  Android emulator image `300` / `500` / `6000` cut `boot_completed` from ~50 s to ~37 s.
  Setting any of them on a persistent stack edits the launch template, which replaces its
  instance.
- **`EphemeralInstance.launch()` defaults to the launch template's `$Latest` version**, not
  `$Default`. A stack update adds a version, and `$Default` (which a template cannot set) need not
  follow it — a launch could keep using the stack's first version and silently ignore an update.
  Only the stack writes versions (`BrokerPolicy` cannot), so `$Latest` is what it last deployed.

Upgrading an ephemeral stack needs `ExecAllow=<the manifest's exec.allow, comma-separated>`.
Update it while no lease is live: a lease running under the old layout loses its `leases/`
grants the moment the stack updates.

## 0.8.1 — 2026-10-14

`BrokerPolicy` may read the launch template's AMI parameter (`ssm:GetParameter(s)` on
`/canopy/capabilities/<Owner>/<Capability>/ami`): EC2 resolves `resolve:ssm:` as the
RunInstances caller, so every launch failed with SsmAccessDenied without it.

## 0.8.0 — 2026-10-14

`canopy_sdk.ondemand`: ephemeral capabilities — one fresh instance per lease, terminated after.

- `EphemeralInstance(region=, launch_template_id=, capability=, owner=)`: `launch` (RunInstances
  from the launch template with `ClientToken=lease_id`, so a retry returns the same instance;
  user data is JSON; instances tagged `canopy:capability`, `canopy:owner`, `canopy:lease`),
  `describe`, `run_async` (SSM `SendCommand`, returns the CommandId at once, full output to S3),
  `invocation` (read a command back from SSM), `terminate`, `list_tagged`. `LaunchFailed.transient`
  says whether a retry can help. `OnDemandInstance` is unchanged.
- `send_command`: the fire-and-forget half of `run_command`, with `executionTimeout` and S3 output.
- `ondemand-capability.cfn.yaml` gains `Mode: persistent | ephemeral` (default `persistent`).
  Persistent renders exactly the resources and outputs 0.7.2 did (a test compares the two), so
  existing stacks update to a no-op. `AmiId` and `ConsumerTaskRoleName` now default to `""`
  (Rules require them in persistent mode); new parameters `Mode`, `Owner`, `BrokerRoleName` all
  have defaults. Ephemeral mode: no Instance and no idle alarm; the launch template resolves its
  AMI from `/canopy/capabilities/<Owner>/<Capability>/ami` at every launch, terminates on halt and
  tags instances `canopy:capability` / `canopy:owner`; `BrokerPolicy` grants the broker exactly the
  lease lifecycle on those tags; the instance role writes only `vm/${aws:userid}/*` and the lease
  `out/` and `exec/` prefixes and reads `in/`. Outputs `LaunchTemplateId`, `ArtifactsBucketName`,
  `InstanceRoleArn`, `InstanceRoleId`, `Capability`, `Owner`.

## 0.7.2 — 2026-10-08

`canopy_sdk.ondemand`: three public methods, so consumers stop reaching into private ones.

- `OnDemandInstance.state()`: the EC2 state name from a single describe call, with no SSM. Use it for cheap warm/cold checks; `status()` still adds the idle age over SSM.
- `OnDemandInstance.wait_ec2_ok(timeout_s)`: waits for the instance and system status checks.
- `JobStore.put(job)`: stores an edited job record.

## 0.7.1 — 2026-10-08

`canopy_sdk.ondemand` fixes from the first live run:

- `OnDemandInstance.ensure_running`: the default `boot_timeout_s` goes from 180 s to 420 s.
  A real m8i.xlarge took about 230 s from start to passing its status checks, so 180 s
  failed every cold start.
- The EC2 gate now waits for **system** status ok as well as instance status ok, which is
  the check ace-web's mobile runner always used. A `ClientError` during the wait is now
  raised as `OnDemandError` instead of escaping.
- `ondemand-capability.cfn.yaml` (the first real stack exposed these):
  - CloudFormation rejects `"$Latest"` as an instance's launch-template version, so no
    stack could be created. The template now pins `LatestVersionNumber`, which means a
    launch-template edit replaces the instance (new id).
  - The bucket now expires only `requests/` and `results/`. Worker files and caches no
    longer vanish after 7 days.
  - The CloudWatch backstop window is a parameter, `IdleStopMinutes`, defaulting to 60
    (it was a fixed 5 minutes). That matches the watchdog's 3600 s, so a box no longer
    stops between interactive requests.
  - The instance can read the bucket, and the consumer can put under `requests/`.

## 0.7.0 — 2026-10-08

`canopy_sdk.ondemand`: run a dedicated capability on a stopped-when-idle EC2
instance. Minor: a new optional module (the `ondemand` extra: boto3 + redis),
nothing in the core changed.

- `OnDemandInstance` — start on demand, wait for EC2 status checks and the
  capability's ready file (`/run/<capability>/ready`) over SSM, touch the
  activity marker (`/var/run/<capability>/last-activity`), stop; never terminate.
- `run_command` / `CommandResult` — SSM `AWS-RunShellScript` exec with polling
  and one throttling retry (ported from ace-web's mobile runner).
- `Lease` — a cross-process Redis lease per capability; `JobStore` / `Job` —
  Redis records for long-running operations.
- Errors: `OnDemandError`, `InstanceGone`, `SSMFailure`, `SSMTimeout`.
- Package data, via `canopy_sdk.ondemand.assets.asset_path(name)`: the in-VM
  idle watchdog (`idle-shutdown.sh`, `ondemand-idle-shutdown.service` /
  `.timer`) and `ondemand-capability.cfn.yaml`, the per-capability stack
  (instance, egress-only SG, SSM instance role, artifacts bucket, CPU idle-stop
  alarm, and the consumer's IAM policy scoped by the `capability` tag).

## 0.6.1 — 2026-10-05

`protected_resource_metadata` no longer adds the grant's tool scopes to
`scopes_supported`. An interactive MCP client registers for exactly that list,
so on a host whose registration accepts only its sign-in scopes, every new
sign-in failed (canopy-web#1157; connect-labs worked around it in #2220). The
RFC 8414 document still lists them, and that is where canopy reads them.

## 0.6.0 — 2026-09-30

A host can require runner flags of its visitors' conversations. Minor: new
public API, nothing removed.

- `contract.RUNNER_REQUIREMENTS_CLAIM`, `RUNNER_FLAGS` (`zdr`), `parse_runner_requirements`.
- `HostConfig.runner_requirements` / `CANOPY_HOST["RUNNER_REQUIREMENTS"]`; every
  visitor assertion carries the claim, and `extra` cannot forge or drop it.

## 0.5.0 — 2026-09-29

A page that changes what it shows without reloading can tell the agent. Minor:
new public API, nothing removed, contract version unchanged.

- **`window.canopyHost` + the `canopy:ready` event** — the panel template kept the
  widget in a closure, so a page that drills, filters or selects a row without a
  reload had no way to update what the agent sees. `canopyHost.updatePageState(patch)`
  merges the patch into the declared state and pushes the whole (canopy replaces
  state on every push); `resource` and `backing_tool` stay what the server
  rendered, since a script on the page may narrow the view but not change what
  the page is. The selection is trimmed to the same byte budget `pages.py` uses.
  `canopyHost.pageState()` reads the current state; `canopyHost.widget` is the
  widget itself, for the rest of its API.
- `panel_options` carries `stateByteBudget` and `maxVisibleIds` for that trim.

## 0.4.1 — 2026-09-28

- **`DPoPGate` refuses a DPoP-bound token presented as a plain `Bearer`** (401
  `invalid_token`, RFC 9449 §7.1), instead of leaving it to the host's own
  verifier to fail. A correctly built host already refused it; a looser one let
  it reach the tool layer. Found by canopy's live probe. `ResourceVerifier.is_bound`.

## 0.4.0 — 2026-09-28

The live probe: canopy can exercise a REAL grant against a host on a schedule,
without a visitor. Minor: new public API, nothing removed, contract version
unchanged (every new field is an optional extension).

- **`canopy_sdk.host.ProbeIdentity` + `ProbeHandler`** — a host configures a
  dedicated low-privilege probe principal, one read-only scope, one tool (+
  arguments), an optional out-of-scope `denied_tool` and page key. The probe
  endpoint accepts ONLY canopy's configured client (`private_key_jwt`, verified
  by the token endpoint's own code, `aud` = issuer or the probe URL) plus a
  DPoP proof; refuses any request naming a principal, tool, arguments, or a
  scope/resource other than the probe's; spends every `jti` only after all
  checks; and returns a real ID-JAG for the probe principal (≤ 300 s,
  single-use `jti`, marked `canopy_probe: true`). `HostConfig(probe=...)`,
  `HostConfig.probe_enabled`. Unconfigured = `ProbeDisabled` (404).
- **Metadata** — `authorization_server_metadata` adds
  `canopy_probe_endpoint` while a probe is configured.
- **Django** — `CANOPY_HOST["PROBE"]` (`ENDPOINT`, `SUBJECT` or
  `SUBJECT_RESOLVER`, `SCOPE`, `TOOL`, `ARGUMENTS`, `DENIED_TOOL`, `PAGE`),
  `views.probe_endpoint` at `canopy_host:probe` (`probe/`). A broken `PROBE`
  block logs and turns the probe off — never the grant. `SUBJECT_RESOLVER` is
  never called on the event loop (the MCP DPoP gate builds its config there,
  where a database read is forbidden, and never needs the probe).
- **`issue_id_jag(..., extra_claims=)`** — claims beside the contract's, which
  always win.
- **`GrantHandler` logs `probe=True`** for a probe ID-JAG it redeems.
- **`canopy_sdk.conformance.live`** — `request_probe`, `check_live_grant`
  (probe → normal redemption → token material), and the three assertions:
  `check_probe_tool` (a), `check_out_of_scope_refused` (b),
  `check_requires_dpop` (c: no proof, a stranger's key, a plain bearer);
  `run_live` for all of it.

## 0.3.0 — 2026-09-27

Fixes for the gaps three hosts (connect-labs, ace-web, canopy-web) found
moving onto the SDK. Minor, not patch: new public API. Nothing is removed.

- **The DPoP gate never 500s on configuration.** `DPoPGate` answers 401
  `invalid_dpop_proof` when its verifier factory raises `HostNotConfigured`
  (or cannot be built at all), and non-DPoP traffic still passes through
  untouched, so `canopy_sdk.django.asgi.dpop_gate` is safe to install while the
  grant is off. `canopy_sdk.django.asgi.resource_verifier()` now raises
  `HostNotConfigured` whenever the grant is off (not only without a key), so a
  token issued while it was on stops resolving. New `resolve_delegated(raw,
  jkt)` for a host's bearer verifier: `None`, never an exception, while off.
  `dpop_gate(..., run_sync=)` takes a host's own sync runner. The Django
  metadata views 404 (were 500) while the grant is off; the panel-token view
  503s (was 500) on an unreadable key.
- **`CANOPY_HOST` may be a callable** (or a dotted path to one) resolved on
  every read, for values that derive from settings overridden later. A plain
  dict still works; what the SDK reads is always a real `dict`.
- **`PANEL_TOKEN_URL_NAME`** — name a URL of the host's own for the panel to
  mint at, reversed per request.
- **`panel.html` renders its options through `json_script`**, not `escapejs`,
  so the token URL reaches the page literally (`=` stayed `\u003D` before).
  A host that overrides the template gets a new `canopy_panel.options` dict.
- **`mint_contact_token` returns canopy's whole response** (`kind`,
  `host_grant`, `contact_id`, …), defaulting `kind`/`host_grant` for an older
  canopy. The panel-token view passes `kind` on to the browser. `MintFailed`
  carries `status` (canopy's HTTP status for a refusal, else `None`).
- **SPA page keys — two modes of one API.** `canopy_sdk.host.PageRegistry`
  (key mode: the browser names a page key, or a path `patterns` recognises;
  read-only scopes only unless `writable_scopes`) and `PageTokens` (signed
  mode, now a subclass) share `scopes_for(value, user_id)`. Django:
  `CANOPY_HOST["PAGE_MODE"] = "key"` + optional `PAGE_PATTERNS` /
  `WRITABLE_SCOPES`, `conf.page_registry()`, `conf.page_scopes(value, user)`.
- Docs: opt in to the pytest fixtures with `-p canopy_sdk.conformance.pytest_plugin`
  (or `addopts`); `pytest_plugins` works only in the rootdir conftest.

## 0.2.0 — 2026-09-27

- `canopy_sdk.conformance.check_client` — does a host accept canopy as its
  client, provable WITHOUT the host's signing key: the real `private_key_jwt`
  assertion and DPoP proof, carrying an ID-JAG signed by a throwaway key.
  `invalid_grant` means the client authenticated; `invalid_client` means it
  did not; a 200 means the host accepted a grant it never issued. Nothing is
  spent at the host. `run()` includes it when given `credentials` and no
  `id_jag`. canopy-web's Connected sites "Test connection" uses it.

## 0.1.0 — 2026-09-27

First release. Implements host grant contract v1 (`CONTRACT_VERSION = "1"`).

- `canopy_sdk.contract` — the contract's constants and pure helpers, defined once.
- `canopy_sdk.host` — the host half, extracted from connect-labs PR #2060 and
  generalised: visitor assertion + ID-JAG signing, the page registry and page
  token, the jwt-bearer `GrantHandler`, the MCP-side `ResourceVerifier` and ASGI
  `DPoPGate`, metadata helpers.
- `canopy_sdk.consumer` — canopy's half, extracted from canopy-web
  `apps/tokens/`: visitor-assertion and ID-JAG verification, the client
  assertion and DPoP proofs, token request/response handling.
- `canopy_sdk.django` — optional Django wiring (settings, views, models +
  migration, the DPoP gate, `{% canopy_panel %}`).
- `canopy_sdk.conformance` — live checks and opt-in pytest fixtures.
- `canopy_sdk.fetch` — SSRF-safe, address-pinned outbound fetches (stdlib only).
