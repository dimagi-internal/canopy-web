# HCP v1 conformance self-audit

> **Owner:** Jonathan Jackson. **Start here before touching HCP** (the design is
> `hcp-service.md`; CLAUDE.md points here). `tests/test_hcp_docs_evidence.py` resolves
> every symbol, test, path, route, event and figure this page cites, so a change to the
> code this page depends on fails there until the page is true again.
> `.github/workflows/hcp-spec-probe.yml` watches for the one thing that closes the
> open MUST (below) to appear.

**What this audits:** canopy's HCP instance (`apps/contacts/hcp/`, `hcp_api.py`,
`hcp_oauth.py`, `hcp_webhooks.py`, `hcp_oauth_views.py`, `hcp_clients_api.py`, `hcp_collections.py`,
`contact_hcp_api.py`) against HCP v1, draft 4 (*"Human Context Protocol (HCP) v1 — Draft Reference Specification,
1.0-draft-4"*, Stanford HAI, Sept 2026).

**Method** (the service itself is `hcp-service.md`): each criterion of
5.2 (Core), plus what 5.2.1 adds for Interop, is marked **pass**, **gap** (with
the reason), **n/a**, or **SHOULD-gap**. Evidence is a code path, or a test in
`tests/test_hcp.py`, `test_hcp_personal.py`, `test_hcp_service.py`,
`test_hcp_oauth_security.py`, `test_hcp_agent_grants.py`, `test_hcp_trusted_contacts.py`
or `test_hcp_relevance.py`.

## Verdict

**Interop needs Core plus `authorization_profile: oauth2` (5.1, 5.2.1).**
- **The OAuth half is met.** The HCP service is an RFC 6749 authorization-code
  server with PKCE S256.
- **One Core MUST is open, so neither level is fully met:** Tier 1 JSON Schema
  validation (below). The first-party grants criterion is met (see "Closed").
- **What discovery declares:** `conformance_level: HCP-v1-Core` with the gaps
  listed in `canopy.conformance.known_gaps`, and does not claim Interop. Per 5.2.1
  the discovery document is authoritative, so it must not overclaim.
- **When to claim Interop:** once the schema is published and entries validate
  against it, change `hcp.CONFORMANCE_LEVEL` to `HCP-v1-Interop`. Until then
  discovery stays at Core, because 2.1.1 is a Core MUST.

| Open MUST | Why it is open | What closes it |
|---|---|---|
| **2.1.1 Tier 1 JSON Schema validation.** | Appendix A says the schema *will be* published at `https://hcp.me/schemas/v1`. That URL answers 200 `text/html` (the spec site's single-page-app shell), not a schema, so there is nothing authoritative to validate against. | Validate on write and emit once the schema is published. The scheduled probe (`scripts/hcp_spec_probe.py`, run by `.github/workflows/hcp-spec-probe.yml`) opens an issue when that URL starts returning JSON with a `$schema`; that issue is the signal to build the validation and change `hcp.CONFORMANCE_LEVEL`. |

### Closed

| MUST | Closed by | Evidence |
|---|---|---|
| **4.1.4 / 4.1.6 for canopy's own agents** (no grant presumed persistent by the control plane). | Grants are per agent and only the person's act. In a session's UI the person is shown the agent, categories, actions and expiry, then **allows it for this session** (temporary, the default outcome, ≤ 24 h and ending with the session). **Keeping it** is a second, separate act asked afterwards, with what persistent means stated first and nothing preselected; the server refuses `always` unless that session already allows it. Each act is a `grant.issued` recording type, modality (`canopy-chat` / `canopy-widget`) and what was shown. The person's settings are policy that bounds a grant, never a grant. No presumed grant is active: migration `contacts/0022` revokes each, with a `grant.revoked` on the person's log. | `hcp.issue_agent_grant`, `hcp._record_grant`; `tests/test_hcp_agent_grants.py`: `test_nothing_is_granted_without_the_persons_act`, `test_one_act_never_both_allows_and_keeps_it`, `test_a_session_grant_is_temporary_shown_logged_and_works`, `test_a_session_grant_lapses_with_the_session_and_after_the_cap`, `test_an_always_grant_is_reused_in_later_sessions_without_asking`, `test_granting_one_agent_grants_no_other`, `test_widening_is_its_own_act_and_replaces_the_grant`; frontend `SessionMemoryToggles.test.tsx` |

## HCP v2 (2.0-draft-3), tracked

The v1 audit above is what discovery claims. canopy also models HCP v2 internally and serves a
v2 HTTP binding at `/api/hcp/v2/` (`docs/architecture/hcp-service.md`, "HCP v2"). It does **not**
yet claim any v2 conformance identifier: this page names the draft it has read so that
`scripts/hcp_spec_probe.py` reports the next draft rather than this one. v2 is an
unstable draft in invited partner review; each v2 concept lands as its own piece and this section
grows a row per piece, with the same evidence rule as above.

| v2 concept | Status | Evidence |
|---|---|---|
| Purposes on every grant and every read (6.1.5.2), per-entry limits (4.2.6, 7.1) | built | `hcp.read_purpose`, `hcp.restrict_purposes`; `tests/test_hcp_purposes.py`: `test_a_search_naming_a_purpose_the_grant_does_not_carry_is_refused`, `test_an_entry_limited_to_other_purposes_is_not_served_for_this_one` |
| Relationships: `derivedFrom` (an entry, or a source by a loose `(kind, ref)` pair), `supersedes`, `contradicts` (symmetric), `duplicates`; the provider records who and when (4.2.3) | built (`derivedFrom` writable over v1 `hcp_addPreference`; the rest by the provider and, in the v2 context plane, by clients) | `EntryRelationship`; `tests/test_hcp_v2_conflicts.py`: `test_derived_from_names_an_entry_or_a_source_and_the_provider_records_who`, `test_a_client_may_not_name_an_entry_its_grant_cannot_read_and_is_not_told_whether_it_exists` |
| Confirmation records replace `userVerified` (3.8); standing (4.7.1); assurance (3.6, 3.7) | built; v1 `userVerified` is derived | `EntryConfirmation`, `hcp.confirmations.confirm`, `PersonFact.assurance`; `test_a_confirmation_belongs_to_one_version_and_a_revision_starts_with_none`, `test_an_agents_claim_that_the_person_said_it_has_no_standing_until_they_confirm_it` |
| Conflicts by relationship, consequences by standing (4.7, 4.7.2, 4.7.3) | built | `hcp.detect_conflicts`, `hcp.reevaluate`; `test_a_persons_own_declaration_has_standing_and_holds_only_the_inference`, `test_rejecting_withdraws_it_and_releases_what_it_held`, `test_accepting_a_held_entry_deprecates_the_authoritative_one_but_never_an_attestation`, `test_two_inferences_by_the_same_asserter_set_the_earlier_aside_and_different_asserters_are_both_held` |
| Memory Manager detection method declared (15.2) | **partial**: `hcp.CONFLICT_DETECTION` is `structural` (same category and dimension, different words, one side model-inferred). It is a hint-driven method, so it goes beyond "a dimension alone must not conflict" only by requiring different content; semantic judgment would need a model call, which canopy-web does not make. Re-evaluated on every revision (15.3) | `test_sharing_a_dimension_alone_is_not_a_conflict`, `test_a_revision_that_no_longer_conflicts_removes_the_relationship_and_releases_the_entry` |
| Confirm or reject is governance (1.6, 9.4.9) | built | `test_the_confirmation_route_is_the_persons_and_refuses_an_agent_login` |
| Standing rules: deny-only, every grant now and later, enforced at issue and on every request; revokes a grant it covers entirely; an entry is withheld by any denied category (6.3, 7.1, 4.5) | built | `hcp.set_standing_rule`, `hcp.enforce_rules`, `hcp.rules.denial`; `tests/test_hcp_v2_rules.py`: `test_a_rule_set_after_a_grant_fails_the_grants_next_read`, `test_a_rule_that_covers_everything_a_grant_reaches_revokes_it`, `test_a_grant_is_never_issued_reaching_a_category_a_rule_denies_its_operator`, `test_an_entry_with_a_denied_category_is_withheld_whichever_category_is_asked`, `test_requires_denies_only_clients_that_do_not_make_every_claim` |
| Directives delivered with every result and named in the audit (6.4, 8.2) | built | `hcp.rules.delivered`; `test_directives_ride_with_every_result_and_the_log_names_them` |
| Rules and directives are governance: the person, HTTP only (1.6, 9.4.14, 9.4.15) | built | `test_rules_and_directives_are_the_persons_and_an_agent_login_is_refused` |
| Restrict purposes is governance: the person, HTTP only (1.6, 9.4.10) | built | `test_the_governance_route_is_the_persons_and_refuses_an_agent_login` |

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
| Complete provenance as stored | pass | `provenance_source`, `captured_by` on every row; redaction is response-only. Extension: `record.provenance.site` (`provenance_site`) — the host site and page an agent was reached through when it recorded the entry; never affects scope, redacted with the 4.4.4 fields (`test_an_entry_recorded_through_a_site_carries_the_site`) |
| Version history; `version` / `previousVersion` | pass | `record_fact(supersedes=…)`; `test_an_update_is_a_new_version…` |
| Conflict detection both write orders, same category + dimension | pass | `hcp.detect_conflicts`; what is held is decided by standing, not declaration type alone (v2 section below) |
| Conflicted entries withheld from agents, visible to the person | pass | `hcp.servable`; `/people/me` and export include them |
| Conflict resolution transitions logged `conflict.resolved` | pass | `update_entry`, `delete_entry`, `hcp.confirmations.confirm` |

## Actions

| Criterion | Status | Evidence |
|---|---|---|
| All four CRUD operations | pass | search / add (+ `POST /v1/preferences`) / update / delete |
| At least one transport fully implemented | pass | REST and MCP. OAuth clients use REST; MCP is first-party (`hcp-service.md`) |
| No entry outside granted scopes (4.4.1) | pass | `hcp.search` refuses out-of-scope categories; `within_grant` per entry, including OAuth-grant sources |
| Quantity bound, not padded (4.4.2) | pass | `min(maxEntries, 20)`; nothing added |
| Relevance ordering, method declared (4.4.3) | pass | `hcp.rank`: lexical, plus project and (with `HCP_EMBEDDINGS_MODEL` set) embedding similarity; discovery and every response declare `lexical` or `hybrid` accordingly |
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
| Authorization distinct from authentication (4.1.6), no silent scope expansion | pass | Contacts: arriving through a trusted site authorizes nothing; opting in is their act in canopy's frame, after the disclosure (`test_the_site_holding_the_token_can_do_none_of_it`). OAuth: consent acts 1 + 2. canopy's agents: the session's allow act, after the disclosure; a feature the agent's grant lacks needs a new act (`test_widening_is_its_own_act…`) |
| Grants SHOULD use OAuth 2.0; else declared per 5.2.1 | pass | `authorization_profile: oauth2` |
| Grant objects; the person can enumerate and revoke | pass | `GET /v1/grants`, `DELETE /v1/grants/{grantId}`; `/people/me` |
| A grant never disclosed to another client | pass | `GET /v1/grants` with a client token returns its own; `test_a_token_opens_hcp_v1_only…` |
| `grantor` when the authorizer is not the subject | pass | always the subject at v1; `grantor` null |
| Narrowed grants: restriction carried, presented, enforced (4.1.5.1) | pass | Every grant carries its reach in `narrowedTo.sources`, and one `hcp.reach_of` answers it on every read path (search, get, the envelope). OAuth grants: personal plus the workspaces the person ticked. First-party agent grants: derived, never chosen in a prompt — `tree:<agent's root workspace>` (every division of the agent's own organisation, as it is when read; never another organisation's, never personal). A site is not a context: an agent reached through a trusted site's widget gets the same reach. To let an agent reach more, the person points it at one of their memory collections (`MemoryCollection`, `hcp_collections` — built from whole organisations, single workspaces and entries in no workspace; the **Reads** picker on each agent's card and the Collections panel on `/people/me`); the restriction then names `collection:<id>` plus the resolved sources. Any widening — pointing an agent at a wider collection, or adding a context to a collection in use — is refused until the person confirms, naming every affected grant, and each gets a `grant.issued`; a narrowing applies at once with `grant.narrowed`. The disclosure, `grant.issued` detail, `/people/me` and `GET /v1/grants` all name the reach. Agent grants that predate reach carry `workspace:<slug>` (migration 0023) — exactly the reach they were enforced to, so nothing is widened and no `grant.issued` is owed. `test_workspace_entries_are_reached_only_when_ticked`, `test_an_agent_in_another_division_reads_org_wide_but_not_personal`, `test_personal_is_reached_only_through_a_collection_with_confirmation`, `test_editing_a_collection_needs_confirmation_to_widen_and_narrows_at_once`, `test_a_collection_of_one_workspace_narrows_an_agent_to_it`, `test_another_organisations_agent_never_reaches_dimagis_memory` |
| REST: `GET /v1/grants` scoped to the client's own | pass | as above |
| Temporary vs persistent; temporary the default | pass | OAuth: `issue_grant`. canopy's agents: "allow for this session" is the only authorizing act, and it is temporary (`test_one_act_never_both_allows_and_keeps_it`) |
| Temporary grants carry `expiresAt`, shown at authorization time | pass | DB check constraint; consent shows the duration and act 2 the time |
| Persistence needs a distinct affirmative act | pass | OAuth: act 2, which validates the client and scopes again before writing (`hcp_oauth.recheck_pending`, `test_a_client_disabled_between_the_acts_gets_no_grant_and_no_code`); `test_the_keep_question_preselects_nothing`, `test_keeping_access_is_its_own_act…`. canopy's agents: the "Keep allowing" question after allowing; the server refuses `always` without the session's grant |
| Revocation available and propagates per 4.2 | pass | `hcp_oauth.revoke`: every token at once; audited in the same transaction |
| Revocation notifications signed and retried; revocation independent of delivery | pass | `HcpRevocationDelivery`, `hcp_webhooks.attempt`, off-request clock; `test_revoking_kills_every_token…`, `test_a_failing_webhook…` |
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
| Temporary-by-default authorization; separate persistence act | pass | OAuth: consent acts 1 + 2. canopy's agents: allow-for-this-session, then a separate keep |
| The same, for contacts on a trusted site (no canopy login while internal) | pass, except correct | `/api/contact/hcp/` in the widget: list + revoke grants, list + delete entries, export with audit, opt out; every call needs canopy's frame proof (`tests/test_hcp_trusted_contacts.py`). Correcting an entry's wording is not offered there (remove it instead) |
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
| Authorization code + PKCE S256 required; `plain` refused; a plain verifier sent as the challenge buys nothing | pass | `test_pkce_is_required`, `test_a_wrong_verifier_gets_no_token`, `test_a_plain_verifier_offered_as_a_challenge_buys_nothing` |
| Exact redirect URI; an unregistered one is shown, not redirected to | pass | `test_a_redirect_uri_not_registered_is_never_redirected_to`, `test_a_redirect_uri_must_match_exactly` |
| Scopes per 4.1.1, no wildcards, never beyond registration | pass | `test_a_client_cannot_ask_beyond_its_registration_or_with_a_wildcard` |
| Declining creates no grant | pass | `test_denying_creates_no_grant` |
| Codes single use; replay revokes; a code or refresh token belongs to its client | pass | `test_a_code_works_once`, `test_a_code_is_redeemable_only_by_the_client_it_was_issued_to`, `test_a_refresh_token_is_redeemable_only_by_its_client` |
| Refresh rotation; reuse revokes; refreshing never widens scope | pass | `test_a_rotated_refresh_token_presented_again_revokes_the_grant`, `test_refreshing_cannot_widen_the_scope` |
| Temporary grant not refreshed past expiry; `grant.expired` logged; its access token stops too | pass | `test_a_temporary_grant_is_not_refreshed_past_its_expiry`, `test_an_expired_temporary_grant_stops_its_access_token` |
| A revoked grant opens nothing; a token minted into a session cannot answer the consent screen | pass | `test_a_revoked_grant_cannot_be_redeemed_for_tokens`, `test_a_session_minted_from_a_token_cannot_answer_the_consent_screen` |
| One person's token reaches nothing of another's | pass | `test_one_persons_token_reaches_nothing_of_anothers`, `test_two_people_authorizing_one_client_get_separate_grants` |
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
