# A runner's Claude credentials live in a vault

**Status:** design, 2026-10-23. Jonathan asked for it after the cloud-ec2-2 token audit the same day (the token-misread fix is #1929). He settled the sharing model in that conversation (§Decisions).

## Intent

A cloud runner's long-lived Claude tokens and API keys are secrets like any agent's, and they should be kept the way Canopy keeps secrets: **in a vault, opened by the box with its own device key**, with membership deciding who and what can read them. Jonathan: *"I don't want you to reinvent something that is vault-like but not the vault, and then the vault access is coming from the device key."*

Nothing new is built here that the vault already does. The one extension is that a vault can serve a **runner**, not only agents.

## Today

- `RunnerLogin.value_enc` (kind `token` / `api_key`) is Fernet under the app-wide `FIELD_ENCRYPTION_KEY` (`apps/common/encryption.py`). canopy-web can read every value.
- `GET /api/harness/runners/{id}/credential` decrypts and returns them **in plaintext** to anything that authenticates as the runner's owner: the box's `RunnerAccessToken`, and also the owner's browser session or any of the owner's PATs. There is no step-up, no read log and no device proof. (It is excluded from MCP, `apps/mcp/api_tools.py`.)
- `wire.sh` copies tokens between boxes through that route, and falls back to Secrets Manager `canopy/cloud-runner/claude-oauth-token`.
- A full **login** (`kind=login`) never leaves the box (its refresh token rotates). That is unchanged by this design: a login is not a stored secret.

## Decisions (Jonathan, 2026-10-23)

1. **The real vault.** Tokens and API keys are `VaultSecret`s in a workspace vault (`Vault`, `kind=workspace`), always zero-knowledge, readable only by the device keys and people the vault's membership allows. canopy-web never holds them in plaintext.
2. **Shared within one isolation boundary, never across one.** A vault may serve several runners, so one vault can serve, for example, every general dimagi box. Every runner it serves must sit in the same isolation boundary (below). A new box gets **its own** vault by default; sharing is an explicit attach.
3. **Device keys are never shared.** Each box opens a vault with its own copy of the vault key, wrapped to its own approved ECDH key, as every machine does today. Removing a box drops its copy; if it may have kept the values, rotate them.
4. **Resilience comes from accounts, not copies.** The cap is per Claude account, so two boxes on one token share one cap. The cascade spreads load across tokens from different accounts, with each box's own full login first.

## The model

### A vault can serve a runner

`VaultAttachment` gains a third target, `runner` (FK to `harness.Runner`). The one-target check becomes "exactly one of agent, workspace, runner", with a unique (vault, runner) constraint.

- **Who may hold its key** (`workspace_vaults.eligible_device_keys` and `device_may_hold`, asked on every read — nothing sweeps copies): add the runners' own device keys. A device key on an attached runner is eligible when it is `ACTIVE` with an approved ECDH key **and** its person (`vault_members.person_of_device`: the runner's owner, else the approver) is a member of the vault. That is the same machine rule as today, reached through a runner attachment instead of through an agent. The machine layer still never grants on its own.
- **Members:** a vault created for a runner starts with one direct `manage` member, the runner's owner, plus the `workspace_owners` link that every workspace vault has. A runner admin (`can_administer_runner`) can be added directly. No `attached` link: a runner has no "full access" set of people.
- **Who may attach:** a manager of the vault who also administers the runner; from a browser behind a passkey step-up, and a token is let through as on every other vault route (so `wire.sh` can attach a new box). Attaching only makes the box eligible: a holder still has to grant it a copy. Attaching is a `@gate(..., setting=True)` write, audited like any setting.

### The isolation boundary

Every runner attached to one vault must be in one boundary. `attach` refuses (422 `vault_crosses_isolation`) unless the new runner and every runner already attached share:

