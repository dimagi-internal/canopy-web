# Vaults: who may open an agent's secrets

Design: `docs/superpowers/specs/2026-10-22-vault-access-follows-canopy-access-design.md`. Byte formats: `docs/vault/zk-v1/zk_v1_reference.py`.

## A vault and its members

A **vault** (`agents.Vault`) is a named set of secrets with its own membership. Every agent has one (`kind=agent`), made when the agent is created (`apps/agents/apps.py`) and moved with it (`move._vault`). Its secrets and key copies are still stored on the agent (`AgentCredential`, `AgentVaultWrap`).

Who may open a vault is `credentials.vault_members.members(vault)`, the union of:

- **direct members** (`VaultMember`): people added by name. A direct member counts only while they are in the vault's workspace (directly or inherited), until a manager removes them;
- **links** (`VaultLink`), re-read on every question:
  - `agent`: everyone with full access to the agent (`access.roster`), plus everyone with an active license to use it locally (`AgentLicense`). The agent's owner comes in as `owner`, its admins as `admin`, everyone else as the link's role;
  - `workspace_owners` / `workspace_admins`: the owners (or admins) of a workspace, inherited down the tree as those roles are.

Members are **people only**: an agent's own canopy login (`Agent.user`) and a workspace's system account are never members by any source, and cannot be added (`vault_members.not_a_person`). They hold no vault key, and an agent that could change its own vault's membership could hand its key to anyone.

A vault's roles are the agent's, without contacts: `owner`, `admin` and `member`. A person's role is the highest any source gives them. Owners and admins see and edit values and change membership (`vault_members.is_admin`); members open the values. Owner is never stored: it comes only from owning the linked agent, or, for a workspace vault, the workspace. A direct member or a link gives `admin` or `member` (`manage` and `use`, the earlier words, are still accepted). Every member carries their sources, so the vault's page says why each person is in it. A new agent vault starts with one `agent` link, so giving someone the agent gives them its vault.

A change that would leave the vault with no owner or admin is refused (409 `vault_needs_an_admin`). Membership writes lock the vault's row, so two at once cannot together remove the last owner or admin. Membership routes: `GET|POST /api/agents/{slug}/vault/members`, `DELETE …/members/{user_id}`, `PUT …/vault/agent-link`; writes are gated `vault-admin`.

## Everyone has a vault key

A person can be given a vault's key only once they have a vault key of their own, so everyone sets one up at sign-in: after Google sign-in, a person at a browser with no vault key sees one screen (`VaultSetupGate`, from `GET /api/vault/setup`) that registers a passkey and makes the key with it, or with a passphrase where the passkey cannot hold one. A failed check or a browser that cannot make a passkey lets them continue for that tab, and they are asked again next time.

## Everyone has a vault key

A person can be given a vault's key only once they have a vault key of their own, so everyone sets one up at sign-in: after Google sign-in, a person at a browser with no vault key sees one screen (`VaultSetupGate`, from `GET /api/vault/setup`) that registers a passkey and makes the key with it, or with a passphrase where the passkey cannot hold one. A failed check or a browser that cannot make a passkey lets them continue for that tab, and they are asked again next time.

## Who may hold a copy of the vault's key (zero-knowledge)

- **A person** (`zk.eligible_members`): a member of the vault who has a vault key of their own.
- **A machine** (`zk.eligible_device_keys`): an active device key with an approved ECDH key, on a runner that serves the agent (`device_keys.runner_serves_agent`), **and** whose person is a member of the vault. The person is the runner's owner, else whoever approved the key (`vault_members.person_of_device`). The machine layer never grants on its own.

`pending` (`zk.pending_targets`), every wrap write and every `zks1` seal read these two functions, so nothing else decides who may hold the key. A keeper's grant is checked again under the vault's lock and refused (409 `zk_not_pending`) for a target that already holds a copy, so two keepers make one grant. Replacing a person's vault key, or a machine's ECDH key, drops their copies in agent and workspace vaults alike.

