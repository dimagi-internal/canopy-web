# Connections canopy-web Can Use (Part 1: the switch) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A per-connection switch that lets canopy-web open, refresh and revoke a sealed connection through a KMS key, and a clear success screen after sign-in.

**Architecture:** canopy-web becomes a `device` recipient of the existing `zks1` envelopes; its P-256 key lives in KMS and is used only through `DeriveSharedSecret`. An optional `canopy_web` key in the owner's signed statement tells machines to accept its writes and keep it as a recipient. Refresh reuses the machines' lease and compare-and-swap.

**Tech Stack:** Django + django-ninja, boto3 KMS, CloudFormation, React/TypeScript (WebCrypto); the `canopy` CLI (Python) in its own repo.

**Spec:** `docs/superpowers/specs/2026-11-03-connections-server-use-calls-jobs-design.md` (Part 1). Background: `docs/superpowers/specs/2026-10-31-connections-zero-knowledge-design.md`.

**Worktrees:** canopy-web `~/emdash/worktrees/canopy-web-connections-server` (branch `feat/connections-server-use`). CLI: create `~/emdash/worktrees/canopy-connections-server` from canopy `origin/main` (branch `feat/connections-server-use`).

## Global Constraints

- Every route has `@gate` directly above its def; route docstrings are published OpenAPI; regenerate `frontend/src/api/generated.ts` the way CI does.
- `apps/harness` and `apps/agents` never import `apps/connections`.
- No token or plaintext in any event, log, exception or response. Never log a KMS derived secret.
- `server_use_default`: Salesforce `True`, Google `False`. `Provider.server_calls`: Salesforce `True`, Google `False`; the switch isn't shown when false.
- `CONNECTIONS_SERVER_KMS_KEY` unset → `server_key.available()` is false and every connection behaves as switched off.
- `CONNECTIONS_SERVER_STATEMENTS` (default false) gates accepting `canopy_web` in a share.
- Statement with the switch off stays byte-identical to today's: omit the key, never `null`.
- Screen copy is exact where the spec gives it, in plain sentences, no jargon ("envelope", "statement", "recipient", "KMS" never appear on a screen).
- Do not deploy, run CloudFormation or touch AWS. Deploying needs Jonathan's approval.

## Review Focus

1. KMS denied or down at connect → the sign-in still succeeds, sealed to owner only, `server_use=false`, and the screen says canopy-web can't use it. The refresh token is never lost. (Task 4)
2. Switch turned off on a shared connection → a machine that reads afterwards opens both envelopes without a `writer_allowed` refusal, since both are rewritten by the owner. (Task 7)
3. Salesforce rotates the refresh token during a canopy-web refresh → a machine then reads the server-written envelope under a statement naming it; a keeper reseal keeps canopy-web as a recipient. (Tasks 2, 5)
4. Two web workers refreshing at once → one lease, one provider call; the loser waits for the write up to 30 s. (Task 5)
5. Reconnect → an owner's off stays off, an on stays on, and the row is `unshared` until shared again. (Task 4)

---

### Task 1: The contract, canopy-web side

**Files:** Modify `docs/vault/zk-v1/connections_v1_reference.py`, `docs/vault/zk-v1/connections_v1_vectors.json`, `apps/connections/wire.py`. Test: the existing wire vector test (find it with `git grep connections_v1_vectors -- tests apps`).

**Interfaces:** Produces statement shape `{account_id, agents, connection, endorsers, my_computers, v, version, canopy_web?}`, where `canopy_web` is a 43-char b64u thumbprint.

- [ ] Add a vector for a statement with `canopy_web`, signed, and must-fail vectors: an empty string, a malformed thumbprint, `null`. Keep every existing vector byte-identical.
- [ ] Test: `wire` accepts a statement without the key and with a valid one, and refuses the three bad ones with `bad-statement`. Run it; it fails.
- [ ] Implement in the reference, regenerate the vectors, then `wire.py`.
- [ ] Run the wire tests and `frontend` `connectionShare.test.ts`; existing vectors still pass.
- [ ] Commit.

