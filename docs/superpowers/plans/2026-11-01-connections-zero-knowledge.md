# Connections Hold Nothing canopy-web Can Use — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move every OAuth connection's refresh token, access token and app client secret out of canopy-web's reach: minted, sealed, refreshed and revoked on runners, opened only by the machines and people the owner's passkey-signed grant allows.

**Architecture:** canopy-web keeps the browser legs, the provider authorize URL, policy on what runners report, recipient bookkeeping, a refresh lease and compare-and-swap storage of `zks1` envelopes it cannot open. The canopy CLI (separate repo) gains the secret half of each provider, the mint, the local refresh with write-back, and the keeper's connection work, and refuses any recipient or envelope the owner's signed grant statement does not allow. The cloud runner claims connection mints and stages tokens through the CLI as today.

**Tech Stack:** Django 6 + Django Ninja + Pydantic v2 (canopy-web), React 19 + WebCrypto (frontend), Python 3 + click + `cryptography` (canopy CLI), pytest / vitest.

**Spec:** `docs/superpowers/specs/2026-10-31-connections-zero-knowledge-design.md` (binding). Executors read both. Approved decisions: owner signs grants with a passkey touch (§4.3); `google_mint` / `gog-token` / `google-delegated-*` are OUT of scope.

**Repos:**
- canopy-web: `/Users/jjackson/emdash/worktrees/canopy-web-salesforce`, branch `feat/connections-zero-knowledge` (checked out). Parts A, C, D, E.
- canopy CLI: do NOT touch `~/emdash/worktrees/canopy-cred-salesforce`'s branch. Create a new worktree: `git -C ~/emdash/worktrees/canopy-cred-salesforce fetch -q origin && git -C ~/emdash/worktrees/canopy-cred-salesforce worktree add ~/emdash/worktrees/canopy-connections-zk -b feat/connections-zero-knowledge origin/main`. Part B runs there (`$CLI` below = that path).

## Global Constraints

- canopy-web never holds a usable refresh token, access token or client secret, at rest or in transit (spec Goal 1). Anti-goal: no server fallback that refreshes, exchanges or revokes.
- A runner never seals to a recipient only because canopy-web listed it; canopy-web's list can narrow, never widen (§4.3). Refusal code `zk_unendorsed_recipient`.
- Staged token file path and format unchanged: `~/.canopy/connections/<slug>/<provider>.json` = `{access_token, instance_url, expires_at, account_label}`, mode 0600, dir 0700; person cache `~/.canopy/connections/<provider>.json` (`<provider>@<workspace>.json` with `--workspace`). Turn usage `canopy cred connection-token <provider> --agent <slug>` and `$CANOPY_CONNECTIONS` unchanged.
- Lease: one holder, 60 s, renewable; a waiter polls for the new `access_sealed` up to 30 s (§4.2). Keeper pass every 120 s (`KEEP_INTERVAL_SECONDS`, unchanged).
- Revoking: the first holder revokes and confirms; after 7 days with no holder the ciphertext is deleted as `revoke_unconfirmed` (§4.5).
- Google: a refusal at mint discards WITHOUT revoking (revoking ends every token of that account on the client); a disconnect revokes only when no other connection of that account through that app remains (§4.5).
- Salesforce hosts: `login_host` ∈ {`login.salesforce.com`, `test.salesforce.com`} or `*.my.salesforce.com` (non-empty label before the suffix); `instance_url` host must end in `.my.salesforce.com`; scheme `https` only. Every endpoint is derived from these, never taken as given (§1).
- Only cloud runners mint, keep or revoke; a person's laptop only reads and refreshes its own use (§2, §3; Ruling R3). Minting runner declares `capabilities.connection_mint`.
- No server path decrypts: no `decrypt_secret`/`encrypt_secret`/`apps.common.encryption` anywhere under `apps/connections` (§Order of work 1).
- Audit vocabulary `connections.*` keeps its plumbing (`services.record`); new verbs `minted`, `refreshed` (`rotated`), `resealed`, `revoked`, `revoke_unconfirmed`, `lease_expired_unwritten`; `token_issued` names the runner/machine. No plaintext token in any event, log or response (§9).
- No migration of existing rows: delete `Connection` rows, empty `client_secret_enc`, drop the Fernet columns (§10).
- canopy-web rules: every route `@gate(...)` directly above its `def`; route docstrings are published OpenAPI (rationale in `#` comments); regenerate and commit `frontend/src/api/generated.ts` after any schema/route change; framework layering — `harness` and `agents` never import `connections` (L2); a `RunPython` that deletes referenced rows goes in its own migration before schema changes; CLAUDE.md holds no dates or PR refs; design tokens only in frontend.
- canopy CLI rules: bump with `canopy version bump` (required `check-version`); every new command prints JSON on stdout and never a token.

## Review Focus

1. **Clock skew between a box and canopy-web** — lease and expiry decisions must not depend on the box's wall clock agreeing with the server's. Expect: the lease response carries `ttl_seconds` and the CLI renews on a monotonic clock. Test added to Task A6 (`test_lease_answers_ttl_seconds_not_only_a_timestamp`) and B5 (`test_lease_renewal_uses_monotonic_ttl`).
2. **An app removed or a connection disconnected while a refresh is mid-flight** — the holder's write-back lands on a `revoking` row. Expect: 409 `revoking`, nothing stored, the box stages nothing. Test in A6 (`test_write_back_to_a_revoking_connection_is_refused`) and B5 (`test_revoking_answer_stages_nothing`).
3. **A lender stops administering an agent after lending** — the statement still lists the agent. Expect: the server stops listing that agent's machines and the material route refuses that agent's runner (`not_connected`), while other grants keep working. Test in A4 (`test_grant_out_of_force_narrows_recipients`) and A6 (`test_material_refused_for_an_agent_whose_lender_left`).
4. **Two turns of the same agent on one box stage at once** — both CLI processes want a token. Expect: one takes the lease and refreshes; the other polls for the new `access_sealed` and uses it, never refreshing in parallel (a parallel Salesforce refresh would kill the rotated token). Test in B5 (`test_second_process_waits_for_new_access_instead_of_refreshing`).
5. **The browser tab closes at the confirmation step** — the mint has stored an unconfirmed connection. Expect: reopening the Connecting page resumes it; with no statement by `CONFIRM_SECONDS` it is discarded through the revoking path (no orphaned grant). Tests in A7 (`test_unconfirmed_connection_is_discarded_after_confirm_window`) and D3 (`resumes a confirming mint after reload`).

## Rulings (where the spec left a choice or contradicted itself)

