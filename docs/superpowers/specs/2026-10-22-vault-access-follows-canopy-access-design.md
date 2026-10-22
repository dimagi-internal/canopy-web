# Vault access follows Canopy access

**Status:** design, 2026-10-22. Jonathan asked for this on 2026-10-22, after granting a new machine access to ACE took a forwarded email, a button that did nothing, and a CLI command run by Ada. Ada wrote it from that conversation. The open questions at the end are his to settle.

## Intent

Today, giving someone an agent takes two steps: add them to the agent in Canopy, then add them to the agent's vault. The second step used to happen in 1Password and now happens in Canopy Vault, but it is still a separate, manual step. Jonathan's words: *"I want the vault system to be just as robust as 1pass, but the 'add the user to the vault' is automated when I add them to the agent."* And: *"I built the vault system so we could align ACLs in a uniform and clear way in Canopy rather than having to also manage everything in 1pass."*

So: **who may open a vault is derived from Canopy's access list, and kept in step with it automatically.** No second list, in 1Password or anywhere else.

## What went wrong on 2026-10-22 (the worked example)

Sarvesh (an ACE admin) set up a runner on his Mac and approved its device key. ACE is zero-knowledge, so canopy-web cannot hand ACE's vault key to that machine. Only a holder can. The only person holding it was Jonathan, so `zk_notify` emailed him a "Give access" link. Two things then went wrong:

1. **The button did nothing.** The read log shows both of his clicks reached `GET /vault/zk?values=true`, and neither reached `POST /vault/zk/wraps`. The flow failed in the browser at the unlock step. Ada granted the machine instead with `canopy vault zk grant --agent ace`, from a runner that holds ACE's key.
2. **The grant should not have been possible.** Sarvesh has no personal vault key, so he is not a vault user of ACE. His Mac now reads every ACE secret anyway, because the machine layer stands on its own today (`zk.eligible_device_keys`: an active device key on a runner that serves the agent). Nothing checks that the person the machine works for is a vault user.

A third finding: ACE's vault has **no endorsements** (`GET /vault/zk/recipients` → `endorsements: []`). Its wraps were made from the CLI, which does not endorse, so the §4 recipient trust in `2026-10-19-give-an-agent-a-secret-design.md` has nothing to stand on for ACE.

## The model: three owners, two layers

**A vault belongs to whoever owns its secrets, and access comes from that owner's access list.**

| Secrets | Example | Owner, and whose list decides | Vault |
|---|---|---|---|
| An agent's own | ACE's HQ password, OCS token, gog token | **The agent**: its access in Canopy | Agent vault (exists) |
| Shared across a tenant | The gog OAuth clients (`Canopy-Shared` in 1Password today) | **The workspace**, inherited down the tree | **Workspace vault (new)** |
| A machine's identity | Secure Enclave / TPM / EC2-role device key | **The runner** (a laptop without the runner app has a `computer`-kind runner, canopy-web#1787) | Not a vault: a key holder (exists) |

Agent secrets are **not** copied into runner vaults. ACE runs on several machines, so per-runner copies would bring back the two-lists problem: removing someone from ACE would mean cleaning every runner vault they could reach. There is one copy per secret, and machines *read* it.

**Two layers, both required:**

| | Person layer (always required) | Machine layer (added on top) |
|---|---|---|
| Asks | Is this person allowed into this vault? | Is this machine known and trusted? |
| Proved by | Their vault key, unlocked by their passkey or passphrase | The machine's device key |
| Granted by | Their role in Canopy (derived, never set by hand) | Approving the machine, once, with a passkey |
| Alone | Enough only in that person's own browser | **Never enough** |

**The rule:** a machine may hold a vault's key only while (a) its device key is active and endorsed, **and** (b) the person it works for (the runner's owner, or the person who owns a `computer` runner) is a current vault user of that vault. If either stops being true, it loses access.

## Who is a vault user

Derived from `access.decide`, never stored as a separate grant:

- **Agent vault.** The agent's owner and admins can view and edit values in the browser. The agent's members with `full` access are also vault users, so they can run the agent on their own machines (open question 1). Contacts never are: a contact's turn runs on a runner whose owner is a vault user, and gets only what its capability needs.
- **Workspace vault.** The workspace's owners can view and edit values. Owner flows down the tree, so an org owner manages every division's vault, as `own` does today. A machine may *read* a workspace vault only for a turn of an agent in that workspace or below it, and only under the agent-vault rule above. A parent's editors and viewers gain nothing, which matches `WorkspaceMembership.INHERITS_DOWN`.