### Task 2: The CLI understands `canopy_web`

**Files (canopy repo):** `tests/fixtures/vault_zk/connections_v1_{reference.py,vectors.json}` (verbatim copies), `src/orchestrator/connections/wire.py`, `src/orchestrator/connections/trust.py`, `tests/test_connections_wire.py`, the trust test file beside it.

**Interfaces:** `trust._device_allowed` and `writer_allowed` admit `jkt == statement.get("canopy_web")`. `allowed()` then returns canopy-web's row, so a keeper reseal or write-back keeps it.

- [ ] Tests: `test_statement_with_canopy_web_verifies`; `test_writer_allowed_accepts_named_canopy_web`; `test_writer_allowed_refuses_canopy_web_when_statement_omits_it`; `test_allowed_keeps_canopy_web_recipient_when_named`; `test_allowed_skips_canopy_web_row_when_not_named`.
- [ ] Run; they fail. Implement. Run the connections tests; they pass.
- [ ] A statement-shape refusal on an unknown key adds to its detail: "If this machine's canopy is old, update it." Test it.
- [ ] Commit, and open the CLI PR when Task 1's fixtures are final. Merge it first; note the version on main for Task 6.

### Task 3: The key

**Files:** Create `deploy/aws/canopy-web-connections-key.cfn.yaml`, `apps/connections/server_key.py`. Modify `deploy/aws/canopy-web-task-role.cfn.yaml` (inventory comment), `deploy/aws/canopy-web.cfn.yaml` (optional `CONNECTIONS_SERVER_KMS_KEY` environment value, empty by default), `config/settings` (both settings), `apps/agents/credentials/zk_seal.py`. Test: `tests/test_task_role_stack.py`, new `tests/test_connections_server_key.py`.

**Interfaces:**
- `server_key.available() -> bool`; `public_jwk() -> dict` (cached per process); `thumbprint() -> str`; `derive(peer_jwk: dict) -> bytes`; raises `server_key.Unavailable` on any KMS failure, with no secret in its message.
- `zk_seal.open_for(value: str, *, agent_id, name: str, kv: int, kind: str, target: str, derive) -> str`, where `derive(epk_jwk) -> bytes`.
- Tests swap in a software key with `server_key.use_software_key_for_tests(private_key)`, which raises unless `settings.TESTING`.

- [ ] Tests: `open_for` opens a value `seal_to` made and reproduces a zk-v1 vector; a wrong binding or tampered entry raises `ZkError`; `Unavailable` on a boto `ClientError`; the stack is `ECC_NIST_P256`/`KEY_AGREEMENT` and grants the task role exactly `kms:DeriveSharedSecret` and `kms:GetPublicKey`; `available()` is false when unset.
- [ ] Run; they fail. Implement, modelled on `runner/ec2/ecdh-kms.cfn.yaml` and the CLI's `device_key_kms.py` (`KeyAgreementAlgorithm: ECDH`).
- [ ] Run; they pass. Commit.

### Task 4: The switch on the row, and connecting

**Files:** Modify `apps/connections/models.py` (+migration `0008`), `recipients.py`, `services.py`, `providers/base.py`, `providers/salesforce.py`, `providers/google.py`. Test: new `tests/test_connections_server_use.py`.

**Interfaces:**
- `Connection.server_use: bool`, default False.
- `recipients.server_recipient(conn, statement=None) -> Recipient | None`, included in `recipients()`: present when `server_use` and `server_key.available()`, and on an `active` row only when the statement's `canopy_web` equals `server_key.thumbprint()`.
- `Provider.server_use_default`, `Provider.server_calls` (class attributes).
- `services.complete` seals to owner plus canopy-web per the spec's "Connecting".

