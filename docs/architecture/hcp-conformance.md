# HCP v1 conformance self-audit

**What this audits:** canopy's HCP instance (`apps/contacts/hcp*.py`) against HCP
v1, draft 4 (*"Human Context Protocol (HCP) v1 — Draft Reference Specification,
1.0-draft-4"*, Stanford HAI, Sept 2026).

**Written:** 2026-10-09, with the HCP service (`hcp-service.md`). Each criterion of
5.2 (Core), plus what 5.2.1 adds for Interop, is marked **pass**, **gap** (with
the reason), **n/a**, or **SHOULD-gap**. Evidence is a code path, or a test in
`tests/test_hcp.py`, `test_hcp_personal.py` or `test_hcp_service.py`.

## Verdict

**Interop needs Core plus `authorization_profile: oauth2` (5.1, 5.2.1).**
- **The OAuth half is met.** The HCP service is an RFC 6749 authorization-code
  server with PKCE S256.
- **Two Core MUSTs are open, so neither level is fully met.** One is presumed
  first-party grants, the other is Tier 1 JSON Schema validation (below).
- **What discovery declares:** `conformance_level: HCP-v1-Core` with the gaps
  listed in `canopy.conformance.known_gaps`, and does not claim Interop. Per 5.2.1
  the discovery document is authoritative, so it must not overclaim.
- **When to claim Interop:** close both gaps, then change `hcp.CONFORMANCE_LEVEL`
  to `HCP-v1-Interop`.

| Open MUST | Why it is open | What closes it |
|---|---|---|
| **4.1.4 / 4.1.6 for canopy's own agents.** A first-party agent's grant is *presumed* persistent by the canopy control plane the first time it serves a person, not created by a separate affirmative act. | Owner policy (Jonathan, 2026-10-08): canopy decides which agent serves whom. The person's agent-memory switches (#1380/#1381) are affirmative, but they are not a per-client grant act with categories, actions, type and expiry shown. | A first-run per-agent consent (the non-OAuth channel of 4.1.6) in chat or on `/people/me`, or routing canopy's agents through the same consent the HCP service uses. OAuth clients already conform. |
| **2.1.1 Tier 1 JSON Schema validation.** | Appendix A says the schema *will be* published at `https://hcp.me/schemas/v1`. It is not, so there is nothing authoritative to validate against. | Validate on write and emit once the schema is published. |

## Data model

| Criterion | Status | Evidence |
|---|---|---|
| Entries representable in the Common Envelope; form declared | pass | `hcp.to_entry` (grouped); discovery `envelope_form: grouped` |
| Tier 2 grouped form | n/a | Tier 2 not supported; `tier2_securing` omitted |
| Tier 1 validates against the published JSON Schema | **gap** | see Verdict |
| `record.declarationType` never omitted or null | pass | `DECLARATION[fact.basis]` |
| `record.confidence` iff model-inferred | pass | `hcp._check_confidence`; `test_confidence_is_required…` |
| No HCP vocabulary inside Tier 2 `claim` | n/a | no Tier 2 |
| Category change only by the person, audited | pass | `hcp.update_entry` refuses non-person; audit detail names both categories |
| Complete provenance as stored | pass | `provenance_source`, `captured_by` on every row; redaction is response-only |
| Version history; `version` / `previousVersion` | pass | `record_fact(supersedes=…)`; `test_an_update_is_a_new_version…` |
| Conflict detection both write orders, same category + dimension | pass | `hcp.detect_conflicts` |
| Conflicted entries withheld from agents, visible to the person | pass | `hcp.servable`; `/people/me` and export include them |
| Conflict resolution transitions logged `conflict.resolved` | pass | `update_entry`, `delete_entry`, `release_conflicts_against` |

## Actions

| Criterion | Status | Evidence |
|---|---|---|
| All four CRUD operations | pass | search / add (+ `POST /v1/preferences`) / update / delete |
| At least one transport fully implemented | pass | REST and MCP. OAuth clients use REST; MCP is first-party (`hcp-service.md`) |
| No entry outside granted scopes (4.4.1) | pass | `hcp.search` refuses out-of-scope categories; `within_grant` per entry, including OAuth-grant sources |
| Quantity bound, not padded (4.4.2) | pass | `min(maxEntries, 20)`; nothing added |
| Relevance ordering, method declared (4.4.3) | pass | lexical; discovery `minimization_method: lexical` |
| Provenance source / capturedBy omitted for agent actors (4.4.4) | pass | `to_entry(redact=True)` for agents and OAuth clients; `test_the_whole_flow…` |
| `minimization` object on search and single-entry (4.4.5) | pass | `hcp.minimization` |
| Hard deletion on the person's request | pass | `delete_entry(hard=True)`, person only |
| Ingested export does not reinstate deleted entries | n/a | canopy does not ingest exports |
| REST: Idempotency-Key on POST / PUT | pass | `hcp_api._idempotent`, per user or per OAuth client; `test_idempotency_works_for_a_client` |
| REST: RFC 9457 problems with registered types | pass | `hcp_api._problem`. The bearer middleware's 401 uses `invalid-token` |

## Authorization

| Criterion | Status | Evidence |
|---|---|---|
| Category-scoped, revocable grants exposed per 4.1.5 | pass | `PersonGrant`, `hcp.grant_dict` |
| Authorization distinct from authentication (4.1.6), no silent scope expansion | pass for OAuth clients; **gap** for presumed first-party grants | consent acts 1 + 2; a client asking beyond its grant needs a new grant |
| Grants SHOULD use OAuth 2.0; else declared per 5.2.1 | pass | `authorization_profile: oauth2` |
| Grant objects; the person can enumerate and revoke | pass | `GET` / `DELETE /v1/grants`; `/people/me` |
| A grant never disclosed to another client | pass | `GET /v1/grants` with a client token returns its own; `test_a_token_opens_hcp_v1_only…` |
| `grantor` when the authorizer is not the subject | pass | always the subject at v1; `grantor` null |
| Narrowed grants: restriction carried, presented, enforced (4.1.5.1) | pass | OAuth grants carry `narrowedTo.sources`; `/people/me` shows the reach; `within_grant` enforces it. `test_workspace_entries_are_reached_only_when_ticked` |
| REST: `GET /v1/grants` scoped to the client's own | pass | as above |
| Temporary vs persistent; temporary the default | pass for OAuth; **gap** for presumed first-party (persistent) | `issue_grant`; first-party see Verdict |
| Temporary grants carry `expiresAt`, shown at authorization time | pass | DB check constraint; consent shows the duration and act 2 the time |
| Persistence needs a distinct affirmative act | pass for OAuth | act 2; `test_the_keep_question_preselects_nothing`, `test_keeping_access_is_its_own_act…` |
| Revocation available and propagates per 4.2 | pass | `hcp_oauth.revoke`: every token at once; audited in the same transaction |
| Revocation notifications signed and retried; revocation independent of delivery | pass | `HcpRevocationDelivery`, `attempt`, heartbeat; `test_revoking_kills_every_token…`, `test_a_failing_webhook…` |
| Token lifetimes per 4.1.3 | pass | access 1 h; refresh 30 d persistent, grant expiry temporary; codes 10 min |

## Audit

| Criterion | Status | Evidence |
|---|---|---|
| All required event types | pass | `PersonAuditEvent.EVENT_TYPES`, including `grant.expired` (on first access after lapse) and `revocation.notified` |
| Append-only | pass | `PersonAuditEvent.save` refuses updates |
| Inspectable by the person | pass | `GET /v1/audit`; `/people/me` |
| Agents and clients cannot read it | pass | `_person_principal` refuses agent logins, caller tokens and OAuth tokens |
| REST: cursor pagination, opaque cursor | pass | encrypted, MAC'd cursor in `hcp_api` |
| Durability declared and disclosed | pass | synchronous for every type; no bounded window declared |

## Person-facing capabilities

| Criterion | Status | Evidence |
|---|---|---|
| Enumerate all stored entries, including conflicted | pass | `/people/me` (live, including conflicted); export (including soft-deleted) |
| Correct or delete any entry | pass | `/people/me` Correct / Retract; REST PUT / DELETE |
| Enumerate and revoke all active grants | pass | `/people/me` "Who can read it" |
| Temporary-by-default authorization; separate persistence act | pass for OAuth | consent acts 1 + 2 |
| Bounded durability window disclosed | n/a | none declared |

## HTTP transport (3.4) and discovery (Appendix C)

| Criterion | Status | Evidence |
|---|---|---|
| `hcp_version` on every response body | pass | `_ok`, `_problem`, the bearer middleware's 401 |
| Unsupported `HCP-Version` gets 400 `unsupported-version` | pass | `hcp_route` |
| 429 carries `Retry-After` | pass | `_problem`; `test_a_client_is_rate_limited_with_retry_after` |
| Never rate-limit `DELETE /v1/grants/{id}` | pass | only OAuth-client calls are limited, and they cannot revoke via that route |
| Rate-limit policy published (SHOULD) | pass | discovery `rate_limits` |
| Discovery at `/.well-known/hcp-configuration` | pass | at the origin and under the issuer |
| MCP manifest (Appendix B) | pass | `/api/hcp/.well-known/mcp-manifest`, `authorizationScheme: first-party` (MCP is first-party) |

## OAuth (what Interop adds) and its gates

| Gate | Status | Test |
|---|---|---|
| Authorization code + PKCE S256 required; `plain` refused | pass | `test_pkce_is_required`, `test_a_wrong_verifier_gets_no_token` |
| Exact redirect URI; an unregistered one is shown, not redirected to | pass | `test_a_redirect_uri_not_registered_is_never_redirected_to` |
| Scopes per 4.1.1, no wildcards, never beyond registration | pass | `test_a_client_cannot_ask_beyond_its_registration_or_with_a_wildcard` |
| Declining creates no grant | pass | `test_denying_creates_no_grant` |
| Codes single use; replay revokes | pass | `test_a_code_works_once` |
| Refresh rotation; reuse revokes | pass | `test_a_rotated_refresh_token_presented_again_revokes_the_grant` |
| Temporary grant not refreshed past expiry; `grant.expired` logged | pass | `test_a_temporary_grant_is_not_refreshed_past_its_expiry` |
| RFC 7009 revocation | pass | `test_a_client_revoking_its_token_ends_the_grant` |
| RFC 8414 metadata | pass | `test_discovery_says_oauth2_and_where_everything_is` |
| Token introspection (RFC 7662) | n/a | not required by HCP |
| Device Authorization and Client Credentials grants (4.1.2) | not built | optional grant types |

## SHOULD-level items not met

- **4.4.2: no detection of rephrased-query sweeps.** The spec makes it a MAY.
  The per-client rate limit bounds the volume.
- **4.3.5: a controller-side log separate from the person's** isn't built. It isn't
  evaluated by conformance testing; canopy's `MCPAuditLog` and the `events` app
  cover operator questions today.
- **2.2.2: `capturedBy` naming the inferring model** is honoured for canopy's agents
  (`model=`), but OAuth clients have no field for it; their `capturedBy` is the
  client id.
