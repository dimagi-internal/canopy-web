# Connections Hold Nothing canopy-web Can Use — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** canopy-web finishes each OAuth sign-in, seals the refresh token to the owner's vault key at once and keeps no readable token; the owner shares it to machines with one passkey touch; runners refresh, write back, re-seal and revoke.

**Architecture:** canopy-web keeps the browser legs, the code exchange, identity policy and the app client secret (Fernet, as today); after the exchange it seals the token to the owner's member key only. The owner's browser opens it, signs a grant statement and re-seals it to the devices the statement allows. The canopy CLI (separate repo) refreshes under a server lease with compare-and-swap write-back, keeps and revokes, and refuses any recipient or envelope the signed statement does not allow. The cloud runner stages tokens through the CLI as today.

**Tech Stack:** Django 6 + Django Ninja + Pydantic v2 (canopy-web), React 19 + WebCrypto (frontend), Python 3 + click + `cryptography` (canopy CLI), pytest / vitest.

**Spec:** `docs/superpowers/specs/2026-10-31-connections-zero-knowledge-design.md` (binding; simplified 2026-11-02). Executors read both. `google_mint` / `gog-token` / `google-delegated-*` are OUT of scope.

**Repos:**
- canopy-web: `/Users/jjackson/emdash/worktrees/canopy-web-salesforce`, branch `feat/connections-zero-knowledge`. Parts A, C, D, E.
- canopy CLI: do NOT touch `~/emdash/worktrees/canopy-cred-salesforce`'s branch. Create a worktree: `git -C ~/emdash/worktrees/canopy-cred-salesforce fetch -q origin && git -C ~/emdash/worktrees/canopy-cred-salesforce worktree add ~/emdash/worktrees/canopy-connections-zk -b feat/connections-zero-knowledge origin/main`. Part B runs there (`$CLI` below).

## Global Constraints

- canopy-web stores no readable refresh or access token and never refreshes or revokes a stored token; the only plaintext token it ever sees is the one in hand during the callback's exchange (spec "The guarantee"). Anti-goal: no server fallback.
- The app client secret stays `ConnectionApp.client_secret_enc` (Fernet), pasted in Settings as today; it leaves canopy-web only in a material response to an authenticated machine (§4.2, §8).
- A runner never seals to a recipient only because canopy-web listed it; canopy-web's list can narrow, never widen (§4.3). Refusal code `zk_unendorsed_recipient`.
- Staged token file path and format unchanged: `~/.canopy/connections/<slug>/<provider>.json` = `{access_token, instance_url, expires_at, account_label}`, mode 0600, dir 0700; person cache `~/.canopy/connections/<provider>.json` (`<provider>@<workspace>.json` with `--workspace`). `canopy cred connection-token <provider> --agent <slug>` and `$CANOPY_CONNECTIONS` unchanged.
- Lease: one holder, 60 s, renewable; a waiter polls for the new `access_sealed` up to 30 s (§4.2). Keeper pass every 120 s (unchanged).
- Revoking: the first holder revokes and confirms; a connection no machine holds is deleted at once and one still unconfirmed after 7 days is deleted, both recorded `revoke_unconfirmed` (§4.5).
- Google: a refusal after the exchange discards WITHOUT revoking; Salesforce revokes. A disconnect revokes only when no other connection of that account through that app remains (§2, §4.5).
- Salesforce hosts: `login_host` ∈ {`login.salesforce.com`, `test.salesforce.com`} or `*.my.salesforce.com` (non-empty label before the suffix); `instance_url` host ends in `.my.salesforce.com`; `https` only. Refresh/revoke endpoints derive from these, never from the server or as given (§1).
- Only cloud runners keep or revoke; a person's laptop reads and refreshes its own use (§3; Ruling R3).
- Audit: `connections.*` plumbing (`services.record`); new verbs `shared`, `refreshed` (`rotated`), `resealed`, `revoked`, `revoke_unconfirmed`, `lease_expired_unwritten`; `token_issued` names the runner/machine. No plaintext token in any event, log or response (§8).
- No migration of existing rows: delete `Connection` rows, drop the token columns; keep `client_secret_enc` (§9).
- canopy-web rules: every route `@gate(...)` directly above its `def`; route docstrings are published OpenAPI (rationale in `#` comments); regenerate and commit `frontend/src/api/generated.ts` after schema/route changes; `harness` and `agents` never import `connections` (L2); a `RunPython` deleting referenced rows goes in its own migration before schema changes; CLAUDE.md has no dates or PR refs; frontend uses design tokens only.
- canopy CLI rules: `canopy version bump` (required `check-version`); commands never print a token.

## Review Focus

1. **Clock skew between a box and canopy-web** — expect lease and expiry decisions to survive a skewed box clock: the lease answers `ttl_seconds` and the CLI renews on a monotonic clock. Tests in A5 (`test_lease_answers_ttl_seconds_not_only_a_timestamp`) and B4 (`test_lease_renewal_uses_monotonic_ttl`).
2. **An app removed or a connection disconnected mid-refresh** — the holder's write-back lands on a `revoking` row. Expect 409 `revoking`, nothing stored, nothing staged. Tests in A5 (`test_write_back_to_a_revoking_connection_is_refused`) and B4 (`test_revoking_answer_stages_nothing`).
3. **A lender stops administering an agent after lending** — the statement still lists the agent. Expect the server to stop listing that agent's machines and the material route to refuse that agent's runner, while other grants work. Tests in A4 (`test_grant_out_of_force_narrows_recipients`) and A5 (`test_material_refused_for_an_agent_whose_lender_left`).
4. **Two turns of one agent on one box stage at once** — expect one lease and one refresh; the other process waits for the new `access_sealed` (a parallel Salesforce refresh would kill the rotated token). Test in B4 (`test_second_process_waits_for_new_access_instead_of_refreshing`).
5. **The owner closes the tab before sharing** — expect the connection to stay `unshared`, sealed to the owner only, listed "not shared yet", runners told `not_connected`, and Share from Settings to finish it later. Tests in A3 (`test_unshared_connection_is_listed_as_not_shared_yet`), A5 (`test_unshared_connection_is_handed_to_no_machine`) and D2 (`resumes sharing from Settings`).

## Rulings (where the spec leaves a choice)