## Removal: membership is checked on every access, never swept

Nothing deletes a copy of the vault key when someone stops being a member. Every place a copy, a value or an endorsement is used asks whether its person is a member **now**, so a leftover copy is ciphertext canopy-web never hands out:

- a machine's resolve refuses one whose person is not a member (403 `not_a_vault_member`), for the agent's own vault and each workspace layer (`workspace_vaults.layers_payload`);
- the browser's `GET …/vault/zk?values=true` gives the ciphertext only to a vault member (403 `not_a_vault_member`), so an agent admin outside an unlinked vault cannot pair it with an old copy;
- a box is a keeper of a vault only while it may hold that key now (`vault_keeper._held`, `workspace_vaults.device_may_hold`);
- `zk_notify.holders` and the endorsements a keeper checks (`zk_endorse.endorsements`) count current members only.

"Holds a copy" is never itself a permission. A person who comes back opens the vault again with the copy they had. Copies are deleted only by the action that makes them useless: replacing a person's vault key or a machine's ECDH key (`zk.forget_member_wraps` / `forget_device_wraps`) or changing the vault key. A revoked device key is refused at every read. A copy made for someone who has since left still shows as not current (`rotation_recommended`), because it still opens the key it was made for.

The vault key is **not** changed on removal: as in 1Password, the server stops serving the vault. "Change the key" (rotate), together with rotating the outside secrets, is the response when someone may have kept a copy.

## Granting without a person

Approving a machine signs its ECDH key with the approver's own vault key (an endorsement, `approveAndGrant`), even when the approver holds no copy of the agent's vault key, so a **keeper** (a canopy runner that holds it, `canopy vault zk keep`) can pass the key on. `zk_notify` emails the key holders only after `GRACE` (15 minutes), for what no machine could pass on, for agent vaults and workspace vaults alike (`ZkGrantNotice` names one or the other). Each vault's members list says who is waiting (`has_copy`) and who can pass the key on (`holders`: the people holding a copy, and the keeper runners), and the page says it in words (`VaultWaitingNote`), to the waiting person too.


**The keepers' record.** Which key each keeper trusted for each person is kept once, in the vault itself: `Vault.keeper_pins_zk`, a `zk1` value under the vault key (name `~keeper-pins`, at `keeper_pins_kv`). canopy-web stores it and can neither read nor forge it. A keeper reads it with its work (`pins`) and writes it back over the version it read (`PUT …/vault-keeper/{slug}/pins` or `…/vaults/{id}/pins`; 409 `keeper_pins_stale`). So every keeper, including a brand-new cloud box, refuses a person whose key changed, and leaves them waiting for a person. Granting them is the confirmation: an owner's or admin's browser grant (`grantPending`, or a workspace vault's grant on its page, `grantWorkspaceVaultPending`) also records the key it granted to in the record (`recordGranted`, `PUT …/vault/zk/pins` or `/api/vaults/{id}/zk/pins`), so keepers trust it from then on. Someone who started over with a new key (`UserVaultKeyset.replaced_at`) is emailed to the key holders at once, as a new key to check with them, with no grace period, since no keeper will grant it. A rotation leaves the record unreadable, and keepers start a new one. Every keeper grant to a person is emailed to the vault's owners and admins (`vault_keeper._announce`). Only a machine whose person is an owner or admin of the vault is a keeper (`vault_members.device_works_for_an_admin`): a member's machine reads the vault but never passes its key on.
## Workspace vaults

A workspace may keep any number of vaults (`Vault`, `kind=workspace`), always zero-knowledge, in the agent vault's formats with the vault's **binding id** (`vault:<uuid>`) where an agent's id goes in every AAD and HKDF info (`credentials/workspace_vaults.py`). Its secrets are `VaultSecret`, its key copies `VaultKeyWrap`.

