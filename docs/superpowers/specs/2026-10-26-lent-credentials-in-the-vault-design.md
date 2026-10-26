# Agent credentials and act-as-you permissions

**Status:** design, 2026-10-26. No code yet. Jonathan asked for it after an audit flagged that a person's GitHub token, set for an agent, could be read by the agent's other admins. He set the model and the words in review.

## Glossary

**The rule: an agent acts as itself (including with its agent credentials), or as you only under an act-as-you permission you granted.**

- **Agent credential.** A standing credential someone sets up for an agent: the owner's GitHub token for Hal, Eva's Salesforce login for the agents that use it. The agent uses it as its own, in whatever turns canopy's ACL (`access.decide`) allows. It lives in a vault, like every other secret of the agent, with metadata saying who provided it ("provided by Jonathan Jackson"), what it is for, and when it expires.
- **Act-as-you permission.** A time-limited permission that the person asking grants in the moment. It lets the agent act within *that person's* access: a page grant on Connect Labs, minted when they first send, for 30 minutes. It is visible ("Hal can act as you on this page until 3:42 pm · Stop") and can be stopped. It is never stored as an agent credential.

## Intent

Jonathan's words, lightly edited: *"Agents should never act with the delegated authority of a user unless it's explicitly configured that way (like GitHub for all users to use), or with a very explicit, time-limited grant. Now that we have the vault system, even delegated access stored in canopy-web should go through the vault, stored with the knowledge that it's delegated access — metadata on the key. Right now we have two different, confusing ways to do this."*