- [ ] Tests: `test_new_salesforce_connection_seals_to_owner_and_canopy_web`; `test_new_google_connection_seals_to_owner_only`; `test_no_key_configured_seals_to_owner_only`; `test_kms_failure_at_connect_still_connects_owner_only` (Review Focus 1); `test_reconnect_keeps_off`; `test_reconnect_keeps_on_and_goes_unshared` (Review Focus 5); `test_unshared_row_ignores_old_statement`; `test_prune_keeps_canopy_web_when_named`; `test_active_row_without_canopy_web_in_statement_drops_it`.
- [ ] Run; they fail. Implement. `prune` and `_writer_provable` must treat canopy-web's key as provable when the statement names it.
- [ ] Run all connection tests; they pass. Commit.

### Task 5: canopy-web uses a token

**Files:** Create `apps/connections/server_tokens.py`. Modify `tokens.py`, `providers/base.py`, `keeper.py`, `services.py` (`AUDITED`, `disconnect`). Test: new `tests/test_connections_server_tokens.py`, and the secret-scan test.

**Interfaces:**
- `server_tokens.access_token(conn) -> tuple[str, str]` (token, instance_url). Raises `services.NotConnected`, `server_tokens.ServerUseOff`, `server_tokens.Waiting` (lease held past 30 s), `server_key.Unavailable`.
- `Provider.refresh(refresh_token: str) -> TokenResponse`; `Provider.revoke` exists.
- `tokens.take_lease` and `write_back` factor out their device-key lookup so `server_tokens` can use them with `holder = server_key.thumbprint()`.
- `server_tokens.revoke_if_able(conn) -> bool`, called by `services.disconnect` after the commit and by `keeper.sweep`.

Steps follow the spec's "canopy-web using a token", 1 to 5. Cache TTL 300 s, never past `expires_at`; minimum life 60 s; wait 30 s.

- [ ] Tests: `test_uses_cached_token_without_kms`; `test_cache_misses_when_digest_changes`; `test_opens_access_envelope`; `test_refreshes_when_near_expiry_and_writes_back_with_macs`; `test_rotated_refresh_token_is_resealed_with_same_floor_and_owner_key`; `test_write_back_conflict_retries_from_new_row`; `test_two_callers_one_provider_call` (Review Focus 4); `test_waits_for_machine_lease_then_uses_its_write`; `test_invalid_grant_marks_needs_reconnect`; `test_off_raises_server_use_off`; `test_disconnect_revokes_at_once`; `test_google_revoke_skipped_when_other_connection_remains`; `test_no_token_in_events_or_logs`; a test that a machine-side check of the written envelope passes, using the reference's `verify_envelope_mac` (Review Focus 3).
- [ ] Run; they fail. Implement. No provider call inside a database transaction.
- [ ] Run; they pass. Add a test pinning `rotates_refresh_token` and token URL rules against the CLI's values as literals. Commit.

### Task 6: Routes

**Files:** Modify `apps/connections/sharing.py`, `api.py`, `schemas.py`, `problems.py`, `tokens.py` (material carries nothing new but the statement), `runner/ec2/cloud_runner.py` (`CONNECTIONS_ZK_MIN_CLI` to the Task 2 version), `frontend/src/api/generated.ts`. Test: `tests/test_connections_server_use.py`, the route-gate lint.

**Interfaces:**
- `POST /api/connections/{connection_id}/server-use {on: false}`: owner or agent admin; prunes canopy-web with `keep_only`/`keep_only_sig` on a row with no statement; 409 `needs_share` on a row with one; 422 for `on: true` (turning on goes through Share). Gate: same owner check as `/share`, with no step-up.
- `share_state` gains `server: {available, thumbprint, public_jwk, on, statements_enabled, provider_calls}`.
- `sharing.share` accepts `canopy_web` only if `statements_enabled` and it equals the current thumbprint; sets `server_use` from the statement; 422 `bad_canopy_web` otherwise. A first share naming only canopy-web is allowed.

