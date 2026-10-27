# Changelog — dimagi-canopy

The import name is `canopy_sdk` and does not change with the distribution name.

## 0.24.0 — 2026-10-27

**Site sign-offs: a site asks canopy for a person's passkey approval of one action.**

- `canopy_sdk.sign_offs`: `request_sign_off` (the site asks, signed with its own key, and
  gets the `approve_url` to send the approver to), `fetch_status` (reads one request,
  approval included), and `verify_approval` (checks canopy's signed approval: canopy's
  client key, this site, the action, the pending action's `site_ref`, a passkey step-up,
  optionally the subject). canopy's half is `verify_request` and `sign_approval`.
- `canopy_sdk.django.sign_offs`: `request`, `status` and `verify` from `CANOPY_HOST`. The
  host spends the approval's `jti` itself, in the transaction that acts on it.
- Needs a canopy with `POST /api/sites/sign-offs`; an older one answers 404 (`refused`).

## 0.23.0 — 2026-10-26

**An act-as-you permission lasts 30 minutes, and the panel asks for one only after the person sends.**

- `contract.ACCESS_TOKEN_MAX_LIFETIME` is 1800 (was 900): a host access token lives at
  most 30 minutes. `HostConfig.access_token_ttl` defaults to it, so an SDK host issues
  30-minute tokens unless configured lower. A canopy older than this clamps them to 15
  minutes, which still works; upgrade canopy first to get the full half hour.
- `canopy_sdk.django`'s `panel_token` issues an ID-JAG only when the mint's JSON body is
  `{"grant": true}`. canopy's panel sends that once the person has sent a message in it
  (and while that tab stays open after), never on page load, so the agent acts as the
  person only after they used the panel. A mint with `{}` (or no body) gets a token and
  no grant, whatever the page. **Behaviour change** for a host on this view: no grants
  until canopy's widget sends the flag (canopy-web serves `widget.js`, so it does once
  canopy is deployed). A host with its own token endpoint should do the same.

## 0.22.1 — 2026-10-24

**The panel tells its page which conversation is open, and opens one by id.**

- `canopy_host/panel.html` passes the widget's `onSession` (canopy-web's embedding guide
  §5e): the conversation open in the panel, `{id, resource, private}` or `null`, is kept
  for `canopyHost.session()` and announced as a `canopy:session` event on `document`.
  Never its title or words — a pointer, for a page that keeps conversations (`resource`
  is where it began, as canopy recorded it).
- `canopyHost.openSession(id)` opens the panel on a conversation by its canopy id; canopy
  reads it as the visitor, so its session ACL decides. Returns false on a widget older
  than the call.

## 0.22.0 — 2026-10-23

**A kept link is `<scheme>://<id>` and nothing more; the request digest is unambiguous.**

- `validate_link` keeps a link only when its URI is `<scheme>://` and a url-safe id
  (`resources.id_of`: `A–Z a–z 0–9 _ -`, no path, query or fragment; at most
  `MAX_ID_LENGTH`, 64) and its `mimeType` is one of `resources.MIME_TYPES`
  (`application/json`, `application/pdf`, `image/jpeg`, `image/png`, `text/csv`,
  `text/markdown`, `text/plain`). `resource_link(...)` raises on anything else. **Breaking**
  for a host whose result URIs have a path or query: give each result an opaque id.
- `schemes` may be `{scheme: id length}`: the id must then be exactly that long.
- `site.json`: new optional `site.resource_id_lengths` (`{"labs-result": 24}`), from
  `CANOPY_HOST["RESOURCE_ID_LENGTHS"]`; `SiteFacts.resource_id_lengths` and
  `SiteFacts.resource_policy()`. Each length is 16–64 (`resources.check_id_length`) and
  names a declared scheme; a bad entry fails `canopy_host.E011`. A canopy on 0.21 or older
  reads a document carrying the new key as malformed, so canopy upgrades first.
