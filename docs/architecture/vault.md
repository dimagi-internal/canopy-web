# Vaults: who may open an agent's secrets

Design: `docs/superpowers/specs/2026-10-22-vault-access-follows-canopy-access-design.md`. Byte formats: `docs/vault/zk-v1/zk_v1_reference.py`.

## A vault and its members

A **vault** (`agents.Vault`) is a named set of secrets with its own membership. Every agent has one (`kind=agent`), made when the agent is created (`apps/agents/apps.py`) and moved with it (`move._vault`). Its secrets and key copies are still stored on the agent (`AgentCredential`, `AgentVaultWrap`).

Who may open a vault is `credentials.vault_members.members(vault)`, the union of:

- **direct members** (`VaultMember`): people added by name. A direct member must be in the vault's workspace, and stays a member until a manager removes them;
- **links** (`VaultLink`), re-read on every question:
  - `agent`: everyone with full access to the agent (`access.roster`), plus everyone with an active license to use it locally (`AgentLicense`). The agent's owner and admins come in as `manage`, everyone else as the link's role;
  - `workspace_owners` / `workspace_admins`: the owners (or admins) of a workspace, inherited down the tree as those roles are.

A person's role is the highest any source gives them: `manage` (see and edit values, change membership) or `use`. Every member carries their sources, so the vault's page says why each person is in it. A new agent vault starts with one `agent` link, so giving someone the agent gives them its vault.

A change that would leave the vault with no one who can manage it is refused (409 `vault_needs_a_manager`). Membership routes: `GET|POST /api/agents/{slug}/vault/members`, `DELETE …/members/{user_id}`, `PUT …/vault/agent-link`; writes are gated `vault-manage`.

## Who may hold a copy of the vault's key (zero-knowledge)

- **A person** (`zk.eligible_members`): a member of the vault who has a vault key of their own.
- **A machine** (`zk.eligible_device_keys`): an active device key with an approved ECDH key, on a runner that serves the agent (`device_keys.runner_serves_agent`), **and** whose person is a member of the vault. The person is the runner's owner, else whoever approved the key (`vault_members.person_of_device`). The machine layer never grants on its own.

`pending` (`zk.pending_targets`), every wrap write and every `zks1` seal read these two functions, so nothing else decides who may hold the key.

## Removal: key copies follow membership

`vault_members.reconcile(agent)` deletes every copy of the vault key, and every endorsement, that no longer has a vault member behind it: a person who left, or a machine whose person left. It runs on the off-request clock every minute (`agents.vault_reconcile`) and right after each membership write. Credential resolve also refuses a machine whose person is not a member (403 `not_a_vault_member`), whatever copies it still holds.

The vault key is **not** changed on removal: as in 1Password, the server stops serving the vault. "Change the key" (rotate), together with rotating the outside secrets, is the response when someone may have kept a copy.

## Granting without a person

Approving a machine signs its ECDH key with the approver's own vault key (an endorsement, `approveAndGrant`), even when the approver holds no copy of the agent's vault key, so a **keeper** (a canopy runner that holds it, `canopy vault zk keep`) can pass the key on. `zk_notify` emails the key holders only after `GRACE` (15 minutes), for what no machine could pass on.
