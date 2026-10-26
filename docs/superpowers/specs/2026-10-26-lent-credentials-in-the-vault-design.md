# Lent credentials live in the vault, marked as lent

**Status:** design, 2026-10-26. No code yet. Jonathan asked for it after an audit flagged that a person's lent GitHub token could be read by the agent's other admins.

## Intent

Jonathan's words, lightly edited: *"Agents should never act with the delegated authority of a user unless it's explicitly configured that way (like GitHub for all users to use), or with a very explicit, time-limited grant. Now that we have the vault system, even delegated access stored in canopy-web should go through the vault, stored with the knowledge that it's delegated access — metadata on the key. Right now we have two different, confusing ways to do this."*

The two ways today are the **vault** (an agent's own secrets, sealed so canopy-web cannot read them when the vault is zero-knowledge) and **`AgentDelegation`** (a person's GitHub token, held by canopy-web under the app-wide encryption key, plus a Salesforce row with no secret that points at another agent's credential). This design makes one of them: a **lent credential** is a vault entry whose clear metadata says whose it is, what it is for, which turns may use it, and when it stops.

## Goals

1. Every credential a person (or an agent's owner, for an agent's own identity) lends to an agent is stored in a vault, sealed, and canopy-web cannot read it.
2. Only the lender and the machines the lender allows can open it. The agent's other admins, editors and members cannot, and neither can canopy-web.
3. Each one carries clear metadata: lender, provider, identity, purpose, scope, which turns may use it, expiry, revocation.
4. Every one expires. The lender can renew, and can revoke it at any time from one list.
5. A person who is not an admin can lend their own credential for their own turns.
6. Credentials canopy-web must itself use on the server keep the same metadata model, in a second storage tier, and show in the same list.
7. Moving today's GitHub and Salesforce delegations leaves no agent without its credential for a single turn.

## Non-goals

- An agent's own secrets (its gog token, its HQ password, its own Salesforce login when it is the lender). They stay in its own vault, readable by its vault's members. Only *borrowing* changes.
- Tokens canopy issues (PATs, `DelegatedToken`, caller tokens, turn-scoped agent tokens, HCP grants) and permissions without a secret (`AgentLicense`, session-review grants). They are not secrets canopy-web holds for someone. Permissions may appear in the same list (§UI); their storage does not change.
- Stopping a box that already opened a credential from keeping it. No design can; see §Weaker.
- Replacing the fine-grained PAT with the GitHub App for agent work (`feedback_github_pat_not_app`: the PAT is the agent's GitHub identity).

## Today

### What a person lends, and who reads it

| Credential | Stored | Who uses it, and how | Who else can read it |
|---|---|---|---|
| **GitHub fine-grained PAT** (owner's, else an admin's) | `AgentDelegation.secret_enc`, Fernet under `FIELD_ENCRYPTION_KEY` (`apps/agents/models.py` ~1100–1135, `delegations.set_github`) | **Runner**, per turn: `POST /api/harness/runners/{rid}/turns/{tid}/github-token` (`apps/harness/api_runners.py` ~433, `delegations.github_token_for_turn`), plaintext; `cloud_runner.py` puts it in the turn's `GH_TOKEN`. **Server**: `set_github` probes the pasted plaintext; `check_github` decrypts the stored token to re-probe GitHub (`/user`, PR probe, push probe, expiry header). It runs from `POST /api/agents/{slug}/github/check` (`api_credentials.py` ~794) and from `GET /api/harness/runners/{rid}/github-readiness` at every box boot (`api_runners.py` ~538). | **Any agent admin with a bearer token**: `/credentials/resolve` returns `github_token` to an `admin` or `runner` caller (`credential_resolve.py` ~308–314; only `license` reads withhold it). Anyone with the database and the app key. This is the audit finding. |
| **Salesforce** (an *agent's* own identity, Eva's, lent to other agents) | No secret on the row (`secret_enc=""`, `lender` FK); the value is the lender's `AgentCredential` named `salesforce` (`delegations.py` ~455–601) | **Runner**: `/credentials/resolve` returns `salesforce_creds` in plaintext; `cloud_runner.py` ~3087–3167 writes `.sf-creds.json` per agent. **Server**: `probe_salesforce` refreshes it at set/check. Refused if the lender is zero-knowledge. | Every admin of every borrowing agent (same `resolve` path), plus everyone who can read Eva's vault. |
| **GitHub App user connection** (`GitHubConnection`) | `refresh_token_enc`, `access_token_enc`, Fernet (`apps/github_connect/models.py`) | **Server only**: canopy-web reads the agent's repo as its owner for skill history (`apps/agents/skill_history.py` → `github_app.access_token_for(agent.owner)`). The refresh token rotates on every use, so exactly one process may hold it. | canopy-web. |
| **A visitor's host grant** (`HostGrant.access_token_enc`, `apps/tokens/models.py` ~790) | Fernet, per arrival, at most `MAX_TOKEN_SECONDS`, DPoP-bound | **Server only**: the gateway calls the host's MCP as the visitor on an agent's turn (`apps/host_grants/host_grants.py`). | canopy-web. |
| **A secret shared into a chat** (`SessionSecret.value_enc`, `apps/canopy_sessions/models.py` ~820) | Fernet, scoped to one session | **Runner/agent**: `services.resolve_secret`, a bearer who can act in the session or is its agent. A hand-off ("put this in your vault"), not standing access. | canopy-web; any participant who can act in the session. |
| Scaffold push at agent creation | Not stored: "used once, never stored" (`apps/agents/api.py` ~389) | **Server**, once, in the request. The same token is then saved with `PUT /api/agents/{slug}/github`. | canopy-web, for the length of the request. |

Not lent (out of scope, listed so nobody looks for them): the agent's own gog token (`oauth_api.py` mints it for the **agent's** mailbox and refuses any other account; sealed `zks1` on a zero-knowledge agent; `apps/admission/gmail.py` reads it server-side for Gmail push, which therefore cannot work for a zero-knowledge agent), `Agent.op_sa_token_enc` and `Workspace.shared_op_sa_token_enc` (being retired), Slack installation tokens, capability slot secrets.

### The vault, as far as this design needs it

- A `Vault` has members (direct ∪ links), and **every member can open every value in it**. A machine may hold its key while its device key is active and ECDH-approved, it serves an attached agent, and its person is a member (`docs/architecture/vault.md`).
- Values are `zk1` (under the vault key, §2 of `docs/vault/zk-v1/zk_v1_reference.py`) or `zks1` (sealed to each recipient, §6). A holder of the vault key "settles" a `zks1` value into `zk1`, after which every member can read it.
- Endorsements (§7) are a person's signature over "this key may read this vault". A keeper (a box that holds the key) grants only to endorsed targets, and only a box whose person is an owner or admin of the vault is a keeper.
- Workspace vaults use the binding id `vault:<uuid>` everywhere the agent id goes in the formats, and are always zero-knowledge. A runner's Claude credentials already live in one (`2026-10-23-runner-claude-credentials-in-the-vault-design.md`), sealed by the box or the browser, never by canopy-web.

So the format and engine exist. What is missing is a vault whose readership is not "every member".

## The design

### 1. A lent credential is its own small vault

Each lent credential is a `Vault` of a new kind, **`lent`**, holding exactly **one** secret, beside a `LentCredential` row with the metadata. One vault per credential, not per lender or per agent, because the vault key is the unit of readership. A box that holds one lent vault's key can open that credential and no other.

It reuses everything a workspace vault has: `VaultSecret` for the ciphertext (`zk1` under the lent vault's own key), `VaultKeyWrap` for copies of the key, `VaultAttachment(agent=…)` for the agents that may borrow it, `vault_engine` for every write, the binding id `vault:<uuid>` in every AAD/HKDF info. **No new byte format.** What differs is in `engine.Store` (who is eligible) and in four refusals:

- **Never a member but the lender.** `vault_members.members(lent vault)` is the lender only, as `owner`, from a new membership source, `lender`, that reads `LentCredential.lender`. (`owner` cannot be assigned through a `VaultMember` row, `vault_members._ASSIGNABLE`, so a source is the right place.) Membership routes refuse on a lent vault (409 `lent_vault_has_one_member`). No links. The agent's owner and admins are not members.
- **Never settled, never layered.** A `lent` vault's values are never read by `/credentials/resolve`. `workspace_vaults.layers_for` already returns only `kind=workspace` vaults; a test pins that it stays so. They are never imported into another vault, and never re-encrypted by anyone but the lender's browser or the lender's own box.
- **Where it lives.** `Vault.workspace` is the borrowing agent's workspace (`lent` is added to `Vault.KIND_CHOICES` and to the `vault_agent_iff_agent_kind` constraint, `models.py` ~785). A lent vault may be attached outside its own tree, unlike a workspace vault (`workspace_vaults.attach` refuses that today), only under today's Salesforce rule: the person attaching owns both the lending agent and the borrower (`set_salesforce`), re-checked on every issue.
- **Always zero-knowledge**, whatever the borrowing agent's mode. A non-zero-knowledge agent (`AgentCredential.value_enc` under KMS/Fernet) can still borrow; its own secrets stay where they are.
- **No recovery wrap of its own.** The lender's vault key (and their personal recovery code, §4c) opens it, so `vault_engine.check_full_set` is kept as is: a lender whose vault key has no recovery code is sent to Settings → Vault key before lending. A lost lent credential is replaced, not recovered: the lender makes a new token.

### 2. Who can open it

| | Who | Rule |
|---|---|---|
| Person | **The lender only** | They have a vault key. Their copy is made by their own browser when they lend. |
| Machine | **A box the lender allows** | Its device key is active with an approved ECDH key; its runner serves an agent the vault is attached to and may hold it (`runner_may_hold_agent`); **and the device key carries the lender's own endorsement for this vault** (§7, `by` = lender, message bound to `vault:<uuid>`). |

The endorsement replaces "the box's person is a member". It is what makes "the lender and nobody else" hold: canopy-web cannot forge one, an agent admin cannot make one, and a keeper grants only to an endorsed target.

**Endorsements bound to a vault are new.** Today `AgentVaultEndorsement` requires an agent (`models.py` ~719), the signed message carries an agent id (`zk_endorse.endorse_msg`), and workspace vaults have none. This design adds `VaultEndorsement(vault, kind, target, key_jkt, by, by_jkt, sig)`, with the §7 message carrying `vault:<uuid>` where the agent id goes (the same substitution the binding id already makes in AAD and HKDF info; a new test vector pins it), plus `GET|POST /api/vaults/{id}/zk/endorsements`. Unlike every other vault, **canopy-web verifies the lender's signature on every wrap write to a lent vault** (`zk_endorse.verify` against the lender's current keyset key) and refuses a device wrap without one. A workspace vault's wrap writes check eligibility only (`vault_engine.parse_wraps`), which is not enough here, because the endorsement is the readership rule.

Which boxes the lender endorses:

- **The lender's own boxes** (runner owner = lender) that run the agent: endorsed in the same passkey touch as lending, and automatically afterwards. Approving a new device key of one's own already signs an endorsement (`approveAndGrant`); that touch also signs for each of one's lent vaults the box is eligible for.
- **Somebody else's box** (Sarvesh lends to ACE, whose cloud boxes Jonathan owns): only by the lender choosing it by name, one box at a time, on a screen that says plainly: *"Jonathan owns cloud-ec2-2 and has root on it. Once it opens your token, Jonathan could read it."* Stored as `LentBox(lent, runner, endorsed_at)`, audited as a setting. Until the lender chooses, that box is not eligible and the turn gets a 409 that names the box and the lender.

**Keepers.** Only a box the lender owns is ever a keeper of a lent vault (the existing rule: a keeper's person must be an owner or admin of the vault, and the lender is the only one). It passes the key only to a target with the lender's endorsement. `vault_keeper._held_workspace` does not filter on kind today, so a lent vault would already appear in a keeper's work and then be refused by `grant_workspace`. Both are extended to lent vaults, with the work listing each target's `VaultEndorsement`. The check itself runs in the `canopy` CLI (`canopy vault zk keep`) and is repeated by canopy-web at the write. `zk_notify`'s grace email goes only to the lender for a lent vault. So a new box of the lender's own gets the key within a minute. A box of somebody else's gets it from the lender's browser, or from a keeper once the lender has endorsed it.

**Removal is checked on every read, never swept**, as for every vault: the lender leaving the workspace, a box's owner losing admin of the agent (`runner_may_hold_agent`), a revoked device key, an attachment removed. Each of these makes the box ineligible at once, whatever copy it still holds.

### 3. The metadata, in clear

`LentCredential` (1:1 with its vault; clear columns because canopy-web must enforce them without reading the secret):

| Field | Meaning |
|---|---|
| `vault` | The `lent` vault holding the one secret. |
| `lender` | The person accountable. Required. |
| `lender_agent` | Set when the identity is an *agent's* own (Salesforce: Eva). The loan is in force only while `lender_agent.owner == lender`, as today (`_in_force_salesforce`). |
| `provider` | `github`, `salesforce`, `other`. Decides how a box injects it and probes it. |
| `identity` | Who it acts as, e.g. `{login, user_id, name}` for GitHub, `{username, organization_id, instance_url}` for Salesforce. Used for the git identity (noreply email) and every label. |
| `purpose` | One line the lender writes or accepts ("Hal opens PRs on canopy-web as me"). |
| `resource`, `scopes` | What it reaches as the lender declared it (repo list, permission names). Shown, not enforced: the provider enforces them. |
| `for_turns_by` | Whose turns may cause it to be used: `lender` (only the lender's own turns), `admins` (turns whose accountable person is the agent's owner or an admin), `full` (any full-profile turn, editors included: "GitHub for all users to use", an explicit choice). A confined (caller) turn never gets one, whatever this says. |
| `not_after` | Required. When canopy stops handing it out. |
| `provider_expires_at` | What the provider says (GitHub's token-expiration header), once a browser or box has seen it. `not_after` is never later than it. |
| `checks`, `checked_at`, `checked_by` | The last probe: which repo it can push to and open PRs on, and **who ran the probe** (`browser` or a runner id). A claim, not a server proof (§5). |
| `revoked_at`, `revoked_by` | Set on revocation; the vault is emptied the same moment. |
| `last_issued_at` | When a turn last received it. |

### 4. Which turns get it

One function decides, `lent.for_turn(turn, provider) -> LentCredential | Refusal`, and every route that hands a lent credential to a box calls it:

1. **A confined turn** (`turn.capability`): refused, as today.
2. **The turn's accountable person `P`**: `initiator_user` for a user turn; the recorded accountable person for a system turn (a schedule's creator, `turn_mode._accountable_cap` reads the same field); none for a contact. For an agent-to-agent dispatch, see Open question 2.
   The accountable person is always a **person**: an agent's own canopy login is never `P` (`Agent.is_own_login`). An agent-to-agent dispatch today is recorded as a user turn whose `initiator_user` is the dispatching agent's login. For such a turn, `P` is found by walking `Turn.parent_turn` (`harness/models.py` ~783) back to the first turn whose initiator is a person; a broken chain means no `P`. Turns with no `P` (inbound mail from a contact, a Slack message from a non-member, a schedule with no known creator, a drill, a dispatch chain that ends in none of these) are Open question 2.
3. **`P`'s own loan first.** If `P` lent this provider to this agent themselves, any `for_turns_by`, use theirs: the work is attributed to the person who asked for it.
4. **Else the agent's standing loan**, picked as `delegation_for` picks today: the owner's while they are a member of the workspace, else (GitHub only; Salesforce stays owner-only, as `_in_force_salesforce`) the most recently saved loan of a current admin. It is used only if its `for_turns_by` admits `P`. `admins`: `P` is the owner or an admin (`Agent.is_admin`). `full`: `access.decide` gives `P` the full profile. `lender`: `P` is the lender.
5. **Else 409**, saying whose loan exists and why it does not apply ("the owner's GitHub token is for turns started by the owner or an admin; maria@ is an editor — lend your own at …").

Then: not revoked, `now < not_after`, the box is eligible (§2) and holds a copy of the key. Each issue writes an `agent.lent.issued` event (lender, loan, turn, runner, `P`) and stamps `last_issued_at`. The `Requested-by:` trailer stays.

`RepoIdentity` (an agentless repo turn runs as an agent) is unchanged: it picks the agent, then this function picks the loan.

**Per-person lends by non-admins.** Anyone with full access to an agent may lend their own credential to it, with `for_turns_by=lender` (the only value they may set; `admins` and `full` need the lender to be the owner or an admin, as `set_github` requires today). Their turns then act as them.

This matters only on **fleet boxes**. A box runs an agent's turns only if its owner is one of the agent's admins (`runner_may_hold_agent`, a CLAUDE.md invariant this design does not relax), so a non-admin's turns always run on someone else's box, and their loan reaches that box only after they endorse it by name, on the screen that names its owner. On their **own machine**, running the agent locally under a license, they need no loan: Claude Code there uses their own ambient `gh` login, and no laptop runner fetches a GitHub token (only `cloud_runner.py` calls the per-turn route). `LICENSE_WITHHELD_EXTRAS` is unchanged.

### 5. Validating a credential canopy-web cannot read

Today canopy-web decrypts the token to probe GitHub. In this design:

- **At lending, in the lender's browser.** The browser checks the token's shape (`_TOKEN_SHAPE`), calls `GET https://api.github.com/user`, and, if the browser can make them, runs the PR and push probes (`probe_pull_request`, `probe_push`, ported to TypeScript). It shows the result before sealing, and refuses to seal a token that cannot reach the agent's repo, as `set_github` does today. What it learned goes into `identity`, `checks` (with `checked_by: browser`) and `provider_expires_at`.
  - **Build step 1 is to measure** whether api.github.com allows these calls from a browser: the POST probes under CORS, and whether `github-authentication-token-expiration` is in `Access-Control-Expose-Headers`. If either is blocked, the browser does only `/user` and the shape check, and the lender sets `not_after` themselves. The first box (below) fills in the rest.
- **On a box, after it gets its copy.** The box runs the same two probes and reads the expiry header (ported to the standard library: `cloud_runner.py` cannot import `apps`), then reports `POST /api/harness/runners/{rid}/lent/{id}/checks` `{checks, provider_expires_at}` (device proof required; only a box that holds a copy). The box then runs this probe on each readiness pass, so `github.<slug>` readiness (`delegations.readiness`) keeps reading stored checks, now written by boxes. `runner_github_readiness` and `POST /api/agents/{slug}/github/check` stop calling `check_github` for a lent credential: the first reads the box-reported checks, the second asks the agent's boxes to re-probe on their next tick. Salesforce is probed only this way (Salesforce APIs refuse cross-origin calls unless each org allow-lists the origin).
- **What canopy-web enforces itself:** `not_after` (it hands out nothing afterwards), `for_turns_by`, the box rules, revocation. These need no plaintext.
- **What it now takes on trust:** `identity` and `checks` come from the lender's browser or a box. A lender who lies harms only themselves, and a box that lies is already trusted with the token. The git identity a box commits with comes from `identity`; a wrong one makes commits that GitHub attributes to the token's real owner anyway.

### 6. How a box receives it

`POST /api/harness/runners/{rid}/turns/{tid}/github-token` keeps its path and gains a sibling for any provider, `…/turns/{tid}/lent/{provider}`. Both now **require a device proof** of a key on that runner. The `RunnerAccessToken`'s own `device_key` (bought with a device proof) counts as the proof, so no new header is needed. Both are runner-protocol routes, listed in `apps/mcp/api_tools.py`'s runner exclusions as `turn_github_token` is. They answer:

```
{ "lent": "<id>", "vault": "<uuid>", "kv": 3,
  "value": "zk1:…",                    # the one secret, under the lent vault's key
  "wrap": { …this device's copy… },     # null → 409 lent_box_not_allowed, naming the lender
  "identity": {…}, "git_name": …, "git_email": …, "repo": …,
  "requested_by": …, "not_after": … }
```

The box opens it with the `canopy` CLI (`canopy vault …` with its device key, as for its runner credentials), never inside `cloud_runner.py`, which stays stdlib-only. GitHub goes into the turn's `GH_TOKEN` as today. Salesforce moves from the agent-level resolve to this per-turn fetch: the claim payload names the providers lent to the turn's agent, and the runner writes `.sf-creds.json` into a directory for that turn (today it is per agent, `cloud_runner.py` ~1495–1520, so two concurrent turns would share it) and removes it when the turn ends.

`/credentials/resolve` stops returning `github_token` and `salesforce_creds` to anyone (Phase 0 below). After Phase 3 the fields are gone from `AgentCredentialsResolveOut`.

### 7. Time limits, renewal, revocation

- **Every lent credential has `not_after`.** The lender picks it when lending: a default of 90 days, never later than `provider_expires_at`, and never more than a cap (Open question 1). A GitHub token with no expiry still gets a `not_after`.
- **Renewal.** From 14 days before (`EXPIRY_WARNING`), the lender sees a Home item and an email: "Your GitHub token for hal stops working on 12 Jan." **Renew** is either *paste a new token* (the browser re-seals into the same vault; boxes keep their key copies, since the key does not change) or *extend*, when the provider's own expiry is later: one passkey step-up moves `not_after` up to `provider_expires_at`, and nothing is re-sealed. An agent admin sees "expires 12 Jan, lent by Sarvesh" and a "Remind Sarvesh" button, never a renew button.
- **On expiry** canopy hands out nothing, `github.<slug>` readiness goes red, and the 409 names the lender. Thirty days after `not_after` without renewal, canopy deletes the ciphertext and the key copies, from a job on the off-request clock (`apps/common/ticks.py`). The row stays as history.
- **Revocation by the lender, at any time**, from `/settings` or the agent's Credentials page (browser, step-up; a token may revoke too, because revoking never needs a passkey to be safe). In one transaction: `revoked_at` is set, the secret and every key copy are deleted, and every box that held a copy gets a durable `LentForget(runner, lent)` row it drains on its next poll tick, as `RunnerBinding.close_requested` is drained (the doorbell rule: never a WS frame alone). On that tick the box deletes its key copy and any staged file.
- **Running turns are not recalled.** A turn that already received the token has it in its environment until the turn ends. The revoke screen says how many turns are running with it now, and links to the provider's own revoke page (`https://github.com/settings/personal-access-tokens`) as the way to cut them off at once. An agent admin can also cancel those turns (`cancel_turn`), as today.
- **Others can stop using it, never read or change it.** An agent's owner or admin can detach a loan from their agent ("stop using Sarvesh's token here", a new agent-admin route that deletes the `VaultAttachment`; they are not vault members, so the vault's own routes refuse them). Losing admin of the agent stops an `admins`/`full` loan at once (checked on every issue, as today); the lender is told and can delete it. Leaving the workspace revokes the lender's loans outright, from the same place that ends their other access (`apps/workspaces/departure.py`).

### 8. One model, two storage tiers

Credentials canopy-web must itself use on the server cannot be zero-knowledge. They keep their storage but get **the same metadata and the same list**: lender, provider, identity, purpose, which agents and what use, `not_after`, revoke.

| Credential | Tier | Why | What changes |
|---|---|---|---|
| GitHub PAT lent to an agent | **Vault** (`lent`) | Used only on boxes. | This design. |
| Salesforce identity lent between agents | **Vault** (`lent`, `lender_agent` = Eva) | Used only on boxes. One secret, N borrowers: one lent vault attached to each borrower, so a re-mint still reaches every borrower in one write. The vault is attached to Eva too, so Eva's own turns read it and Eva's box (Jonathan's, so a keeper) may hold its key and seal Eva's re-mints into it. Eva's own vault stops holding a copy. | This design. |
| GitHub App connection (`GitHubConnection`) | **Server** | canopy-web itself reads repos (skill history), and the refresh token rotates on every use, so exactly one process may refresh it. | Gains `purpose`, `not_after` (= the refresh token's expiry), and a row in the lender's list ("canopy-web reads hal's skill history as you"). Disconnect is the revoke. |
| Host grant (`HostGrant`) | **Server** | The gateway calls the host as the visitor during a turn. Short-lived (≤ `MAX_TOKEN_SECONDS`) and DPoP-bound. | Already has `expires_at`; it shows in the "live page grants" list (another PR) under the same columns. |
| Chat-shared secret (`SessionSecret`) | **Server today; vault later** | A hand-off to the agent, not standing access. | Phase 4: the browser seals it to the eligible boxes of the session's agent (`zks1`, never settled), with a default `not_after` of 24 hours; it shows in the list while it lives. |
| Scaffold push at creation | **Server, transient** | One request, nothing stored. | Unchanged. The same paste is sealed by the browser right after (§Migration). The weaker spot is named below. |

The server tier is one interface (`lent.ServerHeld`: `lender`, `purpose`, `identity`, `not_after`, `revoke()`) implemented by each model, so the list and the alerts read one shape. Its rows are labelled **"canopy-web uses this itself"** so nobody mistakes them for sealed ones.

### 9. UI

- **`/settings` → "What you've lent"**, beside "Session reviewers" and the "live page grants" list (another PR). Pair them under one heading, **"Access you've given"**, as three tables. This one lists, one row per credential: provider and identity (*GitHub as @sarvesh*), the agents it is lent to, whose turns may use it, the boxes that can open it (with each box's owner), `not_after`, last used, and **Renew** / **Revoke**. A second small table lists the server tier, labelled as such. An expired loan stays visible for 30 days, greyed, with **Renew**.
- **Agent → Settings → Credentials → GitHub / Salesforce**: the loan in force and its rule for everyone with access ("Lent by Jonathan · for turns started by the owner or an admin · until 12 Jan · can push to dimagi-internal/hal · checked by cloud-ec2-2 yesterday"). The lender also sees Renew, Revoke and "Allow another box". Anyone with full access who is not the lender sees **"Lend your own, for your own turns"**. Admins see "Stop using it here".
- **Lending** is one screen: paste the token (canopy pre-fills GitHub's form as today, `create_url`), then a browser check with results, `not_after` (90 days preselected), `for_turns_by` (owner/admins see three choices with `admins` preselected; others see only theirs), and the boxes that will be able to open it, listed with their owners, the lender's own preselected. One passkey touch then makes the vault key, seals the token, wraps the key to the lender and to the chosen boxes, and endorses them.
- **Home**: expiring soon, expired, and "cloud-ec2-3 now runs ace but can't open your GitHub token until you allow it", each as an alert through the one `<AlertList>`.

## Migration

No agent loses its credential between steps. Each step ships alone.

**Phase 0: close the leak now, without changing storage.**
1. `/credentials/resolve` stops returning `github_token` to every caller. The cloud runner never asks for it (`RESOLVE_EXTRAS` in `cloud_runner.py` excludes it); before merging, the builder greps the `canopy` CLI repo for readers of `github_token` from resolve and moves any to the per-turn route.
2. `salesforce_creds` goes only to a `runner` read, never an `admin` read. The gate uses `credential_access`'s answer, before `_classify_read` relabels an owner's own laptop from `runner` to `admin`. The laptop canopy plugin stages `.sf-creds.json` too (`cloud_runner.py` ~1495 says so): the same grep of the `canopy` repo confirms it reads as `runner` before this ships.
3. `github_token_for_turn` applies `for_turns_by=admins` to every existing delegation: the restriction Jonathan approved, with `P` as §4 defines it. This step waits for the answer to Open question 2, because today every turn with no person behind it (dispatches, inbound mail, schedules with no creator) gets the owner's token, and refusing them stops Hal's scheduled and inbound PR work. Ship it with an `agent.github.refused` event so the first week shows who it stopped.

**Work in the `canopy` CLI repo**, needed by Phase 1: a command that opens a lent value with the box's device key (`canopy vault lent open`); the keeper's lent-vault work and its `VaultEndorsement` check; the stdlib GitHub/Salesforce probes on the box; and the CLI version a box must run before canopy-web sends it the sealed form. On an isolation-unit box, the key that opens a lent vault is the unit's own software key (`runners.md` § Isolation units), and that key is what the lender endorses.

**Phase 1: lent vaults exist; both paths are live.** `Vault(kind=lent)`, `LentCredential`, `LentBox`, the eligibility and keeper rules, `lent.for_turn`, the sealed per-turn response, box probes, the lending screen and the `/settings` list. The per-turn route returns the sealed form when the agent's loan in force is a `LentCredential`, and the old plaintext when it is still an `AgentDelegation`. The runner accepts both, so a runner on the old code keeps working: the sealed form is sent only to a runner that announces it (`capabilities.lent_v1`).

**Phase 2: lenders move their own tokens over, with one passkey touch each.**
- Each GitHub lender gets a Home item: "Move your GitHub token for hal into your vault." The browser asks canopy-web for the old token **once**, through a lender-only route (`POST /api/me/lent/legacy/{id}/release`). From a browser it needs a passkey step-up, since it reveals a secret. A token is allowed too, because the token is the lender's own and they pasted the secret ("MCP does everything": no token-refused gates). A runner token is refused (`runner_auth` reaches runner-protocol routes only). It validates the token, seals it, and writes the lent vault. In the same transaction as the ciphertext lands, canopy-web deletes `secret_enc`.
- **Is canopy-web decrypting it once acceptable?** Yes. canopy-web has held the plaintext since the lender pasted it, so handing it back to the person who pasted it, behind their passkey, exposes nothing new. The alternative, asking every lender to make a new token, buys nothing unless the old token is also revoked at GitHub. The screen offers that as an option ("make a fresh token instead"), not the default.
- **Salesforce:** Jonathan, as Eva's owner, makes Eva's lent vault on Eva's Credentials page. Eva's box, which already holds the plaintext and is Jonathan's, seals it, as the runner-credentials migration did. The vault is attached to every agent with a Salesforce `AgentDelegation` row, and Eva's re-mint path writes to it.
- **Lenders who don't act** keep working on the old row until a cutoff, 30 days after Phase 2 ships, with weekly reminders to them and a notice to each agent's owner. After the cutoff, the old row stops being issued (409 naming the lender) and its `secret_enc` is deleted.

**Phase 3: remove the old path.** Drop `AgentDelegation.secret_enc` and the plaintext branch of the per-turn route. `AgentDelegation` rows become `LentCredential` history (a data migration copies `meta`, `expires_at` and the lender). Drop `github_token` / `salesforce_creds` from the resolve schema and from `RESOLVE_SECRET_EXTRAS`, `LICENSE_WITHHELD_EXTRAS` and `vault_alerts._EXTRAS`, and regenerate `generated.ts`. Docs: `runners.md` § "The GitHub identity a turn gets" and `vault.md` get a § "Lent credentials". `access.md` § Credentials drops "the owner's GitHub delegation" from what a license withholds and says what a license may now receive (its holder's own loan).

**Phase 4: chat-shared secrets** become browser-sealed, short-lived lent entries (§8).

## What is weaker, what is stronger

**Stronger**
- A canopy-web compromise (database plus `FIELD_ENCRYPTION_KEY` or KMS access, or a malicious deploy reading memory over time) no longer yields any lent GitHub token or Salesforce refresh token. Today it yields every one.
- The agent's other admins cannot read a lender's token. Today any admin with a bearer token gets it from `/credentials/resolve`.
- Which boxes can open it is the lender's own signed choice. canopy-web cannot add a box, and an agent admin cannot add their laptop as a runner and receive the token.
- Every loan expires, is listed in one place, and is revocable by the person whose authority it is.

**Weaker, and the mitigation for each**

| Weaker | Mitigation |
|---|---|
| canopy-web cannot check the token itself. A wrong `identity` or a stale `checks` is believed until a box probes. | The box probes on first use and on every readiness pass, and the probe records who ran it. `not_after`, `for_turns_by` and revocation, the controls that matter, need no plaintext. |
| **A box that once opened it could keep it**, and canopy-web can only stop handing it the ciphertext. Today a box gets the plaintext every turn, so this is the same exposure, not a new one. What is new is that the box holds a standing key copy rather than asking per turn. | Fine-grained tokens scoped to one repo; `not_after` (90 days by default); boxes of other owners only by name, with the owner's root access spelled out; a `forget` row on revoke; and a "revoke at GitHub" link, which is the only true recall. Rotating a lent vault's key is pointless here (one value, and a box that kept the key kept the value too): the response to a box that may have kept it is a new token. |
| Running turns keep the token after a revoke. | Said on the revoke screen, with the count, the GitHub link, and Cancel for the turns. |
| Renewal and adding a box owned by somebody else need the lender's browser. If the lender is away, the agent waits. | Reminders start 14 days ahead; the 409 and readiness name the lender; "extend" needs no re-paste; the lender's own boxes are added by their own keeper with no click. An owner who wants no single point of failure lends their own token with `admins`, and admins may each lend theirs. |
| Lost access to the lender's vault key means the loan cannot be renewed. | It is replaced, not recovered: the lender lends a new token. This is the same cost as today. |
| The scaffold push at agent creation still sends the token through canopy-web once. | It is never stored, and the lending screen seals the same paste right after. Moving the push into the browser (GitHub's git data API) is possible later if this matters. |

## Testing

- **Readership:** an agent admin, an editor, a workspace owner and canopy-web's own code path each fail to obtain the plaintext or a key copy of a lent vault. Membership routes refuse on it. `layers_payload` never includes it, and neither does `/credentials/resolve` in any form.
- **Boxes:** a box of the lender's own gets a copy from a keeper; a box of another owner waits until the lender endorses it; a forged or missing endorsement, a box whose owner lost admin of the agent, a revoked device key and a detached agent are each refused at issue.
- **`for_turn`:** the matrix of accountable person (owner, admin, editor, contact, schedule by owner, schedule by editor, agent dispatch traced to an owner, to an editor and to nobody) × `for_turns_by` × "P has their own loan", with the expected loan or 409 text. A confined turn is always refused.
- **Expiry and revocation:** nothing is issued at or after `not_after`; extend cannot pass `provider_expires_at`; revoke empties the vault and writes `forget` rows in one transaction; a box drains its `forget` row and deletes its copy; the 30-day sweep deletes the ciphertext of expired loans.
- **Migration:** Phase 1 serves the old and the new form side by side; the legacy release route refuses a PAT, a runner token, a non-lender and a request without step-up, and deletes `secret_enc` only when the ciphertext lands; Salesforce borrowers keep resolving across the switch.
- **End to end:** a real turn on a cloud box opens a PR with a lent token, and the token never appears in any canopy-web response, log or event.

## Open questions only Jonathan can answer

1. **The longest a lent credential may last.** Recommendation: 90 days by default and 366 days at most (GitHub's own maximum for a fine-grained token), the same for everyone. A shorter cap means more renewals; a longer one means more exposure from a box that kept a copy.
2. **What happens to turns with no owner or admin behind them?** Today all of these get the owner's GitHub token: an agent's dispatch (Ada → Hal), inbound mail or Slack from a contact, a schedule with no known creator, a drill. Under "owner/admin-started only" they would get a 409, and Hal's scheduled and inbound PR work would stop. Recommendation: (a) a dispatch counts when its chain traces back to the owner or an admin (§4), else no; (b) a schedule counts by its creator; (c) a turn caused by a contact or nobody gets no token unless the owner set the loan to `full`, so the owner opts into "anyone who can make this agent work may make it push as me", per agent. The alternative is to keep giving every unconfined turn the token and restrict only editors. That is simpler, but it is today's exposure.
3. **Running a lender's token on a box owned by someone else.** Recommendation: allowed only when the lender names the box, told that its owner could read the token. The alternative is to forbid it, so an admin who lends must run a box of their own. That is safer, but it means ACE's admins cannot use their own tokens on Jonathan's cloud boxes.