- `contract.rpc_digest`: a `resources/read` URI's query is decoded, sorted and re-encoded
  (`urlencode`) instead of joined decoded, so `%26` and `&` no longer hash the same; a
  non-UTF-8 escape stays distinct, and a lone surrogate (in a URI or a tool's arguments)
  is hashed instead of raising. `tools/call` digests are unchanged.
- `contract.rpc_matches(claimed, method, params)`: what `host.verify_execution` and
  `host.verify_view` now check. **For one deploy window** it also accepts the pre-0.22
  `resources/read` form, so a 0.22 host verifies a canopy that has not upgraded, and a
  0.21 host verifies an upgraded canopy for every URI whose query needs no escaping (every
  Labs read). The old form is dropped in 0.23.

## 0.21.0 — 2026-10-23

**What canopy keeps of a `resource_link` is an address, and only that.** In a sensitive
conversation the link is the one thing canopy keeps of a tool's result, in plaintext, so it
can no longer double as a place to put data.

- `resources.MAX_URI_LENGTH` 2048 → **256**; `resources.MAX_LINKS` 20 → **10**.
- `validate_link` / `validate_links` keep `{type, uri, name, mimeType}` and **never
  `description`** (nor `_meta`, `annotations`). New `keep_name=False` keeps the URI's
  scheme as `name` instead of the host's words (canopy uses it for a sensitive
  conversation). `resource_link(..., description=)` still returns the description to the
  model; canopy does not keep it.
- Django: a system check, `canopy_host.E011`, fails startup on a
  `CANOPY_HOST["RESOURCE_SCHEMES"]` entry `check_scheme` refuses — one bad scheme made the
  whole signed `site.json` malformed, and canopy then read none of the site's facts.

## 0.20.0 — 2026-10-23

**`Canopy-View`: canopy's positive word that a request is the person's own View.** A host
that gives a person's MCP Apps View more than an agent (Connect Labs: the rows of a
sensitive result) must decide it on a signed statement, never on a missing
`Canopy-Execution` — anything that makes canopy issue a delegated request without a
statement would otherwise read as the person.

- `contract.VIEW_HEADER` (`Canopy-View`), `VIEW_TYP` (`canopy-view+jwt`), `VIEW_ACTOR`
  (`"view"`), `VIEW_MAX_LIFETIME` (60s), `VIEW_REQUIRED_CLAIMS`.