**An honest limit.** A person whose machine holds a key can read everything that key opens. "Use but not view" can be enforced against the browser, not against someone who owns the machine. So making someone a vault user means trusting them with the values. That is also true of 1Password.

## How it works

### 1. Grants happen without a person: the keeper

A **keeper** is a machine that holds a vault's key and is online: in practice the always-on cloud runners, and any runner while it is up. On its heartbeat it reads `pending` for each vault it holds (the same list `zk_notify` sweeps today) and wraps the key to each target that passes the trust check below, using the existing `run_grant` path in `canopy vault zk`. The work happens within a minute of the Canopy change that made the target eligible. `zk_notify`'s email becomes a fallback, sent only if no keeper has granted the target after a grace period. This is the same role 1Password's SCIM bridge plays for automated provisioning.

**The trust check (reuse §4 endorsements, do not invent a second mechanism).** A keeper wraps only to a target whose key carries a valid endorsement from an anchor it trusts. The keeper's anchors are the people pinned by its owner (canopy CLI `~/.canopy/vault-pins.json`, as `set --sealed` already uses). Endorsements get made where a person already acts:

- **A person's own key.** When they create their vault key, they sign their own public key. A keeper pins a person on first sight (TOFU, keyed by person, like `known_hosts`) and refuses any later key change for that person until an admin confirms it (open question 2).
- **A machine.** Approving a device key already takes the approver's passkey touch. That touch also signs the machine's ECDH key, so a machine is trusted through the person who approved it. The approver must be a vault user. Sarvesh approving his own Mac would then produce a valid endorsement only once he has a vault key.
- **Backfill.** A one-time "confirm who can read this agent" pass by each agent's owner signs today's unendorsed wraps (§4's *confirm* step). ACE needs it.

A keeper never wraps to an unendorsed target "to make it work". An unendorsed target stays pending, and the Settings page says why.

### 2. Removals happen without a person

When someone stops being a vault user (removed from the agent or workspace, demoted, or their machine's key revoked), canopy-web deletes their wraps and endorsements, and resolve refuses their machines. **The vault key is not changed by default.** 1Password does not change a vault's key on removal either; the server stops serving the vault's contents. Changing the key only helps if the person kept a copy of it, and in that case they could also have kept the values. So the real response to a departure on bad terms is the manual "Change the key" action **plus** rotating the outside secrets themselves. The Settings page offers both together.

### 3. A person's first time

A person can't be wrapped to until they have a vault key, just as a 1Password invitee must create an account first. When someone becomes a vault user without one, canopy-web prompts them once (Settings → Vault key, passkey first, passphrase fallback) and the keeper picks them up afterwards. The giver sees "waiting for Sarvesh to set up his vault key", not a button.

### 4. Workspace vaults, inherited down the tree

Each workspace may have one zero-knowledge vault, in the same format as an agent vault (its own VK, wraps, endorsements, read log).

- **Layered, not nearest-wins.** A turn for an agent in `connect` sees `dimagi`'s workspace vault, then `connect`'s, then the agent's own vault. On a name clash the nearer one wins (agent over child workspace, child over parent). The agent's Credentials page shows each value's source and any override, so an override is never silent. This replaces `Workspace.shared_vault_source`'s rule (the nearest workspace's whole vault wins) because a division should *add to* its org's secrets, not have to copy them all.
- **Migration.** `Canopy-Shared` (1Password) → `dimagi`'s workspace vault, imported encrypted on a key-holding machine with the existing `canopy vault import --zk`. Once every agent resolves the gog OAuth clients from it, canopy-web drops `shared_op_vault` / `shared_op_sa_token_enc`. That removes the last 1Password service-account token canopy-web stores, which canopy-web could itself decrypt.
- **Audit.** Workspace-vault reads go through the same read log as agent credentials (person, machine, turn), which `Canopy-Shared` never had.

### 5. Canopy's own token leaves the vault

`CANOPY_WEB_PAT` is a long-lived secret that every machine holding an agent's vault can read. It moves out per `2026-10-21-turn-scoped-agent-token-design.md`: runner turns get a short-lived token minted for the turn. A person running an agent locally with no runner signs in as themselves (their own PAT, `canopy:canopy-web-pat-mint`), and is still a vault user like anyone else (§Who is a vault user). That spec's phase 2 (laptop and emdash turns) is the open dependency. Nothing here changes it.

## What already exists (and is reused unchanged)

| Piece | Where |
|---|---|
| Wraps, `pending` targets, grant, rotate | `apps/agents/credentials/zk.py`; canopy `vault_zk_cli.run_grant` / `run_rotate` |
| Endorsement format, anchors and pinning | `zk_v1_reference.py` §7; `2026-10-19-give-an-agent-a-secret-design.md` §4; `POST /vault/zk/endorsements` |
| Pending-grant sweep and email | `apps/agents/credentials/zk_notify.py` (becomes the fallback) |
| Device keys for laptops without the runner app | canopy-web#1787 (`computer`-kind runner) |
| The one access decision | `apps/agents/access.decide` |
| Workspace tree and what flows down | `docs/architecture/roles.md`, `Workspace.ancestor_slugs` |
| Turn-scoped agent tokens | `2026-10-21-turn-scoped-agent-token-design.md` |

## Changes

1. `zk.eligible_device_keys` also requires the device's person to be a current vault user. `zk.eligible_members` widens from "agent admins with a keyset" to "vault users with a keyset" (§Who is a vault user).
2. Device approval signs an endorsement of the machine's ECDH key in the same passkey touch. The CLI's `enable` / `grant` / `rotate` endorse what they wrap, signed with the device's owner's key where the CLI can reach it. Otherwise the wrap is marked for the owner's confirm pass.
3. The keeper loop in the canopy runner: heartbeat → pending → trust check → `run_grant`, with a per-vault lock so two keepers do not race. Every grant goes to the setting-change log as `via: keeper`.
4. Removal deletes wraps and endorsements on the Canopy change that ends someone's access. Both the server's role-change hooks and a periodic reconcile do this, so a missed hook is caught.
5. Workspace vaults: model, routes (owner-gated, browser step-up, token allowed), layered resolve in `credential_resolve`, Credentials-page source column, `Canopy-Shared` import, then removal of the 1Password shared-vault fields.
6. Backfill: an owner's confirm pass on every zero-knowledge agent, plus a report of wraps whose person is not a vault user. Sarvesh's Mac on ACE is in that report until he sets up his vault key (open question 3).

## Phases

1. **Person layer and keeper**, for agent vaults: changes 1 to 4 and 6. This removes the "Give access" step.
2. **Workspace vaults**, and retiring `Canopy-Shared`: change 5.
3. **Turn-scoped tokens** replace `CANOPY_WEB_PAT`: tracked in its own spec.

## Open questions (Jonathan's to decide)

1. **Do members with `full` access (editors) become agent-vault users**, so they can run the agent on their own machines? Proposed default: yes. Contacts never.
2. **First-sight trust for a person's key**: pin it on first sight (TOFU, like 1Password), or require an admin's endorsement before any keeper wraps to it? Proposed default: TOFU, with a key change requiring an admin.
3. **Sarvesh's Mac on ACE** holds ACE's key under the old rule. Leave it until he sets up his vault key, or revoke it now? Proposed default: leave it, and report it in the backfill.
4. **One workspace vault per workspace, or several named ones** ("additional workspace vaults")? Proposed default: one per workspace, with several layered down the tree. Named extra vaults can come later if a workspace needs them.

## Testing

- Access matrix: for each role in `access.md` × {agent vault, workspace vault at the agent's workspace, at an ancestor}, the expected vault-user answer, asked through `access.decide`.
- Keeper: wraps an endorsed pending target; refuses an unendorsed one, a target whose person is not a vault user, and a person whose pinned key changed; two keepers produce exactly one grant.
- Removal: removing a member deletes their wraps and their machines' wraps, and resolve then refuses those machines.
- Layering: an agent in a child workspace resolves a parent value, a child override and an agent override, and the source column reports each one.
- Regression for 2026-10-22: a machine approved by a non-vault-user never appears in `pending`.