- the **same owner**;
- the **same unit** (`routing.unit_refusal`'s notion): the same workspace tree root for a tree unit, or the same agent for an agent unit;
- the **same data mode and flags that change what a box may hold**: a `zdr` box shares only with `zdr` boxes of the same data mode.

So cloud-zdr-1 can never share with cloud-ec2-2, and an agent's own unit only ever shares with that agent's other unit boxes. The check runs on every attach and again on every read (`serving_runners`, measured against the vault's first attached runner): a runner whose boundary changed after it was attached (re-enrolled, flags changed) is handed nothing from the vault until it is detached or put back, and its `claude.vault` check says so.

### What `RunnerLogin` keeps

The credential list stays the one ordered cascade the runner page edits (name, kind, position). A `token` or `api_key` row stops holding a value: `value_enc` is replaced by a reference, `(vault, secret name)`. The secret's name is the row's `key` (`claude-token:<key>`, `anthropic-api-key:<key>`), so a row and its secret can never disagree. Removing a row deletes its secret; a vault shared by several boxes keeps a secret for as long as any attached runner's list names it.

A list row that names a secret in a vault the runner is not attached to is refused at write time (422).

### How the box reads them

A new route `GET /api/harness/runners/{id}/credential/resolve`, modelled on `/agents/{slug}/credentials/resolve` and sharing its machinery (`credential_resolve`): **runner token only** (`runner_auth`, the box's own `RunnerAccessToken`; a person's PAT or session gets 403 `runner_only`), **with a device proof** of an active key on that runner, returning each attached vault's ciphertext values for the names on the list, plus this device's wrap of each vault key (`wrap: null` when it may not hold one). Every read writes the read log (a runner-scoped sibling of `AgentCredentialRead`) and the fleet event.

The box decrypts with the `canopy` CLI it already uses for vault work (`canopy vault …`, with the box's device key, an isolation unit's own software key), never in `cloud_runner.py`, which stays stdlib-only. The cascade and probes are unchanged; only where the values come from changes.

The plaintext `GET /runners/{id}/credential` keeps serving the non-secret parts (the list, `logins`, `updated_at`) and stops returning any value once every row has moved (below).

### How a value gets in

- **A sign-in on the box** (`RunnerMint`, `slot: token`): the box probes the new token (#1929), then seals it into the vault itself with its copy of the vault key and writes the ciphertext (`PUT /api/vaults/{id}/zk/values`, the workspace vault's own route). canopy-web never sees the token. `mint/result` carries only success and the row key.
- **A token or API key pasted on the runner page:** the browser seals it, as it already does for a workspace vault's values (the person unlocks their vault key).
- **A box with no copy of the vault key yet** cannot seal. It posts the token to canopy-web once, which seals it `zks1` to every eligible recipient and keeps nothing. The next key holder settles it into an ordinary value, exactly as for a server-minted agent secret today (`zk_seal.py`).

### A new box

`wire.sh` stops copying values. It attaches the new runner to the boundary's vault (or creates the box's own), and the box's key copy comes from the existing grant paths: the vault keeper on a box that holds it, or the owner's "Give access" approval with a passkey. Until then the box has no Claude credential and waits, as it does today (`fetch_and_stage_credential`, "needs a Claude sign-in"), and its page says it is waiting for vault access, not for a sign-in. The Secrets Manager bootstrap token is retired.

## Migration

1. Ship the attachment, eligibility, resolve and box read path, with the box reading the vault first and the old plaintext bundle as a fallback.
2. For each cloud runner with `token`/`api_key` rows: create **its own** vault (`<runner> Claude credentials`; Jonathan, 2026-10-23: per-box first, attach to a shared vault later), and seal each `value_enc` into it as `zks1` to the eligible recipients (`manage.py runner_credentials_to_vault`, dry-run by default). Clear `value_enc` in the same transaction. The box settles the values on its next pass.
3. Once no row holds a `value_enc`, delete the column and the plaintext values from `GET …/credential`, and the box's fallback with them.

## Not in scope

- Full logins (they stay on the box).
- Laptops: an `emdash` runner uses its person's own ambient Claude login and holds no stored credential.
- Agent turns on the box can still see the token in their environment: `claude` needs it, and commands an agent runs inherit it. Isolation units and confined turns are the protection there, and this design does not change it.

## Tests

- Eligibility: a runner attachment makes the runner's active, ECDH-approved key eligible only while its owner is a member; once the owner leaves, the copy it still holds is handed out to nobody.
- `attach` refuses across owners, across tree roots, across agent units, and between a sensitive-data box and another; a box whose boundary changes after attaching is no longer served.
- `…/credential/resolve`: refuses a PAT, a browser session and a missing or foreign device proof; logs every read; returns `wrap: null` to a key that may not hold one.
- Mint: the box seals and canopy-web stores only ciphertext; a box without a key copy falls back to `zks1`.
- Migration: dry run changes nothing; apply leaves no `value_enc` and every value readable by the box.
