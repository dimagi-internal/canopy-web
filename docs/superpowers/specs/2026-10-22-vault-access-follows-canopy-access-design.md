# Vault access follows Canopy access

**Status:** design, 2026-10-22. Jonathan asked for this on 2026-10-22, after granting a new machine access to ACE took a forwarded email, a button that did nothing, and a CLI command run by Ada. Ada wrote it from that conversation. Jonathan settled the open questions the same day (§Decisions).

## Intent

Today, giving someone an agent takes two steps: add them to the agent in Canopy, then add them to the agent's vault. The second step used to happen in 1Password and now happens in Canopy Vault, but it is still a separate, manual step. Jonathan's words: *"I want the vault system to be just as robust as 1pass, but the 'add the user to the vault' is automated when I add them to the agent."* And: *"I built the vault system so we could align ACLs in a uniform and clear way in Canopy rather than having to also manage everything in 1pass."*

So: **vaults are first-class, with their own membership, and a vault's membership is easy to align with an agent and stays in step with it automatically.** In his words: *"I want vault permissions to be easily aligned to agents, but the vault membership system should be first class and not get its ACL directly from only the agent ACL."* There is no second list to keep by hand, in 1Password or anywhere else.

## Decisions (Jonathan, 2026-10-22)

1. Members with `full` access to an agent (editors) are members of the agent's vault, so they can run it on their own machines. Contacts never are.
2. A person's vault key is trusted on first sight (TOFU, as in 1Password). A later change of that key needs an admin's confirmation.
3. The stewari-mbp-cdp grant on ACE (below) needs no special handling.
4. A workspace can have **more than one** vault.
5. Vault membership is **first-class**. Linking a vault to an agent is a convenient source of members, not the only one.

## What went wrong on 2026-10-22 (the worked example)

Sarvesh (an ACE admin) set up a runner on his Mac and approved its device key. ACE is zero-knowledge, so canopy-web cannot hand ACE's vault key to that machine. Only a holder can. The only person holding it was Jonathan, so `zk_notify` emailed him a "Give access" link. Two things then went wrong:

1. **The button did nothing.** The read log shows both of his clicks reached `GET /vault/zk?values=true`, and neither reached `POST /vault/zk/wraps`. The flow failed in the browser at the unlock step. Ada granted the machine instead with `canopy vault zk grant --agent ace`, from a runner that holds ACE's key.
2. **The grant should not have been possible.** Sarvesh has no personal vault key, so he is not a vault user of ACE. His Mac now reads every ACE secret anyway, because the machine layer stands on its own today (`zk.eligible_device_keys`: an active device key on a runner that serves the agent). Nothing checks that the person the machine works for is a vault member.

A third finding: ACE's vault has **no endorsements** (`GET /vault/zk/recipients` → `endorsements: []`). Its wraps were made from the CLI, which does not endorse, so the §4 recipient trust in `2026-10-19-give-an-agent-a-secret-design.md` has nothing to stand on for ACE.

## The model

### Vaults are first-class

A **vault** is its own object: it belongs to a workspace, has a name, holds secrets (one VK, its wraps, endorsements and read log, in today's zero-knowledge format), and has **its own membership**. It is the Canopy equivalent of a 1Password vault.

| Vault | Example | Default membership | Count |
|---|---|---|---|
| **Agent vault** | ACE's HQ password, OCS token, gog token | Linked to its agent (below) | One per agent (what exists today) |
| **Workspace vault** | The gog OAuth clients (`Canopy-Shared` in 1Password today) | The workspace's owners | **Any number per workspace** |

A machine's identity (its Secure Enclave, TPM or EC2-role device key) is **not** a vault. It is a key holder on a runner; a laptop without the runner app has a `computer`-kind runner (canopy-web#1787). Agent secrets are never copied into per-runner vaults: there is one copy per secret, and machines *read* it, so removing someone never means cleaning N copies.

### Membership: first-class, with linked sources

A vault's member list has entries of two kinds, and its effective membership is their union:

- **Direct members.** A person added to the vault by name, by someone who manages it, exactly as in 1Password.
- **Linked members.** A rule that stands for a set of people Canopy already knows and is re-evaluated whenever that set changes. Initial rules:
  - *everyone with `full` access to agent X* (from `access.decide`: its owner, admins and full-access members; decision 1);
  - *the owners (or admins) of workspace W*, which flows down the tree as those roles already do (`WorkspaceMembership.INHERITS_DOWN`).

Each entry carries a **role**: `manage` (view and edit values, change membership) or `use` (open values from their own machines and browser). An agent's owner and admins come in as `manage`; its full-access members come in as `use`.

**Aligned with agents by default.** A new agent's vault starts with one linked entry: *everyone with full access to this agent*. So adding someone to the agent in Canopy adds them to its vault with nothing else to do, which is the intent above. A manager can also add direct members (a teammate who only needs one vault), link more rules (*connect's owners*), or remove the agent link entirely for a vault that should follow its own list. The vault's page lists every member with the **source** of their membership ("direct", "full access to ACE", "owner of dimagi"), so it always answers "why can this person open it?".