- **R1 Envelope authentication is an ECDH-keyed MAC per recipient (Part 0.4).** aws-iam cloud boxes have no signing key (`device_key._aws_key` raises on sign), and only canopy-web ties a signing key to an ECDH key; statements and endorsements name ECDH keys.
- **R2 Keeper connection work is its own route in `apps/connections`** (route 13), not a field on `VaultKeeperOut`: `agents`/`harness` never import `connections`.
- **R3 A laptop may refresh and write back its own use**, re-sealing to exactly the current allowed set; it never adds recipients or revokes.
- **R4 canopy-web's first seal carries no writer MAC** (it has no ECDH key of its own). Only the owner's browser accepts an unauthenticated envelope, and only while `state == "unshared"`; every machine requires a MAC.
- **R5 Connect seals only `refresh`**; the access token from the exchange is dropped and `access_sealed` stays empty until a machine's first refresh.
- **R6 The same account connected again by the same owner through the same app** replaces that row's envelope (today's upsert), sets `state = "unshared"`, keeps its grants, and the confirmation screen re-shares it with `version + 1`.
- **R7 Disconnecting a connection no device holds deletes it at once** (`revoke_unconfirmed`): no machine can ever receive it without the owner.
- **R8 Connecting needs the owner's vault key** (person, or the connecting admin for an agent): 409 `no_vault_key` at start, pointing at vault setup.
- **R9 `client_id` is sealed in the plaintext**; a refresher refuses material whose app `client_id` differs (`client-mismatch`). `client_id` is already locked while connections exist (`app_locked`).
- **R10 Only the `owner_key` holder re-signs, and a row's `owner_key` never changes.** Boxes pin it per connection, and only that pin catches a self-consistent forged set. If an agent's connecting admin leaves, or a person replaces their vault key, connecting the same account again is refused (`owner_changed`, the new grant discarded, the row untouched): disconnect the old connection first, then connect, which makes a new row with a new id and so a new pin.
- **R11 Statement agents are agent primary keys** (what endorsements sign); boxes pin `(owner_jkt, version)` per connection, and refuse rollback and a changed owner (`statement-owner-changed`, see R10).
- **R12 Removing an app** sets `removed_at` and hides it; the sweep deletes it once its connections are revoked (a cascade would delete revoking rows before any holder revoked them).
- **R13 A write-back on an expired lease is accepted if nobody took the lease since.**
- **R14 The browser trusts devices** by endorsements from people it trusts (`vault/trust.ts`, as `sealAndStore`/`approveAndGrant` do), and the owner's own machines by the owner's choice (name + fingerprint).
- **R16 Getting a token reads twice** (amends Task B4's call order): material → (live access? return it) → lease → material again with full validation → (a newer access landed? release and return it) → refresh from the second read → write back with its digest → release the lease. Once a box holds the lease no earlier holder can land a rotation, so it can never refresh with a token already rotated away, which would mark a healthy connection `needs_reconnect`.
- **R17 A rotated token gets a time budget, not a retry count** (amends Task B4's "up to 3 times"): holding a rotated refresh token, a box retries the write-back with backoff for up to 90 s, renewing the lease, on transient answers and on `device_proof_required`.
- **R18 A laptop's write-back seals to the envelope's current recipients narrowed by what the statement allows**, never to machines still waiting (§3: a laptop never re-seals for others).
- **R15 Release safety:** the runner logs one `stage_failed` when its CLI is older than `CONNECTIONS_ZK_MIN_CLI`, and a new CLI treats a missing material route (404) as "not connected".

---

# Part 0 — Contract

Normative reference: `docs/vault/zk-v1/connections_v1_reference.py` with vectors `docs/vault/zk-v1/connections_v1_vectors.json` (Task A1); the CLI copies both into `$CLI/tests/fixtures/vault_zk/` (B1); frontend tests read them by `?raw` import like `zk.test.ts`.

## 0.1 Names and bindings

- Binding id `connection:<uuid>` (lower-case hyphenated `Connection.id`) in the agent-id slot of zk-v1 AAD and HKDF.
- Envelope names: `refresh` (`sealed`), `access` (`access_sealed`). Both `zks1`, `kv = 0`.
- Recipient kinds: `device` (target = ECDH thumbprint), `member` (target = user id string; only the owner).

## 0.2 Sealed plaintexts (UTF-8 compact JSON)

`refresh`:
```json
{"v":1,"provider":"salesforce","refresh_token":"…","client_id":"…","login_host":"login.salesforce.com",
 "instance_url":"https://acme.my.salesforce.com","account_id":"00D…/005…",
 "owner_key":"<RFC7638 jkt of the owner's member public key>","issued_at":1793500000,"scopes":"api refresh_token id",
 "statement_floor":3}
```
Google: `login_host` and `instance_url` are `""`.

`statement_floor` (integer ≥ 0): canopy-web's first seal writes 0; the owner's browser writes the new statement's `version` at every share; machines keep the value unchanged on every re-seal and write-back. A reader refuses a statement with `version` below `statement_floor`, so an old validly-signed statement replayed at a box with no pin cannot re-admit a device the owner removed. An old envelope replayed along with its old statement opens only on a box that held it, and that box has a pin, provided boxes save the pin after a validated read (B4, B5).

`access`:
```json
{"v":1,"access_token":"…","expires_at":1793501800,"instance_url":"https://acme.my.salesforce.com","account_id":"00D…/005…","scope":"…","owner_key":"<same jkt as refresh>","statement_floor":3}
```
`statement_floor` here is set by whoever writes `access`, to the `refresh` plaintext's value. A reader refuses a statement whose `version` is below the floor in `refresh` OR the floor in `access`. These are independent checks against the statement, with no equality between the two envelopes, so an `access` left over from before a share stays readable (its token was already known to that envelope's recipients). The floor in `refresh` alone is only as honest as the `refresh` carrying it: a removed device could forge one naming the real owner key with floor 0, beside an old statement that still lists it, and the real `access` would be re-sealed to it.
A reader refuses an `access` whose `account_id` or `owner_key` differs from the `refresh` plaintext's, or whose `instance_url` fails the host rule. `account_id` alone is no cross-check, because the server knows it: on a fresh box a forged `refresh` with an attacker's owner key, paired with the real `access`, would pass, and a keeper re-seal would hand the real access token to the attacker.

App block in material (plain JSON over the authenticated route, never sealed): `{"client_id","client_secret","options":{…ConnectionApp.options}}`. Only `access_token_seconds` (Salesforce, default 1800) is read from `options`; endpoints never are.

## 0.3 Grant statement

Object (ASCII values; `account_id` matches `^[\x21-\x7e]{1,255}$` without `"` or `\`):
```json
{"account_id":"00D…/005…","agents":["12","40"],"connection":"<uuid>","endorsers":["<person key jkt>"],"my_computers":["<ecdh jkt>"],"v":1,"version":3}
```
- Keys are exactly these seven; `v` is 1.
- `agents`: agent pks as decimal strings, sorted as strings, unique; agent-owned MUST contain the owner agent. `my_computers`: ECDH thumbprints, sorted, unique (empty for agent-owned). `endorsers`: RFC 7638 thumbprints of person public keys, sorted, unique, at most 64, same pattern as `my_computers`; may be empty, and then only the owner vouches. `version` ≥ 1, strictly greater than the stored one, and the `statement_floor` of the `refresh` envelope sealed with it (0.2).
- Message: `"canopy-connections/v1/grant|" + json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)`, UTF-8. The browser writes keys in that sorted order.
- Signature: ECDSA P-256/SHA-256 by the owner's person scalar (as `endorse` in `frontend/src/vault/zk.ts`), P1363 `r‖s` 64 bytes, b64u. Stored `{statement, sig, by_jkt}`; `by_jkt` must equal `owner_key`.

## 0.4 Envelope authentication (writer MAC)

Stored beside each envelope (`sealed_sig`, `access_sig`):
```json
{"by":{"kind":"device","target":"<writer ecdh jkt>","public_jwk":{"kty":"EC","crv":"P-256","x":"…","y":"…"}},
 "expect":"<sealed_digest this write replaces, or \"\">",
 "macs":[{"kind":"device","target":"<jkt>","mac":"<b64u>"},{"kind":"member","target":"7","mac":"<b64u>"}]}
```
For each recipient entry `e` (`{kind,target,epk,ct}`):
- `entry_digest = sha256_hex(json.dumps(e, sort_keys=True, separators=(",", ":")))`
- `msg = f"canopy-connections/v1/envelope|{binding}|{name}|{expect}|{entry_digest}"`
- `key = HKDF-SHA256(ikm=Z, salt=b"", info=f"canopy-connections/v1/envelope-mac|{binding}|{name}|{kind}:{target}", length=32)`, `Z` = x-coordinate of ECDH(writer's static key, recipient's key); the recipient derives the same `Z` against `by.public_jwk`.
- `mac = b64u(HMAC-SHA256(key, utf8(msg)))`; `macs` covers exactly the envelope's recipients; `by.target` = thumbprint of `by.public_jwk` (device) or the owner's user id (member, the browser at share time).

A reader recomputes its own MAC (constant-time) and accepts only if `by` is a device the statement allows or the owner's member key. `sealed_sig = null` only on canopy-web's first seal (R4).

## 0.5 Who a box (or the owner's browser) seals to

For statement `S` and owner key `O` (`owner_key` from the `refresh` plaintext):
- `("member", owner_user_id)` whose key thumbprint is `O`;
- a device whose ECDH thumbprint ∈ `S.my_computers`;
- a device with a valid zk-v1 §7 endorsement of its exact ECDH key (`kind=device, target=key_jkt=jkt`) for agent id `a ∈ S.agents`, signed by a person key whose thumbprint is `O` or is in `S.endorsers`. The endorser's thumbprint is computed from the key used to verify the signature. First-sight pinning plays no part for connections.

canopy-web's `recipients` narrows by the same rule, so what it lists is never something a box would refuse. Who the signed statement names as `endorsers` is the owner's browser's choice (`vault/trust.ts`), so `ShareStateOut.candidates` is not narrowed by it.

## 0.6 Routes

Errors are RFC 7807 with an `error` member. "Device proof" = DPoP verified by `apps.harness.device_keys.verify_proof` + `known_key`, the key ACTIVE and ECDH-approved, else 401 `device_proof_required`.

**Browser / person**

| # | Route | Gate | Request → Response | Errors |
|---|---|---|---|---|
| 1 | `GET/PUT/DELETE /api/workspaces/{ws}/connection-apps[/{provider}]` | `integrations` (PUT/DELETE setting, step_up) | unchanged (`client_secret` write-only, `has_secret`); DELETE marks connections `revoking` and sets `removed_at` | 409 `app_locked` |
| 2 | `GET /api/connections/` | `self` | → `ConnectionsOut`; `ConnectionOut` gains `id: uuid`, `state`, `statement {version, agents:[slug], my_computers:[jkt], endorsers:[jkt]}`, `holders:[runner]`, `waiting:[{agent, runner}]`, `my_machines:[{jkt, name, fingerprint}]` | — |
| 3 | `GET /api/connections/{id}/share` | `self` | → `ShareStateOut {id, binding, provider, account_id, account_label, state, sealed, sealed_sig, sealed_digest, access_sealed, access_sig, statement, statement_sig, endorsers:[jkt], owner{user_id, public_jwk}, owner_agent:{id, slug, name}|null, candidates:[{kind, target, public_jwk, agent_id?, runner_name, mine: bool}], endorsements:[ZkEndorsementOut + agent_id; `by_public_jwk` is the signer's key], agents:[{id, slug, name}]}` — owner only | 403 `not_the_owner_key`, 409 `revoking` |
| 4 | `PUT /api/connections/{id}/share` | `self`, setting, step_up | `ShareIn {statement, sig, by_jkt, sealed, sealed_sig, access_sealed?, access_sig?, expect}` → `ConnectionOut` | 422 `bad_statement`, 422 `bad_signature`, 403 `not_the_owner_key`, 409 `statement_stale`, 403 `not_an_admin`, 422 `agent_outside_workspace`, 422 `not_your_machine`, 422 `statement_missing_owner_agent`, 409 `zk_value_changed`, 422 `foreign_reader`, 422 `bad_writer_sig`, 409 `revoking` |
| 5 | `DELETE /api/connections/{id}` · `DELETE /api/agents/{slug}/connections/{id}` | `self` / `agent-admin` (+step_up), setting | → 204; `revoking`, or deleted at once when no device holds it | — |
| 6 | `GET /api/agents/{slug}/connections` | `agent-admin` | → `ConnectionsOut` (owned + lent) | — |
| — | `PUT/DELETE /api/agents/{slug}/connections/{id}/grants/{other}`, `POST /api/me/connections/{provider}/token` | removed | lending is route 4 | — |

Bare views (paths unchanged): `auth/connections/<provider>/<workspace>/start/` (adds R8 check), `…/callback/` (exchange, policy, seal to owner, redirect to `/w/<ws>/connections/<id>/share`). Connect errors, each landing as `reason=<code>`: `owner_changed` (409 in the problem mapping; R10), and `provider_error` also for a Salesforce host the rule in Global Constraints refuses (the provider normalises the host to lower case, derives `login_host` through its own base URL); a refused grant is discarded (Google without revoke, Salesforce with).

**Person CLI (plain PAT over REST, as `_plain_pat`, plus device proof of a machine the caller owns)**

| # | Route | Request → Response | Errors |
|---|---|---|---|
| 7 | `GET /api/me/connections/{provider}/material?workspace=` | → `ConnectionMaterialOut` | 409 `not_connected`; 409 `ambiguous {workspaces}`; 403 `machine_not_enrolled` ("Enroll this computer: canopy vault device --enroll"); 403 `not_on_my_computers` ("Turn on Use on my computers for <label> in Settings → Connections"); 409 `waiting` |
| 8 | `POST /api/me/connections/{id}/refresh-lease` / `DELETE …/refresh-lease/{lease_id}` | `{lease_id?}` → `LeaseOut {lease_id, ttl_seconds: 60, expires_at}` / 204 | 409 `lease_held {retry_after}` |
| 9 | `PUT /api/me/connections/{id}/sealed` | `WriteBackIn` → `{sealed_digest}` | as 11 |
| 10 | `POST /api/me/connections/{id}/reconnect-required` | `{lease_id, error}` → 204 | 409 `lease_lost` |

**Runner (`@gate("runner", …)`, the box's runner token; device proof on all)**

| # | Route | Request → Response | Errors |
|---|---|---|---|
| — | `GET /api/harness/runners/{rid}/turns/{tid}/connections` | unchanged `TurnConnectionsOut {providers}` | — |
| — | `POST …/turns/{tid}/connection-token` | removed | — |
| 11 | `GET …/turns/{tid}/connections/{provider}` | → `ConnectionMaterialOut`; records `token_issued` (`to=turn`, runner, turn; coalesced per turn) | 409 `not_connected` (also `unshared`/`revoking`), 409 `needs_reconnect`, 409 `waiting` |
| 12 | `POST …/connections/{id}/refresh-lease` · `DELETE …/refresh-lease/{lease_id}` · `PUT …/connections/{id}/sealed` · `POST …/connections/{id}/reconnect-required` | as 8–10; `WriteBackIn {lease_id, sealed?, sealed_sig?, access_sealed, access_sig, access_expires_at, expect, rotated}` | 409 `lease_lost`, 409 `zk_value_changed`, 409 `revoking`, 422 `foreign_reader`, 422 `zk_rewrap_required`, 422 `bad_writer_sig` |
| 13 | `GET …/vault-keeper/connections` | → `{connections: [ConnectionKeeperWorkOut]}` | — |
| 14 | `PUT …/vault-keeper/connections/{id}` | `ResealIn {sealed, sealed_sig, access_sealed, access_sig, expect}` → `{machines}` | 403 `zk_not_a_holder`, 409 `zk_value_changed`, 422 `foreign_reader`, 422 `zk_rewrap_required` |
| 15 | `POST …/connections/{id}/revoked` | `{revoked: bool, error: ""}` → 204; deletes the row | 403 `zk_not_a_holder`, 409 `not_revoking` |

**Shapes**
- `ConnectionMaterialOut {id, provider, workspace, binding, account_id, account_label, sealed, sealed_sig, sealed_digest, access_sealed, access_sig, access_expires_at, statement, statement_sig, owner{user_id, public_jwk}, endorsements:[ZkEndorsementOut + agent_id], recipients:[{kind, target, public_jwk, runner_name}], app:{client_id, client_secret, options}, lease:{mine: bool, ttl_seconds}|null, rotates_refresh_token}`.
- `endorsements` in material always include those covering the device named in `sealed_sig.by` and `access_sig.by`, for any agent the statement lists, even when that device has left `recipients` (its key travels in `by.public_jwk`), or nobody could validate that envelope to re-seal it. If that endorsement no longer exists the route still answers and the box refuses.
- Share state's `endorsements` likewise include those covering the device named in `sealed_sig.by` and `access_sig.by`. A writer whose endorsement is gone blocks a re-share as well, since the owner's browser refuses that envelope too: recovery is a Reconnect (R6), which replaces the envelope, not a re-share.
- The browser reads a non-null stored `statement` only when `statement_sig` verifies against the key whose thumbprint is the sealed `owner_key`, whoever wrote the envelope; a missing or bad signature refuses. Its floor check, next version and shown choices all rest on it, so D2 and D3 read the verified statement `openForOwner` returns, never `state.statement`.
- `ConnectionKeeperWorkOut` = material fields + `waiting:[target]`, `revoking: bool`, `revoke_at_provider: bool`, `reseal_path`, `revoked_path`.

## 0.7 CLI commands

| Command | stdout (never a token) | Exit |
|---|---|---|
| `canopy cred connection-token <p> --runner --agent X --runner-id R --turn T [--min-life S]` | staged path (as today) | 0; 3 not connected / revoking (silent); 4 waiting (silent; the runner keeps its hold and retries on the heartbeat); 1 other |
| `canopy cred connection-token <p> --agent X` | staged path (no network, unchanged) | 0 / 2 |
| `canopy cred connection-token <p> [--workspace W] [--refresh]` (person) | cache path | 0; 2 refused with the sentence and the command |
| `canopy vault zk keep` | text (unchanged), plus connection lines | 0 |

---

# Part A — canopy-web backend

### Task A1: Wire format reference, vectors and server-side checks

**Files:**
- Create: `docs/vault/zk-v1/connections_v1_reference.py`, `docs/vault/zk-v1/connections_v1_vectors.json`, `apps/connections/wire.py`
- Test: `tests/test_connections_wire.py`

**Interfaces:**
- Produces (`wire.py`): `REFRESH_NAME = "refresh"`, `ACCESS_NAME = "access"`, `STATEMENT_PREFIX = "canopy-connections/v1/grant|"`; `class WireError(Exception)(status: int, code: str, detail: str)`; `binding_id(connection_id: uuid.UUID) -> str`; `statement_msg(st: dict) -> bytes`; `check_statement(st: dict, *, connection_id, account_id: str) -> dict` (422 `bad_statement`); `verify_statement(st: dict, sig: str, signer_jwk: dict) -> bool`; `check_writer_sig(value: str, sig: dict) -> dict` (structure only: `macs` targets == `zk_seal.sealed_targets(value)`, `by.kind` ∈ device|member, device `by.target == thumbprint(by.public_jwk)`; 422 `bad_writer_sig`); `keep_only_sig(sig: dict, allowed: set[tuple[str, str]]) -> dict`.
- Reference module: `statement_msg`, `sign_statement(st, priv)`, `verify_statement`, `entry_digest(entry)`, `envelope_mac(binding, name, expect, entry, writer_priv, recipient_pub)`, `verify_envelope_mac(...)`, `vectors()`.

- [ ] **Step 1: Write the failing tests**

```python
def test_vectors_file_is_what_the_reference_generates():
    assert json.loads(VECTORS.read_text()) == ref.vectors()

def test_statement_msg_matches_vector():
    v = vectors()["statement"]
    assert wire.statement_msg(v["statement"]) == v["msg"].encode()
    assert v["msg"].startswith('canopy-connections/v1/grant|{"account_id":')

def test_vector_signature_verifies_and_a_changed_agent_list_does_not():
    v = vectors()["statement"]
    assert wire.verify_statement(v["statement"], v["sig"], v["signer_jwk"])
    assert not wire.verify_statement({**v["statement"], "agents": ["12"]}, v["sig"], v["signer_jwk"])

@pytest.mark.parametrize("bad", [{"agents": ["40", "12"]}, {"agents": ["12", "12"]}, {"version": 0},
                                 {"account_id": 'a"b'}, {"v": 2}, {"extra": 1}])
def test_malformed_statement_is_refused(bad):
    with pytest.raises(wire.WireError) as e:
        wire.check_statement({**GOOD, **bad}, connection_id=CID, account_id=GOOD["account_id"])
    assert (e.value.status, e.value.code) == (422, "bad_statement")

def test_statement_for_another_account_or_connection_is_refused(): ...  # 422 bad_statement
def test_envelope_mac_vector_recomputes_for_each_recipient(): ...       # ref.verify_envelope_mac True for every vector recipient
def test_writer_sig_must_cover_exactly_the_sealed_recipients():
    with pytest.raises(wire.WireError) as e:
        wire.check_writer_sig(SEALED_TO_TWO, {**SIG, "macs": SIG["macs"][:1]})
    assert e.value.code == "bad_writer_sig"
def test_keep_only_sig_drops_pruned_macs(): ...
```

- [ ] **Step 2:** `uv run pytest tests/test_connections_wire.py -v` — FAIL (module missing).
- [ ] **Step 3:** Write the reference (fixed ephemerals/nonces as `zk_v1_reference.vectors`; vectors pin one valid ECDSA signature), generate `uv run python docs/vault/zk-v1/connections_v1_reference.py > docs/vault/zk-v1/connections_v1_vectors.json`; implement `wire.py` reusing `zk_seal.sealed_targets`, `zk.public_key`, `zk.b64u_bytes`, `device_keys.thumbprint`, and `zk_endorse.verify`'s P1363→DER conversion.
- [ ] **Step 4:** Run — PASS.
- [ ] **Step 5:** `git add docs/vault/zk-v1/connections_v1_* apps/connections/wire.py tests/test_connections_wire.py && git commit -m "connections: wire format for grant statements and writer-authenticated envelopes"`

### Task A2: No stored token, sealed connection rows

**Files:**
- Delete from `apps/connections/services.py`: `_cached_token`, `_handout`, `access_token_for`, `issue_token`, `AccessToken`, `_decrypted_refresh`, `_refresh_token_of`, `_revoke`; from `providers/base.py`: `refresh`, `revoke` used for stored tokens (keep `exchange_code`, `identity`, `revoke` for connect-time refusals); routes `my_connection_token`, `turn_connection_token`, `grant_connection`, `revoke_connection_grant`
- Create: `apps/connections/migrations/0006_delete_connections.py` (RunPython only: delete `ConnectionGrant`, `Connection`), `0007_sealed_connections.py`
- Modify: `apps/connections/models.py`; delete `tests/test_connections_turn_token.py` and the refresh/token cases in `tests/test_connections_services.py`, `tests/test_connections_api.py`; adapt `tests/fixtures/connections.py` (rows built with `sealed="zks1:…"`), `tests/test_connections_model.py`
- Test: `tests/test_connections_hold_nothing.py`

**Interfaces:**
- Produces (models):
  - `ConnectionApp`: unchanged fields (`client_secret_enc` stays); add `removed_at = DateTimeField(null=True)`.
  - `Connection`: `id = UUIDField(primary_key=True, default=uuid.uuid4)`; `user`/`agent` (XOR constraint kept); `app`; `provider`; `account_id`; `account_label`; `instance_url` (display); `scopes`; `state` ∈ `UNSHARED="unshared"`, `ACTIVE="active"`, `REVOKING="revoking"`; `sealed`, `access_sealed` (Text); `sealed_sig`, `access_sig` (JSON, null); `sealed_digest` (Char 64); `access_expires_at`; `owner_key` (Char 64); `signer` (FK user, SET_NULL: whose member key it is); `statement` (JSON), `statement_sig`, `statement_version` (PositiveInt, 0 = none); `lease_id` (UUID null), `lease_holder` (Char 64, device ECDH jkt), `lease_expires_at`, `lease_written` (bool); `refresh_failed_at`; `last_used_at`; `revoking_since`; `revoke_at_provider` (bool); `connected_by`; timestamps. Uniques `(user, app, account_id)`, `(agent, app, account_id)`. `needs_reconnect` = `refresh_failed_at is not None`; `binding` = `wire.binding_id(self.id)`.
  - `ConnectionGrant`: unchanged fields; `clean()` restriction removed.

- [ ] **Step 1: Write the failing tests**

```python
TOKEN_COLUMNS = {"refresh_token_enc", "access_token_enc", "access_token_expires_at", "access_token_seconds"}

def test_fernet_is_used_only_for_the_app_client_secret():
    for path in pathlib.Path("apps/connections").rglob("*.py"):
        tree = ast.parse(path.read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", getattr(n.func, "attr", "")) in {"encrypt_secret", "decrypt_secret"}]
        if path.name == "models.py":
            assert all(_inside(tree, c, "ConnectionApp", "client_secret") for c in calls), path
        else:
            assert calls == [], path

def test_no_connection_column_holds_a_token():
    assert not {f.name for f in Connection._meta.get_fields()} & TOKEN_COLUMNS

def test_old_token_routes_are_gone(client):
    assert client.post("/api/me/connections/salesforce/token").status_code == 404
    assert client.post(f"/api/harness/runners/{rid}/turns/{tid}/connection-token").status_code == 404

def test_a_person_connection_may_be_granted():
    ConnectionGrant.objects.create(connection=person_conn, agent=agent)  # no ValidationError
```

- [ ] **Step 2:** `uv run pytest tests/test_connections_hold_nothing.py tests/test_connections_model.py -v` — FAIL.
- [ ] **Step 3:** Remove the listed code; 0006 RunPython only; 0007 removes and recreates `ConnectionGrant` and `Connection` (empty after 0006) with the fields above, adds `ConnectionApp.removed_at`. `services.complete` temporarily raises `ConnectionError("provider_error")` after the checks until A3.
- [ ] **Step 4:** `uv run pytest tests/test_connections_* tests/test_migrations_apply.py -q` — PASS.
- [ ] **Step 5:** Commit `"connections: canopy-web stores no token; sealed connection rows"`.

### Task A3: Connect seals the token to its owner

**Files:**
- Modify: `apps/connections/services.py` (`begin`, `complete`, `_discard_grant`), `apps/connections/views.py` (start: R8; callback: redirect to `/w/<ws>/connections/<id>/share`)
- Test: `tests/test_connections_connect.py` (replaces the connect cases of `tests/test_connections_services.py`, `tests/test_connections_google.py`, `tests/test_connections_views.py`)

**Interfaces:**
- Consumes: `zk_seal.seal_to(plaintext, *, agent_id, name, kv, recipients)`, `device_keys.thumbprint`, `UserVaultKeyset`, `wire.binding_id`.
- Produces: `services.owner_keyset(owner_kind: str, owner, by) -> UserVaultKeyset` (the person's own, or for an agent the connecting admin's; 409 `no_vault_key` when none); `services.complete(session, *, provider, workspace, code, state, redirect_uri, by) -> Connection` keeps its checks (bad_state, exchange, identity, no refresh token, `insufficient_scope`, `check_owner` → `wrong_account`, app changed) and now: `_discard_grant(p, subject, token)` revokes only when `p.name != "google"`; builds the Part 0.2 `refresh` plaintext (`owner_key = thumbprint(keyset.public_jwk)`, `client_id = app.client_id`, `login_host` = host of the app's `login_url`); seals it with `kv=0`, name `refresh`, to `[("member", str(keyset.user_id), keyset.public_jwk)]`; upsert by `(owner, app, account_id)` sets `sealed`, `sealed_sig=None`, `sealed_digest`, `access_sealed=""`, `access_expires_at=None`, `owner_key`, `signer`, `state=UNSHARED`, `refresh_failed_at=None`; drops the plaintext tokens; records `connected`.

- [ ] **Step 1: Write the failing tests**

```python
def test_connect_stores_only_ciphertext_sealed_to_the_owner(world, fake_provider):
    conn = connect(world.person)
    assert conn.sealed.startswith("zks1:") and zk_seal.sealed_targets(conn.sealed) == {("member", str(world.person.pk))}
    assert FAKE_REFRESH not in json.dumps(model_to_dict(conn), default=str)
    assert conn.state == "unshared" and conn.access_sealed == ""
def test_the_owner_opens_it_and_finds_the_spec_plaintext(world, fake_provider):
    plain = json.loads(ref_open(conn.sealed, conn.binding, "refresh", world.person_priv))
    assert set(plain) == {"v", "provider", "refresh_token", "client_id", "login_host", "instance_url",
                          "account_id", "owner_key", "issued_at", "scopes", "statement_floor"} and "client_secret" not in plain
def test_an_agent_connection_is_sealed_to_the_connecting_admin(world, fake_provider): ...
def test_start_without_a_vault_key_is_refused_and_says_where_to_set_one_up(world): ...   # no_vault_key
def test_google_wrong_domain_is_dropped_without_revoking(world, fake_provider):
    connect(world.person, provider="google", email="x@gmail.com", allowed="dimagi.com")
    assert fake_provider.revokes == [] and Connection.objects.count() == 0
def test_google_agent_own_account_mismatch_is_dropped_without_revoking(world, fake_provider): ...
def test_salesforce_refusal_is_revoked(world, fake_provider): ...
def test_insufficient_scope_is_refused(world, fake_provider): ...
def test_connecting_the_same_account_again_replaces_the_envelope_and_unshares(world, fake_provider): ...
def test_callback_redirects_to_the_share_screen(world, fake_provider): ...
def test_unshared_connection_is_listed_as_not_shared_yet(world, fake_provider):   # Review Focus 5
    row = my_connections(world)[0]
    assert row["state"] == "unshared" and row["statement"]["agents"] == [] and row["holders"] == []
```

- [ ] **Step 2:** `uv run pytest tests/test_connections_connect.py -v` — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `"connections: canopy-web finishes the sign-in and seals the token to its owner"`.

### Task A4: Recipients and sharing (statement + browser-sealed envelope)

**Files:**
- Create: `apps/connections/recipients.py`, `apps/connections/sharing.py`
- Modify: `apps/agents/credentials/accounts.py` (`may_hold_for(runner, person, *, own_use: bool = False)`: `own_use` admits `Runner.COMPUTER` owned by `person`), `apps/connections/api.py` (routes 2, 3, 4), `apps/connections/schemas.py` (`ShareStateOut`, `ShareIn`, `ConnectionOut` additions)
- Test: `tests/test_connections_recipients.py`, `tests/test_connections_sharing.py`

**Interfaces:**
- Consumes: A1 `check_statement`, `verify_statement`, `check_writer_sig`; `accounts.delegated_devices`, `services.runner_may_hold_agent`, `zk_endorse.endorsements`, `vault_engine.value_digest`.
- Produces (`recipients.py`): `grants_in_force(conn) -> list[Agent]` (statement agents ∩ `ConnectionGrant` rows, owner agent included for agent-owned; each still in `conn.app.workspace` and, person-owned, still administered by the person); `devices(conn, statement: dict | None = None) -> list[RunnerDeviceKey]` (agent-owned: ECDH-ready keys on runners serving each in-force agent with `runner_may_hold_agent`; person-owned: `accounts.delegated_devices(agent, person)` per in-force agent, plus the person's ACTIVE ECDH-approved keys with `ecdh_jkt ∈ statement.my_computers` via `may_hold_for(..., own_use=True)`); `owner_member(conn) -> tuple[str, str, dict] | None` (`signer`'s keyset while its thumbprint == `owner_key`); `recipients(conn, statement=None) -> list[tuple[str, str, dict]]`; `holders(conn) -> list[RunnerDeviceKey]` (devices in `sealed_targets(sealed)`); `waiting(conn) -> list[dict]`; `prune(conn) -> list[str]` (`zk_seal.keep_only` on both envelopes + `wire.keep_only_sig`, under row lock; never drops the last device holder).
- Produces (`sharing.py`): `share_state(conn, user) -> dict` (`ShareStateOut`; candidates = `recipients(conn, statement=<all the user may choose>)` labelled, with `mine`); `share(conn, *, user, statement, sig, by_jkt, sealed, sealed_sig, access_sealed=None, access_sig=None, expect) -> Connection`. Rules: `user`'s keyset thumbprint == `by_jkt` == `conn.owner_key` (403 `not_the_owner_key`); signature verifies (422 `bad_signature`); version > `statement_version` (409 `statement_stale`); added agents administered by `user` (403 `not_an_admin`) and in the app's workspace (422 `agent_outside_workspace`); `my_computers` ⊆ `user`'s own enrolled keys (422 `not_your_machine`; agent-owned must be empty); agent-owned includes the owner agent (422 `statement_missing_owner_agent`); `expect == sealed_digest` (409 `zk_value_changed`); new targets ⊆ `recipients(conn, statement)` and include the owner member (422 `foreign_reader`); `check_writer_sig` with `by == owner member` (422 `bad_writer_sig`); diff `ConnectionGrant` rows (`granted` / `grant_revoked`); store; `state = ACTIVE`; record `shared` with `version`; then `prune(conn)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_agent_owned_recipients_are_its_machines_and_the_connecting_admin(world): ...
def test_person_owned_lent_to_two_agents_reaches_both_agents_cloud_boxes(world): ...
def test_my_computers_admits_the_persons_computer_runner_only_when_listed(world): ...
def test_another_admins_laptop_is_never_a_recipient(world): ...
def test_grant_out_of_force_narrows_recipients(world):          # Review Focus 3
    revoke_admin(world.person, world.echo)
    assert world.echo_box.ecdh_jkt not in {t for _, t, _ in recipients(conn)}
def test_prune_never_drops_the_last_device_holder(world): ...

def test_share_signs_seals_and_activates(world):
    conn = sharing.share(conn, user=world.person, **browser_share(world, agents=["eva", "echo"]))
    assert conn.state == "active" and {("device", world.eva_box.ecdh_jkt), ("member", str(world.person.pk))} <= zk_seal.sealed_targets(conn.sealed)
    assert set(conn.grants.values_list("agent__slug", flat=True)) == {"eva", "echo"} and recorded("connections.shared", version=1)
def test_share_sealed_to_a_device_the_statement_does_not_allow_is_refused(world):   # 422 foreign_reader
def test_share_without_writer_mac_is_refused(world):                                 # 422 bad_writer_sig
def test_share_signed_by_another_admin_is_refused(world):                            # 403 not_the_owner_key
def test_an_older_or_equal_version_is_refused(world):                                # 409 statement_stale
def test_lending_to_an_agent_you_do_not_administer_is_refused(world):                # 403 not_an_admin
def test_my_computers_must_be_the_signers_own_machines(world):                       # 422 not_your_machine
def test_agent_owned_statement_must_list_the_owner_agent(world):                     # 422 statement_missing_owner_agent
def test_share_needs_a_passkey_step_up(world):                                       # 403 step_up_required
def test_share_state_is_the_owners_only(world): ...                                  # 403 for anyone else
```

- [ ] **Step 2:** `uv run pytest tests/test_connections_recipients.py tests/test_connections_sharing.py -v` — FAIL.
- [ ] **Step 3:** Implement; route 3 `@gate("self")`, route 4 `@gate("self", setting=True, step_up=True)`; agent-owned also needs `agent.is_admin(user)`.
- [ ] **Step 4:** Run with `tests/test_delegated_tokens.py tests/test_google_delegation.py -q` (`may_hold_for` default unchanged) — PASS.
- [ ] **Step 5:** Commit `"connections: one recipients rule; the owner shares with a signed statement"`.

### Task A5: Material, refresh lease and write-back

**Files:**
- Create: `apps/connections/tokens.py`, `apps/connections/proofs.py`, `apps/connections/api_runner.py` (router mounted at `/harness` in `apps/api/api.py`)
- Modify: `apps/connections/api.py` (routes 7–10), `apps/connections/api_turns.py` (route 11 beside the kept listing), `apps/connections/schemas.py`, `apps/mcp/api_tools.py` (`EXCLUDED` += routes 7–15 operation ids: "sealed credentials and an app secret for a machine, never a model's context")
- Test: `tests/test_connections_tokens.py`

**Interfaces:**
- Consumes: A4 `recipients`, `holders`; A1 `check_writer_sig`; `vault_engine.value_digest`; `ConnectionApp.client_secret`.
- Produces (`proofs.py`): `proving_device(request, runner) -> RunnerDeviceKey`; `proving_machine(request, user) -> RunnerDeviceKey` (key's runner owned by `user`, any kind incl. `computer`; else 403 `machine_not_enrolled`).
- Produces (`tokens.py`): `LEASE_SECONDS = 60`; `material(conn, device_key, *, turn=None, runner=None) -> dict` (`not_connected` for `unshared`/`revoking`, `needs_reconnect`, `waiting` when the key is not in `sealed_targets`; adds `app = {client_id, client_secret, options}` from the `ConnectionApp`; records `token_issued` coalesced per turn; sets `last_used_at`); `take_lease(conn, device_key, lease_id=None) -> dict` (`{lease_id, ttl_seconds, expires_at}`; same device + same id renews; another live holder → 409 `lease_held` with `retry_after`; taking over an expired lease with `lease_written=False` records `lease_expired_unwritten`); `release_lease(conn, device_key, lease_id) -> None`; `write_back(conn, *, device_key, lease_id, access_sealed, access_sig, access_expires_at, expect, rotated, sealed=None, sealed_sig=None) -> str` (row lock: lease id matches and nobody took it since, even if expired, else 409 `lease_lost`; not `revoking` else 409; `expect == sealed_digest` else 409 `zk_value_changed`; `by.target == device_key.ecdh_jkt`; targets ⊆ `recipients(conn)` (422 `foreign_reader`) and ⊇ current still-eligible recipients (422 `zk_rewrap_required`); store; `lease_written=True`; record `refreshed` with `rotated`, `machine`); `mark_reconnect_required(conn, device_key, lease_id, error) -> None` (only `invalid_grant` stamps `refresh_failed_at`; records `reconnect_required`).

- [ ] **Step 1: Write the failing tests**

```python
def test_material_is_ciphertext_plus_the_app_client(world):
    body = material_for(world.eva_box).json()
    assert body["sealed"].startswith("zks1:") and body["app"]["client_secret"] == "app-secret"
    assert FAKE_REFRESH not in json.dumps(body)
def test_material_without_device_proof_is_401(world): ...
def test_material_for_a_waiting_machine_is_409_waiting(world): ...
def test_material_refused_for_an_agent_whose_lender_left(world): ...       # Review Focus 3 → not_connected
def test_unshared_connection_is_handed_to_no_machine(world): ...           # Review Focus 5 → 409 not_connected
def test_material_read_records_token_issued_once_per_turn_with_runner(world): ...
def test_a_rotated_app_secret_reaches_the_next_material_read(world): ...   # §4.4
def test_one_lease_holder_at_a_time(world):
    assert lease(world.box_a).status_code == 200 and lease(world.box_b).json()["error"] == "lease_held"
def test_lease_answers_ttl_seconds_not_only_a_timestamp(world):            # Review Focus 1
    assert lease(world.box_a).json()["ttl_seconds"] == 60
def test_taking_an_expired_unwritten_lease_records_lease_expired_unwritten(world): ...
def test_write_back_with_a_lost_lease_is_refused(world): ...               # 409 lease_lost
def test_write_back_cas_on_sealed_digest(world): ...                       # 409 zk_value_changed
def test_write_back_must_keep_every_still_eligible_recipient(world): ...   # 422 zk_rewrap_required
def test_write_back_to_a_stranger_is_refused(world): ...                   # 422 foreign_reader
def test_write_back_to_a_revoking_connection_is_refused(world): ...        # Review Focus 2
def test_rotated_write_back_records_refreshed_rotated_true(world): ...
def test_invalid_grant_marks_needs_reconnect_other_errors_do_not(world): ...
def test_person_material_needs_a_plain_pat_and_their_own_machine(world): ...
def test_person_material_refuses_a_machine_not_on_my_computers(world):
    assert r.json()["error"] == "not_on_my_computers" and "Use on my computers" in r.json()["detail"]
def test_ambiguous_person_material_names_the_workspaces(world): ...
```

- [ ] **Step 2:** `uv run pytest tests/test_connections_tokens.py -v` — FAIL.
- [ ] **Step 3:** Implement. Gates: route 11 `@gate("runner", "runner-holds-agent", kind="runner-protocol", step_up="runner protocol: ciphertext and an app client for a box")`; route 12 `@gate("runner", kind="runner-protocol", step_up=…)` with an in-function check that the box serves an in-force agent (`runner_may_hold_agent`); person routes 7–10 `@gate("self", kind="ephemeral-credential", step_up="a person's own machine; PAT and device proof only")` + `_plain_pat`.
- [ ] **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `"connections: material, one refresh lease, and compare-and-swap write-back"`.

### Task A6: Keeper work, revoking and sweeps

**Files:**
- Create: `apps/connections/keeper.py`, `apps/connections/scheduler.py` (registers `keeper.sweep` on `apps/common/ticks.py`, as `apps/capabilities/scheduler.py`), `apps/connections/management/commands/connections_sweep.py`
- Modify: `apps/connections/api_runner.py` (routes 13–15), `apps/connections/services.py` (`disconnect`, `remove_app`), `apps/connections/agent_move.py`
- Test: `tests/test_connections_keeper.py`, `tests/test_connections_agent_move.py` (adapt)

**Interfaces:**
- Consumes: A4 `recipients`, `holders`, `waiting`, `prune`; A5 lease fields; A1 `check_writer_sig`.
- Produces (`keeper.py`): `REVOKE_GRACE = timedelta(days=7)`; `work(runner) -> list[dict]` (`ConnectionKeeperWorkOut` for connections this box's device holds that are `revoking`, or `active` with `waiting(conn)` non-empty; endorsements of every in-force agent tagged `agent_id`); `reseal(conn, runner, device_key, *, sealed, sealed_sig, access_sealed, access_sig, expect) -> list[str]` (holder in old and new targets else 403 `zk_not_a_holder`; other checks as `write_back`; records `resealed`); `confirm_revoked(conn, runner, device_key, *, revoked: bool, error: str) -> None` (`revoking` else 409 `not_revoking`; deletes; records `revoked` or `revoke_failed`); `sweep(now=None) -> dict` (`revoking` older than 7 days → delete + `revoke_unconfirmed`; apps with `removed_at` and no connections → delete).
- `services.disconnect(conn, *, by) -> None`: no device holder → delete at once + `revoke_unconfirmed` (R7); else `state=REVOKING`, `revoking_since=now`, `revoke_at_provider = not` another non-revoking connection of the same `app` + `account_id`; records `disconnected`. `services.remove_app(app, *, by)`: `disconnect` each, set `removed_at`.

- [ ] **Step 1: Write the failing tests**

```python
def test_keeper_work_lists_only_connections_this_box_holds(world): ...
def test_keeper_reseal_adds_a_waiting_box_and_records_resealed(world): ...
def test_keeper_reseal_by_a_non_holder_is_refused(world): ...                  # 403 zk_not_a_holder
def test_disconnect_hides_the_row_at_once_and_hands_it_out_nowhere(world): ...
def test_disconnecting_an_unshared_connection_deletes_it_at_once(world):
    services.disconnect(unshared, by=world.person)
    assert not Connection.objects.exists() and recorded("connections.revoke_unconfirmed")
def test_revoke_at_provider_false_when_another_connection_of_that_account_remains(world): ...
def test_first_holder_confirms_revoked_and_the_row_is_deleted(world): ...
def test_revoking_for_seven_days_deletes_and_records_revoke_unconfirmed(world): ...
def test_removing_an_app_revokes_its_connections_then_deletes_the_app(world): ...
def test_moved_agent_loses_its_grants(world): ...
```

- [ ] **Step 2:** `uv run pytest tests/test_connections_keeper.py tests/test_connections_agent_move.py -v` — FAIL.
- [ ] **Step 3:** Implement. Gates: 13 `@gate("runner", step_up="runner protocol: ciphertext only machines open")`; 14 `@gate("runner", setting=True, step_up=…)`; 15 `@gate("runner", kind="runner-protocol", step_up=…)`; all with device proof.
- [ ] **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit `"connections: keepers re-seal, holders revoke, nothing waits forever"`.

### Task A7: Audit, shared names, OpenAPI

**Files:**
- Modify: `apps/connections/services.py` (`AUDITED` += `shared`, `refreshed`, `resealed`, `revoked`, `revoke_unconfirmed`, `lease_expired_unwritten`; `token_issued` keys `to`, `runner`, `machine`, `turn`), `apps/connections/__init__.py` docstring (the contract), `apps/connections/providers/__init__.py` docstring (recipe: "a provider is this module plus `orchestrator/connections/providers/<name>.py` in the canopy CLI")
- Modify: `tests/test_connections_audit.py` (full run, A3–A6 fixtures: no `refresh_token`, `access_token`, verifier or code in any log, Event or response; the app client secret only in material responses)
- Create: `tests/test_connections_shared_names.py`
- Regenerate: `frontend/src/api/generated.ts`

**Interfaces:**
- Produces: `SHARED = {"salesforce": {"required_scopes": ("api", "refresh_token"), "rotates_refresh_token": True}, "google": {"required_scopes": ("openid", "https://www.googleapis.com/auth/userinfo.email"), "rotates_refresh_token": False}}` — the CLI pins the same dict (B2).

- [ ] **Step 1: Write the failing tests**

```python
def test_provider_shared_names_match_the_cli_contract():
    for name, want in SHARED.items():
        cls = providers.get_class(name)
        assert (cls.required_scopes, cls.rotates_refresh_token) == (want["required_scopes"], want["rotates_refresh_token"])
def test_new_verbs_are_audited():
    assert {"shared", "refreshed", "resealed", "revoked", "revoke_unconfirmed", "lease_expired_unwritten"} <= services.AUDITED
def test_full_connection_lifecycle_writes_no_plaintext_token(full_run): ...
def test_app_secret_appears_only_in_material_responses(full_run): ...
```

- [ ] **Step 2:** `uv run pytest tests/test_connections_audit.py tests/test_connections_shared_names.py -v` — FAIL.
- [ ] **Step 3:** Implement; `cd frontend && npm run gen:api:local`.
- [ ] **Step 4:** `uv run pytest -q` (whole suite, incl. `tests/test_every_route_declares_its_gate.py`, `tests/test_architecture_boundary.py`, `tests/test_layered_app_imports.py`) — PASS; `cd frontend && npm run build` — PASS.
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
- Produces: `REFRESH_NAME`, `ACCESS_NAME`, `binding_id(connection_id: str) -> str`; `class WireError(Exception)`; `statement_msg(st: dict) -> bytes`; `verify_statement(st: dict, sig: str, signer_jwk: dict) -> bool`; `seal_envelope(binding: str, name: str, plaintext: dict, recipients: list[tuple[str, str, dict]], *, writer: EcdhKey, writer_target: str, expect: str) -> tuple[str, dict]`; `open_envelope(value: str, sig: dict | None, *, binding: str, name: str, me_kind: str, me_target: str, ecdh: EcdhKey) -> tuple[dict, dict]` (raises `WireError("unsigned")` for `sig is None`, `WireError("bad-mac")` on a wrong MAC); `digest(value: str) -> str` (= canopy-web `value_digest`).

- [ ] **Step 1: Write the failing tests**

```python
def test_vectors_match_the_canopy_web_reference(): assert json.loads(VECTORS.read_text()) == ref.vectors()
def test_statement_msg_and_signature_match_vectors(): ...
def test_seal_then_open_round_trips_and_names_the_writer(ecdh_pair):
    value, sig = wire.seal_envelope(B, "refresh", PLAIN, [("device", me.jkt, me.public_jwk)], writer=me, writer_target=me.jkt, expect="")
    plain, by = wire.open_envelope(value, sig, binding=B, name="refresh", me_kind="device", me_target=me.jkt, ecdh=me)
    assert plain == PLAIN and by["target"] == me.jkt
def test_open_refuses_an_unsigned_envelope(): ...                        # canopy-web's first seal is for the owner only
def test_open_refuses_a_mac_made_by_another_key_claiming_the_writer(): ...
def test_a_refresh_envelope_does_not_open_as_access(): ...
def test_pruning_another_recipient_keeps_my_mac_valid(): ...
```

- [ ] **Step 2:** `cd $CLI && uv run pytest tests/test_connections_wire.py -v` — FAIL. **Step 3:** Implement Part 0.3–0.4. **Step 4:** PASS.
- [ ] **Step 5:** Commit `"connections: grant statements and writer-authenticated envelopes"`.

### Task B2: Provider modules — refresh and revoke

**Files:**
- Create: `$CLI/src/orchestrator/connections/providers/__init__.py`, `base.py`, `salesforce.py`, `google.py`
- Test: `$CLI/tests/test_connection_providers.py`

**Interfaces:**
- Produces (`base.py`): `class ProviderError(Exception)(code: str, detail: str)`; `@dataclass Tokens(access_token, refresh_token, expires_at: int, scope: str)`; `class Provider` (`name`, `required_scopes`, `rotates_refresh_token`; `check_plain(plain: dict) -> None` (host rules; `ProviderError("bad-host")`), `refresh(plain: dict, app: dict) -> Tokens` (`app["client_id"] == plain["client_id"]` else `ProviderError("client-mismatch")`), `revoke(plain: dict, app: dict) -> None`); `get(name) -> Provider`; HTTP injectable (`requests`, 15 s timeout).
- `salesforce.py`: `LOGIN_HOSTS = ("login.salesforce.com", "test.salesforce.com")`, `MY_DOMAIN_SUFFIX = ".my.salesforce.com"`; `login_host_ok(host) -> bool`; `instance_ok(url) -> bool`; endpoints `https://{plain.login_host}/services/oauth2/token|revoke`; no `expires_in` → `expires_at = now + app.options.access_token_seconds` (default 1800); a rotated `refresh_token` returned in `Tokens`.
- `google.py`: fixed `TOKEN_URL`, `REVOKE_URL` (canopy-web's constants); `rotates_refresh_token = False`.
- `SHARED` identical to A7.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("host,ok", [("login.salesforce.com", True), ("test.salesforce.com", True),
    ("acme.my.salesforce.com", True), ("acme.sandbox.my.salesforce.com", True), ("my.salesforce.com", False),
    ("evil.com", False), ("login.salesforce.com.evil.com", False), ("acme.my.salesforce.com.evil.com", False)])
def test_login_host_rule(host, ok): assert salesforce.login_host_ok(host) is ok

@pytest.mark.parametrize("url,ok", [("https://acme.my.salesforce.com", True), ("http://acme.my.salesforce.com", False),
    ("https://na1.salesforce.com", False), ("https://acme.my.salesforce.com.evil.com", False)])
def test_instance_url_rule(url, ok): assert salesforce.instance_ok(url) is ok

def test_refresh_endpoint_comes_from_the_sealed_login_host_never_the_server(http):
    salesforce.Provider().refresh(PLAIN, {**APP, "options": {"login_url": "https://evil.example", "token_url": "https://evil/"}})
    assert http.calls[0].url == "https://login.salesforce.com/services/oauth2/token"
def test_a_plaintext_naming_a_bad_host_is_refused_before_any_call(http):
    with pytest.raises(ProviderError) as e: salesforce.Provider().refresh({**PLAIN, "login_host": "evil.com"}, APP)
    assert e.value.code == "bad-host" and http.calls == []
def test_a_client_id_other_than_the_sealed_one_is_refused(http): ...      # client-mismatch
def test_salesforce_without_expires_in_lives_access_token_seconds(http): ...
def test_google_endpoints_are_constants(http): ...
def test_shared_names_match_canopy_web(): ...
```

- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `"connections: provider refresh and revoke; endpoints from rules only"`.

### Task B3: Trust — who a box will seal to

**Files:**
- Create: `$CLI/src/orchestrator/connections/trust.py`
- Test: `$CLI/tests/test_connection_trust.py`

**Interfaces:**
- Produces: `PINS_FILE = ~/.canopy/connection-pins.json` (`{connection_id: {"owner_jkt", "version"}}`); `class TrustError(Exception)(code: str, detail: str)`; `verified_statement(material: dict, *, owner_jkt: str, pins: dict) -> dict` (owner key thumbprint == `owner_jkt`; signature verifies; version ≥ pinned else `TrustError("statement-rollback")`; pins it); `allowed(material: dict, statement: dict, *, owner_jkt: str, res: KeepResult) -> dict[tuple[str, str], dict]` (Part 0.5); `refuse_unallowed(rows: list[dict], allowed: dict) -> list[tuple[str, str, dict]]` (`TrustError("zk_unendorsed_recipient")`); `writer_allowed(by: dict, material: dict, statement: dict, *, owner_jkt: str) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_device_not_in_the_statement_is_refused_even_when_canopy_web_lists_it(material):
    with pytest.raises(TrustError) as e:
        trust.refuse_unallowed(material["recipients"] + [STRANGER_ROW], trust.allowed(material, st, owner_jkt=O, res=R))
    assert e.value.code == "zk_unendorsed_recipient"
def test_an_endorsement_for_an_agent_not_in_the_statement_admits_nothing(material): ...
def test_an_endorsement_subject_is_checked_against_the_statement_not_the_payload(material): ...
def test_my_computers_admits_exactly_the_listed_thumbprints(material): ...
def test_a_member_other_than_the_owner_is_never_allowed(material): ...
def test_a_statement_signed_by_a_key_other_than_owner_key_is_refused(material): ...
def test_an_older_statement_than_one_seen_is_refused(material): ...   # statement-rollback
def test_writer_must_be_allowed_or_the_owner(material): ...
```

- [ ] **Step 2–4:** Run → FAIL → implement → PASS.
- [ ] **Step 5:** Commit `"connections: a box seals only where the owner's signed statement allows"`.

### Task B4: Getting an access token — lease, refresh, write-back

**Files:**
- Create: `$CLI/src/orchestrator/connections/access.py`
- Modify: `$CLI/src/orchestrator/cred_cli.py` (`connection_token`, `stage_connection_token`, `_refusal`; keep `connection_token_path`, `_write_connection_token`, `staged_connection_token`, the options and the `salesforce-token` alias)
- Test: `$CLI/tests/test_connection_access.py`, `$CLI/tests/test_cred_cli.py` (adapt connection cases)

**Interfaces:**
- Consumes: B1–B3; `device_key.load_ecdh`; `canopy_web.call` (adds device proof); `runner_session.token`.
- Produces: `WAIT_SECONDS = 30`, `POLL_SECONDS = 2`; `@dataclass Paths(material: str, lease: str, sealed: str, reconnect: str)`; `access_token(paths: Paths, *, min_life: int, call, ecdh, pins: dict, refresh: bool = False, sleep=time.sleep, monotonic=time.monotonic, http=None) -> dict` → `{access_token, instance_url, expires_at, account_label}`. Pins are saved after a validated read (writer check and plaintext check passed, including `statement_floor` and the `access` `owner_key`), never before, and not only after a write. Algorithm (§4.2): GET material; `verified_statement` + `open_envelope` both envelopes with `writer_allowed` (B3); `check_plain`; save the pin; fresh `access` with more than `min_life` left and not `refresh` → return; else POST lease → on `lease_held` poll material every `POLL_SECONDS` up to `WAIT_SECONDS` for a newer `access_sealed`, else `CredError("refresh in progress elsewhere")`; with the lease: `provider.refresh(plain, material.app)`; build the new `access` (and `refresh` if rotated); seal to the current allowed recipients (`refuse_unallowed` over `material.recipients`, keeping every still-allowed current target); PUT write-back with `expect = sealed_digest`; on failure retry the PUT from memory up to 3 times, renewing the lease on the monotonic clock from `ttl_seconds`; return only after it lands. `invalid_grant` → POST reconnect-required, `CredError(…, EXIT_REFUSED)`. 404 material → exit 3; 409 `not_connected`/`waiting`/`revoking` → exit 3 in runner mode.
- `stage_connection_token(provider, slug, runner_id, turn_id, *, token=None, min_life=None, refresh=False) -> Path` keeps its signature (runner routes 11–12); `connection_token(provider, *, refresh=False, workspace="") -> Path` uses PAT + device proof against routes 7–10 and prints the server's sentence plus `canopy vault device --enroll` where it applies.

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
def test_forged_access_sealed_for_a_stranger_account_is_refused(fake_web): ...
def test_unsigned_envelope_is_refused_by_a_machine(fake_web): ...
def test_access_sealed_written_by_an_unlisted_device_is_refused(fake_web): ...
def test_write_back_never_adds_a_recipient_the_statement_does_not_allow(fake_web, http): ...
def test_revoking_answer_stages_nothing(fake_web): ...                                   # Review Focus 2
def test_material_404_is_not_connected_exit_3(fake_web): ...
def test_person_path_without_an_enrolled_key_says_how_to_enroll(fake_web):
    assert "canopy vault device --enroll" in str(err)
def test_person_path_refused_when_use_on_my_computers_is_off(fake_web): ...
def test_staged_file_format_is_unchanged(fake_web, tmp_home):
    assert set(json.loads(path.read_text())) == {"access_token", "instance_url", "expires_at", "account_label"}
    assert oct(path.stat().st_mode & 0o777) == "0o600"
```

- [ ] **Step 2:** `uv run pytest tests/test_connection_access.py tests/test_cred_cli.py -v` — FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `"connections: refresh on the box under one lease, written back before use"`.

### Task B5: The keeper's connection work and revokes

**Files:**
- Modify: `$CLI/src/orchestrator/vault_keeper.py` (`run_keep` also GETs `/api/harness/runners/<id>/vault-keeper/connections`; docstring gains a Connections paragraph)
- Create: `$CLI/src/orchestrator/connections/keep.py`
- Test: `$CLI/tests/test_connection_keeper.py`

**Interfaces:**
- Consumes: B1–B3; B2 `revoke`; `vault_keeper.KeepResult`.
- Produces: `keep_connection(work: dict, *, pins: dict, call, ecdh, http=None) -> KeepResult`. `revoking`: open `sealed`, revoke at the provider when `revoke_at_provider` (B2 rules, `work.app`), POST `revoked_path {revoked, error}`. Pins are saved after a validated read (writer check and plaintext check passed), never before, and not only after a write. Else: verify statement + writer (B3); targets = current still-allowed ∪ waiting machines that are `allowed`; re-seal the same plaintexts (both envelopes, `statement_floor` and `owner_key` unchanged) with this box as writer and `expect`; PUT `reseal_path`.

- [ ] **Step 1: Write the failing tests**

```python
def test_keeper_reseals_for_a_waiting_endorsed_box(fake_web): ...
def test_keeper_refuses_device_not_in_statement_even_if_server_lists_it(fake_web):
    res = keep_connection(work_with_waiting(STRANGER), ...)
    assert fake_web.puts == [] and "zk_unendorsed_recipient" in " ".join(res.skipped)
def test_keeper_revokes_then_confirms(fake_web, http): ...
def test_keeper_skips_provider_revoke_when_told_another_connection_remains(fake_web, http): ...
def test_keeper_refuses_a_forged_envelope_and_writes_nothing(fake_web): ...
def test_run_keep_includes_connections(fake_web): ...
```

- [ ] **Step 2–4:** Run → FAIL → implement → `uv run pytest tests/test_connection_keeper.py tests/test_vault_keeper.py -q` PASS.
- [ ] **Step 5:** Commit `"connections: keepers re-seal and revoke connections they hold"`.

### Task B6: CLI release

**Files:** `$CLI/CHANGELOG.md`, `$CLI/CLAUDE.md` (cred command notes), version files via `canopy version bump`
- [ ] **Step 1:** `cd $CLI && uv run pytest -q` — PASS.
- [ ] **Step 2:** `canopy version bump && canopy version verify` — PASS; record the version `0.2.N` for C1's `CONNECTIONS_ZK_MIN_CLI`.
- [ ] **Step 3:** Commit `"release: connections refreshed on runners"`, open the PR, `gh pr merge <n> --auto`. canopy-web must not deploy before this release (Part E).

---

# Part C — cloud runner (`runner/ec2` in canopy-web)

### Task C1: Staging through the new CLI path

**Files:**
- Modify: `runner/ec2/cloud_runner.py` connections block (~1791–2025): keep `_stage_connections`, holds, heartbeat, adoption, cleanup and `_stage_connection_token`'s command line; exit 3 stays silent; add `CONNECTIONS_ZK_MIN_CLI = (0, 2, N)` (B6) and one `connections.stage_failed … reason="canopy CLI too old for connections"` when `_canopy_cli_version()` is older
- Test: `runner/ec2/tests/test_connection_token_staging.py` (adapt; nothing assumes a server-issued token)

- [ ] **Step 1: Write the failing tests**

```python
def test_stage_runs_the_cli_with_min_life_900(fake_run): assert fake_run.argv[-2:] == ["--min-life", "900"]
def test_waiting_for_a_holder_is_silent_and_leaves_no_file(fake_run): ...   # exit 3
def test_old_cli_logs_one_stage_failed_line(fake_run, caplog): ...
def test_heartbeat_restage_still_only_refreshes_held_files(fake_run): ...
```

- [ ] **Step 2–4:** `uv run pytest runner/ec2/tests/test_connection_token_staging.py -v` → FAIL → implement → `uv run pytest runner/ec2/tests -q` PASS.
- [ ] **Step 5:** Commit `"runner: stage connection tokens the box refreshes itself"`.

---

# Part D — frontend

### Task D1: API client and share crypto

**Files:**
- Modify: `frontend/src/api/connections.ts`
- Create: `frontend/src/vault/connectionShare.ts`, `frontend/src/vault/connectionShare.test.ts`

**Interfaces:**
- Produces (`connections.ts`): `getShareState(id: string): Promise<ShareStateOut>`, `putShare(id: string, body: ShareIn): Promise<ConnectionOut>`, `disconnectMine(id: string)`, `disconnectAgentConnection(slug: string, id: string)`; remove `grantConnection`, `revokeConnectionGrant`.
- Produces (`connectionShare.ts`): `statementMessage(st: Statement): string`; `openForOwner(state: ShareStateOut, me: Unlocked): Promise<{account: {account_id, provider, instance_url}; refresh: Bytes}>` (accepts `sealed_sig === null` only when `state.state === 'unshared'`, otherwise verifies the MAC; the plaintext stays inside this module); `chooseRecipients(state: ShareStateOut, choice: {agents: string[]; myComputers: string[]}, trusted: Trust): Recipient[]` (Part 0.5 with `vault/trust.ts`; a candidate without a trusted endorsement is left out and named); `buildShare(state: ShareStateOut, me: Unlocked, choice): Promise<ShareIn>` (next statement with `version + 1`, `endorsers` set to the people whose endorsements it trusts (`vault/trust.ts`) for the chosen machines, `statement_floor` in the re-sealed `refresh` set to that new version, `owner_key` kept and `statement_floor` set to that new version in `access` when it re-seals one, signs it as `endorse` does, re-seals `refresh` (and `access` when present) with `sealValue`, writes member-writer MACs, `expect = state.sealed_digest`).

- [ ] **Step 1: Write the failing tests** (vitest; vectors via `import raw from '../../../docs/vault/zk-v1/connections_v1_vectors.json?raw'`)

```ts
it('statementMessage matches the vector byte for byte', ...)
it('a statement signed here verifies with the reference key', ...)
it('accepts the unsigned first seal only while unshared', ...)
it('refuses an unsigned envelope once shared', ...)
it('leaves out a device whose endorsement is by nobody this browser trusts', ...)
it('seals to the owner and chosen devices with a MAC per recipient', ...)
it('never returns the refresh token to callers', () => expect(Object.keys(opened.account)).toEqual(['account_id', 'instance_url', 'provider']))
```

- [ ] **Step 2:** `cd frontend && npx vitest run src/vault/connectionShare.test.ts` — FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `"frontend: open, sign and share a connection in the owner's browser"`.

### Task D2: "Connected as …" — the share screen

**Files:**
- Create: `frontend/src/pages/connections/ShareConnectionPage.tsx` (+ test)
- Modify: `frontend/src/router.tsx` (`/w/:workspace/connections/:id/share`), `frontend/src/guide/surfaces.ts` (descriptor), `docs/architecture/pages.md` (row)

**Interfaces:**
- Consumes: D1, `useVaultUnlock`.
- Behaviour: one Unlock (the passkey touch) opens the envelope and shows "Connected as <account>" (from the opened plaintext); lists the agents the person administers in the app's workspace and "Use on my computers" with their enrolled machines (name + fingerprint); agent-owned: owning agent pre-checked and fixed. Share → `putShare(buildShare(...))` with the same unlocked key. "Not now" leaves it unshared with the line "Not shared yet: no agent or computer can use it". Devices left out by trust are named with why ("not vouched for by anyone you trust").

- [ ] **Step 1: Write the failing tests**

```ts
it('shows the account from the opened envelope before sharing', ...)
it('sends a signed statement and a re-sealed envelope with the chosen agents', ...)
it('Not now leaves it unshared and says so', ...)
it('resumes sharing from Settings', ...)            // Review Focus 5
```

- [ ] **Step 2–4:** Run → FAIL → implement → PASS; `npx vitest run src/routeTenancy.test.ts src/guide` PASS.
- [ ] **Step 5:** Commit `"frontend: connected as … — share with one touch"`.

### Task D3: Settings — lend, withdraw, use on my computers, waiting machines

**Files:**
- Modify: `frontend/src/components/settings/ConnectionsPanel.tsx` (+ test), `frontend/src/pages/agents/AgentConnectionsPanel.tsx` (+ test)

**Interfaces:**
- Consumes: D1 `buildShare`, `putShare`.
- Behaviour: every change (lend, withdraw, toggle a machine) = one Unlock + `putShare`. Rows show `unshared` ("Not shared yet" → Share, the D2 page), `waiting` ("waiting for a machine that holds it" + "Give waiting machines a copy", which re-shares with the same choices and `version + 1`), `needs_reconnect` (Reconnect = the existing start view), holders. `revoking` rows are never shown. No separate grant buttons.

- [ ] **Step 1: Write the failing tests**

```ts
it('lending re-signs and re-seals with the agent added', ...)
it('turning on Use on my computers lists this machine’s ECDH thumbprint', ...)
it('offers Give waiting machines a copy when no machine holds it', ...)
it('reconnect posts to the start view', ...)
```

- [ ] **Step 2–4:** Run → FAIL → implement → PASS; `cd frontend && npm run build` PASS.
- [ ] **Step 5:** Commit `"frontend: lend, withdraw and use on my computers, each one passkey touch"`.

---

# Part E — migration, docs, release order

### Task E1: Docs

**Files:**
- Modify: `docs/architecture/vault.md` (replace § "OAuth connections are server-held" with § "OAuth connections are sealed to their owner": the guarantee, connect-and-seal, sharing and the signed statement, envelope authentication, recipients, lease/write-back, revoking; update § Connection audit verbs; keep the log-line format text), `docs/architecture/api-surface.md` (§ Connections: Part 0.6), `docs/architecture/deploy.md` (each person reconnects once after deploy; boxes need CLI ≥ `CONNECTIONS_ZK_MIN_CLI`), `ARCHITECTURE.md` (`connections` row), `CLAUDE.md` (the invariant "A token from canopy's own OAuth flow …": canopy-web exchanges at connect and seals to the owner at once, stores no readable token, never refreshes; machines refresh under the owner's signed statement; the app client secret stays server-held — no dates or PR refs)
- Test: `tests/test_claude_md_budget.py`, `tests/test_old_address_in_docs.py`

- [ ] **Step 1:** Edit. **Step 2:** `uv run pytest tests/test_claude_md_budget.py tests/test_old_address_in_docs.py -q` — PASS.
- [ ] **Step 3:** Commit `"docs: connections are sealed to their owner and refreshed on runners"`.

### Release order (not a task)

1. CLI: merge B6; cloud boxes reinstall from the marketplace clone (`runner/ec2/bootstrap_agents.sh::reinstall_cli_from_marketplace_clone`).
2. canopy-web: Parts A, C, D, E in one PR; `gh pr merge <n> --auto`; then `gh workflow run "Deploy to Labs (AWS)" --ref main` (migrations 0006/0007 run automatically); confirm with `gh run list --workflow="Deploy to Labs (AWS)" --limit 3 --json headSha,conclusion` and `/api/openapi.json` listing `/api/connections/{id}/share`.
3. Each person reconnects once and shares on the "Connected as …" screen.
4. Verify: a turn's `canopy cred connection-token salesforce --agent <slug>` returns the staged path; `SELECT count(*) FROM connections_connection WHERE sealed NOT LIKE 'zks1:%'` is 0; a stage log shows no `stage_failed`.
