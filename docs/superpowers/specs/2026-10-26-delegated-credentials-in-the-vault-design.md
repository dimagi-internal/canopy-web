# Delegated credentials live in the vault; agents act as you only with your permission

**Status:** design 2026-10-26; Parts 1–2 built for GitHub the same day (see "As built"). Jonathan asked for it after an audit flagged that a person's GitHub token, given to an agent, could be read by the agent's other admins. He set the model and the words in review, including the name: a **delegated** credential.

## As built (GitHub)

Where the build settled a question this design left open, or chose differently:

- **Format: zk-v1 §6 `zks1` (sealed to recipients), not a new `zke1`.** A delegated value is sealed directly to each machine that runs the agent (and to its person's vault key, when they have one), with no vault key involved — the format every client already opens. What makes it delegated is the entry's `account`, enforced server-side: it is **never settled** into the vault key (every generic value write refuses its name, 409 `delegated_entry`; enable and rotate leave it out of coverage), a member's read of the vault names it and never carries its ciphertext, and a resolve hands it out only in its own `delegated` field to a `runner` read proved by one of those machines (`apps/agents/credentials/accounts.py`). Old CLIs never see it, so none can settle or inject it.
- **Sealed by canopy-web at the moment it is given.** The paste reaches canopy-web once, as it always has: it is checked against GitHub, sealed, and no copy canopy-web can read is kept. Sealing in the person's browser, with the endorsement check, is a later step; until then a compromised canopy-web at the moment of giving could take the token, which is the exposure the paste always had.
- **Which machines** (`accounts.delegated_devices`): an active device key with an approved ECDH key, on a runner that serves the agent, whose owner is one of its admins (`runner_may_hold_agent`), and that runs turns (not a plain computer), and that is a cloud box or the delegating person's own machine (`accounts.may_hold_for`). Giving one is refused (409 `no_machine_can_open`) while there is none.
- **A new machine** gets it from a machine that holds it: the keeper's work (`GET …/vault-keeper`) lists each delegated entry the box holds that a machine is waiting for, and `PUT …/vault-keeper/{slug}/delegated/{name}` takes the re-sealed value, refused for any recipient that is not one of those machines or its person, for dropping a machine that holds it, and when stale. The box checks each waiting machine's endorsement, as for the vault key (`canopy vault zk keep`).
- **Checks** run on the boxes: a box that opens the token probes it at most every six hours and reports `POST /api/harness/runners/{id}/delegated-checks`. canopy-web shows what the last machine found, and by whom.
- **A turn's token**: `POST …/turns/{id}/github-token` returns the git identity and `delegated: "github-token"` (no token); the cloud runner takes the token from its own resolve of that agent, opened by the canopy CLI.
- **Moving the old rows**: `manage.py move_delegations_to_vault [--apply]` (dry run by default). An agent no machine can open one for keeps its old row, which still serves.
- **Your list**: `GET/DELETE /api/me/delegated-credentials/`, the Settings section "Your credentials delegated to agents".
- **Salesforce** is not moved yet. Eva's own Salesforce credential is zero-knowledge, so the old lending already handed nothing out; `salesforce_creds` now goes only to a box's own runner read, never an admin's.
- **Act-as-you permissions** for pages shipped separately (explicit page grants, 30 minutes, Stop).

## Glossary

**The rule: an agent acts as itself — with its own credentials or ones a person delegated to it — or as you only under an act-as-you permission you granted.**

- **Agent's own credential.** The agent's own login or account: ACE's labs token for ace@dimagi-ai.com, an agent's own Google account. Most of what is in an agent's vault is this.
- **Delegated credential.** A standing credential from a *person's* own account, which they gave the agent to use: Jonathan's GitHub token for ACE. Shown as "GitHub · delegated by Jonathan Jackson". canopy gives it no expiry of its own: the external system's expiry, such as GitHub's own token expiry, is the only one.
- **Act-as-you permission.** A time-limited permission that the person asking grants in the moment, letting the agent act within *that person's* access: a page grant on Connect Labs, minted when they first send, for 30 minutes. It is visible ("Hal can act as you on this page until 3:42 pm · Stop") and can be stopped. It is never stored as a credential.

The first two are used by the same turns and sit in the same vault. They are **distinct in the data and in the code**, and they differ in one thing that matters: **who can read them.** The agent's own credentials have the vault's normal readership. A delegated credential is readable by **no person** through canopy, not even the agent's admins: only by the machines that run the agent's turns, and, for writing, by the person it was delegated by (§1.4).

## Intent

Jonathan's words, lightly edited: *"Agents should never act with the delegated authority of a user unless it's explicitly configured that way (like GitHub for all users to use), or with a very explicit, time-limited grant. Now that we have the vault system, even delegated access stored in canopy-web should go through the vault, stored with the knowledge that it's delegated access — metadata on the key. Right now we have two different, confusing ways to do this."*

The two ways today are the **vault** (an agent's own secrets) and **`AgentDelegation`**. `AgentDelegation` is a person's GitHub token, held by canopy-web under the app-wide key, plus a Salesforce row with no secret that points at another agent's credential. Beside them sit several **implicit** paths by which an agent acts with a person's authority, which nobody granted as such. This design moves delegated credentials into the vault, marked as delegated on the entry itself. It also turns each implicit path into an explicit act-as-you permission or removes it.

## Goals

1. Every vault entry records whose account it is: `agent` or `delegated`. A delegated one also records whose account it comes from, the provider and the external identity. Code reads this through one helper, and the vault UI shows it on each entry.
2. Delegated credentials live in the vault. `AgentDelegation.secret_enc` goes away, and its GitHub and Salesforce rows migrate as `delegated`.
3. The agent's own entries keep the vault's ACL. A delegated entry is opened only by the machines that run the agent's turns. The person it came from can replace and withdraw it, but cannot view it, and no other person can read it through canopy.
4. canopy sets no expiry on a delegated credential; the provider's own expiry is the only one, shown as information. A machine's failed probe tells the person to replace it. The person whose account it is can withdraw it at any time.
5. An agent acts within a person's own access only under an act-as-you permission. Every path that does this today becomes one or goes away.
6. Credentials canopy-web must use itself on the server keep their storage, carry the same fields, and show in the same lists.
7. No agent loses a credential for a single turn during the move.

## Non-goals

- A per-entry rule about which turns may use it. A delegated credential is used in the turns `access.decide` and the turn's profile allow, like the agent's own. A confined (caller) turn's environment is an allowlist that holds neither (`_confined_env` in `cloud_runner.py`).
- Hiding a delegated credential from the owners of the machines that run the agent. A machine's owner can extract anything the machine decrypts for a turn (§1.4, "The limit").
- Tokens canopy issues to people for themselves (PATs, the widget's `DelegatedToken`, OAuth sign-in), where the person acts, not an agent.

## Today

### Credentials from a person's account, held for an agent

| Credential | Stored | Who uses it, and how | Who can read it today |
|---|---|---|---|
| **GitHub fine-grained PAT** (owner's, else an admin's) | `AgentDelegation.secret_enc`, Fernet under `FIELD_ENCRYPTION_KEY` (`apps/agents/models.py` ~1100–1135; `delegations.set_github`) | **Runner**, per turn: `POST /api/harness/runners/{rid}/turns/{tid}/github-token` (`apps/harness/api_runners.py` ~433), plaintext, into `GH_TOKEN` (`cloud_runner.py` ~5119). **Server**: `check_github` decrypts it to re-probe GitHub, from `POST /api/agents/{slug}/github/check` (`api_credentials.py` ~794) and at every box boot (`runner_github_readiness`, `api_runners.py` ~538). | Any agent admin with a bearer token, through `/credentials/resolve` (`credential_resolve.py` ~308–314); canopy-web. That is a third ACL beside the vault's, which is the confusion. |
| **Salesforce** (Eva's Salesforce user, used by Echo) | No secret on the row (`secret_enc=""`, `lender` FK); the value is Eva's `AgentCredential` named `salesforce`, which must be non-zero-knowledge (`delegations.py` ~455–601) | **Runner**: `/credentials/resolve` returns `salesforce_creds` in plaintext; `cloud_runner.py` ~3087–3167 writes `.sf-creds.json`. **Server**: `probe_salesforce` at set and check. | Every admin of the using agent (the same `resolve` path); everyone who can read Eva's vault; canopy-web. |
| **A secret shared into a chat** (`SessionSecret.value_enc`, `apps/canopy_sessions/models.py` ~820) | Fernet, scoped to one session | A hand-off: the agent moves it where it belongs (`services.resolve_secret`). | canopy-web; whoever can act in the session. |
| **Scaffold push at creation** (`apps/agents/api.py` ~389) | Not stored | **Server**, once in the request; the same token is then saved with `PUT /api/agents/{slug}/github`. | canopy-web, for the length of the request. |

Nothing in the vault today says whose account an entry is. `AgentCredential` and `VaultSecret` hold a name and a value only.

### Credentials canopy-web uses itself

| Credential | Stored | Use |
|---|---|---|
| **GitHub App connection** (`GitHubConnection.refresh_token_enc`, `apps/github_connect/models.py`) | Fernet | canopy-web reads an agent's repo as its owner for skill history (`skill_history.py` → `github_app.access_token_for(agent.owner)`). The refresh token rotates on every use, so exactly one process may hold it. |
| **Host grant** (`HostGrant.access_token_enc`, `apps/tokens/models.py` ~765) | Fernet, DPoP-bound, at most 900 s (`canopy_sdk.contract.ACCESS_TOKEN_MAX_LIFETIME`) | The gateway calls the host's MCP as the visitor. An act-as-you path (below), held server-side. |

### Paths where an agent acts with a person's authority

| Path | What it does today | Granted by the person? |
|---|---|---|
| **Host grants** (`apps/host_grants/host_grants.py`, `HostGrant`; canopy as its own host, `apps/host_grants/self_host.py`, `apps/mcp/delegation.py`) | At every page **arrival** the host's ID-JAG is redeemed for a token, and the agent's `site_call`s on that visitor's turns run as the visitor, within the page's scope. A page re-arrives every few minutes, so the grant is effectively standing while the page is open. | No. It is implicit in opening the page, and nothing lists it or stops it. |
| **Caller tokens** (`apps/harness/caller_tokens.py`, `CallerToken`) | A confined turn's, or a non-admin's full-profile turn's (`scoped`), canopy MCP calls run **as the conversation's current asker**: their own ACL, filtered to the capability's tools. The asker's ACL is not a subset of the agent's, so the agent can reach what is the asker's own (their private sessions, their projects). Lives 7 days. | No. It is implicit in asking. |
| **`X-Canopy-On-Behalf-Of`** (`apps/harness/turn_asker.py`) | An agent declares it acts for the person who started the turn. canopy-web then judges runner pinning (`access.may_pin_runner`) and whether a dispatch may run `auto` (`turn_gates`) on that person's access. | No. The agent declares it, and canopy checks only that the person started the turn. |
| **Laptop and emdash agent turns** | A turn on a person's laptop reaches canopy's MCP with the canopy plugin's PAT, which is that person's own. The turn-scoped agent token's phase 2 (laptops) is still open (`2026-10-21-turn-scoped-agent-token-design.md`). | No. It is the box owner's standing PAT. |

**Already explicit, and not act-as-you:** an HCP grant (`contacts.PersonGrant`) and a session-review grant (`canopy_sessions.SessionReviewGrant`). Each is a person's own act, revocable, that lets the agent's **own** login read named data of theirs: the agent still acts as itself, with more to see. They stay as they are and appear on `/settings` (§UI).

## Part 1: whose account each vault entry is

### 1.1 The schema

Every vault entry carries the same account fields, as clear columns beside its ciphertext. canopy-web enforces some of them without reading the secret. One abstract model, `VaultEntryAccount` (`apps/agents/models.py`), is mixed into **both** entry tables:

- **`AgentCredential`**, an agent vault's entries. One row holds either `value_enc` (an agent vault that is not zero-knowledge, KMS/Fernet) or `value_zk` (`zk1`/`zks1`, a zero-knowledge vault; `zke1`, a delegated entry in any vault, §1.4), so a single set of columns covers every mode.
- **`VaultSecret`**, a workspace vault's entries, always zero-knowledge. A runner's Claude-credentials vault is a workspace vault, so its entries carry the fields too.

| Column | Type | Rule |
|---|---|---|
| `account` | `"agent"` \| `"delegated"`, not null, default `"agent"` | Whose account the entry is. |
| `delegated_by` | FK user, null | The person whose account it is. **Required** when `account="delegated"`. |
| `delegated_by_agent` | FK agent, null | Set only when the account belongs to an *agent*, and another agent uses it (Eva's Salesforce user, used by Echo). `delegated_by` is then the person who set up the arrangement, Eva's owner. For `delegated_by_agent` itself the entry is its own (§1.2). |
| `provider` | `"github"` \| `"salesforce"` \| `"anthropic"` \| `"other"`, blank for most `agent` entries | Decides how a box injects and probes it. Required when delegated. |
| `identity` | JSON | The external login: `{login, user_id, name}` for GitHub (gives the commit identity and noreply email), `{username, organization_id, instance_url}` for Salesforce, `{email}` for a Claude account. Required when delegated. |
| `purpose` | short text | One line, shown everywhere ("ACE opens PRs on ace-web"). |
| `scopes` | JSON list | What it reaches, as declared (repos, permission names). Shown, not enforced: the provider enforces them. |
| `provider_expires_at` | datetime, null | What the provider reports (GitHub's token-expiration header), seen at a check. **Information only**: canopy shows it ("GitHub says this token expires 12 Jan") and never refuses or expires an entry on a date of its own. |
| `checks`, `checked_at`, `checked_by` | JSON, datetime, text | The last probe and who ran it: `browser`, `server` or a runner id. |

**Database constraints:**

- `account="delegated"` requires `delegated_by`, `provider` and `identity`.
- `account="agent"` requires `delegated_by` and `delegated_by_agent` to be null.

### 1.2 One helper the code reads

`apps/agents/credentials/accounts.py`:

- `is_delegated(entry, agent) -> bool`: `account == "delegated"`, except that an entry whose `delegated_by_agent` is `agent` is that agent's own. Every agent-vault path asks this, never the column directly.
- `delegated_by(entry) -> User | None`, and `label(entry, agent)`: "GitHub · delegated by Jonathan Jackson" (plus "GitHub says it expires 12 Jan" when known, or "no longer works — replace it" after a failed probe), or "Salesforce · Eva's own account", the one sentence every screen and alert uses.
- `serves(entry, agent) -> bool`: whether resolve may hand the entry out now. It never refuses on a date. For a delegated entry it refuses while `delegated_by` is not an admin of `agent` (the owner counts as one) or not a member of its workspace, which is today's `delegation_for` rule ("an ex-member's token must stop acting the moment they are out"). For `delegated_by_agent`, it also refuses while that agent's owner is not `delegated_by`, which is today's `_in_force_salesforce` rule.

**Readers:** `credential_resolve.resolve` (for the agent's values and each workspace layer), the readiness checks, the per-turn git identity, the vault and Credentials pages, the "no longer works" alerts, and `/settings` → "Your credentials delegated to agents".

### 1.3 Writing an entry

- Every value write takes the account fields beside the value: the agent's `PUT /api/agents/{slug}/credentials` and `…/vault/zk/values`, the workspace vault's `/api/vaults/{id}/zk/values` and `secrets/{name}`, and a box's seal (`canopy vault runner-seal`). They are validated server-side against the constraints above.
- **Only the person themselves can mark an entry as delegated by their account.** `delegated_by` must be the writer. The one exception is the Phase 2 migration, which writes `delegated_by` from `AgentDelegation.user`.
- A vault admin who finds an existing entry that is really someone's personal account ("Mark as delegated by…") sends that person a request. The entry becomes `delegated` when they accept, and then appears in their list.
- A delegated entry's fields can be changed only by its `delegated_by` (replace, withdraw) or, for the agent's side, removed by a vault admin.
- Accepting "Mark as delegated" means the person pastes a fresh token, sealed as in §1.4. The old value has been readable by the whole vault, so it is deleted rather than converted.
- `resolve` returns, beside `values`, an `accounts` map `{name: {account, delegated_by_email, provider, identity, provider_expires_at}}`. A box uses it for the git identity and to label what it holds. It contains no secret.

### 1.4 Where delegated credentials live

- **GitHub:** the agent's vault, name `github-token`, `account="delegated"`, `delegated_by` = the person, `provider="github"`. One per agent. An owner or admin gives theirs, and giving one replaces the current one ("replaces the one delegated by Jonathan Jackson").
- **Salesforce (Eva's user):** a **workspace vault** in `dimagi`, "Salesforce (Eva's account)", attached to Eva and to every agent that uses it. Today that is Echo, in `connect`, below `dimagi`, so inside the vault's own tree. The entry is `account="delegated"`, `delegated_by_agent=eva`, `delegated_by` = Eva's owner. To Echo it is delegated; to Eva it is her own (`is_delegated` = False). One secret and one rotation serve every agent that uses it, as today. Eva's box may hold the vault's key, because Eva is attached, so Eva's re-mint writes there. Eva's own vault stops holding a copy.
- **A Claude account on a box** (a runner's vault) that is a person's own subscription is `delegated` from them, with `provider="anthropic"`. It is not lent to an agent, but it is a person's account, and it shows in that person's list.
- **A secret shared into a chat** stops being a server-held hand-off: "share a secret" seals it from the person's browser into the agent's vault, choosing *the agent's own account* or *delegated from mine*. `SessionSecret` goes away (Phase 4).

### Who can read a delegated entry

**The agent's own entries** keep the vault's readership: every member `credentials.vault_members.members` returns, and their eligible machines.

**A delegated entry** is readable by:

- **The machines that run the agent's turns, and nothing else.** A device key qualifies under today's machine rule: it is active with an approved ECDH key, its runner serves the agent (`device_keys.runner_serves_agent`), and the runner's owner is an admin of the agent (`runner_may_hold_agent`). For a delegated entry in a workspace vault (Salesforce), the agent is any agent the vault is attached to, and the same rule applies.
- **The person it was delegated by, for writing only.** They can replace or withdraw it. No screen or route returns the value to them. If they need it, they make a new token.

No other person can read it through canopy: not the agent's admins, editors, vault members or license holders. Their browsers' `GET …/vault/zk?values=true` and the workspace vault's value read return the delegated entry's name and account fields, never its ciphertext. A `license` read and an `admin` read of `/credentials/resolve` omit it, as `LICENSE_WITHHELD_EXTRAS` withholds the GitHub token today. Only a `runner` read with a device proof from an eligible device receives it.

### How, in zk-v1 terms: a value under its own entry key

A delegated value is **not** encrypted under the vault key VK, because every vault member holds a copy of VK. Instead it gets its own **entry key** EK, and a new §8 in `docs/vault/zk-v1/zk_v1_reference.py` defines it, with test vectors:

```
8. ENTRY-KEYED value ("zke1") — a value only its own recipients can open.
   EK   = 32 random bytes, made by whoever seals the value; ekv = EK's version (1, 2, …)
   binding = the vault's binding id (agent_id, or "vault:<uuid>")
   aad  = "canopy-vault/v1/entry-value|{binding}|{name}|{ekv}"
   store = "zke1:" + b64u(nonce12 || AES-GCM(EK, nonce, utf8(plaintext), aad))
   Wrap of EK to a recipient: §3 ECDH-ES, with
   info = device: "canopy-vault/v1/wrap-entry-device|{binding}|{name}|{ekv}|{jkt}"
          person: "canopy-vault/v1/wrap-entry-member|{binding}|{name}|{ekv}|{user_id}"
```

- **Storage.** The `zke1` ciphertext sits in the entry's own row (`AgentCredential.value_zk` or `VaultSecret.value_zk`), with its `ekv`. Copies of EK go in a new table, `VaultEntryWrap(vault, name, ekv, kind, target, wrap)`. Writes to it go through `vault_engine`, which re-checks every target against the device rule above.
- **Recipients.** EK is wrapped to each eligible device key. It is also wrapped to the person it was delegated by: a write key, so their browser can re-seal it (replace) and act as a keeper. canopy-web never returns the value to them (write-only), and it is their own token. EK is wrapped to no other person.
- **Never under VK.** `vault_engine.enable` / `rotate` leave `zke1` entries alone: they are not "every value re-encrypted" (`require_coverage` skips them), and a VK rotation does not touch them. A browser holding VK can neither settle nor read one.
- **Sealing.** When a person gives one, their browser makes EK, seals the value and wraps EK to themselves and to each eligible device. Before wrapping, it checks each device against the approver endorsements already kept for the agent (`AgentVaultEndorsement`, `zk_endorse.endorsements`), so a server that added a device of its own would be refused. canopy-web re-checks eligibility at the write. If no machine is eligible yet, the value is still sealed (the person holds an EK copy), and the first box to become eligible gets it from them or from a keeper.

### A new box

A new box that becomes eligible (approved device key, the runner serves the agent, its owner an admin) is pending for each delegated entry, in `vault_engine.pending_targets`'s list for that entry. A **keeper**, any box that holds EK, unwraps EK and wraps it to the newcomer if the newcomer carries an approver's endorsement for the agent, the same trust check keepers already make for VK. Every eligible box qualifies as a keeper, since its owner is an agent admin. canopy-web re-checks eligibility under the vault's lock (409 `zk_not_pending`). If no keeper is online, the person it was delegated by is asked on Home: "ace's new box cloud-ec2-3 is waiting for your GitHub token. Give it access" (one passkey touch). As for every vault, a copy is never swept: a box that stops being eligible is refused on every read.

### Agent vaults that are not zero-knowledge

A delegated entry is **always `zke1`**, whatever the vault's mode, so canopy-web can never read it, even when it can read the agent's own entries (`value_enc`). This needs the agent's boxes to have approved ECDH keys. An agent with none cannot take a delegated credential until one of its boxes has one: the giving screen says so, and in migration its old row keeps serving until then. The device-proof requirement on `/credentials/resolve` applies to delegated entries even when the agent does not set `require_device_binding`.

### The limit

**A machine's owner can extract anything that machine decrypts for a turn.** The token sits in the turn's environment, and whoever has root on the box can read it. So a delegated credential is readable, in practice, by the owners of the boxes that run the agent. Those owners are its admins, by the rule above. That is the same exposure as today, and no storage design removes it. What this design removes is every *other* way to read it: the vault UI, `/credentials/resolve` under an admin's or a license's token, canopy-web's own database and key.

The screen where a person gives a delegated credential says so before sealing: *"Only the machines that run ACE can open this token: cloud-ec2-2 (Jonathan Jackson's) and stewari-mbp (Sarvesh's). Their owners, who are ACE's admins, could extract it from those machines. Nobody can read it in canopy, including you. ACE will use it in any turn canopy allows."*

### 1.5 How a turn gets it

The box resolves the agent's values under the existing runner, device-proof and zero-knowledge rules. For each delegated entry it receives the `zke1` value and this device's EK wrap, and opens it with its device key (`canopy vault …`). Resolve hands out only entries for which `serves()` is true, and delegated ones only to a `runner` read from an eligible device.

- The cloud runner sets `GH_TOKEN` for a full-profile turn from `github-token`, and writes the Salesforce value into `.sf-creds.json` in a directory for that turn (today it is per agent, `cloud_runner.py` ~1495–1520). A confined turn gets neither, as today.
- `POST …/turns/{tid}/github-token` keeps one job: the git identity (from `identity`) and the `Requested-by:` trailer. It returns no token.
- The special extras `github_token` and `salesforce_creds` leave `/credentials/resolve`: from `RESOLVE_SECRET_EXTRAS`, `LICENSE_WITHHELD_EXTRAS`, `vault_alerts._EXTRAS`, the schema and `generated.ts`. They are vault entries now, and being delegated keeps them from license and admin reads, as above.

### 1.6 Checking a delegated credential

- **When it is given, in the browser.** The browser checks the shape (`_TOKEN_SHAPE`), calls GitHub's `/user`, and, where GitHub allows it from a browser, runs the PR and push probes. It shows the results, then seals. Build step 1 is to measure whether api.github.com allows the POST probes cross-origin and exposes `github-authentication-token-expiration`. Where it doesn't, the first box fills in the rest.
- **On a box.** A box that opens it runs the probes on each readiness pass, ported to the standard library (`cloud_runner.py` cannot import `apps`). It reports `POST /api/harness/runners/{rid}/credential-checks` `{vault, name, checks, provider_expires_at}`, with a device proof. `runner_github_readiness` and `…/github/check` read what boxes reported instead of decrypting. Salesforce is checked only this way: Salesforce refuses cross-origin calls unless each org allow-lists the origin.
- canopy-web never checks a delegated entry itself, since it is `zke1` in every vault; `checked_by: server` remains only for credentials canopy-web uses itself (Part 3).

### 1.7 Expiry, replacement, withdrawal

- **No canopy expiry.** canopy gives a delegated credential no expiry of its own and never refuses one on a date. The external system's expiry is the only one: a GitHub fine-grained token stops working when GitHub says.
- **What canopy shows.** When a check sees the provider's own expiry (`provider_expires_at`), canopy shows it as information ("GitHub says this token expires 12 Jan"). When a machine's probe fails (rejected, expired at the provider, lost access to the repo), the entry, `github.<slug>` readiness and a Home item for the person it is delegated by and the agent's admins say: "This token no longer works — replace it." canopy keeps serving the entry; the provider is what refuses it.
- **Replacement.** By the person it is delegated by: paste a new token. Their browser makes a new EK (`ekv+1`) and wraps it to them and to every eligible device. An admin who wants it replaced sees "Ask Jonathan to replace it". An admin can also give their own, which replaces it.
- **Withdrawal.** The person it is delegated by can withdraw it at any time from `/settings`, even after losing admin of the agent, because it is their account. Vault admins can remove it from the agent (delete the entry and its EK wraps), but never read or change it. Turns that already received it keep it until they finish, and a box that opened it could have kept a copy. So the withdraw screen links to the provider's own revoke page (`https://github.com/settings/personal-access-tokens`), the only true recall.

## Part 2: act-as-you permissions

### 2.1 The object

`ActAsYou(person, agent, scope, granted_at, expires_at, stopped_at, via)`. `scope` is one **page** at one site (a host grant's arrival) or one **conversation** (a canopy chat).

- **Granted only by the person's own act in the moment**: sending a message on that page or in that conversation. A widget's first send on a page mints it, and so does a chat's first send. Opening a page mints nothing.
- **Time-limited**: 30 minutes, slid forward by each further send by that person in that scope, and never by the agent's activity.
- **Visible**: "Hal can act as you on this page until 3:42 pm · Stop", shown while live.
- **Stoppable**: Stop ends it at once. So does signing out or leaving the workspace.
- **Held by canopy-web, never by a box.** What it carries (a host token, a resolution "as this person") is applied server-side, at the gate. It is never written into a runner's profile or into a vault.
- **Every use is recorded** as an `actasyou.used` event, readable from the person's `/settings`.

### 2.2 Today's implicit paths, each made explicit or removed

| Path | Becomes |
|---|---|
| **Host grants** (canopy as its own host included) | **An act-as-you permission, scope = page.** The ID-JAG still arrives with the page, but canopy redeems it only while the visitor's permission for that page is live, so the first send mints both. The host token (at most 900 s) is re-redeemed from the next arrival inside the 30 minutes. Outside a live permission the gateway refuses with "ask again on the page". The "live page grants" list another PR is adding on `/settings` becomes the scope=page view of this object. |
| **Caller tokens** | (a) Without a live act-as-you permission, a caller token authenticates as **the agent narrowed to the asker**: the agent's own ACL, intersected with the asker's, and with the capability's tools for a confined turn, so the agent never reaches something only the asker could. (b) With the asker's live permission for that conversation, it may run as the asker, as today. `CallerToken` stays the conversation's carrier; its 7-day life no longer carries authority. |
| **`X-Canopy-On-Behalf-Of`** | Honoured only while the named person's act-as-you permission for the turn's conversation is live (`turn_asker.resolve` checks it beside today's checks); otherwise refused, and the agent decides as itself. |
| **Laptop and emdash turns on the owner's PAT** | **Removed**: those turns get a turn-scoped agent token, as cloud turns do (phase 2 of the turn-scoped token spec). Until then, the laptop's row on `/settings` says "agent turns on this laptop act as you". |

## Part 3: what canopy-web uses itself

Credentials canopy-web must use itself cannot be zero-knowledge. They keep their storage and take the same account fields: `GitHubConnection` gains `purpose`, and shows the refresh token's own expiry as information. They show in the same lists, labelled **"canopy-web uses this itself"**.

| Credential | Storage | Shown as |
|---|---|---|
| GitHub token for an agent | Agent vault, `delegated` | Delegated credential |
| Salesforce (Eva's user) | Workspace vault, `delegated` (`delegated_by_agent=eva`) | Delegated to Echo; Eva's own to Eva |
| Person's Claude account on a box | Runner's vault, `delegated` | Delegated credential |
| Chat-shared secret | Agent vault, from Phase 4; `agent` or `delegated` as the person chooses | Either |
| GitHub App connection | Server | In "Your credentials delegated to agents", labelled "canopy-web uses this itself" ("reads ACE's skill history as you"); disconnect withdraws it |
| Host grant token | Server, inside an act-as-you permission | Act-as-you permission |
| Scaffold push | Server, transient, never stored | Not listed. The same paste is sealed as a delegated entry right after. |

## UI

- **The vault page and the agent's Credentials page** show, on every entry, whose account it is: "ACE's own account" or "GitHub · delegated by Jonathan Jackson · GitHub says it expires 12 Jan" (`accounts.label`). They also show the last check and by whom. A delegated entry also shows "only ACE's machines can open it" with the list of machines, and never offers a reveal or edit, to anyone. Its person gets Replace / Withdraw; a vault admin gets Remove.
- **`/settings` → "Your credentials delegated to agents"**: one row per delegated entry from your accounts. Each row shows the provider and identity ("GitHub as @jjackson"), the agent (or box), its purpose, **which machines can open it** and their owners, the provider's own expiry when known (as information), last check, last used, and **Replace** / **Withdraw**. canopy-web's own uses (the GitHub App connection) follow, labelled.
- **`/settings` → "Act-as-you permissions"**: the live ones, in the same line as on the page, with Stop, then the last 30 days of uses. Under it, **"Standing access you've given"**: session-review grants and HCP grants, each with Revoke.
- **On a page or in a chat**: the act-as-you line with Stop, while live.
- **Home**: delegated credentials a machine found no longer work ("replace it"), sent to the person they are delegated by and to the agent's admins, through the one `<AlertList>`.

## Migration

No agent loses a credential between steps. Each phase ships alone.

**Phase 0: close the leak now.** `/credentials/resolve` stops returning `github_token` and `salesforce_creds` to `admin` reads. The cloud runner never asks for `github_token` (`RESOLVE_EXTRAS` in `cloud_runner.py` excludes it), and it reads `salesforce_creds` as `runner`. The gate uses `credential_access`'s answer, before `_classify_read` relabels an owner's own laptop. Before merging, grep the `canopy` CLI repo for readers of either field.

**Phase 1: the account fields.** Add `VaultEntryAccount` to `AgentCredential` and `VaultSecret`. Every existing row is `account="agent"`. Add the §8 `zke1` format with its vectors (Python, browser and the `canopy` CLI), `VaultEntryWrap`, `accounts.py`, the `serves()` filter and the delegated-entry withholding in resolve, the `accounts` map in its response, box-reported checks, the per-entry UI, and the runner reading `github-token` and the Salesforce value from the vault. Resolve and the per-turn route keep serving the old source wherever no vault entry exists yet.

**Phase 2: move `AgentDelegation` into the vault as `delegated`. No person needs to act.**
- **GitHub, per row in force.** canopy-web decrypts `secret_enc` once and writes it into the agent's vault as `github-token`, `zke1`, with `account="delegated"`, `delegated_by` = the row's user, `provider="github"`, `identity` from its `meta`, and `provider_expires_at` = its known expiry, as information. It makes EK itself, seals the value, and wraps EK to the row's user's public key and to every eligible device's public key (ECDH-ES needs public keys only). Then it forgets the plaintext and EK. Whatever the vault's mode, the result is one canopy-web cannot read. `secret_enc` is cleared in the same transaction. Decrypting once is acceptable: canopy-web has held the plaintext all along, and this is the last time. Rows not in force, left by a former owner or admin, are deleted, not moved.
- **Salesforce, per row.** Jonathan, as Eva's owner, makes the `dimagi` workspace vault "Salesforce (Eva's account)" and its key (one passkey touch), and attaches Eva and every agent with a Salesforce row. Eva's box, which holds the plaintext and is attached, seals the value into it as `zke1`, wrapped to every eligible device of an attached agent and to Jonathan, with `account="delegated"`, `delegated_by_agent=eva`, `delegated_by=jjackson`, `provider="salesforce"`, as the runner-credentials migration did. Eva's re-mint path writes there from then on. `salesforce_creds_for` is retired.
- **Agents with no eligible device** (no box with an approved ECDH key) are listed for their owner. Their old row keeps serving until it is fixed.

**Phase 3: remove the old path.** Drop `AgentDelegation` and the special resolve extras, and regenerate `generated.ts`. `RepoIdentity` stays: it picks the agent, and the agent's delegated GitHub entry follows. Update the docs:
- `runners.md` § "The GitHub identity a turn gets";
- `vault.md`, with a new § "Whose account an entry is";
- `access.md` § Credentials.

**Phase 4: act-as-you permissions.** Add `ActAsYou`, then convert host grants, then caller tokens, then `X-Canopy-On-Behalf-Of`. Each conversion has its own switch, with its refusals logged for a week before it flips. Chat-shared secrets move into the vault. The laptop path follows the turn-scoped token spec.

## What is stronger, what is weaker

**Stronger**
- A delegated credential is readable by no person through canopy: not admins (today they read it through `/credentials/resolve`), editors, members or license holders. Only the machines running the agent can open it, and the person giving it sees which ones, and who owns them, before sealing.
- Every entry says whose account it is, in the data, so screens, alerts and the person's own list cannot disagree.
- canopy-web can no longer read a delegated GitHub token or Salesforce refresh token in any vault, so a canopy-web compromise no longer yields them.
- Every delegated credential says whose account it is, and the person it is delegated by can replace or withdraw it in one place.
- An agent acts as a person only after that person's own act, for 30 minutes, visibly. Today's four implicit paths are gone.

**Weaker, said plainly**

| Weaker | Mitigation |
|---|---|
| A new box can be given a delegated credential only by a keeper box or by the person it is delegated by. An agent admin's browser cannot do it, because it holds no EK. | Every eligible box is a keeper; Home asks the person when no keeper is online. |
| canopy-web cannot check a token in a zero-knowledge vault itself, so a wrong identity or stale check is believed until a box probes. | Boxes probe on first use and on every readiness pass, and record who ran the probe. |
| A box that opened it can keep it after withdrawal. | The same as a box that got the plaintext per turn today. Withdraw links to the provider's revoke page; the provider's own expiry, where the person set one, bounds it. |
| Act-as-you permissions add a step: Connect Labs pages and confined chats that worked silently need the person's send before the agent acts as them. | The first send is already the act that starts the work. Refusals are logged for a week before each switch flips. |

## Testing

- **Schema:** the constraints refuse a delegated entry without `delegated_by`, `provider` or `identity`, and an `agent` entry with a `delegated_by`. A person can mark only their own account as delegated.
- **`accounts`:** `is_delegated` is false for `delegated_by_agent`'s own agent and true for every other agent. `serves()` never refuses on a date; it refuses while the person is not an admin or has left, and while the lending agent's owner has changed.
- **Readership:** the agent's own entries are readable by `vault_members.members` and their machines. A delegated entry's EK is wrapped only to eligible devices and its own person. No browser read, and no `admin` or `license` resolve, returns its ciphertext. A wrap write to an ineligible device or to another person is refused. A VK rotation leaves `zke1` entries untouched. The §8 vectors pass in Python and in the browser.
- **New box:** a keeper wraps EK to a newly eligible, endorsed box; an unendorsed one stays pending; a box whose owner lost admin is refused at the next read.
- **Migration:** GitHub rows land as delegated `zke1` entries, wrapped to eligible devices and their own person only, with `secret_enc` cleared in one transaction, and a box resolves the same token before and after. Salesforce keeps working for Echo, and Eva sees it as her own.
- **Act-as-you:** nothing on arrival, one on the first send; expires at 30 minutes; only the person's sends extend it; Stop ends it; a host grant is refused outside it; a caller token without one cannot read the asker's private session; `X-Canopy-On-Behalf-Of` without one is refused.
- **End to end:** a real cloud turn opens a PR with the vault-held delegated token, and the token appears in no canopy-web response, log or event.

## Open questions only Jonathan can answer

None. The model, the three kinds and their names, and the 30-minute act-as-you window are Jonathan's, from his review.