Removing someone from an agent removes only the membership that came from that agent's link. A direct membership stays until a manager removes it. The vault's page shows when a person is a member only directly, so that case is never invisible.

**An honest limit.** A person whose machine holds a key can read everything that key opens. `use` without seeing the values can be enforced in the browser, not against someone who owns the machine. So `use` means trusting them with the values, as membership of a 1Password vault does.

### Which vaults a turn reads

An agent reads its own vault, plus any vault **attached** to it. A workspace vault can be attached to:

- a specific agent; or
- *every agent in this workspace and below*. This replaces `Canopy-Shared` and flows down the workspace tree.

A turn sees the values of every vault available to it, layered: inherited workspace vaults first (from the root down), then vaults attached directly to the agent (in the order they were attached), then the agent's own vault. On a name clash the later one wins. The agent's Credentials page shows each value's vault and any override, so an override is never silent. This replaces `Workspace.shared_vault_source`'s rule (the nearest workspace's whole vault wins), because a division should *add to* its org's secrets, not copy them all.

### Two layers, both required

| | Person layer (always required) | Machine layer (added on top) |
|---|---|---|
| Asks | Is this person a member of this vault? | Is this machine known and trusted? |
| Proved by | Their vault key, unlocked by their passkey or passphrase | The machine's device key |
| Granted by | The vault's membership (direct or linked) | Approving the machine, once, with a passkey |
| Alone | Enough only in that person's own browser | **Never enough** |

**The rule:** a machine may hold vault V's key only while (a) its device key is active and endorsed, **and** (b) the person it works for (the runner's owner, or the person who owns a `computer` runner) is a current member of V. And it may open V for a turn only when V is available to that turn's agent (§Which vaults a turn reads). A contact's turn runs on a runner whose owner is a member, and gets only what its capability needs.

## How it works

### 1. Grants happen without a person: the keeper

A **keeper** is a machine that holds a vault's key and is online: in practice the always-on cloud runners, and any runner while it is up. On its heartbeat it reads `pending` for each vault it holds (the same list `zk_notify` sweeps today) and wraps the key to each target that passes the trust check below, using the existing `run_grant` path in `canopy vault zk`. The work happens within a minute of the membership change that made the target eligible. `zk_notify`'s email becomes a fallback, sent only if no keeper has granted the target after a grace period. This is the same role 1Password's SCIM bridge plays for automated provisioning.

**The trust check (reuse §4 endorsements, do not invent a second mechanism).** A keeper wraps only to a target whose key carries a valid endorsement from an anchor it trusts. Endorsements get made where a person already acts:

- **A person's own key.** When they create their vault key, they sign their own public key. A keeper pins a person on first sight (decision 2; keyed by person, like `known_hosts`, in the canopy CLI's `~/.canopy/vault-pins.json`, which `set --sealed` already uses) and refuses any later key change for that person until an admin confirms it.
- **A machine.** Approving a device key already takes the approver's passkey touch. That touch also signs the machine's ECDH key, so a machine is trusted through the person who approved it, who must be a member of the vault.
- **Backfill.** A one-time "confirm who can read this vault" pass by each vault's managers signs today's unendorsed wraps (§4's *confirm* step). ACE needs it.

A keeper never wraps to an unendorsed target "to make it work". An unendorsed target stays pending, and the vault's page says why.

### 2. Removals happen without a person

When someone stops being a member (a direct entry removed, or a linked source no longer includes them), or their machine's key is revoked, canopy-web deletes their wraps and endorsements, and resolve refuses their machines. **The vault key is not changed by default.** 1Password does not change a vault's key on removal either; the server stops serving the vault's contents. Changing the key only helps if the person kept a copy of it, and in that case they could also have kept the values. So the real response to a departure on bad terms is the manual "Change the key" action **plus** rotating the outside secrets themselves. The vault's page offers both together.

### 3. A person's first time

A person can't be wrapped to until they have a vault key, just as a 1Password invitee must create an account first. When someone becomes a member without one, canopy-web prompts them once (Settings → Vault key, passkey first, passphrase fallback) and the keeper picks them up afterwards. The giver sees "waiting for Sarvesh to set up his vault key", not a button.

### 4. Workspace vaults and the end of `Canopy-Shared`

- **Several per workspace** (decision 4), each with its own membership, attachments and read log.
- **Migration.** `Canopy-Shared` (1Password) → a `dimagi` workspace vault attached to *every agent in this workspace and below*, imported encrypted on a key-holding machine with the existing `canopy vault import --zk`. Its membership is *the owners of dimagi* (`manage`) plus *everyone with full access to any agent it is attached to* (`use`), so every agent's machines can read the gog OAuth clients. Once every agent resolves them from it, canopy-web drops `shared_op_vault` / `shared_op_sa_token_enc`. That removes the last 1Password service-account token canopy-web stores, which canopy-web could itself decrypt.
- **Audit.** Reads go through the same read log as agent credentials (person, machine, turn), which `Canopy-Shared` never had.

### 5. Canopy's own token leaves the vault

`CANOPY_WEB_PAT` is a long-lived secret that every machine holding an agent's vault can read. It moves out per `2026-10-21-turn-scoped-agent-token-design.md`: runner turns get a short-lived token minted for the turn. A person running an agent locally with no runner signs in as themselves (their own PAT, `canopy:canopy-web-pat-mint`), and still needs to be a vault member to read its secrets. That spec's phase 2 (laptop and emdash turns) is the open dependency. Nothing here changes it.

## What already exists (and is reused unchanged)

| Piece | Where |
|---|---|
| Wraps, `pending` targets, grant, rotate | `apps/agents/credentials/zk.py`; canopy `vault_zk_cli.run_grant` / `run_rotate` |
| Endorsement format, anchors and pinning | `zk_v1_reference.py` §7; `2026-10-19-give-an-agent-a-secret-design.md` §4; `POST /vault/zk/endorsements` |
| Pending-grant sweep and email | `apps/agents/credentials/zk_notify.py` (becomes the fallback) |
| Device keys for laptops without the runner app | canopy-web#1787 (`computer`-kind runner) |
| The one access decision (source of the agent link) | `apps/agents/access.decide` |
| Workspace tree and what flows down | `docs/architecture/roles.md`, `Workspace.ancestor_slugs` |
| Turn-scoped agent tokens | `2026-10-21-turn-scoped-agent-token-design.md` |

## Changes

1. **Vault as a model.** Today's per-agent ZK state (`zk_enabled`, `zk_kv`, `AgentVaultWrap`, values) moves under a `Vault` owned by a workspace. Each agent gets one, with the same ids and wraps, so nothing is re-encrypted. Plus `VaultMember` (direct, with a role), `VaultLink` (a rule: agent full access, or workspace owners/admins, with a role) and `VaultAttachment` (a vault → an agent, or → a workspace subtree).
2. **Effective membership** is one function (`vault_members(vault)`: direct ∪ linked, each with its source), used by `pending`, resolve, the keeper and the vault's page. `zk.eligible_members` and `zk.eligible_device_keys` read it, and the device path also requires the device's person to be a member.
3. Device approval signs an endorsement of the machine's ECDH key in the same passkey touch. The CLI's `enable` / `grant` / `rotate` endorse what they wrap, signed with the device's owner's key where the CLI can reach it. Otherwise the wrap is marked for the managers' confirm pass.
4. The keeper loop in the canopy runner: heartbeat → pending → trust check → `run_grant`, with a per-vault lock so two keepers do not race. Every grant goes to the setting-change log as `via: keeper`.
5. Removal deletes wraps and endorsements when effective membership shrinks. Both the role-change hooks and a periodic reconcile do this, so a missed hook is caught.
6. Workspace vaults: routes (manage-gated, browser step-up, token allowed), layered resolve in `credential_resolve`, a source column on the Credentials page, a vault page (members with sources, attachments, readers), the `Canopy-Shared` import, then removal of the 1Password shared-vault fields.
7. Backfill: the managers' confirm pass on every vault, and a report of wraps whose person is not a member.

## Phases

1. **Vault model, membership and keeper**, for agent vaults: changes 1 to 5 and 7. This removes the "Give access" step.
2. **Workspace vaults** and retiring `Canopy-Shared`: change 6.
3. **Turn-scoped tokens** replace `CANOPY_WEB_PAT`: tracked in its own spec.

## Testing

- Membership: direct-only, linked-only and both; removing a person from an agent removes only the linked entry; unlinking an agent drops exactly its linked members; every member has a source.
- Access matrix: for each role in `access.md` × {agent vault, workspace vault attached to the agent, attached to an ancestor workspace's subtree}, the expected member answer.
- Keeper: wraps an endorsed pending target; refuses an unendorsed one, a target whose person is not a member, and a person whose pinned key changed; two keepers produce exactly one grant.
- Removal: a member's wraps and their machines' wraps go, and resolve then refuses those machines.
- Layering: an agent in a child workspace resolves a parent value, a child override, an attached-vault override and an agent override, and the source column reports each one.
- Regression for 2026-10-22: a machine approved by a non-member never appears in `pending`.