- **R1 Envelope "signature" is an ECDH-keyed MAC per recipient (Part 0.4), not a signing-key signature.** Cloud boxes enrolled as `aws-iam` have no signing key (`device_key._aws_key` raises on sign), and a signing key is tied to its ECDH key only by canopy-web's word; the ECDH key is what the statement and endorsements name.
- **R2 Mint polling and keeper work are their own routes in `apps/connections`** (routes 15, 22), not fields on `mint/claim` / `VaultKeeperOut`: `harness` and `agents` are below `connections` and never import it.
- **R3 A laptop may refresh and write back its own use** (lease + `PUT sealed`), re-sealing to exactly the current allowed set; it never mints, keeps (adds recipients) or revokes. Otherwise a rotating Salesforce token refreshed on a laptop would die.
- **R4 Refusal at mint: Google never revokes, Salesforce revokes** (§2 step 5 vs §4.5; the user's instruction pins Google).
- **R5 Bindings and ids:** `connection:<uuid>`, names `refresh`/`access`, `kv = 0`; `Connection.id` becomes a UUID and the (emptied) tables are recreated.
- **R6 `canopy connection mint` posts its own result**, so it can revoke while it still holds the grant in memory (unlike `google-mint`, where the runner posts).
- **R7 Lending is only the signed statement** (route 6); the old grant routes go.
- **R8 Only the `owner_key` holder re-signs.** For an agent-owned connection, if that admin leaves, another admin reconnects (runners check the statement against the `owner_key` sealed inside the envelope).
- **R9 "The identity in the agent's vault"** = the email in the agent's `gog-token`; if the agent has none, the owner's signed confirmation closes it.
- **R10 Salesforce identity** comes from `https://<checked instance host>/services/oauth2/userinfo`, never the `id` URL the token response names.
- **R11 Confirmation window** `CONFIRM_SECONDS = 1800`; an unconfirmed connection goes down the revoking path.
- **R12 Statement agents are agent primary keys** (what endorsements sign); boxes pin `(owner_jkt, version)` per connection and refuse rollback.
- **R13 Connections vault** is a `Vault` kind whose machine rule adds "cloud runner"; `layers_for` already reads `WORKSPACE` only, which keeps it out of turns.
- **R14 Removing an app** sets `removed_at`; the app row is deleted by the sweep once its connections are revoked (a cascade would delete revoking rows before any holder revokes them).
- **R15 A write-back on an expired lease is accepted if nobody took the lease since.**
- **R16 A new connect of an account the owner already holds through that app is refused** (`duplicate_account`), not merged; Reconnect is the way to replace it.
- **R17 Release safety:** a box declares `connection_mint` only with CLI ≥ `CONNECTIONS_ZK_MIN_CLI`, and a new CLI treats a missing material route (404) as "not connected".

---

# Part 0 — Contract

Both repos and the browser implement these exactly. The normative reference is a new file `docs/vault/zk-v1/connections_v1_reference.py` with vectors `docs/vault/zk-v1/connections_v1_vectors.json` (Task A1); the CLI copies both into `$CLI/tests/fixtures/vault_zk/` (Task B1); the frontend tests read them by `?raw` import like `zk.test.ts`.

## 0.1 Names and bindings

- Binding id: `connection:<uuid>` (lower-case hyphenated UUID of `Connection.id`), used in the agent-id slot of zk-v1 AAD and HKDF, exactly as workspace vaults use `vault:<uuid>`.
- Envelope names (the zk-v1 `name`): `refresh` for `sealed`, `access` for `access_sealed`. Both `zks1`, `kv = 0`.
- Recipient kinds: `device` (target = ECDH key thumbprint) and `member` (target = user id as string). Member recipient is only ever the owner key's person.
- App secret vault value name: `connection-app:<provider>` (`zk1`, in the workspace's Connections vault, binding `vault:<uuid>`).

## 0.2 Sealed plaintexts (UTF-8 JSON, compact)

`refresh` (the `sealed` envelope):
```json
{"v":1,"provider":"salesforce","refresh_token":"…","client_id":"…","client_secret":"…",
 "login_host":"login.salesforce.com","instance_url":"https://acme.my.salesforce.com",
 "account_id":"00D…/005…","owner_key":"<RFC7638 jkt of the owner's member public key>",
 "issued_at":1793500000,"scopes":"api refresh_token id"}
```
Google: `login_host` and `instance_url` are `""`. `issued_at` is integer epoch seconds.

`access` (the `access_sealed` envelope):
```json
{"v":1,"access_token":"…","expires_at":1793501800,"instance_url":"https://acme.my.salesforce.com",
 "account_id":"00D…/005…","scope":"api refresh_token id"}
```
A reader refuses an `access` whose `account_id` differs from the `refresh` plaintext's, or whose `instance_url` fails the host rule.

App secret (`connection-app:<provider>`): `{"client_id":"…","client_secret":"…","options":{"login_url":"https://login.salesforce.com","access_token_seconds":1800}}` (Salesforce) or `{"…","options":{"allowed_domains":"dimagi.com, dimagi-ai.com"}}` (Google; key absent = any).

## 0.3 Grant statement (signed by the owner's vault key, one passkey touch)

Object (all values ASCII; `account_id` matches `^[\x21-\x7e]{1,255}$` with no `"` or `\`):
```json
{"account_id":"00D…/005…","agents":["12","40"],"connection":"<uuid>","my_computers":["<ecdh jkt>"],"v":1,"version":3}
```
- `agents`: agent primary keys as decimal strings, sorted ascending as strings, unique. For an agent-owned connection it MUST contain the owning agent.
- `my_computers`: ECDH thumbprints, sorted, unique (empty for agent-owned).
- `version`: integer ≥ 1, strictly greater than the stored one.
- Message bytes: `"canopy-connections/v1/grant|" + json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)` encoded UTF-8. The browser builds the same string with keys written in that sorted order.
- Signature: ECDSA P-256 / SHA-256 by the owner's person scalar (the same key and import as `endorse` in `frontend/src/vault/zk.ts`), IEEE P1363 `r‖s` 64 bytes, base64url unpadded. Stored as `{statement, sig, by_jkt}`; `by_jkt` = thumbprint of the signer's public key and must equal `owner_key`.

## 0.4 Envelope signature (writer authentication)

Ruling R1: authenticated per recipient with the writer's ECDH key, not its signing key (an aws-iam cloud box has no signing key: `device_key._aws_key` → `no_sign`). Stored beside each envelope as JSON (`sealed_sig`, `access_sig`):
```json
{"by":{"kind":"device","target":"<writer ecdh jkt>","public_jwk":{"kty":"EC","crv":"P-256","x":"…","y":"…"}},
 "expect":"<sealed_digest this write replaces, or \"\">",
 "macs":[{"kind":"device","target":"<jkt>","mac":"<b64u>"},{"kind":"member","target":"7","mac":"<b64u>"}]}
```
For each recipient entry `e` of the `zks1` body (`{kind,target,epk,ct}`):
- `entry_digest = sha256_hex(json.dumps(e, sort_keys=True, separators=(",", ":")))` (epk keys `crv,kty,x,y`).
- `msg = f"canopy-connections/v1/envelope|{binding}|{name}|{expect}|{entry_digest}"`.
- `key = HKDF-SHA256(ikm=Z, salt=b"", info=f"canopy-connections/v1/envelope-mac|{binding}|{name}|{kind}:{target}", length=32)`, `Z` = x-coordinate of ECDH(writer static ECDH key, recipient public key) — the recipient computes the same `Z` with its own key against `by.public_jwk`.
- `mac = b64u(HMAC-SHA256(key, utf8(msg)))`.
- `macs` covers exactly the envelope's recipients; `by.target` equals the thumbprint of `by.public_jwk` (device) or the owner's user id (member).

A reader: opens its entry, recomputes its own `mac` (constant-time compare), and accepts only if `by` is a device the statement allows (0.5) or the owner's member key. Pruning drops entries and their `macs` together without invalidating the rest.

## 0.5 Who a runner seals to (CLI rule; canopy-web only narrows)

Allowed set for connection `C` with verified statement `S` and owner key `O` (thumbprint = `owner_key` from the `refresh` plaintext, or from the mint work at first mint):
- `("member", owner_user_id)` whose key thumbprint is `O`;
- a device whose ECDH thumbprint is in `S.my_computers`;
- a device with a valid zk-v1 §7 endorsement of its exact ECDH key (`kind=device, target=key_jkt=jkt`) signed for agent id `a ∈ S.agents` by a person this box trusts (pins, first sight; `vault_keeper._trust_person`).

Before a statement exists (state `confirming`) a box seals only to itself and the owner.

## 0.6 Routes

All JSON. Errors are RFC 7807 with `error` member (existing `problems.problem_for` / `zk_problem` style). "device proof" = DPoP header verified by `apps.harness.device_keys.verify_proof` + `known_key`, the key ACTIVE and ECDH-approved; missing/invalid → 401 `device_proof_required`.

**Browser / person (session or PAT where stated)**

| # | Route | Gate | Request → Response | Errors |
|---|---|---|---|---|
| 1 | `GET /api/workspaces/{ws}/connection-apps` | `integrations` | → `ConnectionAppsOut`; each app adds `has_secret: bool` (vault holds `connection-app:<p>`), `secret_name`, `vault: {id, kv} \| null` | — |
| 2 | `PUT /api/workspaces/{ws}/connection-apps/{provider}` | `integrations`, setting, step_up | `ConnectionAppIn {client_id, options, scopes}` (no `client_secret`; an extra field is 422) → `ConnectionAppOut`; makes `<ws> connections` vault if missing | 409 `app_locked` |
| 3 | `DELETE /api/workspaces/{ws}/connection-apps/{provider}` | `integrations`, setting, step_up | → 204; every connection → `revoking`; app hidden (`removed_at`) until its connections are gone | — |
| 4 | `POST /api/connections/mints` | `self`, kind=`action` | `{provider, workspace, agent?: slug, reconnect?: uuid}` → 201 `ConnectionMintOut` | 404 unknown; 403 `not_an_admin`; 409 `not_configured`; 409 `no_mint_runner` (detail in words); 409 `not_reconnectable` |
| 5 | `GET /api/connections/mints/{id}` | `self` | → `ConnectionMintOut {id, status, outcome, provider, workspace, owner{kind, slug?}, runner, authorize_url, account_label, connection, confirm_by, detail, created_at, updated_at, envelope?}`; `envelope = {binding, sealed, sealed_sig}` only for `requested_by` while `confirming` | 404 |
| 6 | `PUT /api/connections/{id}/statement` | `self`, setting, step_up | `{statement, sig, by_jkt}` → `ConnectionOut` | 422 `bad_statement`, 422 `bad_signature`, 403 `not_the_owner_key`, 409 `statement_stale`, 403 `not_an_admin`, 422 `agent_outside_workspace`, 422 `not_your_machine`, 422 `statement_missing_owner_agent`, 409 `revoking` |
| 7 | `GET /api/connections/` | `self` | → `ConnectionsOut`; `ConnectionOut` gains `id: uuid`, `state`, `statement {version, agents:[slug], my_computers:[jkt]}`, `holders:[runner name]`, `waiting:[{agent, reason}]`, `my_machines:[{jkt, name}]` | — |
| 8 | `DELETE /api/connections/{id}` · `DELETE /api/agents/{slug}/connections/{id}` | `self` / `agent-admin` (+step_up), setting | → 204; row → `revoking` | — |
| 9 | `GET /api/agents/{slug}/connections` | `agent-admin` | → `ConnectionsOut` (owned + lent to it) | — |
| — | `PUT/DELETE /api/agents/{slug}/connections/{id}/grants/{other}` | removed | lending is route 6 only | — |
| — | `POST /api/me/connections/{provider}/token` | removed | — | — |

**Person CLI (plain PAT over REST, as `_plain_pat`, plus device proof of a machine the person owns)**

| # | Route | Request → Response | Errors |
|---|---|---|---|
| 10 | `GET /api/me/connections/{provider}/material?workspace=` | → `ConnectionMaterialOut` | 409 `not_connected`; 409 `ambiguous {workspaces}`; 403 `machine_not_enrolled` ("Enroll this computer: canopy vault device --enroll"); 403 `not_on_my_computers` ("Turn on Use on my computers for <label> in Settings → Connections"); 409 `waiting` (not yet sealed to it) |
| 11 | `POST /api/me/connections/{id}/refresh-lease` | `{lease_id?}` → `LeaseOut {lease_id, ttl_seconds: 60, expires_at}` | 409 `lease_held {retry_after}` |
| 12 | `DELETE /api/me/connections/{id}/refresh-lease/{lease_id}` | → 204 | — |
| 13 | `PUT /api/me/connections/{id}/sealed` | `WriteBackIn` → `{sealed_digest}` | as 19 |
| 14 | `POST /api/me/connections/{id}/reconnect-required` | `{lease_id, error}` → 204 | 409 `lease_lost` |

**Runner (`@gate("runner", …)`, the box's runner token; device proof where marked ●)**

| # | Route | Request → Response | Errors |
|---|---|---|---|
| 15 | `GET /api/harness/runners/{rid}/connection-mints/claim` | → `{mint: ConnectionMintWorkOut \| null}`; reading a code consumes it | — |
| 16 | `POST …/connection-mints/{mid}/challenge` | `{code_challenge}` (`^[A-Za-z0-9_-]{43,128}$`) → 204 | 409 `not_waiting`, 422 `bad_challenge` |
| 17 | ● `POST …/connection-mints/{mid}/result` | `MintResultIn {sealed, sealed_sig, access_sealed, access_sig, access_expires_at, identity{account_id, label, email, hosted_domain}, instance_url, scopes, error?, detail?}` → 200 `{connection, status:"confirming"}` | 409 `{error, revoke: bool}` for `wrong_account`, `duplicate_account`, `insufficient_scope`, `not_waiting`; 422 `not_ciphertext`, `foreign_reader`, `bad_writer_sig` |
| 18 | ● `GET …/turns/{tid}/connections/{provider}` | → `ConnectionMaterialOut`; records `token_issued` (`to=turn`, runner, turn; coalesced per turn) | 409 `not_connected`, 409 `needs_reconnect`, 409 `waiting` |
| — | `GET …/turns/{tid}/connections` | unchanged `TurnConnectionsOut {providers}` | — |
| — | `POST …/turns/{tid}/connection-token` | removed | — |
| 19 | ● `PUT …/connections/{id}/sealed` | `WriteBackIn {lease_id, sealed?, sealed_sig?, access_sealed, access_sig, access_expires_at, expect, rotated}` → `{sealed_digest}` | 409 `lease_lost`, 409 `zk_value_changed`, 409 `revoking`, 422 `foreign_reader`, 422 `zk_rewrap_required`, 422 `bad_writer_sig` |
| 20 | ● `POST …/connections/{id}/refresh-lease` / `DELETE …/refresh-lease/{lease_id}` | as 11/12 | as 11 |
| 21 | ● `POST …/connections/{id}/reconnect-required` | `{lease_id, error}` → 204 | 409 `lease_lost` |
| 22 | `GET …/vault-keeper/connections` | → `{connections: [ConnectionKeeperWorkOut]}` | — |
| 23 | ● `PUT …/vault-keeper/connections/{id}` | `ResealIn {sealed, sealed_sig, access_sealed, access_sig, expect}` → `{machines}` | 403 `zk_not_a_holder`, 409 `zk_value_changed`, 422 `foreign_reader`, 422 `zk_rewrap_required`, 409 `no_statement` |
| 24 | ● `POST …/connections/{id}/revoked` | `{revoked: bool, error: ""}` → 204; deletes the row | 403 `zk_not_a_holder`, 409 `not_revoking` |

**Browser legs (bare views, unchanged paths):** `auth/connections/<provider>/<workspace>/start/` (POST: creates a mint as route 4, redirects to `/w/<ws>/connections/connecting/<mint>`), `…/callback/` (signed state names the mint and user; stores the code; redirects to the Connecting page).

**Shared shapes**
- `AppLayerOut {vault_id, binding, kv, wrap: object|null, value: str}` — this device's wrap of the Connections vault key and the `zk1` app value; `null` when the device may not hold it.
- `ConnectionMaterialOut {id, provider, workspace, binding, account_id, account_label, sealed, sealed_sig, sealed_digest, access_sealed, access_sig, access_expires_at, statement, statement_sig, owner{user_id, public_jwk}, endorsements:[ZkEndorsementOut + agent_id], recipients:[{kind, target, public_jwk, runner_name}], app: AppLayerOut|null, lease: {mine: bool, ttl_seconds}|null, rotates_refresh_token}`.
- `ConnectionMintWorkOut {id, provider, workspace, status: "requested"|"completing", code, redirect_uri, client_id, connection_id, binding, owner_kind, agent: slug|"", agent_id: str|"", owner{user_id, public_jwk}, app: AppLayerOut, reconnect: {sealed, sealed_sig, statement, statement_sig}|null, scopes}`.
- `ConnectionKeeperWorkOut` = `ConnectionMaterialOut` fields + `waiting:[target]`, `revoking: bool`, `revoke_at_provider: bool`, `reseal_path`, `revoked_path`.

## 0.7 CLI commands

| Command | stdin | stdout (one JSON line, never a token) | Exit |
|---|---|---|---|
| `canopy connection mint --runner R` (runner token: `$CANOPY_TOKEN` or device-key session) | `ConnectionMintWorkOut` + `"code_verifier"` | `{"outcome":"confirming","connection":"<uuid>","account_label":"…"}` or `{"error":"<code>","detail":"…","revoked":bool}` | 0 / 1 |
| `canopy cred connection-token <p> --runner --agent X --runner-id R --turn T [--min-life S]` | — | the staged path (as today) | 0; 3 = not connected/waiting (silent); 1 other |
| `canopy cred connection-token <p> --agent X` | — | staged path (no network, unchanged) | 0 / 2 |
| `canopy cred connection-token <p> [--workspace W] [--refresh]` (person) | — | cache path | 0; 2 refused with the sentence + command |
| `canopy vault zk keep` | — | text (unchanged), now including connection lines | 0 |

Mint error codes (CLI, stdout `error`): `exchange-failed`, `no-refresh-token`, `insufficient-scope`, `wrong-account`, `no-client`, `client-mismatch`, `bad-host`, `zk-no-recipients`, `bad-request`, plus any server refusal code from route 17 passed through.

---

# Part A — canopy-web backend

### Task A1: Wire format reference, vectors and server-side checks

**Files:**
- Create: `docs/vault/zk-v1/connections_v1_reference.py`, `docs/vault/zk-v1/connections_v1_vectors.json`
- Create: `apps/connections/wire.py`
- Test: `tests/test_connections_wire.py`

**Interfaces:**
- Produces (`apps/connections/wire.py`): `REFRESH_NAME = "refresh"`, `ACCESS_NAME = "access"`, `STATEMENT_PREFIX = "canopy-connections/v1/grant|"`; `class WireError(Exception)(status: int, code: str, detail: str)`; `binding_id(connection_id: uuid.UUID) -> str`; `statement_msg(st: dict) -> bytes`; `check_statement(st: dict, *, connection_id, account_id: str) -> dict` (shape per 0.3, raises 422 `bad_statement`); `verify_statement(st: dict, sig: str, signer_jwk: dict) -> bool`; `check_writer_sig(value: str, sig: dict) -> dict` (structure only: `macs` targets == `zk_seal.sealed_targets(value)`, `by.kind` ∈ device|member, device `by.target == thumbprint(by.public_jwk)`; raises 422 `bad_writer_sig`); `keep_only_sig(sig: dict, allowed: set[tuple[str, str]]) -> dict`.
- Reference module functions (also used by tests and copied to the CLI): `statement_msg`, `sign_statement(st, priv)`, `verify_statement`, `entry_digest(entry)`, `envelope_mac(binding, name, expect, entry, writer_priv, recipient_pub)`, `verify_envelope_mac(...)`, `vectors()`.

- [ ] **Step 1: Write the failing tests**

```python
def test_vectors_file_is_what_the_reference_generates():
    assert json.loads(VECTORS.read_text()) == ref.vectors()

def test_statement_msg_matches_vector():
    v = vectors()["statement"]
    assert wire.statement_msg(v["statement"]) == v["msg"].encode()
    assert v["msg"].startswith("canopy-connections/v1/grant|{\"account_id\":")

def test_vector_signature_verifies_and_a_changed_agent_list_does_not():
    v = vectors()["statement"]
    assert wire.verify_statement(v["statement"], v["sig"], v["signer_jwk"])
    assert not wire.verify_statement({**v["statement"], "agents": ["12"]}, v["sig"], v["signer_jwk"])

@pytest.mark.parametrize("bad", [{"agents": ["40", "12"]}, {"agents": ["12", "12"]}, {"version": 0},
                                 {"account_id": "a\"b"}, {"v": 2}, {"extra": 1}])
def test_malformed_statement_is_refused(bad):
    with pytest.raises(wire.WireError) as e:
        wire.check_statement({**GOOD, **bad}, connection_id=CID, account_id=GOOD["account_id"])
    assert (e.value.status, e.value.code) == (422, "bad_statement")

def test_statement_for_another_account_or_connection_is_refused(): ...  # 422 bad_statement

def test_envelope_mac_vector_recomputes_for_each_recipient():
    v = vectors()["envelope"]
    for r in v["recipients"]:
        assert ref.verify_envelope_mac(v["binding"], "refresh", v["expect"], r["entry"], r["priv"], v["by"]["public_jwk"], r["mac"])

def test_writer_sig_must_cover_exactly_the_sealed_recipients():
    with pytest.raises(wire.WireError) as e:
        wire.check_writer_sig(SEALED_TO_TWO, {**SIG, "macs": SIG["macs"][:1]})
    assert e.value.code == "bad_writer_sig"

def test_keep_only_sig_drops_pruned_macs():
    assert {(m["kind"], m["target"]) for m in wire.keep_only_sig(SIG, {("device", JKT_A)})["macs"]} == {("device", JKT_A)}
```

- [ ] **Step 2:** Run `uv run pytest tests/test_connections_wire.py -v` — FAIL (module missing).
- [ ] **Step 3:** Write the reference (deterministic ECDSA not required: vectors pin one valid signature; ephemerals and nonces fixed like `zk_v1_reference.vectors`) and generate the JSON with `uv run python docs/vault/zk-v1/connections_v1_reference.py > docs/vault/zk-v1/connections_v1_vectors.json`. Implement `wire.py` reusing `apps.agents.credentials.zk_seal.sealed_targets`, `zk.public_key`, `zk.b64u_bytes`, `device_keys.thumbprint`, and the P1363→DER conversion from `zk_endorse.verify`.
- [ ] **Step 4:** Run `uv run pytest tests/test_connections_wire.py -v` — PASS.
- [ ] **Step 5:** `git add docs/vault/zk-v1/connections_v1_* apps/connections/wire.py tests/test_connections_wire.py && git commit -m "connections: wire format for grant statements and writer-authenticated envelopes"`

### Task A2: The Connections vault

**Files:**
- Modify: `apps/agents/models.py` (`Vault`: `CONNECTIONS = "connections"`, `KIND_CHOICES` += `(CONNECTIONS, "Connections vault")`, `kind` `max_length=12`, `SHARED_KINDS = (WORKSPACE, CONNECTIONS)`)
- Create: `apps/agents/migrations/00NN_vault_connections_kind.py`
- Modify: `apps/agents/credentials/workspace_vaults.py` (`create_connections`, eligibility branch), `apps/agents/credentials/vault_members.py` (`attached_agents` branch), `apps/agents/credentials/vault_keeper.py:112`, `apps/agents/api_vaults.py:63,146`, `apps/agents/api_vault_keeper.py:140`, `apps/agents/credentials/zk_notify.py:91`
- Create: `apps/connections/vault.py`
- Test: `tests/test_connections_vault.py`

**Interfaces:**
- Produces (agents): `workspace_vaults.create_connections(workspace, *, by) -> Vault` (name `f"{workspace.slug} connections"`, one `VaultLink(kind=WORKSPACE_ADMINS, workspace=ws, role=ADMIN)`, no attachments). For `kind == CONNECTIONS`: `vault_members.attached_agents(v)` = agents whose workspace is `v.workspace`; `workspace_vaults.eligible_device_keys(v)` and `device_may_hold(v, key)` additionally require `key.runner.kind == Runner.CLOUD`.
- Produces (`apps/connections/vault.py`): `vault_for(workspace, *, create: bool = False, by=None) -> Vault | None`; `secret_name(provider: str) -> str` (`f"connection-app:{provider}"`); `has_secret(app) -> bool`; `app_layer(app, device_key) -> dict | None` (`AppLayerOut` shape, built with `workspace_vaults._layers([vault], device_key, {secret_name})`).

- [ ] **Step 1: Write the failing tests**

```python
# fixture ws_world: workspace with an owner, an admin, an agent, a cloud box serving it (key ws_world.cloud_key)
# and the admin's laptop runner (ws_world.laptop_key), both ECDH-approved (tests/fixtures/machines.a_machine);
# v = vault_for(ws_world.ws, create=True, by=ws_world.owner); app = the workspace's salesforce ConnectionApp
def test_members_are_the_workspace_owners_and_admins(ws_world):
    assert set(vault_members.member_ids(v)) == {ws_world.owner.pk, ws_world.admin.pk}

def test_a_cloud_box_serving_a_workspace_agent_may_hold_its_key(ws_world):
    assert ws_world.cloud_key in workspace_vaults.eligible_device_keys(v)

def test_a_laptop_never_holds_it_even_its_admins(ws_world):
    assert ws_world.laptop_key not in workspace_vaults.eligible_device_keys(v)
    assert not workspace_vaults.device_may_hold(v, ws_world.laptop_key)

def test_it_is_never_layered_into_a_turn(ws_world):
    assert v not in workspace_vaults.layers_for(ws_world.agent)
    assert "connection-app:salesforce" not in workspace_vaults.layered_names(ws_world.agent)

def test_values_route_accepts_the_connections_vault(ws_world): ...  # PUT /api/vaults/{id}/zk/values 200

def test_keeper_grants_its_key_to_a_waiting_cloud_box(ws_world): ...  # grant_workspace accepts kind connections

def test_has_secret_is_whether_the_vault_holds_the_value(ws_world):
    assert not has_secret(app); VaultSecret.objects.create(vault=v, name="connection-app:salesforce", value_zk="zk1:x", kv=1)
    assert has_secret(app)

def test_app_layer_is_null_for_a_device_that_may_not_hold_it(ws_world):
    assert app_layer(app, ws_world.laptop_key) is None
```

- [ ] **Step 2:** Run `uv run pytest tests/test_connections_vault.py -v` — FAIL.
- [ ] **Step 3:** Implement as listed; every `kind=Vault.WORKSPACE` filter in the files above that serves the vault page, values/wraps/pins routes, keeper grant and `zk_notify` becomes `kind__in=Vault.SHARED_KINDS`; `layers_for`, `project_vaults`, `runner_vaults` and `credential_sets` stay `WORKSPACE` only. `uv run python manage.py makemigrations agents`.
- [ ] **Step 4:** Run `uv run pytest tests/test_connections_vault.py tests/test_canopy_vault_zk.py tests/test_project_vaults.py -q` — PASS.
- [ ] **Step 5:** Commit `"vaults: a per-workspace Connections vault, cloud boxes only, never layered into turns"`.

### Task A3: Remove every server-held token path; new models

**Files:**
- Delete: server-held paths in `apps/connections/services.py` (`complete`, `_upsert_connection`, `_discard_grant`, `_cached_token`, `_handout`, `access_token_for`, `issue_token`, `_decrypted_refresh`, `_refresh_token_of`, `_revoke`, `begin`, `pending_for`, `spend`, `authorize_refused`, `_pkce_pair`, `AccessToken`), `apps/connections/providers/base.py` (`client_secret`, `exchange_code`, `refresh`, `revoke`, `_token*`, `_authenticated`, `identity` HTTP calls), `providers/salesforce.py`/`google.py` `identity` HTTP bodies; routes `my_connection_token`, `turn_connection_token`, `grant_connection`, `revoke_connection_grant`
- Create: `apps/connections/migrations/0006_delete_server_held.py` (RunPython only: delete `ConnectionGrant`, `Connection`; set `client_secret_enc=""`), `0007_sealed_connections.py`
- Modify: `apps/connections/models.py`, `apps/connections/schemas.py` (`ConnectionAppIn` drops `client_secret`), `apps/connections/api.py` (`has_secret` via `vault.has_secret`; PUT app calls `vault.vault_for(create=True)`)
- Delete tests that cover removed paths: `tests/test_connections_turn_token.py`, the token/exchange/refresh cases in `tests/test_connections_services.py`, `tests/test_connections_google.py`, `tests/test_connections_views.py` (A5 writes it anew), `tests/test_connections_second_provider.py`; adapt `tests/test_connections_api.py`, `tests/test_connections_app_routes.py`, `tests/test_connections_apps.py` and `tests/fixtures/connections.py` (drop `FakeProviderHTTP` token legs; rows built with `sealed="zks1:…"`) to the new models
- Test: `tests/test_connections_hold_nothing.py`, `tests/test_connections_model.py` (rewrite)

**Interfaces:**
- Produces (models):
  - `ConnectionApp`: drop `client_secret_enc`; add `removed_at = DateTimeField(null=True)`; property `secret_name`.
  - `Connection`: `id = UUIDField(primary_key=True, default=uuid.uuid4)`; `user`/`agent` (XOR constraint kept); `app`; `provider`; `account_id`; `account_label`; `instance_url` (display); `scopes`; `state` ∈ `CONFIRMING="confirming"`, `ACTIVE="active"`, `REVOKING="revoking"`; `sealed`, `access_sealed` (Text); `sealed_sig`, `access_sig` (JSON); `sealed_digest` (Char 64); `access_expires_at`; `owner_key` (Char 64); `signer` (FK user, SET_NULL); `statement` (JSON), `statement_sig` (Char), `statement_version` (PositiveInt, 0 = none); `lease_id` (UUID null), `lease_holder` (Char 64: device ECDH jkt), `lease_expires_at`, `lease_written` (bool); `refresh_failed_at`; `last_used_at`; `revoking_since`; `revoke_at_provider` (bool); `connected_by`; timestamps. Uniques `(user, app, account_id)`, `(agent, app, account_id)` kept. `needs_reconnect` = `refresh_failed_at is not None`. `binding` property = `wire.binding_id(self.id)`.
  - `ConnectionGrant`: unchanged fields; `clean()` restriction removed (person-owned may be lent).
  - `ConnectionMint` (new): `id` UUID; `app` FK; `provider`; `owner_user`/`owner_agent` (XOR); `requested_by`; `runner` FK `harness.Runner` null; `device_key` FK `harness.RunnerDeviceKey` null; `connection_id` (UUID, pre-assigned); `reconnect_of` FK Connection SET_NULL; `status` ∈ `requested, awaiting, completing, confirming, done, failed`; `outcome` (32); `code_challenge` (128); `authorize_url`; `code`; `account_label`; `detail`; timestamps. `TTL_SECONDS = 900`, `CONFIRM_SECONDS = 1800`.

- [ ] **Step 1: Write the failing tests**

```python
CONNECTIONS = pathlib.Path("apps/connections")
FORBIDDEN = {"decrypt_secret", "encrypt_secret"}

def test_no_code_under_apps_connections_can_decrypt():
    for path in CONNECTIONS.rglob("*.py"):
        tree = ast.parse(path.read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
                {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not (names & FORBIDDEN), path
        assert "apps.common.encryption" not in mods, path

def test_no_connection_model_has_a_fernet_column():
    cols = {f.name for m in (Connection, ConnectionApp) for f in m._meta.get_fields()}
    assert not cols & {"refresh_token_enc", "access_token_enc", "client_secret_enc"}

def test_the_app_form_takes_no_client_secret(admin_client):
    r = admin_client.put(APP_URL, {"client_id": "cid", "client_secret": "s", "options": {}}, content_type=JSON)
    assert r.status_code == 422

def test_saving_an_app_makes_the_connections_vault(admin_client):
    admin_client.put(APP_URL, {"client_id": "cid", "options": {}}, content_type=JSON)
    assert Vault.objects.filter(workspace=ws, kind=Vault.CONNECTIONS).count() == 1

def test_old_token_routes_are_gone(client):
    assert client.post("/api/me/connections/salesforce/token").status_code == 404
    assert client.post(f"/api/harness/runners/{rid}/turns/{tid}/connection-token").status_code == 404

def test_a_person_connection_may_be_granted():
    ConnectionGrant.objects.create(connection=person_conn, agent=agent)  # no ValidationError
```

- [ ] **Step 2:** Run `uv run pytest tests/test_connections_hold_nothing.py tests/test_connections_model.py -v` — FAIL.
- [ ] **Step 3:** Remove the listed code; 0006 = RunPython only; 0007 = remove `ConnectionGrant` and `Connection`, recreate them (tables are empty after 0006) with the fields above, add `ConnectionMint`, drop `client_secret_enc`, add `removed_at`. Keep `services.record`/`log_line`/`parse_log_line`/`Subject`/errors/`connections_for`/`providers_for_agent`.
- [ ] **Step 4:** Run `uv run pytest tests/test_connections_* tests/test_migrations_apply.py -q` — PASS.
- [ ] **Step 5:** Commit `"connections: canopy-web holds no token or client secret; sealed connection rows"`.

### Task A4: Recipients and the signed grant statement

**Files:**
- Create: `apps/connections/recipients.py`, `apps/connections/statement.py`
- Modify: `apps/agents/credentials/accounts.py` (`may_hold_for(runner, person, *, own_use: bool = False)`: `own_use` admits `Runner.COMPUTER` owned by `person`)
- Modify: `apps/connections/api.py` (route 6, route 7 fields), `apps/connections/schemas.py` (`StatementIn`, `StatementOut`, `ConnectionOut` additions)
- Test: `tests/test_connections_recipients.py`, `tests/test_connections_statement.py`

**Interfaces:**
- Consumes: `wire.check_statement`, `wire.verify_statement` (A1); `accounts.delegated_devices`, `accounts.may_hold_for`, `services.runner_may_hold_agent`, `zk_endorse.endorsements`, `UserVaultKeyset`.
- Produces (`recipients.py`): `grants_in_force(conn) -> list[Agent]` (statement agents ∩ `ConnectionGrant` rows, owner agent included for agent-owned, each still in `conn.app.workspace` and — person-owned — still administered by the person); `devices(conn) -> list[RunnerDeviceKey]` (agent-owned: `zk.eligible_device_keys`-style keys on runners serving each in-force agent with `runner_may_hold_agent`; person-owned: `accounts.delegated_devices(agent, person)` per in-force agent, plus the person's own ACTIVE ECDH-approved keys whose `ecdh_jkt ∈ statement.my_computers` via `may_hold_for(..., own_use=True)`); `owner_member(conn) -> tuple[str, str, dict] | None` (signer's keyset only while its thumbprint == `owner_key`); `recipients(conn) -> list[tuple[str, str, dict]]`; `waiting(conn) -> list[dict]`; `prune(conn) -> list[str]` (`zk_seal.keep_only` on both envelopes + `wire.keep_only_sig`, under row lock; never removes the last device holder).
- Produces (`statement.py`): `apply(conn, *, statement: dict, sig: str, by_jkt: str, by) -> Connection`. Rules: `by`'s keyset thumbprint == `by_jkt` == `conn.owner_key` else 403 `not_the_owner_key`; signature verifies else 422 `bad_signature`; version > `statement_version` else 409 `statement_stale`; every added agent administered by `by` (403 `not_an_admin`) and in the app's workspace (422 `agent_outside_workspace`); `my_computers` ⊆ `by`'s own enrolled keys (422 `not_your_machine`, person-owned only; agent-owned must be empty); agent-owned includes owner agent (422 `statement_missing_owner_agent`); diff `ConnectionGrant` rows and `record` `granted` / `grant_revoked`; `CONFIRMING → ACTIVE` and the mint → `done`; then `prune(conn)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_agent_owned_recipients_are_its_machines_and_the_signer(world): 
    assert set(t for k, t, _ in recipients(conn)) == {world.eva_box.ecdh_jkt, str(world.admin.pk)}

def test_person_owned_lent_to_two_agents_reaches_both_agents_cloud_boxes(world): ...
def test_my_computers_admits_the_persons_computer_kind_runner_only_when_listed(world): ...
def test_another_admins_laptop_is_never_a_recipient(world): ...
def test_grant_out_of_force_narrows_recipients(world):          # Review Focus 3
    revoke_admin(world.person, world.echo)
    assert world.echo_box.ecdh_jkt not in {t for _, t, _ in recipients(conn)}
def test_prune_never_drops_the_last_device_holder(world): ...

def test_statement_signed_by_the_owner_key_activates_and_writes_grants(world):
    conn = statement.apply(conn, statement=st, sig=sign(st, world.person_scalar), by_jkt=jkt, by=world.person)
    assert conn.state == "active" and set(conn.grants.values_list("agent__slug", flat=True)) == {"eva", "echo"}
def test_statement_signed_by_another_admin_is_refused(world):     # 403 not_the_owner_key
def test_an_older_or_equal_version_is_refused(world):             # 409 statement_stale
def test_lending_to_an_agent_you_do_not_administer_is_refused(world):  # 403 not_an_admin
def test_my_computers_must_be_the_signers_own_machines(world):     # 422 not_your_machine
def test_agent_owned_statement_must_list_the_owner_agent(world):   # 422 statement_missing_owner_agent
def test_statement_route_needs_a_passkey_step_up(world):           # 403 step_up_required without
```

- [ ] **Step 2:** Run `uv run pytest tests/test_connections_recipients.py tests/test_connections_statement.py -v` — FAIL.
- [ ] **Step 3:** Implement. Route 6 `@gate("self", setting=True, step_up=True)`; for agent-owned, the caller must also be `agent.is_admin`.
- [ ] **Step 4:** Run the two files plus `tests/test_delegated_tokens.py tests/test_google_delegation.py -q` (the `may_hold_for` default is unchanged) — PASS.
- [ ] **Step 5:** Commit `"connections: one recipients rule and the owner's signed grant statement"`.

### Task A5: Connection mints on runners

**Files:**
- Create: `apps/connections/mints.py`, `apps/connections/api_runner.py` (router mounted at `/harness`), `apps/connections/api_mints.py`, `apps/connections/proofs.py` (`proving_device(request, runner) -> RunnerDeviceKey`: `device_keys.verify_proof` + `known_key`, key on `runner`, ACTIVE, `ecdh_ready`, else 401 `device_proof_required`)
- Modify: `apps/connections/views.py` (start → `mints.start` + redirect to `/w/<ws>/connections/connecting/<id>`; callback → `mints.unsign_state`, `mints.receive_code`, redirect same page), `apps/connections/providers/base.py` (`authorize_url(state, redirect_uri, code_challenge, *, owner_kind, owner)` kept; nothing secret), `apps/api/api.py` (mount routers), `apps/connections/schemas.py`
- Test: `tests/test_connections_mints.py`, `tests/test_connections_views.py` (rewrite)

**Interfaces:**
- Consumes: `vault.app_layer`, `vault.has_secret` (A2); `recipients.owner_member` (A4); `wire.check_writer_sig` (A1); `google_mint`'s patterns (`_expire`, atomic code consume in `claim`).
- Produces (`mints.py`): `CAPABILITY = "connection_mint"`; `class MintRefused(Exception)(status, code, detail, revoke: bool = False)`; `eligible_devices(app, *, owner_kind, owner, reconnect_of=None) -> list[RunnerDeviceKey]` (cloud, `live_status` ONLINE/DEGRADED, capability true, `ecdh_ready`, `workspace_vaults.device_may_hold(connections vault, key)` with wrap-holders first; agent-owned: serves the agent + `runner_may_hold_agent`; person new: serves an agent of the workspace the person administers; reconnect: device ∈ `sealed_targets(reconnect_of.sealed)`); `start(*, app, owner_kind, owner, by, reconnect_of=None) -> ConnectionMint` (409 `not_configured` when `not has_secret(app)`; 409 `no_mint_runner` with the reason in words; supersedes the requester's unfinished mint); `sign_state(mint) -> str` / `unsign_state(state) -> dict` (django signing, max age `TTL_SECONDS`); `claim(runner) -> tuple[ConnectionMint | None, str]`; `work(mint, code, device_key) -> dict` (`ConnectionMintWorkOut`); `record_challenge(runner, mint_id, challenge) -> None`; `receive_code(mint, code, user) -> None`; `finish(runner, device_key, mint_id, payload) -> Connection`; `runner_failed(runner, mint_id, error, detail) -> None`.
- `finish` policy, in order (each a `MintRefused`): mint `completing` for this runner (409 `not_waiting`); envelopes are `zks1` (422 `not_ciphertext`); `sealed_targets` of both ⊆ {minting device, owner member} and contain both (422 `foreign_reader`); `check_writer_sig` with `by` = minting device (422 `bad_writer_sig`); provider `missing_scopes` (409 `insufficient_scope`); provider `check_owner(identity, …)` server copy — Google `allowed_domains`, agent's own Google account (409 `wrong_account`); reconnect: `identity.account_id == reconnect_of.account_id` (409 `wrong_account`); new connect: no existing row for (owner, app, account_id) (409 `duplicate_account`). `revoke` on every refusal = `provider != "google"`. Success: create/replace the `Connection` (`id = mint.connection_id`, `state=CONFIRMING` for new, unchanged state for reconnect with `refresh_failed_at=None`), `owner_key` = thumbprint of `work.owner.public_jwk`, mint → `confirming` (new) or `done` (reconnect), `record(…, "minted", runner=…)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_start_with_no_cloud_box_online_is_409_no_mint_runner_and_starts_nothing(world): ...
def test_a_laptop_is_never_chosen_to_mint(world): ...
def test_start_without_the_sealed_app_secret_is_not_configured(world): ...
def test_runner_posts_only_a_challenge_and_canopy_web_builds_the_authorize_url(world):
    url = mint_to_consent(world)
    q = parse_qs(urlparse(url).query)
    assert q["code_challenge"] == [CHALLENGE] and q["code_challenge_method"] == ["S256"] and "code_verifier" not in q
def test_the_code_is_handed_out_once(world):
    assert claim(world)["code"] == "the-code" and claim(world) is None
def test_callback_with_state_signed_for_someone_else_is_refused(world): ...
def test_work_carries_the_app_layer_and_owner_key_never_a_secret(world):
    w = claim(world); assert set(w["app"]) == {"vault_id", "binding", "kv", "wrap", "value"} and "client_secret" not in json.dumps(w)
def test_result_sealed_to_a_stranger_is_refused(world):        # 422 foreign_reader
def test_result_in_the_clear_is_refused(world):                 # 422 not_ciphertext
def test_result_without_writer_sig_is_refused(world):           # 422 bad_writer_sig
def test_google_wrong_domain_is_refused_without_revoke(world):
    r = post_result(world, identity={"account_id": "1", "email": "x@gmail.com", "hosted_domain": ""})
    assert r.status_code == 409 and r.json()["error"] == "wrong_account" and r.json()["revoke"] is False
def test_google_duplicate_account_is_refused_without_revoke(world): ...
def test_salesforce_refusal_asks_the_runner_to_revoke(world):   # revoke True
def test_reconnect_must_be_the_same_account(world): ...
def test_a_good_result_leaves_a_confirming_connection_and_records_minted(world):
    assert Connection.objects.get().state == "confirming" and recorded("connections.minted", runner=world.runner.name)
def test_an_unfinished_mint_expires_after_900_seconds(world): ...
def test_mint_get_shows_the_envelope_only_to_the_requester_while_confirming(world): ...
```

- [ ] **Step 2:** Run `uv run pytest tests/test_connections_mints.py tests/test_connections_views.py -v` — FAIL.
- [ ] **Step 3:** Implement. Route gates: 4 `@gate("self", kind="action")`; 5 `@gate("self")`; 15 `@gate("runner")`; 16 `@gate("runner", kind="runner-protocol")`; 17 `@gate("runner", setting=True, step_up="runner protocol: the box's own token, and ciphertext only")` with device proof (`apps/connections/proofs.py::proving_device(request, runner) -> RunnerDeviceKey`). Bare views keep `@login_required`.
- [ ] **Step 4:** Run the two files — PASS.
- [ ] **Step 5:** Commit `"connections: a runner mints every connection; canopy-web applies policy to what it reports"`.

### Task A6: Material, refresh lease and write-back

**Files:**
- Create: `apps/connections/tokens.py`
- Modify: `apps/connections/proofs.py` (add `proving_machine(request, user) -> RunnerDeviceKey`: the proven key's runner is owned by `user`, any kind including `computer`)
- Modify: `apps/connections/api_runner.py` (routes 18–21), `apps/connections/api.py` (routes 10–14), `apps/connections/api_turns.py` (keep route `turn_connections`), `apps/connections/schemas.py`
- Test: `tests/test_connections_tokens.py`

**Interfaces:**
- Consumes: `recipients.recipients/devices/owner_member` (A4); `vault.app_layer` (A2); `wire.check_writer_sig` (A1); `vault_engine.value_digest`.
- Produces (`tokens.py`): `LEASE_SECONDS = 60`; `material(conn, device_key, *, turn=None, runner=None) -> dict` (409 `revoking`/`not_connected` for a revoking row, 409 `needs_reconnect`, 409 `waiting` when `device_key.ecdh_jkt` not in `sealed_targets`; records `token_issued` with `to`, `runner`, `turn`, coalesced per turn; sets `last_used_at`); `take_lease(conn, device_key, lease_id=None) -> dict` (`{lease_id, ttl_seconds, expires_at}`; same device + same `lease_id` renews; a different live holder → 409 `lease_held` with `retry_after`; taking over an expired lease with `lease_written=False` records `lease_expired_unwritten`); `release_lease(conn, device_key, lease_id) -> None`; `write_back(conn, *, device_key, lease_id, access_sealed, access_sig, access_expires_at, expect, rotated, sealed=None, sealed_sig=None) -> str` (under row lock: `lease_id` matches and nobody took it since, even if expired, else 409 `lease_lost`; `state != REVOKING` else 409 `revoking`; `expect == sealed_digest` else 409 `zk_value_changed`; writer `by.target == device_key.ecdh_jkt`; targets ⊆ `recipients(conn)` (422 `foreign_reader`) and ⊇ current still-eligible recipients (422 `zk_rewrap_required`); stores; `lease_written=True`; records `refreshed` with `rotated`); `mark_reconnect_required(conn, device_key, lease_id, error) -> None` (only `invalid_grant` stamps `refresh_failed_at`; records `reconnect_required`).
- Person routes 10–14 require `_plain_pat(request)` and `proving_machine` (key's runner owned by the caller; else 403 `machine_not_enrolled`); route 10 refuses 403 `not_on_my_computers` when the key's jkt is not in `statement.my_computers`. Neither person nor runner routes are MCP tools: add their operation ids to `apps/mcp/api_tools.EXCLUDED` ("a sealed credential for a machine, never a model's context").

- [ ] **Step 1: Write the failing tests**

```python
def test_material_is_ciphertext_and_never_a_token(world):
    body = material_for(world.eva_box).json()
    assert body["sealed"].startswith("zks1:") and "refresh_token" not in json.dumps(body)
def test_material_without_device_proof_is_401(world): ...
def test_material_for_a_waiting_machine_is_409_waiting(world): ...
def test_material_refused_for_an_agent_whose_lender_left(world):   # Review Focus 3 → 409 not_connected
def test_material_read_records_token_issued_once_per_turn_with_runner(world): ...
def test_one_lease_holder_at_a_time(world):
    assert lease(world.box_a).status_code == 200 and lease(world.box_b).json()["error"] == "lease_held"
def test_lease_answers_ttl_seconds_not_only_a_timestamp(world):    # Review Focus 1
    assert lease(world.box_a).json()["ttl_seconds"] == 60
def test_taking_an_expired_unwritten_lease_records_lease_expired_unwritten(world): ...
def test_write_back_with_a_lost_lease_is_refused(world):           # 409 lease_lost
def test_write_back_cas_on_sealed_digest(world):                   # 409 zk_value_changed
def test_write_back_must_keep_every_still_eligible_recipient(world):  # 422 zk_rewrap_required
def test_write_back_to_a_stranger_is_refused(world):               # 422 foreign_reader
def test_write_back_to_a_revoking_connection_is_refused(world):     # Review Focus 2, 409 revoking
def test_rotated_write_back_records_refreshed_rotated_true(world): ...
def test_invalid_grant_marks_needs_reconnect_other_errors_do_not(world): ...
def test_person_material_needs_a_plain_pat_and_their_own_machine(world): ...
def test_person_material_refuses_a_machine_not_on_my_computers(world):
    assert r.json() == {**r.json(), "error": "not_on_my_computers"} and "Use on my computers" in r.json()["detail"]
def test_ambiguous_person_material_names_the_workspaces(world): ...
```

- [ ] **Step 2:** Run `uv run pytest tests/test_connections_tokens.py -v` — FAIL.
- [ ] **Step 3:** Implement. Gates: 18, 20, 21 `@gate("runner", "runner-holds-agent", kind="runner-protocol", step_up="runner protocol")` for the turn route, `@gate("runner", kind="runner-protocol", step_up=…)` for the connection-id routes with an in-function check that the box serves an in-force agent of the connection (`runner_may_hold_agent`); 19 `setting=False, kind="runner-protocol"`; person 10–14 `@gate("self", kind="ephemeral-credential", step_up="a person's own machine; PAT and device proof only")`.
- [ ] **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `"connections: material, one refresh lease, and compare-and-swap write-back"`.

### Task A7: Keeper work, reseal, revoking and sweeps

**Files:**
- Create: `apps/connections/keeper.py`, `apps/connections/scheduler.py` (registers `keeper.sweep` on `apps/common/ticks.py`, as `apps/capabilities/scheduler.py`)
- Modify: `apps/connections/api_runner.py` (routes 22–24), `apps/connections/services.py` (`disconnect`, `remove_app` mark `revoking`), `apps/connections/agent_move.py` (moved agent's connections: grants out of force, no decrypt)
- Create: `apps/connections/management/commands/connections_sweep.py`
- Test: `tests/test_connections_keeper.py`, `tests/test_connections_agent_move.py` (adapt)

**Interfaces:**
- Consumes: `recipients.*` (A4), `tokens` lease state (A6), `wire.check_writer_sig` (A1), `vault.app_layer` (A2).
- Produces (`keeper.py`): `REVOKE_GRACE = timedelta(days=7)`; `work(runner) -> list[dict]` (`ConnectionKeeperWorkOut`: connections this box's device holds — `ecdh_jkt ∈ sealed_targets(sealed)` — and that are `revoking`, or `active` with a statement and `waiting(conn)` non-empty; each with `endorsements` for every in-force agent, rows tagged `agent_id`); `reseal(conn, runner, device_key, *, sealed, sealed_sig, access_sealed, access_sig, expect) -> list[str]` (holder in old and new targets else 403 `zk_not_a_holder`; 409 `no_statement` while confirming; other checks as `write_back`; records `resealed`); `confirm_revoked(conn, runner, device_key, *, revoked: bool, error: str) -> None` (state must be `revoking` else 409 `not_revoking`; deletes the row; records `revoked` or `revoke_failed`); `sweep(now=None) -> dict` (`confirming` older than `CONFIRM_SECONDS` → `revoking`; `revoking` older than 7 days → delete + `revoke_unconfirmed`; expired mints → `failed/expired`; apps with `removed_at` and no connections → deleted with their vault value).
- `services.disconnect(conn, *, by) -> None`: sets `state=REVOKING`, `revoking_since=now`, `revoke_at_provider = not Connection.objects.filter(app=conn.app, account_id=conn.account_id).exclude(pk=conn.pk).exclude(state=REVOKING).exists()`; records `disconnected`; every list hides `revoking` rows.

- [ ] **Step 1: Write the failing tests**

```python
def test_keeper_work_lists_only_connections_this_box_holds(world): ...
def test_keeper_work_for_a_confirming_connection_is_empty(world): ...
def test_keeper_reseal_adds_a_waiting_box_and_records_resealed(world): ...
def test_keeper_reseal_by_a_non_holder_is_refused(world):          # 403 zk_not_a_holder
def test_disconnect_hides_the_row_at_once_and_hands_it_out_nowhere(world):
    services.disconnect(conn, by=world.person)
    assert my_connections(world) == [] and material_for(world.eva_box).status_code == 409
def test_revoke_at_provider_false_when_another_connection_of_that_account_remains(world): ...
def test_first_holder_confirms_revoked_and_the_row_is_deleted(world): ...
def test_revoking_for_seven_days_deletes_and_records_revoke_unconfirmed(world): ...
def test_unconfirmed_connection_is_discarded_after_confirm_window(world):   # Review Focus 5
    keeper.sweep(now=minted_at + timedelta(seconds=1801)); assert Connection.objects.get().state == "revoking"
def test_removing_an_app_marks_every_connection_revoking_then_deletes_the_app(world): ...
def test_moved_agent_loses_its_grants_without_decrypting(world): ...
```

- [ ] **Step 2:** Run `uv run pytest tests/test_connections_keeper.py tests/test_connections_agent_move.py -v` — FAIL.
- [ ] **Step 3:** Implement. Routes 22 `@gate("runner", step_up="runner protocol: ciphertext only machines open")`; 23 `@gate("runner", setting=True, step_up=…)`; 24 `@gate("runner", kind="runner-protocol", step_up=…)`.
- [ ] **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `"connections: keepers re-seal, holders revoke, and nothing waits forever"`.

### Task A8: Audit, OpenAPI, architecture checks

**Files:**
- Modify: `apps/connections/services.py` (`AUDITED` += `minted`, `refreshed`, `resealed`, `revoked`, `revoke_unconfirmed`, `lease_expired_unwritten`; `token_issued` keys `to`, `runner`, `machine`, `turn`), `apps/connections/__init__.py` module docstring (the contract), `apps/connections/providers/__init__.py` docstring (the recipe: "a provider is two small modules, this one and `orchestrator/connections/providers/<name>.py` in the canopy CLI")
- Modify: `tests/test_connections_audit.py` (full-run scan now also asserts no plaintext from the A5–A7 fixtures: `refresh_token`, `access_token`, `client_secret`, `code_verifier`, the authorization code — in any log, Event, or response body of the run)
- Create: `tests/test_connections_shared_names.py`
- Regenerate: `frontend/src/api/generated.ts`
- Test: also `tests/test_every_route_declares_its_gate.py`, `tests/test_architecture_boundary.py`, `tests/test_layered_app_imports.py`

**Interfaces:**
- Produces: `tests/test_connections_shared_names.py::SHARED = {"salesforce": {"required_scopes": ("api", "refresh_token"), "rotates_refresh_token": True}, "google": {"required_scopes": ("openid", "https://www.googleapis.com/auth/userinfo.email"), "rotates_refresh_token": False}}` — the CLI pins the same dict (B2).

- [ ] **Step 1: Write the failing tests**

```python
def test_provider_shared_names_match_the_cli_contract():
    for name, want in SHARED.items():
        cls = providers.get_class(name)
        assert (cls.required_scopes, cls.rotates_refresh_token) == (want["required_scopes"], want["rotates_refresh_token"])

def test_new_verbs_are_audited(): assert {"minted", "refreshed", "resealed", "revoked", "revoke_unconfirmed", "lease_expired_unwritten"} <= services.AUDITED

def test_full_connection_lifecycle_writes_no_plaintext(full_run): ...  # extended scan
```

- [ ] **Step 2:** Run `uv run pytest tests/test_connections_audit.py tests/test_connections_shared_names.py -v` — FAIL.
- [ ] **Step 3:** Implement; then `cd frontend && npm run gen:api:local` and commit `generated.ts`.
- [ ] **Step 4:** Run `uv run pytest -q` (whole suite) — PASS; `cd frontend && npm run build` — PASS.
- [ ] **Step 5:** Commit `"connections: audit vocabulary, shared provider names, regenerated API types"`.

---

# Part B — canopy CLI (`$CLI`, branch `feat/connections-zero-knowledge`)

### Task B1: Wire format in the CLI

**Files:**
- Create: `$CLI/src/orchestrator/connections/__init__.py`, `$CLI/src/orchestrator/connections/wire.py`
- Copy: `docs/vault/zk-v1/connections_v1_reference.py`, `connections_v1_vectors.json` → `$CLI/tests/fixtures/vault_zk/`
- Test: `$CLI/tests/test_connections_wire.py`

**Interfaces:**
- Consumes: `orchestrator.vault_zk` (`seal_value`, `parse_sealed`, `sealed_entry`, `sealed_epk_point`, `open_sealed_with_z`, `hkdf`, `b64u`, `jwk_thumbprint`, `point_from_jwk`, `public_key_from_jwk`); `device_key.EcdhKey.derive`.
- Produces: `REFRESH_NAME`, `ACCESS_NAME`, `binding_id(connection_id: str) -> str`; `class WireError(Exception)`; `statement_msg(st: dict) -> bytes`; `verify_statement(st: dict, sig: str, signer_jwk: dict) -> bool`; `seal_envelope(binding: str, name: str, plaintext: dict, recipients: list[tuple[str, str, dict]], *, writer: EcdhKey, writer_kind: str = "device", writer_target: str, expect: str) -> tuple[str, dict]` (returns `(zks1 value, sig)`); `open_envelope(value: str, sig: dict, *, binding: str, name: str, me_kind: str, me_target: str, ecdh: EcdhKey) -> tuple[dict, dict]` (returns `(plaintext, by)`; raises `WireError("unsigned")` without `sig`, `WireError("bad-mac")` on a wrong MAC); `digest(value: str) -> str` (sha256 hex, = canopy-web `value_digest`).

- [ ] **Step 1: Write the failing tests**

```python
def test_vectors_match_the_canopy_web_reference(): assert json.loads(VECTORS.read_text()) == ref.vectors()
def test_statement_msg_and_signature_match_vectors(): ...
def test_seal_then_open_round_trips_and_names_the_writer(ecdh_pair):
    value, sig = wire.seal_envelope(B, "refresh", PLAIN, [("device", me.jkt, me.public_jwk)], writer=me, writer_target=me.jkt, expect="")
    plain, by = wire.open_envelope(value, sig, binding=B, name="refresh", me_kind="device", me_target=me.jkt, ecdh=me)
    assert plain == PLAIN and by["target"] == me.jkt
def test_open_refuses_envelope_without_writer_sig(): ...            # WireError unsigned
def test_open_refuses_a_mac_made_by_another_key_claiming_the_writer(): ...  # WireError bad-mac
def test_a_refresh_envelope_does_not_open_as_access(): ...         # AAD binds the name
def test_pruning_another_recipient_keeps_my_mac_valid(): ...
```

- [ ] **Step 2:** Run `cd $CLI && uv run pytest tests/test_connections_wire.py -v` — FAIL.
- [ ] **Step 3:** Implement per Part 0.3–0.4.
- [ ] **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `"connections: grant statements and writer-authenticated envelopes"`.

### Task B2: Provider modules — the secret half

**Files:**
- Create: `$CLI/src/orchestrator/connections/providers/__init__.py`, `base.py`, `salesforce.py`, `google.py`
- Test: `$CLI/tests/test_connection_providers.py`

**Interfaces:**
- Produces (`base.py`): `class ProviderError(Exception)(code: str, detail: str)`; `@dataclass Tokens(access_token, refresh_token, expires_at: int, scope: str, raw: dict)`; `@dataclass Identity(account_id, label, email="", hosted_domain="")`; `class Provider` with class attrs `name`, `required_scopes`, `scope_implies`, `rotates_refresh_token`, `revoke_on_refusal`, and methods `exchange(code: str, verifier: str, redirect_uri: str, app: dict) -> Tokens`, `refresh(plain: dict, app: dict | None) -> Tokens`, `revoke(token: str, plain_or_app: dict) -> None`, `identity(tokens: Tokens, app: dict) -> Identity`, `check_account(identity: Identity, app: dict, *, expected_account: str = "") -> None`, `missing_scopes(granted: str) -> tuple[str, ...]`, `check_plain(plain: dict) -> None` (host rules on `login_host`/`instance_url`); `get(name: str) -> Provider`. HTTP via an injectable `post/get` (`requests`, 15 s timeout).
- `salesforce.py`: `LOGIN_HOSTS = ("login.salesforce.com", "test.salesforce.com")`, `MY_DOMAIN_SUFFIX = ".my.salesforce.com"`; `login_host_ok(host) -> bool`; `instance_ok(url) -> bool`; endpoints `https://{login_host}/services/oauth2/token|revoke`; identity `https://{instance_host}/services/oauth2/userinfo` (account `"{organization_id}/{user_id}"`, label `preferred_username`); no `expires_in` → `expires_at = now + app.options.access_token_seconds` (default 1800, from the vault value or the plaintext's last known); `rotates_refresh_token = True`; `revoke_on_refusal = True`.
- `google.py`: fixed `TOKEN_URL`, `REVOKE_URL`, `USERINFO_URL` (as canopy-web's constants); `check_account` enforces `app["options"]["allowed_domains"]` from the vault value with canopy-web's `hd` rule, and `expected_account`; `rotates_refresh_token = False`; `revoke_on_refusal = False`.
- `SHARED` dict identical to A8's.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("host,ok", [("login.salesforce.com", True), ("test.salesforce.com", True),
    ("acme.my.salesforce.com", True), ("acme.sandbox.my.salesforce.com", True), ("my.salesforce.com", False),
    ("evil.com", False), ("login.salesforce.com.evil.com", False), ("acme.my.salesforce.com.evil.com", False)])
def test_login_host_rule(host, ok): assert salesforce.login_host_ok(host) is ok

@pytest.mark.parametrize("url,ok", [("https://acme.my.salesforce.com", True), ("http://acme.my.salesforce.com", False),
    ("https://na1.salesforce.com", False), ("https://acme.my.salesforce.com.evil.com", False)])
def test_instance_url_rule(url, ok): assert salesforce.instance_ok(url) is ok

def test_salesforce_endpoints_come_from_the_vault_login_url_never_the_work(http):
    salesforce.Provider().exchange("c", "v", REDIRECT, {**SF_APP, "token_url": "https://evil/"})
    assert http.calls[0].url == "https://login.salesforce.com/services/oauth2/token"
def test_identity_goes_to_the_checked_instance_host_never_the_id_url(http): ...
def test_refresh_with_a_plaintext_naming_a_bad_host_is_refused_before_any_call(http):
    with pytest.raises(ProviderError) as e: salesforce.Provider().refresh({**PLAIN, "login_host": "evil.com"}, None)
    assert e.value.code == "bad-host" and http.calls == []
def test_salesforce_without_expires_in_lives_access_token_seconds(http): ...  # now + 1800
def test_google_allowed_domains_come_from_the_vault_value(): ...
def test_google_endpoints_are_constants(http): ...
def test_shared_names_match_canopy_web(): ...
```

- [ ] **Step 2:** Run `uv run pytest tests/test_connection_providers.py -v` — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `"connections: provider modules hold every secret-bearing call; URLs from rules only"`.

### Task B3: Trust — who a box will seal to

**Files:**
- Create: `$CLI/src/orchestrator/connections/trust.py`
- Test: `$CLI/tests/test_connection_trust.py`

**Interfaces:**
- Consumes: `wire.verify_statement` (B1); `vault_keeper._trust_person`, `vault_keeper._endorsed`, `vault_zk_cli.load_pins/save_pins`, `KeepResult`.
- Produces: `PINS_FILE = ~/.canopy/connection-pins.json` (`{connection_id: {"owner_jkt", "version"}}`); `class TrustError(Exception)(code: str, detail: str)`; `verified_statement(material: dict, *, owner_jkt: str, pins: dict) -> dict` (owner public key thumbprint == `owner_jkt` from the sealed plaintext; signature verifies; `version` ≥ pinned version else `TrustError("statement-rollback")`; pins it); `allowed(material: dict, statement: dict, *, owner_jkt: str, pins: dict, res: KeepResult) -> dict[tuple[str, str], dict]` (Part 0.5); `refuse_unallowed(rows: list[dict], allowed: dict) -> list[tuple[str, str, dict]]` raising `TrustError("zk_unendorsed_recipient")` for any row not allowed; `writer_allowed(by: dict, statement: dict, allowed: dict, owner_member: tuple) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_device_not_in_the_statement_is_refused_even_when_canopy_web_lists_it(material):
    rows = material["recipients"] + [STRANGER_ROW]
    with pytest.raises(TrustError) as e: trust.refuse_unallowed(rows, trust.allowed(material, st, owner_jkt=O, pins={}, res=R))
    assert e.value.code == "zk_unendorsed_recipient"
def test_an_endorsement_for_an_agent_not_in_the_statement_admits_nothing(material): ...
def test_an_endorsement_subject_is_checked_against_the_statement_not_the_payload(material): ...
def test_my_computers_admits_exactly_the_listed_thumbprints(material): ...
def test_a_member_other_than_the_owner_is_never_allowed(material): ...
def test_a_statement_signed_by_a_key_other_than_owner_key_is_refused(material): ...
def test_an_older_statement_than_one_seen_is_refused(material):   # statement-rollback
def test_writer_must_be_allowed_or_the_owner(material): ...
```

- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement. **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `"connections: a box seals only where the owner's signed statement allows"`.

### Task B4: `canopy connection mint`

**Files:**
- Create: `$CLI/src/orchestrator/connections/mint.py`, `$CLI/src/orchestrator/connection_cli.py` (click group `connection`, registered beside `vault`/`cred`)
- Test: `$CLI/tests/test_connection_mint.py`

**Interfaces:**
- Consumes: B1 `seal_envelope`/`open_envelope`, B2 `providers.get`, B3 `allowed`, `cred_resolve.unwrap_device_vk`, `vault_zk.decrypt_value`, `cred_resolve.resolve(slug, refs=["gog-token"], …)` (agent's own Google identity), `vault_cli._runner_and_token`, `canopy_web.call`.
- Produces: `class MintError(Exception)(code, detail, revoked=False)` with `out() -> dict`; `run(work: dict, *, token: str, runner_id: str, base_url: str | None, ecdh=None, http=None) -> dict`. Steps: open the app value (`AppLayerOut` → VK → `connection-app:<provider>`; `no-client` if absent; `client-mismatch` if its `client_id != work.client_id`); exchange code+verifier; refuse no refresh token (`no-refresh-token`), missing scope (`insufficient-scope`), bad hosts (`bad-host`); identity; `check_account` with the vault value and, for an agent's own Google, the `gog-token` email; reconnect: open `work.reconnect.sealed` and require `identity.account_id == plaintext.account_id` (`wrong-account`); build Part 0.2 plaintexts with `owner_key = jkt(work.owner.public_jwk)`; seal both to exactly `[this device, owner member]` with this device as writer; POST route 17; on a 409 refusal with `revoke: true` and `provider.revoke_on_refusal`, revoke the refresh token at the provider; drop plaintext; return Part 0.7 output.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_mint_posts_only_ciphertext_and_prints_no_token(fake_web, http, capsys): ...
def test_allowed_domains_come_from_the_vault_value_not_canopy_web(fake_web, http):
    fake_web.app_value["options"] = {"allowed_domains": "dimagi.com"}  # ConnectionApp says nothing
    out = run_mint(identity_email="x@gmail.com")
    assert out["error"] == "wrong-account" and fake_web.results == []
def test_reconnect_enforces_the_envelopes_account_id(fake_web, http):
    out = run_mint(reconnect_account="00D1/005A", identity_account="00D1/005B")
    assert out["error"] == "wrong-account"
def test_google_duplicate_refusal_discards_without_revoking(fake_web, http):
    fake_web.refuse = {"error": "duplicate_account", "revoke": False}
    out = run_mint(provider="google"); assert out == {**out, "revoked": False} and not http.revokes
def test_google_wrong_account_never_revokes_even_if_told_to(fake_web, http): ...
def test_salesforce_refusal_revokes_the_new_grant(fake_web, http): ...
def test_seals_to_this_box_and_the_owner_only(fake_web, http):
    assert sealed_targets(fake_web.results[0]["sealed"]) == {("device", ME), ("member", OWNER_ID)}
def test_no_refresh_token_is_refused(fake_web, http): ...
def test_client_id_mismatch_with_the_vault_is_refused(fake_web, http): ...
def test_agent_own_google_must_be_the_gog_token_account(fake_web, http): ...
```

- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement. **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `"connections: canopy connection mint exchanges, checks and seals on the box"`.

### Task B5: Getting an access token — lease, refresh, write-back

**Files:**
- Create: `$CLI/src/orchestrator/connections/access.py`
- Modify: `$CLI/src/orchestrator/cred_cli.py` (`connection_token`, `stage_connection_token`, `_refusal`; keep `connection_token_path`, `_write_connection_token`, `staged_connection_token`, options and the `salesforce-token` alias)
- Test: `$CLI/tests/test_connection_access.py`, `$CLI/tests/test_cred_cli.py` (adapt connection cases)

**Interfaces:**
- Consumes: B1–B3; `device_key.load_ecdh`, `device_key.proof_or_reason` (via `canopy_web.call`), `runner_session.token`.
- Produces: `WAIT_SECONDS = 30`, `POLL_SECONDS = 2`; `class Paths(material: str, lease: str, sealed: str, reconnect: str)` (runner vs person route families); `access_token(paths: Paths, *, min_life: int, call, ecdh, pins: dict, refresh: bool = False, sleep=time.sleep, monotonic=time.monotonic, http=None) -> dict` returning `{access_token, instance_url, expires_at, account_label}`. Algorithm (§4.2): GET material; verify statement + writer of both envelopes (B3); if `access_expires_at - now > min_life` and not `refresh` → open `access_sealed` and return; else POST lease → on `lease_held` poll material every `POLL_SECONDS` up to `WAIT_SECONDS` for a newer `access_sealed`, then fail `CredError("refresh in progress elsewhere")`; holding the lease: client secret = vault value's when `material.app` opens (§4.4), else the plaintext's; refresh; build new access (and refresh if rotated, with the new `client_secret` when the vault's differs); re-seal to the current allowed recipients (`refuse_unallowed` over `material.recipients`, keeping every still-allowed current target); PUT write-back with `expect = sealed_digest`; retry the PUT from memory while renewing the lease (TTL from `ttl_seconds` on the monotonic clock) up to 3 times; only then return. `invalid_grant` → POST reconnect-required and raise `CredError(…, EXIT_REFUSED)`. 404 on material → exit 3 ("not connected"); 409 `revoking`/`not_connected`/`waiting` → exit 3 in runner mode.
- `stage_connection_token(provider, slug, runner_id, turn_id, *, token=None, min_life=None, refresh=False) -> Path` keeps its signature; `connection_token(provider, *, refresh=False, workspace="") -> Path` now uses PAT + device proof against routes 10–14 and refuses with the server's sentence plus the command (`canopy vault device --enroll`).

- [ ] **Step 1: Write the failing tests**

```python
def test_a_fresh_access_token_is_used_without_a_lease(fake_web): ...
def test_refresh_takes_the_lease_writes_back_then_stages(fake_web, http):
    assert fake_web.order == ["material", "lease", "refresh@provider", "PUT sealed"] and staged()
def test_rotated_refresh_token_is_written_back_before_it_is_relied_on(fake_web, http): ...
def test_failed_write_back_retries_from_memory_and_stages_nothing_until_it_lands(fake_web, http): ...
def test_second_process_waits_for_new_access_instead_of_refreshing(fake_web, http):   # Review Focus 4
    fake_web.lease_held_then_new_access(after_polls=2)
    access_token(...); assert http.refreshes == 0 and fake_web.polls == 3
def test_lease_renewal_uses_monotonic_ttl(fake_web, http): ...                          # Review Focus 1
def test_invalid_grant_reports_reconnect_required(fake_web, http): ...
def test_forged_access_sealed_for_a_stranger_account_is_refused(fake_web):
    fake_web.material["access_sealed"] = sealed_by_owner_key_but_account("00D9/005Z")
    with pytest.raises(CredError): access_token(...)
def test_unsigned_access_sealed_is_refused(fake_web): ...
def test_access_sealed_written_by_an_unlisted_device_is_refused(fake_web): ...
def test_write_back_never_adds_a_recipient_the_statement_does_not_allow(fake_web, http): ...
def test_prefers_the_vault_client_secret_when_it_can_read_it(fake_web, http): ...       # §4.4
def test_revoking_answer_stages_nothing(fake_web): ...                                   # Review Focus 2
def test_material_404_is_not_connected_exit_3(fake_web): ...
def test_person_path_without_an_enrolled_key_says_how_to_enroll(fake_web):
    assert "canopy vault device --enroll" in str(err)
def test_person_path_refused_when_use_on_my_computers_is_off(fake_web): ...
def test_staged_file_format_is_unchanged(fake_web, tmp_home):
    assert set(json.loads(path.read_text())) == {"access_token", "instance_url", "expires_at", "account_label"}
    assert oct(path.stat().st_mode & 0o777) == "0o600"
```

- [ ] **Step 2:** Run `uv run pytest tests/test_connection_access.py tests/test_cred_cli.py -v` — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `"connections: refresh on the box under one lease, written back before use"`.

### Task B6: The keeper's connection work and revokes

**Files:**
- Modify: `$CLI/src/orchestrator/vault_keeper.py` (`run_keep` also GETs `/api/harness/runners/<id>/vault-keeper/connections`; module docstring gains a Connections paragraph)
- Create: `$CLI/src/orchestrator/connections/keep.py`
- Test: `$CLI/tests/test_connection_keeper.py`

**Interfaces:**
- Consumes: B1–B3, B2 `revoke`, `vault_keeper.KeepResult`.
- Produces: `keep_connection(work: dict, *, pins: dict, call, ecdh, http=None) -> KeepResult`. If `work.revoking`: open `sealed`; if `revoke_at_provider` revoke the refresh token at the provider (provider rules only); POST `revoked_path` `{revoked, error}`. Else: verify statement + writer (B3); targets = current still-allowed ∪ waiting machines that are `allowed`; re-seal the SAME plaintexts (both envelopes) with this box as writer and `expect`; PUT `reseal_path`. Laptops never run this (cloud runners only run the keeper loop).

- [ ] **Step 1: Write the failing tests**

```python
def test_keeper_reseals_for_a_waiting_endorsed_box(fake_web): ...
def test_keeper_refuses_device_not_in_statement_even_if_server_lists_it(fake_web):
    res = keep_connection(work_with_waiting(STRANGER), ...)
    assert fake_web.puts == [] and "zk_unendorsed_recipient" in " ".join(res.skipped)
def test_keeper_does_nothing_for_a_connection_without_a_statement(fake_web): ...
def test_keeper_revokes_then_confirms(fake_web, http): ...
def test_keeper_skips_provider_revoke_when_told_another_connection_remains(fake_web, http): ...
def test_keeper_refuses_a_forged_envelope_and_writes_nothing(fake_web): ...
def test_run_keep_includes_connections(fake_web): ...
```

- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement. **Step 4:** Run `uv run pytest tests/test_connection_keeper.py tests/test_vault_keeper.py -q` — PASS.
- [ ] **Step 5:** Commit `"connections: keepers re-seal and revoke connections they hold"`.

### Task B7: CLI release

**Files:**
- Modify: `$CLI/CHANGELOG.md`, `$CLI/CLAUDE.md` (command list: `canopy connection mint`), version files via `canopy version bump`
- [ ] **Step 1:** `cd $CLI && uv run pytest -q` — PASS.
- [ ] **Step 2:** `canopy version bump && canopy version verify` — PASS; note the new version `0.2.N` (Part C's `CONNECTIONS_ZK_MIN_CLI`).
- [ ] **Step 3:** Commit `"release: connections on runners"`; open the PR; `gh pr merge <n> --auto`. Do not merge canopy-web Part C before this release is out (Part E).

---

# Part C — cloud runner (`runner/ec2` in canopy-web)

### Task C1: Claim and run connection mints

**Files:**
- Modify: `runner/ec2/cloud_runner.py` (near `_drain_google_mint` ~9754–9830 and the claim tick ~9636; `RUNNER_CAPS` ~153)
- Test: `runner/ec2/tests/test_connection_mint_on_the_box.py`

**Interfaces:**
- Consumes: routes 15–16 (A5), CLI `canopy connection mint` (B4).
- Produces: `CONNECTIONS_ZK_MIN_CLI = (0, 2, N)` (B7's version); `RUNNER_CAPS["connection_mint"]` set at startup only when `_canopy_cli_version() >= CONNECTIONS_ZK_MIN_CLI`; `_CONNECTION_VERIFIERS: dict[str, str]`; `_drain_connection_mints(runner_id: str) -> None` (GET route 15 on the same tick; `requested` → `_pkce_pair()`, keep verifier, POST challenge; `completing` with a verifier → `_run_connection_mint_cli(runner_id, work)`; `completing` without one → POST result `{"error":"interrupted"}`); `_run_connection_mint_cli(runner_id: str, work: dict) -> dict` (stdin `{**work, "code_verifier": v}`, never argv; env bearer; timeout 120; any exception → `{"error":"exchange-failed"}`; the CLI posts the result itself).

- [ ] **Step 1: Write the failing tests**

```python
def test_capability_declared_only_with_a_new_enough_cli(monkeypatch): ...
def test_requested_mint_posts_only_the_challenge_and_keeps_the_verifier(fake_api): ...
def test_verifier_goes_on_stdin_never_argv(fake_api, fake_run): assert "code_verifier" not in " ".join(fake_run.argv)
def test_restart_without_verifier_reports_interrupted(fake_api): ...
def test_cli_crash_reports_exchange_failed(fake_api, fake_run): ...
```

- [ ] **Step 2:** Run `uv run pytest runner/ec2/tests/test_connection_mint_on_the_box.py -v` — FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `"runner: cloud boxes mint connections"`.

### Task C2: Staging through the new CLI path

**Files:**
- Modify: `runner/ec2/cloud_runner.py` connections block (~1791–2025): keep `_stage_connections`, holds, heartbeat, adoption, cleanup; `_stage_connection_token` keeps its command line; treat exit 3 as silent for `not_connected`/`waiting`/`revoking`; a stage refused because the CLI is older than `CONNECTIONS_ZK_MIN_CLI` logs one `connections.stage_failed … reason="canopy CLI too old for connections"`
- Test: `runner/ec2/tests/test_connection_token_staging.py` (adapt; no assertion about a server-issued token)

- [ ] **Step 1: Write the failing tests**

```python
def test_stage_runs_the_cli_with_min_life_900(fake_run): assert fake_run.argv[-2:] == ["--min-life", "900"]
def test_waiting_for_a_holder_is_silent_and_leaves_no_file(fake_run): ...   # exit 3
def test_old_cli_logs_one_stage_failed_line(fake_run, caplog): ...
def test_heartbeat_restage_still_only_refreshes_held_files(fake_run): ...
```

- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement. **Step 4:** Run `uv run pytest runner/ec2/tests -q` — PASS.
- [ ] **Step 5:** Commit `"runner: stage connection tokens the box refreshes itself"`.

---

# Part D — frontend

### Task D1: API client and grant/envelope crypto

**Files:**
- Modify: `frontend/src/api/connections.ts`
- Create: `frontend/src/vault/connectionGrant.ts`, `frontend/src/vault/connectionGrant.test.ts`

**Interfaces:**
- Produces (`connections.ts`): `startConnectionMint(body: {provider: string; workspace: string; agent?: string; reconnect?: string}): Promise<ConnectionMintOut>`, `getConnectionMint(id: string)`, `putStatement(id: string, body: StatementIn): Promise<ConnectionOut>`, `disconnectMine(id: string)`, `disconnectAgentConnection(slug: string, id: string)`; remove `grantConnection`, `revokeConnectionGrant`; `configureConnectionApp` body loses `client_secret`.
- Produces (`connectionGrant.ts`): `statementMessage(st: Statement): string`; `signStatement(me: Unlocked, st: Statement): Promise<{statement: Statement; sig: string; by_jkt: string}>` (reuses the `endorse` import path in `zk.ts`); `openConnectionEnvelope(env: {binding: string; sealed: string; sealed_sig: WriterSig}, me: Unlocked): Promise<{plain: RefreshPlain; writer: string}>` (opens the member entry with `openSealedValue`, verifies its MAC per Part 0.4, throws on unsigned/bad MAC; never returns `refresh_token`/`client_secret` to callers: returns `{account_id, provider, instance_url, owner_key}` only); `nextStatement(conn: ConnectionOut, change: {agents?: string[]; myComputers?: string[]}): Statement`.

- [ ] **Step 1: Write the failing tests** (vitest, vectors via `import raw from '../../../docs/vault/zk-v1/connections_v1_vectors.json?raw'`)

```ts
it('statementMessage matches the vector byte for byte', ...)
it('a statement signed here verifies with the reference public key', ...)
it('opens the vector envelope as the member and checks its MAC', ...)
it('refuses an envelope without a writer signature', ...)
it('never hands back the refresh token or client secret', () => expect(Object.keys(out.plain)).toEqual(['account_id','instance_url','owner_key','provider']))
it('nextStatement sorts agents and bumps the version', ...)
```

- [ ] **Step 2:** `cd frontend && npx vitest run src/vault/connectionGrant.test.ts` — FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `"frontend: grant statements and envelope checks in the browser"`.

### Task D2: Settings → Connections seals the app secret in the browser

**Files:**
- Modify: `frontend/src/components/settings/ConnectionAppsPanel.tsx` (+ test), `frontend/src/guide/surfaces.ts` (the connections descriptor's `what`/`actions`: "the client secret is sealed in your browser")

**Interfaces:**
- Consumes: `makeWorkspaceVaultKey`, `sealWorkspaceVaultValue` (`vault/workspaceVault.ts`), `useVaultUnlock`, D1.
- Behaviour: save = PUT app (no secret) → if `vault.kv === 0`, `makeWorkspaceVaultKey(vault.id, me)` (needs a recovery code; its error text shown as is) → `sealWorkspaceVaultValue(vault.id, secret_name, JSON.stringify({client_id, client_secret, options}), me)`. No key and none can be made: the form says "Set up your vault key first" with a link to Settings → Vault key; nothing is sent. `has_secret` shows "Secret sealed in the Connections vault".

- [ ] **Step 1: Write the failing tests**

```ts
it('never sends client_secret to the connection-apps route', ...)
it('seals connection-app:salesforce with client_id, client_secret and options', ...)
it('makes the vault key first when the vault has none', ...)
it('points to vault key setup and sends nothing without a vault key', ...)
```

- [ ] **Step 2:** `npx vitest run src/components/settings/ConnectionAppsPanel.test.tsx` — FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `"frontend: the app secret is sealed in the admin's browser"`.

### Task D3: The Connecting… page and the confirmation touch

**Files:**
- Create: `frontend/src/pages/connections/ConnectingPage.tsx` (+ test)
- Modify: `frontend/src/router.tsx` (`/w/:workspace/connections/connecting/:mintId`), `frontend/src/guide/surfaces.ts` (descriptor, `home`), `docs/architecture/pages.md` (row)

**Interfaces:**
- Consumes: D1 (`getConnectionMint`, `openConnectionEnvelope`, `signStatement`, `putStatement`), `useVaultUnlock`.
- Behaviour: poll `getConnectionMint` every 1.5 s; `awaiting` with `authorize_url` → `window.location.assign`; `confirming` → one Unlock (the passkey touch) opens the envelope and shows the account ("Salesforce · jj@dimagi.com · acme.my.salesforce.com", from the opened plaintext, not the server's label alone) and the choices (agents the person administers in this workspace; "Use on my computers" with their enrolled machines); Confirm signs and PUTs the statement; agent-owned: the owning agent pre-checked and fixed; nothing chosen for a person-owned connection → Confirm disabled with "Lend it to an agent or use it on your computers". `failed` → the outcome in words with one action. Shows `confirm_by` countdown.

- [ ] **Step 1: Write the failing tests**

```ts
it('navigates to the authorize URL once the runner posted its challenge', ...)
it('shows the account from the opened envelope before asking to confirm', ...)
it('signs and sends a statement with the chosen agents', ...)
it('resumes a confirming mint after reload', ...)          // Review Focus 5
it('says no_mint_runner in words with one action', ...)
```

- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement. **Step 4:** PASS; `npx vitest run src/routeTenancy.test.ts src/guide` — PASS.
- [ ] **Step 5:** Commit `"frontend: connecting happens on a runner; the owner confirms with one touch"`.

### Task D4: Lending, withdrawing, "Use on my computers", waiting states

**Files:**
- Modify: `frontend/src/components/settings/ConnectionsPanel.tsx` (+ test), `frontend/src/pages/agents/AgentConnectionsPanel.tsx` (+ test)

**Interfaces:**
- Consumes: D1 `nextStatement`, `signStatement`, `putStatement`.
- Behaviour: each change (lend to agent, withdraw, toggle "Use on my computers" per machine) = one Unlock + `putStatement(nextStatement(...))`. Rows show `state` (`confirming` → "Finish connecting", link to the Connecting page), `waiting` ("waiting for a machine that holds it"), `needs_reconnect` (Reconnect = `startConnectionMint({reconnect: id})`), holders. `revoking` rows are never shown. Agent panel lists owned and lent connections; no separate grant buttons.

- [ ] **Step 1: Write the failing tests**

```ts
it('lending re-signs the statement with the agent added', ...)
it('turning on Use on my computers lists this machine’s ECDH thumbprint', ...)
it('shows waiting for a machine that holds it', ...)
it('reconnect starts a mint with reconnect set', ...)
```

- [ ] **Step 2–4:** Run → FAIL → implement → PASS; `cd frontend && npm run build` — PASS.
- [ ] **Step 5:** Commit `"frontend: lend, withdraw and use on my computers, each one passkey touch"`.

---

# Part E — migration, docs, release order

### Task E1: Docs

**Files:**
- Modify: `docs/architecture/vault.md` (replace § "OAuth connections are server-held" with § "OAuth connections are sealed on runners": what is stored, connecting, recipients, the signed statement and envelope signatures, lease/write-back, revoking, the Connections vault; update § Connection audit verbs; keep the log-line format text), `docs/architecture/api-surface.md` (§ Connections: Part 0.6 routes), `docs/architecture/deploy.md` (re-enter the Salesforce secret once after deploy; boxes need CLI ≥ `CONNECTIONS_ZK_MIN_CLI`), `ARCHITECTURE.md` (`connections` row: "tokens sealed on runners; canopy-web holds ciphertext"), `CLAUDE.md` (the invariant bullet "A token from canopy's own OAuth flow …" rewritten: minted and refreshed on cloud runners, sealed to machines the owner's signed statement allows, canopy-web stores ciphertext and never decrypts — no dates, no PR refs)
- Test: `tests/test_claude_md_budget.py`, `tests/test_old_address_in_docs.py`

- [ ] **Step 1:** Edit the docs.
- [ ] **Step 2:** `uv run pytest tests/test_claude_md_budget.py tests/test_old_address_in_docs.py -q` — PASS.
- [ ] **Step 3:** Commit `"docs: connections are sealed on runners"`.

### Release order (not a task; the executor follows it)

1. CLI: merge B7, confirm the release (`canopy version verify` on main; cloud boxes reinstall from the marketplace clone — `runner/ec2/bootstrap_agents.sh::reinstall_cli_from_marketplace_clone`).
2. Confirm boxes run it: a cloud runner's capabilities show `connection_mint: true` (`/supervisor` → Runners, or `GET /api/harness/runners/{id}`) — this needs Part C deployed, so Parts A–D ship together in ONE canopy-web PR after step 1.
3. canopy-web: open the PR, `gh pr merge <n> --auto`; after merge `gh workflow run "Deploy to Labs (AWS)" --ref main` (migrations 0006/0007 and the agents vault migration run automatically); confirm with `gh run list --workflow="Deploy to Labs (AWS)" --limit 3 --json headSha,conclusion` and `/api/openapi.json` listing `/api/connections/mints`.
4. Admin re-enters the Salesforce secret in Settings → Connections (sealed in the browser); each person reconnects once and confirms with one touch.
5. Verify: `canopy cred connection-token salesforce --agent <slug>` inside a turn returns the staged path; `SELECT count(*) FROM connections_connection WHERE sealed NOT LIKE 'zks1:%'` is 0.