- `consumer.view_statement(key, client_id, audience, *, subject, uri, session, jkt,
  access_token, viewer="", agent="")` — canopy's half: `actor: "view"`, bound to the person
  (`sub`, the host's id for them), the resource (`uri`), the conversation (`session`), the
  DPoP key (`cnf.jkt`) and the access token (`ath`, required); 60s, single-use `jti`.
- `canopy_sdk.host.verify_view(request, *, config, client_keys, replay_store, uri,
  subject)` → `View`, or `ViewRefused` (`no_statement`, `agent_call` — a request that also
  carries `Canopy-Execution` —, `bad_typ`, `bad_signature`, `bad_audience`, `bad_issuer`,
  `bad_actor`, `expired`, `too_long`, `unbound`, `wrong_person`, `wrong_resource`,
  `replayed`, ...). `same_resource(stated, actual)` compares scheme, authority and path
  (a page of a result is the result). Django: `canopy_sdk.django.execution.verify_view`.
- **Both statements bind the JSON-RPC request they ride** (`rpc` claim,
  `contract.rpc_digest(method, params)`: the method plus, for `tools/call`, the tool name
  and arguments, for `resources/read` the URI with its query sorted; `_meta` and `id` are
  not part of it). `execution_statement(..., rpc=)` / `view_statement(..., rpc=)` sign it;
  `verify_execution(..., rpc=(method, params))` / `verify_view(..., rpc=...)` refuse a
  statement for another request (`wrong_request`), or one with no `rpc` when the host asks.
  Helpers: `contract.rpc_digest_of_body`, `contract.rpc_of_body`. A host that does not pass
  `rpc` sees no change.
- Unchanged: 0.19.0's migration `canopy_host.0003_drop_artifact` still drops the
  `canopy_host_artifact` table on a host that upgrades from 0.18.x.

## 0.19.0 — 2026-10-23

**Breaking: kept results moved out of the SDK; a host keeps its own MCP results; the SDK
only declares link schemes and relays updates.** Keeping a tool's result is the host's
MCP business, not canopy's: a host returns facts plus a standard MCP `resource_link` to a
result it keeps, and answers `resources/read` on its own scheme (README, "Host-held
results").

- Removed: `canopy_sdk.host.artifacts` (`Artifacts`, `ArtifactRefused`,
  `MemoryArtifactStore`, `ArtifactStore`, `current_subject`, `summarize_rows`, the
  module-level helpers); `canopy_sdk.django.artifacts` (`save`, `read`,
  `resource_handler`, `template`, `mount_fastmcp`, `update`, `notify_updated`, `sweep`,
  `DjangoArtifactStore`); `manage.py canopy_artifacts_sweep`; the `Artifact` model; the
  `CANOPY_HOST["ARTIFACTS"]` settings (now ignored).
- Migration `canopy_host.0003_drop_artifact` drops the `canopy_host_artifact` table. Its
  rows were 24-hour results, so nothing is carried over.
- Kept: `CANOPY_HOST["RESOURCE_SCHEMES"]` is now the only source of `site.json`'s
  `site.resource_schemes` (no longer derived from `ARTIFACTS["SCHEME"]`), and
  `canopy_sdk.resources` (`resource_link`, `check_scheme`, `notify_updated` — the signed
  "resources updated" POST to canopy — and canopy's checks) is unchanged.

## 0.18.0 — 2026-10-23

**Host-held results** (spec `2026-10-23-host-held-results-design`): a tool returns facts
for the model plus an MCP `resource_link` to an artifact the host keeps; canopy carries the
link, never the data, and the person's View and the agent read it back as themselves.
Additive: nothing a 0.17 host calls changes shape.

- **`canopy_sdk.host.artifacts`**: `Artifacts(store, scheme=, ttl=, may_read=, on_read=,
  notify=)` — `save(owner, kind, payload, *, tool_call_id, ttl, scheme, name, description)`
  → a `resource_link` dict; `read(uri, reader)` → `Artifact` or `ArtifactRefused`
  (`bad_uri`, `not_found`, `expired`, `forbidden`); `contents(uri, reader)` → the MCP
  `resources/read` contents (JSON text, `_meta["canopy/artifact"]`, a table paged by
  `?page=&page_size=`); `update(uri, payload)` (calls `notify`); `sweep()`; `template()`.
  `MemoryArtifactStore`, the `ArtifactStore` protocol, `current_subject()` (the
  `DPoPGate`'s delegated principal), `summarize_rows(columns, rows, sample=5)`, and
  module-level `configure` / `save` / `read` / `contents` / `sweep`.
- **`canopy_sdk.resources`**: the link (`resource_link`, `validate_link(s)`, `check_scheme`,
  `RESERVED_SCHEMES`, bounds) and "it changed" (`updated_notification`,
  `verify_updated_notification`, `notify_updated`, `UPDATED_PATH` =
  `/api/sites/resources-updated`, typ `canopy-resources-updated+jwt`).
- **`site.json`**: the `site` block gains `resource_schemes` (`SiteFacts.resource_schemes`).
  canopy keeps a host's links only under a scheme it declares there, reading it live.
- **Django**: model `Artifact` + migration `canopy_host.0002_artifact` (table
  `canopy_host_artifact`); `canopy_sdk.django.artifacts` (`save`, `read`,
  `resource_handler`, `template`, `mount_fastmcp`, `update`, `notify_updated`, `sweep`,
  `DjangoArtifactStore`); `manage.py canopy_artifacts_sweep`; settings
  `CANOPY_HOST["ARTIFACTS"]` (`SCHEME`, `TTL`, `MAY_READ`, `ON_READ`, `READER`, `NOTIFY`,
  `EXTRA_SCHEMES`, `MIME_TYPE`) and `CANOPY_HOST["RESOURCE_SCHEMES"]`; `canopy/site.json`
  declares them.

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