- [ ] Tests: `test_turn_off_unshared_prunes_both_envelopes_no_passkey`; `test_turn_off_active_is_needs_share`; `test_someone_elses_connection_404`; `test_share_with_wrong_thumbprint_refused`; `test_share_with_canopy_web_while_statements_disabled_refused`; `test_share_sets_server_use_both_ways`; `test_share_naming_only_canopy_web_is_a_valid_first_share`.
- [ ] Run; they fail. Implement; regenerate `generated.ts` as CI does.
- [ ] Run; they pass, with the gate lint. Commit.

### Task 7: The browser signs and accepts

**Files:** Modify `frontend/src/vault/connectionShare.ts`, its test, `docs/vault/zk-v1/connections_v1_browser_share.json`, `frontend/src/pages/connections/ShareConnectionPage.tsx`, its test.

**Interfaces:** `openForOwner` accepts a canopy-web writer only while the row is `unshared` or the verified statement names it. Share choice gains `canopyWeb: boolean`, a checkbox labelled "Project screens and apps" with one sentence: "canopy-web can use this account for you, with nobody present. If canopy-web were ever hacked, this account could be taken until you disconnect it." The box is hidden when `provider_calls` is false or the server isn't available, and disabled with a sentence when `statements_enabled` is false.

- [ ] Tests: `opens server-written envelope while unshared`; `refuses server-written envelope under a statement not naming it`; `names canopy_web only when ticked and omits the key otherwise`; `ticking off rewrites both envelopes as the owner, leaving canopy-web out` (Review Focus 2); page: `nothingToShare` is false with only canopy-web ticked.
- [ ] Run; they fail. Implement. Regenerate the browser share fixture as `connectionShare.test.ts` does.
- [ ] Run `npx vitest run src/vault src/pages/connections`; pass. Commit.

### Task 8: The success screen and the switches

**Files:** Create `frontend/src/pages/connections/ConnectedPage.tsx` and test. Modify `apps/connections/views.py` (`_share` → redirect to `/w/<ws>/connections/<id>/connected?return_to=`, keeping `return_to` handling), `frontend/src/router.tsx`, `frontend/src/guide/surfaces.ts`, `frontend/src/components/settings/ConnectionsPanel.tsx`, `frontend/src/pages/agents/AgentConnectionsPanel.tsx`, and tests.

**Interfaces:** Reads `share_state` without unlocking the vault. Copy, exact:
- Person, on: "Connected as {account}. Project screens and apps you use can use it now." Links: "Also let agents or my computers use it", "Done".
- Agent, on: "Connected {agent} as {account}. Screens can use it now. One step left for {agent}'s own runs:" Button: "Turn on for {agent}'s machines".
- Off: "Connected as {account}. One step left: choose who can use it." Button: "Choose".

The lists show "Used by canopy-web" with a switch when `provider_calls` is true. Off with no statement calls `server-use`; anything else links to Share with a sentence saying a passkey is needed.

- [ ] Tests: backend `test_callback_lands_on_connected`; frontend one test per copy case, `no vault prompt on this screen`, `turning off an unshared connection calls server-use`, `turning off a shared one goes to Share`, `no switch for Google`.
- [ ] Run; they fail. Implement. Keep the visual language of `ShareConnectionPage`.
- [ ] Run the frontend suite for those paths and `npm run build`; pass. Commit.

### Task 9: Docs, and the whole thing

**Files:** `docs/architecture/vault.md`, `api-surface.md`, `deploy.md` (the key's stack, rollout order: stack, then web with the key set, CLI on boxes, then `CONNECTIONS_SERVER_STATEMENTS`); the status line and anti-goal of `2026-10-31-connections-zero-knowledge-design.md`.

- [ ] Write them. State the trade table from the spec in `vault.md`.
- [ ] Run the full backend, runner and frontend suites and the end-to-end tests as CI does. Everything passes.
- [ ] Commit.