The two ways today are the **vault** (an agent's own secrets) and **`AgentDelegation`**. `AgentDelegation` is a person's GitHub token, held by canopy-web under the app-wide key, plus a Salesforce row with no secret that points at another agent's credential. Beside them sit several **implicit** paths by which an agent acts with a person's authority, which nobody granted as such. This design moves the first kind into the vault, as agent credentials, and turns each implicit path into an explicit act-as-you permission or removes it.

## Goals

1. Every credential set up for an agent is an ordinary entry in a vault, with metadata that marks it as an agent credential: who provided it, provider and identity, scopes, expiry, last check. `AgentDelegation.secret_enc` goes away.
2. The vault's ACL decides who can open it, as for any agent secret, and the person setting it up is told exactly who that is.
3. Every agent credential expires and is renewed on a reminder. Whoever provided it can withdraw it at any time.
4. An agent acts with a person's own access only under an act-as-you permission: granted by that person, time-limited, visible, stoppable. Every path that does this today becomes one or goes away.
5. Credentials canopy-web must use itself on the server keep their storage, carry the same metadata, and show in the same lists.
6. Moving GitHub and Salesforce costs no agent a single turn.

## Non-goals

- A per-credential rule about which turns may use it. An agent credential is the agent's; `access.decide` and the turn's profile decide, as for every other agent secret. A confined (caller) turn's environment is an allowlist that holds none of them (`_confined_env` in `cloud_runner.py`).
- Readership narrower than the vault's. If the agent's vault members should not be able to read a token, it does not belong to the agent. It belongs in an act-as-you permission, or nowhere.
- Tokens canopy issues to people for themselves (PATs, the widget's `DelegatedToken`, OAuth sign-in). There the person acts, not an agent.

## Today

### Credentials set up for an agent

| Credential | Stored | Who uses it, and how | Who can read it today |
|---|---|---|---|
| **GitHub fine-grained PAT** (owner's, else an admin's) | `AgentDelegation.secret_enc`, Fernet under `FIELD_ENCRYPTION_KEY` (`apps/agents/models.py` ~1100–1135; `delegations.set_github`) | **Runner**, per turn: `POST /api/harness/runners/{rid}/turns/{tid}/github-token` (`apps/harness/api_runners.py` ~433), plaintext, into the turn's `GH_TOKEN` (`cloud_runner.py` ~5119). **Server**: `check_github` decrypts it to re-probe GitHub, from `POST /api/agents/{slug}/github/check` (`api_credentials.py` ~794) and at every box boot (`runner_github_readiness`, `api_runners.py` ~538). | Any agent admin with a bearer token, through `/credentials/resolve` (`credential_resolve.py` ~308–314). canopy-web itself. **Not** the agent's vault members as such: this is a third, separate ACL, which is the confusion. |
| **Salesforce** (Eva's own Salesforce user, used by Echo) | No secret on the row (`secret_enc=""`, `lender` FK); the value is Eva's `AgentCredential` named `salesforce`, which must be non-zero-knowledge (`delegations.py` ~455–601) | **Runner**: `/credentials/resolve` returns `salesforce_creds` in plaintext; `cloud_runner.py` ~3087–3167 writes `.sf-creds.json`. **Server**: `probe_salesforce` at set and check. | Every admin of the borrowing agent (same `resolve` path); everyone who can read Eva's vault; canopy-web. |
| **A secret shared into a chat** (`SessionSecret.value_enc`, `apps/canopy_sessions/models.py` ~820) | Fernet, scoped to one session | A hand-off: the agent moves it to where it belongs (`services.resolve_secret`). | canopy-web; whoever can act in the session. |
| **Scaffold push at creation** (`apps/agents/api.py` ~389) | Not stored | **Server**, once in the request; the same token is then saved with `PUT /api/agents/{slug}/github`. | canopy-web, for the length of the request. |

### Credentials canopy-web uses itself

| Credential | Stored | Use |
|---|---|---|
| **GitHub App connection** (`GitHubConnection.refresh_token_enc`, `apps/github_connect/models.py`) | Fernet | canopy-web reads an agent's repo as its owner for skill history (`skill_history.py` → `github_app.access_token_for(agent.owner)`). The refresh token rotates on every use, so exactly one process may hold it. |
| **Host grant** (`HostGrant.access_token_enc`, `apps/tokens/models.py` ~765) | Fernet, DPoP-bound, at most 900 s (`canopy_sdk.contract.ACCESS_TOKEN_MAX_LIFETIME`) | The gateway calls the host's MCP as the visitor. This is an act-as-you path (below), held server-side. |

### Paths where an agent acts with a person's authority

| Path | What it does today | Granted by the person? |
|---|---|---|
| **Host grants** (`apps/host_grants/host_grants.py`, `HostGrant`; canopy as its own host in `apps/host_grants/self_host.py` and `apps/mcp/delegation.py`) | At every page **arrival** the host's ID-JAG is redeemed for a token, and the agent's `site_call`s on that visitor's turns run as the visitor, within the page's scope. A page re-arrives every few minutes, so the grant is effectively standing while the page is open. | No. It is implicit in opening the page, and nothing lists it or stops it. |
| **Caller tokens** (`apps/harness/caller_tokens.py`, `CallerToken`) | A confined turn's, or a non-admin's full-profile turn's (`scoped`), canopy MCP calls run **as the conversation's current asker**: their own ACL, filtered to the capability's tools. The asker's ACL is not a subset of the agent's, so the agent can reach things that are the asker's own (their private sessions, their projects). Lives 7 days. | No. It is implicit in asking. |
| **`X-Canopy-On-Behalf-Of`** (`apps/harness/turn_asker.py`) | An agent declares it acts for the person who started the turn. canopy-web then judges runner pinning (`access.may_pin_runner`) and whether a dispatch may run `auto` (`turn_gates`) on that person's access instead of the agent's. | No. The agent declares it, and canopy checks only that the person started the turn. |
| **Laptop and emdash agent turns** | A turn on a person's laptop reaches canopy's MCP with the canopy plugin's PAT, which is that person's own. The turn-scoped agent token covers cloud runners; its phase 2 (laptops) is still open (`2026-10-21-turn-scoped-agent-token-design.md`). | No. It is the box owner's standing PAT. |

**Already explicit, and not act-as-you:** an HCP grant (`contacts.PersonGrant`) and a session-review grant (`canopy_sessions.SessionReviewGrant`). Each is a person's own act, revocable, that lets the agent's **own** login read named data of theirs. The agent still acts as itself, with more to see. They stay as they are and appear in the same `/settings` area (§UI). A session-review grant has no expiry today; it gets one under Goal 3's renewal pattern (a default of 90 days, renewed with one click).

## Part 1: agent credentials

### 1.1 Where they live

An agent credential is **an ordinary entry in a vault**. Usually that is the agent's own vault (`AgentCredential` under a declared secret name), written with the same routes and formats as any other value. It is `zk1` when the vault is zero-knowledge, and KMS/Fernet `value_enc` when it is not.

- **GitHub:** the agent's vault, name `github-token`.
- **Salesforce (Eva's login):** a **workspace vault** in `dimagi`, "Salesforce (as Eva)", attached to Eva and to every agent that uses it (today Echo, in `connect`, below `dimagi`, so inside the vault's own tree). One secret and one rotation for every agent that uses it, as today. Eva's box can hold the vault's key because Eva is attached, so Eva's re-mint writes the new value there. Eva's own vault stops holding a copy.
- **A secret shared into a chat** stops being a server-held hand-off: the chat's "share a secret" seals the value straight into the agent's vault from the person's browser, as an agent credential with metadata, and `SessionSecret` goes away (Phase 4).

**Who can open it is the vault's ACL, nothing narrower.** For an agent vault that means everyone `credentials.vault_members.members` returns: the agent's owner and admins, everyone with full access to it (editors), everyone with an active license to use it locally, and the direct members. It also means their eligible machines (`docs/architecture/vault.md`), and, for a vault that is not zero-knowledge, canopy-web. The screen that sets one up says so in those words, with the current list: *"Everyone who can open Hal's vault can read this token: Jonathan Jackson (owner), Sarvesh (admin), Maria (editor), Dev (local use), and the machines that run Hal. Hal will use it in any turn canopy allows."* A credential that should not reach all of them is not an agent credential.

### 1.2 The metadata

A new row, `AgentCredentialInfo`, 1:1 with the value, keyed by `(vault, name)` so that it covers agent and workspace vaults alike. It is clear text, because canopy-web enforces some of it without reading the secret:

| Field | Meaning |
|---|---|
| `provided_by` | The person who set it up ("provided by Jonathan Jackson"). Required. |
| `provider`, `identity` | `github` / `salesforce` / `other`, and who it acts as: `{login, user_id, name}` for GitHub (gives the commit identity, the noreply email), `{username, organization_id, instance_url}` for Salesforce. |
| `purpose` | One line, shown everywhere ("Hal opens PRs on canopy-web as Jonathan"). |
| `resource`, `scopes` | What it reaches, as declared (repos, permission names). Shown, not enforced: the provider enforces them. |
| `expires_at` | Required. canopy stops serving the value afterwards. Never later than `provider_expires_at`. |
| `provider_expires_at` | What the provider says (GitHub's token-expiration header), once a browser or box has seen it. |
| `checks`, `checked_at`, `checked_by` | The last probe and **who ran it** (`browser`, `server`, or a runner id). |

An entry with this row **is** an agent credential. Everything else in the vault is the agent's own secret. The agent's Credentials page shows the difference ("provided by …, expires …").

### 1.3 How a turn gets it

As any vault value: the box resolves the agent's values (`/credentials/resolve`, under the existing runner, device-proof and zero-knowledge rules) and opens them with its own key copy. Then:

- The resolve stops serving a value whose `AgentCredentialInfo.expires_at` has passed. It also stops serving it while its `provided_by` is no longer an admin of the agent (the owner counts as one), or no longer a member of the workspace. The same check covers a workspace vault's `provided_by` against the agent that vault serves: a person's identity stops acting through an agent the moment they no longer stand behind it, which is today's `delegation_for` rule.
- The cloud runner sets `GH_TOKEN` for a full-profile turn from the `github-token` value, and writes `.sf-creds.json` from the Salesforce value into a directory for that turn (today it is per agent, `cloud_runner.py` ~1495–1520). A confined turn gets neither, as today.
- `POST …/turns/{tid}/github-token` keeps one job: the git identity (`identity` from the metadata) and the `Requested-by:` trailer. It returns no token. The special extras `github_token` and `salesforce_creds` leave `/credentials/resolve` (`RESOLVE_SECRET_EXTRAS`, `LICENSE_WITHHELD_EXTRAS`, `vault_alerts._EXTRAS`, the schema, `generated.ts`), because they are ordinary values now. A license read therefore receives them like any value of a vault its holder is a member of. That is the vault's ACL, stated above.
- One GitHub credential per agent. An owner or admin may set it, and setting it replaces the current one ("replaces Jonathan Jackson's, expires 12 Jan"). Today's "the owner's wins over an admin's" reduces to that, because there is one entry.

### 1.4 Checking it

- **When it is set, in the browser.** The browser checks the token's shape (`_TOKEN_SHAPE`), calls GitHub's `/user`, and, where GitHub allows it from a browser, runs the PR and push probes. It shows the results, then seals the token into a zero-knowledge vault. Build step 1 is to measure whether api.github.com allows the POST probes cross-origin and exposes the `github-authentication-token-expiration` header. Where it doesn't, the person sets `expires_at` and the first box fills in the rest.
- **On a box.** A box that opens it runs the probes on each readiness pass (ported to the standard library, since `cloud_runner.py` cannot import `apps`) and reports `POST /api/harness/runners/{rid}/credential-checks` `{name, checks, provider_expires_at}` (device proof required). `runner_github_readiness` and `…/github/check` read what boxes reported instead of decrypting. Salesforce is checked only this way: Salesforce refuses cross-origin calls unless each org allow-lists the origin.
- **A vault that is not zero-knowledge** can still be checked by canopy-web directly. It uses the same report shape, with `checked_by: server`.

### 1.5 Expiry, renewal, withdrawal

- **Expiry.** `expires_at` is required, with a default of 90 days. A GitHub token with no expiry still gets one. From 14 days before (`EXPIRY_WARNING`), the provider and the agent's admins get a Home item and an email: "Hal's GitHub token, provided by Jonathan Jackson, stops on 12 Jan."
- **Renewal.** Paste a new token (re-sealed, metadata updated), or **extend** with one passkey step-up, up to `provider_expires_at`, with no re-paste. Anyone who may edit the vault may renew; renewing with their own token makes them `provided_by`.
- **On expiry** the value stops being served, `github.<slug>` readiness goes red, and the agent's Health page names the provider. Thirty days later the value is deleted (a job on `apps/common/ticks.py`); the metadata row stays as history.
- **Withdrawal.** Whoever provided it can withdraw it at any time from `/settings`, even after losing admin of the agent, because it is their identity. Vault admins can remove it as any value. Turns that already received it keep it until they finish, and a member or box that opened it could have kept a copy. So the withdraw screen links to the provider's own revoke page (`https://github.com/settings/personal-access-tokens`), the only true recall.

## Part 2: act-as-you permissions

### 2.1 The object

`ActAsYou(person, agent, scope, granted_at, expires_at, stopped_at, via)`. `scope` names where it holds: one **page** at one site (a host grant's arrival), or one **conversation** (a canopy chat). Its rules:

- **Granted only by the person's own act in the moment**: sending a message on that page or in that conversation. A widget's first send on a page, or a chat's first send, mints it. Opening a page mints nothing.
- **Time-limited**: 30 minutes from the grant. Each further send by the same person in the same scope slides it forward. Only that person's sends do; an agent's activity never does.
- **Visible**: the page or chat shows "Hal can act as you on this page until 3:42 pm · Stop" while it is live.
- **Stoppable**: Stop ends it at once. So does signing out, leaving the workspace, or a new arrival from another person.
- **Held by canopy-web, never by a box.** The authority it carries (a host token, a resolution "as this person") is applied server-side, at the gate. It is never written into a runner's profile or stored as an agent credential.
- **Every use is recorded**: an `actasyou.used` event (person, agent, scope, what was done), readable from the person's `/settings`.

### 2.2 Today's implicit paths, each made explicit or removed

| Path | Becomes |
|---|---|
| **Host grants** (including canopy as its own host) | **An act-as-you permission, scope = page.** The host's ID-JAG still arrives with the page, but canopy redeems it only while the visitor's permission for that page is live, so the first send mints both. The host token (at most 900 s) is re-redeemed from the next arrival inside the 30-minute window. Outside a live permission the gateway refuses with "ask again on the page". The "live page grants" list another PR is adding on `/settings` becomes the scope=page view of this object. |
| **Caller tokens** | **Two cases.** (a) Without a live act-as-you permission, a caller token authenticates as **the agent narrowed to the asker**: the agent's own ACL, intersected with the asker's, and with the capability's tools for a confined turn. The agent never reaches something only the asker could. (b) With the asker's live permission for that conversation (minted by their send), it may run as the asker, as today. `CallerToken` keeps its job as the conversation's carrier, but its 7-day life no longer carries authority: the permission does. |
| **`X-Canopy-On-Behalf-Of`** | **Honoured only while the named person's act-as-you permission for the turn's conversation is live** (`turn_asker.resolve` checks it beside its current checks). Without one, it is refused, and the agent decides runner pinning and `auto` as itself. Today's checks (an issued turn login, an executing unconfined turn started by that verified person) stay. |
| **Laptop and emdash turns on the owner's PAT** | **Removed.** These turns get a turn-scoped agent token, as cloud turns do (phase 2 of `2026-10-21-turn-scoped-agent-token-design.md`). Until then, the laptop row on `/settings` says plainly that "agent turns on this laptop act as you". |

## Part 3: one model, two storage tiers

Credentials canopy-web must use itself cannot be zero-knowledge. They keep their storage, take the same `AgentCredentialInfo` fields, and show in the same lists, labelled **"canopy-web uses this itself"**.

| Credential | Tier | Shown as |
|---|---|---|
| GitHub token for an agent | Vault (agent) | Agent credential |
| Salesforce login (Eva's) | Vault (workspace, attached to Eva and Echo) | Agent credential |
| Chat-shared secret | Vault (agent), from Phase 4 | Agent credential |
| GitHub App connection | Server | Agent credential, canopy-web uses it itself ("reads Hal's skill history as you"); `expires_at` = the refresh token's expiry; disconnect withdraws it |
| Host grant token | Server, inside an act-as-you permission | Act-as-you permission |
| Scaffold push | Server, transient, never stored | Not listed. The same paste is sealed into the vault right after. |

## UI

- **`/settings` → "Credentials you've given agents"**: one row per agent credential you provided. It shows the provider and identity ("GitHub as @jjackson"), the agent, its purpose, **who can open it** (the vault's members, as a count with a link to the vault page), expiry, last check and by whom, last used, and **Renew** / **Withdraw**. canopy-web's own (the GitHub App connection) follows, labelled. Expired ones stay greyed for 30 days with Renew.
- **`/settings` → "Act-as-you permissions"**: the live ones, each a line in the same pattern as on the page ("Hal can act as you on labs.connect … /opportunity/412 until 3:42 pm · Stop"), then the last 30 days of uses. Under it, **"Standing access you've given"**: session-review grants and HCP grants, each with Revoke.
- **Agent → Settings → Credentials**: each agent credential marked "provided by …, expires …, can be opened by everyone in this vault (n)". The setup screen shows the full reader list before sealing (§1.1).
- **On a page or in a chat**: the act-as-you line with Stop, shown while it is live.
- **Home**: credentials expiring soon or expired (to their provider and the agent's admins), through the one `<AlertList>`.

## Migration

No agent loses a credential between steps. Each phase ships alone.

**Phase 0: close the leak now.** `/credentials/resolve` stops returning `github_token` and `salesforce_creds` to `admin` reads. The cloud runner never asks for `github_token` (`RESOLVE_EXTRAS` in `cloud_runner.py` excludes it), and reads `salesforce_creds` as `runner`; the gate uses `credential_access`'s answer before `_classify_read` relabels an owner's own laptop. Before merging, the builder greps the `canopy` CLI repo for readers of either field. This removes the third, separate ACL in the table above. Once the values move into the vault, the vault's own ACL governs them.

**Phase 1: agent credentials in the vault, served side by side.** Add `AgentCredentialInfo`, the expiry filter in resolve, box-reported checks, and the runner reading `github-token` and the Salesforce value from the vault. The per-turn route and resolve keep serving the old source wherever a vault entry does not exist yet.

**Phase 2: move the values. No person needs to act.**
- **GitHub, per agent.** canopy-web decrypts `AgentDelegation.secret_enc` once and writes it into the agent's vault, with `provided_by` = the delegation's user and `expires_at` = its known expiry, else 90 days from the move. A zero-knowledge vault is written as `zks1`, sealed to every current recipient by `zk_seal.seal_for_agent`, which exists for exactly this: a value canopy-web holds once and cannot read once stored. The next key holder settles it into `zk1`. A non-zero-knowledge vault is written as `value_enc`. `secret_enc` is cleared in the same transaction. Decrypting once is acceptable: canopy-web has held the plaintext all along, and this is the last time.
- **Salesforce.** Jonathan, who owns Eva, makes the `dimagi` workspace vault "Salesforce (as Eva)" and its key on its page (one passkey touch), attached to Eva and Echo. Eva's box, which holds the plaintext and is attached, seals the value into it, as the runner-credentials migration did, and Eva's re-mint path writes there from then on. Then `salesforce_creds_for` is retired.
- **Agents that could not take a `zks1` value** (no eligible recipient, `ZkError`) are listed for their owner; their old row keeps serving until it is fixed.

**Phase 3: remove the old path.** Drop `AgentDelegation`, keeping its metadata as `AgentCredentialInfo` history rows, and the special resolve extras. Regenerate `generated.ts`. `RepoIdentity` stays: it picks the agent, and the agent's credential follows. Update `runners.md` § "The GitHub identity a turn gets", `vault.md` (§ Agent credentials) and `access.md` § Credentials.

**Phase 4: act-as-you permissions.** Add `ActAsYou`; convert host grants, then caller tokens, then `X-Canopy-On-Behalf-Of`, each behind its own switch with the refusals logged for a week first. Chat-shared secrets move into the vault. The laptop path follows the turn-scoped token spec.

## What is stronger, what is weaker

**Stronger**
- One ACL per credential, the vault's, instead of a third rule nobody could see. The person setting a credential up sees exactly who can read it before sealing.
- In a zero-knowledge vault, canopy-web can no longer read a GitHub token or Salesforce refresh token, so a canopy-web compromise no longer yields them.
- Every credential expires, says who provided it, and is listed for its provider with Renew and Withdraw.
- An agent acts as a person only after that person's own act, for 30 minutes, visibly. Today's four implicit paths are gone.

**Weaker, said plainly**

| Weaker | Mitigation |
|---|---|
| **More people can read a GitHub token than today:** editors, license holders and direct members of the agent's vault, not only its admins. | It is the owner's model: an agent credential is the agent's. The setup screen lists the readers; a credential they should not read does not belong to the agent. Fine-grained tokens scoped to the agent's repo limit what any reader gets. |
| canopy-web can no longer check a token in a zero-knowledge vault itself. A wrong identity or stale check is believed until a box probes. | Boxes probe on first use and on every readiness pass, and record who ran the probe. Expiry is enforced by canopy-web from clear metadata. |
| A member or box that opened it can keep it after withdrawal. | The same as any vault value, and the same as a box that received the plaintext per turn today. Withdraw links to the provider's own revoke page; expiry bounds it. |
| Act-as-you permissions add friction: Connect Labs pages and confined chats that worked silently now need the person's send before the agent acts as them. | The first send is already the act that starts the work, so most flows notice nothing. Refusals are logged for a week before each switch flips. |

## Testing

- **Readership:** an agent credential in an agent vault is readable by exactly `vault_members.members` and their eligible machines; the setup screen's reader list equals that set.
- **Serving:** no value is served after `expires_at`, nor while its provider is not an admin of the agent or has left the workspace. A confined turn's environment holds neither credential.
- **Migration:** GitHub rows land as `zks1` or `value_enc` with metadata and `secret_enc` cleared in one transaction; a box resolves the same token before and after; Salesforce keeps working for Echo across the switch.
- **Act-as-you:** nothing is minted on arrival, one is minted on the first send; it expires at 30 minutes; only the person's sends extend it; Stop ends it at once; a host grant is refused outside it; a caller token without one cannot read the asker's private session; `X-Canopy-On-Behalf-Of` without one is refused.
- **End to end:** a real cloud turn opens a PR with the vault-held token, and the token appears in no canopy-web response, log or event.

## Open questions only Jonathan can answer

None. The model, the words and the 30-minute act-as-you window are Jonathan's, from his review. The 90-day default expiry for an agent credential is this design's choice, and anyone setting one up can change it.