- **Members:** made with two links: the workspace's owners (`owner`) and `attached`, everyone with full access to an agent the vault serves (`member`). Direct members as for an agent vault.
- **What it serves:** `VaultAttachment` rows, one agent or every agent in a workspace and below (the vault's own tree only). A moved agent keeps an attachment only inside that tree (`move._vault_attachments`).
- **Who may hold its key:** a member with a vault key; a machine whose person is a member and which serves an attached agent, checked on every read as for an agent vault.
- **Reading:** credential resolve returns `vaults`, the workspace vaults layered under the agent's own values (root workspace first, then vaults attached to the agent; a later layer wins, and the agent's own values win over all), each with this device's copy of its key, or `wrap: null` when it may not open it. The client decrypts and merges.
- **Routes:** `GET|POST /api/workspaces/{slug}/vaults` (making one needs `own`), `/api/vaults/{id}` with `members`, `attachments` and `secrets/{name}`, and `/api/vaults/{id}/zk` (`enable`, `values`, `wraps`) in the agent vault's shapes. The keeper grants them through `…/vault-keeper/vaults/{id}/wraps`. A machine is vouched for there by its approver's endorsement for an attached agent.

## A runner's own vault

A workspace vault can also serve one **cloud runner** (`VaultAttachment.runner`): it holds that box's long-lived Claude tokens and API keys (spec `docs/superpowers/specs/2026-10-23-runner-claude-credentials-in-the-vault-design.md`). A secret's name comes from its credential-list entry: `claude-token:<key>`, `anthropic-api-key:<key>` (`RunnerLogin.vault_secret_name`).

- **Making one:** `POST /api/harness/runners/{id}/vault` (runner admin; browser + step-up) gives the runner its OWN vault, `<runner> Claude credentials`, its owner a direct `manage` member, attached to it (`workspace_vaults.create_for_runner`). Sharing is an explicit attach: `POST /api/vaults/{id}/attachments` `{runner}`, by someone who manages the vault and administers the runner.
- **Isolation boundary** (`workspace_vaults.isolation_boundary`): every runner a vault serves has the same owner, the same isolation unit (agent unit's agent, tree unit's root, or — no declared unit — the trees it is enrolled in) and the same sensitive-data standing. `attach` refuses another (422 `vault_crosses_isolation`); a box whose boundary changes after attaching stops being served (`serving_runners`, measured against the FIRST attached runner): its copy of the key is handed out to nobody until it is detached or put back. Only live cloud runners with an owner attach.
- **Who may hold its key:** an active, ECDH-approved device key on a runner the vault serves, while its person is a member — the same machine rule as for agents, checked on every read (`device_may_hold`), never swept.
- **Its key:** the runner's owner makes it on the runner's credentials page (`makeWorkspaceVaultKey`, one passkey touch): a copy for them, covered by their own recovery code, and one for each machine waiting — the box. "Give … a copy" there grants boxes attached later (`grantWorkspaceVaultPending`).
- **Writing:** the box seals its own values with its copy of the key (`canopy vault runner-seal` → `PUT /api/harness/runners/{id}/vault/values`, the box's own runner token with a device proof, names on its own list only): a value canopy-web still keeps (`cascade[].seal_as`), and a token it signs in when the bundle says `vault_open` (`mint/result {vault_key}`, then the seal — the token never reaches canopy-web). A pasted API key is sealed in the browser (`POST …/vault/entries` makes the entry with no value, then `PUT /api/vaults/{id}/zk/values`). Each write drops the copy kept on the runner's entry (`value_enc`) and moves the bundle's stamp (`runners.store_vault_values`, `vault_values_written`). Removing an entry deletes its secret unless another runner the vault serves still lists it.
- **Reading:** `GET /api/harness/runners/{id}/credential/resolve` — the box's OWN runner token (403 `runner_only` for a person's token or session) with a device proof of a key on that runner (401 `device_proof_required`). It answers ciphertext in the agent resolve's layer shape, only the names the runner's list uses, and logs each as a `RunnerCredentialRead` plus a `runner.credentials.resolved` event. The box opens it with `canopy vault runner-credentials`.

