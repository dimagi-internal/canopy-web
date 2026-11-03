# Connections hold nothing canopy-web can use: the server finishes sign-in, seals to the owner, runners refresh

**Status:** design 2026-10-31; approved 2026-11-01 with two decisions: the owner signs grants with a passkey touch (§4.3), and the older Google paths fold in later. Simplified 2026-11-02 (Jonathan): canopy-web finishes the sign-in itself and seals the token to the owner at once; runners no longer mint. It replaces the storage and token paths of the server-held Connections feature (#2287, #2311, #2347). The workspace apps, per-workspace callbacks, providers, grants and the Settings screens stay.

## Intent

Jonathan's framing: *"As long as we can still run autonomously, and canopy-web getting hacked gives nothing (although it does let you trigger sessions to the AI), that's a good framework."* The case he cares about most: *"general Salesforce credentials I have agents step up into (or Gmail more likely) … across multiple agents on my behalf, seamlessly."*

Today canopy-web holds every connection's refresh token, Fernet-encrypted under `FIELD_ENCRYPTION_KEY`, which the running server holds. A compromised server therefore yields standing, portable access to every connected Salesforce and Google account. After this change it yields ciphertext for every connection made before the compromise. It can still start turns, which is a separate problem with separate limits: narrow agent permissions, and tokens kept out of the model's context.

## The guarantee, plainly

- **A connection made before canopy-web is compromised is safe.** canopy-web keeps nothing it can open: the refresh token is sealed to the owner's vault key the moment the sign-in finishes, then to the machines the owner allows. canopy-web never refreshes, never holds an access token, and a later attacker gets ciphertext.
- **A connection made while canopy-web is compromised is exposed.** canopy-web does the code exchange, so it sees that one refresh token in passing. (Moving the exchange to a runner would not help: a server compromised at connect time can substitute the owner key the runner seals to, so it could take that connection either way. That is why the runner mint was dropped.)
- **Browser crypto is served by canopy-web.** A compromised server could serve hostile JavaScript to an owner who opens the confirmation screen or Settings while it is compromised. This is the same limit every canopy zero-knowledge vault has.
- **The app's client secret stays server-held** (Fernet, as today). Alone it is worth little: it cannot be used without a refresh token, and token endpoints come from fixed provider rules, so a lying server cannot redirect a refresh token to itself.

The pattern already exists twice in canopy: **Connect Google mailbox** (`apps/agents/credentials/google_mint.py`) and **delegated credentials**, sealed to the machines that run an agent and re-sealed by keepers (`apps/agents/credentials/accounts.py`, `docs/architecture/vault.md`). This design applies the delegated-credential half to every provider in `apps/connections`.

## Goals

1. **canopy-web keeps no usable token.** No refresh token or access token is stored readable by canopy-web, at rest, and none passes through it after connect time: refreshing, a turn, the CLI, disconnecting and removing an app all happen on machines.
2. **Autonomous turns work as today.** A cloud runner picks up an agent's turn and the token is at `~/.canopy/connections/<slug>/<provider>.json` in the same format. Agents, skills and the chrome-sales MCP need no changes.
3. **One connection, many agents.** A person connects their Gmail or Salesforce once and lends it to any number of agents they administer. Each agent's machines can open it, and every use is recorded.
4. **Rotating refresh tokens work.** Salesforce rotates the refresh token on every refresh. Exactly one machine refreshes at a time, the new token is written back before it is relied on, and a crash between the two is detected rather than silent.
5. **Same screens, same provider setup.** Workspace admins keep Settings → Connections and paste the client secret as today. The Salesforce External Client App and Google OAuth client already registered keep working, with the same callback URLs.

## Non-goals

- **Stopping a compromised server from starting turns.**
- **Protecting a connection made while canopy-web is compromised**, or an owner who loads canopy-web's pages while it is compromised (see the guarantee).
- **Laptop runners staging connection tokens for turns.** Only cloud runners stage them, as today. A person's own laptop can use their own connection through the CLI (§6).
- **Hiding a token from the owner of a machine that holds it.**
- **Lending a connection to an agent you don't administer.**

## Anti-goals

- A fallback where canopy-web refreshes or revokes "when no runner is online". The point is that it cannot.
- A runner sealing to recipients only because canopy-web listed them. A compromised server must not be able to add its own key to a seal (§4.3).

## The design

### 1. What is stored

| Thing | Today | After |
|---|---|---|
| App client secret and policy | `ConnectionApp.client_secret_enc` (Fernet); options in plain JSON | **Unchanged.** Pasted in Settings → Connections, Fernet in the DB, never returned. Handed to refreshers with the material (§4.2). |
| Refresh token | `Connection.refresh_token_enc` (Fernet) | `Connection.sealed`: a `zks1` envelope bound to `connection:<uuid>` (the binding id takes the agent-id slot in AAD and HKDF, as workspace vaults use `vault:<uuid>`). The plaintext is a small JSON: `refresh_token`, `client_id`, `login_host` (Salesforce), `instance_url`, `account_id`, `owner_key` (the thumbprint of the owner's member key, §4.3), `issued_at`, `scopes`. URLs are never taken from it as given: the CLI derives every endpoint from the provider's hard-coded rules and checks `login_host` and `instance_url` against them (Salesforce: `login`/`test.salesforce.com` or `*.my.salesforce.com`). |
| Access token | `Connection.access_token_enc` (Fernet) | `Connection.access_sealed`: a `zks1` envelope to the same recipients, plus a plaintext `access_expires_at` so machines can decide without opening it. Empty until the first refresh on a machine. Shared so machines reuse one access token instead of each refreshing (§4.2). |
| Who can open | — | `Connection.recipients()`, computed, never stored (§3). |

Each `Connection` row keeps its non-secret metadata: owner, app, provider, `account_id`, `account_label`, `scopes`, `needs_reconnect`, `last_used_at`, plus `state` (`unshared`, `active`, `revoking`) and `sealed_digest` (SHA-256 of `sealed`, for compare-and-swap).

### 2. Connecting

The browser flow stays as today: `/auth/connections/<provider>/<workspace>/start/` and `…/callback/`.

1. **Start.** As today, plus one check: the owner (the person, or for an agent the admin connecting it) must have a vault key, or there is nothing to seal to. Without one the start refuses and points to the one-time vault-key setup. There is no fallback.
2. **Callback.** canopy-web exchanges code + verifier with the app's client secret, looks up the identity, and applies every policy check as today: required scopes, `allowed_domains`, an agent's own Google account, wrong account. Connecting the same account again for the same owner through the same app replaces that row's envelope, as today's upsert does.
3. **Refusals.** A Salesforce grant refused after the exchange is revoked at the provider before it is dropped. A Google grant refused after the exchange is dropped **without** revoking, because revoking at Google ends every token of that account on that OAuth client (§4.5).
4. **Seal to the owner.** Immediately after the checks pass, canopy-web seals the §1 plaintext to the owner's member vault key only, drops the access token, keeps no readable copy, and records `connected`. The connection is `unshared`.
5. **Confirm and share.** The callback redirects to the confirmation screen. The owner's browser opens the envelope with their vault key and shows "Connected as <account>". They choose the agents to lend it to and whether to **Use on my computers**. One passkey touch then signs the grant statement (§4.3) and re-seals the envelope to the devices it allows. The browser checks each device's endorsement as `approveAndGrant` does. An owner who closes the tab leaves the connection `unshared`: still sealed to the owner only, listed as "not shared yet", and shareable later from Settings.

No runner needs to be online to connect.

### 3. Who can open a connection

`recipients(connection)` is the single rule, read by every seal, reseal and write-back:

- **Agent-owned** (Eva's own Salesforce user): every ECDH-ready device on a runner that serves the agent and may hold it (`runner_may_hold_agent`), plus the same for every agent it is granted to. Plus the member key of the admin who connected it.
- **Person-owned** (new: today `ConnectionGrant.clean` forbids lending a person's connection, and this design deliberately allows it): for each agent it is granted to, `accounts.delegated_devices(agent, person)` (cloud boxes that run it, or the person's own runners). Plus the person's own enrolled machines if they turned on **Use on my computers**. `may_hold_for` is extended here to allow the `computer` kind, because this use is the person's own CLI, not a turn. A laptop never keeps (re-seals for others) or revokes. Plus the person's member vault key.
- A grant is in force only while its rule holds today: the person still administers the agent, and the agent is still in the app's workspace.

Recipients that stop qualifying are pruned from the envelope without decrypting (`zk_seal.keep_only`, as `accounts.prune`). New ones receive it from a holder (§4.1), or from the owner's browser when no machine holds it yet.

### 4. On the runners

**4.1 Re-sealing for new machines and new grants.** The keeper (`canopy vault zk keep`, every 120 s on cloud boxes) also asks for connection work: each connection it holds, with the waiting machines and their endorsements. It re-seals the **same** plaintext through `PUT …/vault-keeper/connections/{id}` with `expect = sealed_digest`. This is `keep_delegated` with a different binding id. Granting a connection to a new agent therefore needs no action from the person if any holder is online. With no holder, the grant shows "waiting for a machine that holds it", and the owner can give waiting machines a copy from Settings with one touch.

**4.2 Getting an access token** (turn staging, heartbeat re-staging, CLI):
- If `access_expires_at` is more than the needed minimum life away, open `access_sealed` and use it.
- Otherwise take the **refresh lease**: `POST …/connections/{id}/refresh-lease`, one holder at a time, 60 s, renewable. Another machine that wants a token waits for the new `access_sealed`, polling up to 30 s, rather than refreshing in parallel.
- Then refresh at the provider and immediately write back `PUT …/connections/{id}/sealed {sealed?, access_sealed, access_expires_at, expect}`, with `sealed` only when the refresh token rotated. Only after that succeeds is the token staged. The client id and secret for the refresh come with the material from canopy-web, which reads them from the app. The refresher refuses a client id that differs from the one sealed in the envelope.
- If the write-back fails, the lease is kept and the box retries from memory. If the box dies, the lease expires and the next refresher gets `invalid_grant`, which marks the connection `needs_reconnect` (it is not left silently broken).

**4.3 What a runner trusts: the owner's signed grant, never the server's list.**

- **The grant statement.** The owner signs it in the browser with their vault key (ECDSA P-256, as endorsements are). It covers `{connection, account_id, agents: [agent ids], endorsers: [person key thumbprints], my_computers: [device ECDH thumbprints], version}`. For an agent-owned connection, the admin who connected it signs it. It is made on the confirmation screen and re-signed on every lend, withdrawal or "use on my computers" change, one passkey touch each. Its signer's key thumbprint is sealed inside the envelope (`owner_key`), so later runners check the signature against a key the server cannot change after connect time.
- **Who a runner will seal to:** a device listed in `my_computers`, or one carrying a valid endorsement of its exact ECDH key, signed by the owner or a person the statement lists in `endorsers`, naming an agent in the statement. Member keys: only the owner's. canopy-web's recipient list only narrows this set. A request to seal to anything else is refused as `zk_unendorsed_recipient`.
- **Envelopes are authenticated.** Every envelope a runner or the owner's browser writes carries, for each recipient, a MAC keyed by ECDH between the writer's key and that recipient's key, together with `expect`. A reader checks that the writer is a device the statement allows, or the owner. Anyone can build a `zks1` from public keys, so an unauthenticated envelope is refused by every machine. The one exception is the owner's own browser, which accepts canopy-web's first seal while the connection is `unshared`. This is what stops a forged access token pointing an agent at a stranger's account.

A write-back must also keep every current, still-eligible recipient, as `accounts.reseal` requires.

**4.4 A rotated client secret.** When an admin replaces the secret in Settings, refreshers receive the new one with their next material read. Nothing in any envelope changes. If the old secret was revoked at the provider before refresh tokens were re-issued under it, those connections show `needs_reconnect`.

**4.5 Disconnecting and removing an app.**
- **Revoking at Google ends every token of that account on that OAuth client**, not just one. So a disconnect revokes at the provider only when no other connection of that account through that app remains; otherwise it only deletes.
- A disconnect marks the row `revoking`: it disappears from every list at once and is never handed out again.
- The first holder online opens it, revokes at the provider, and confirms (`POST …/connections/{id}/revoked`). The row and its ciphertext are then deleted.
- A connection no machine holds (never shared) is deleted at once, recorded as `revoke_unconfirmed`. After 7 days with no holder, any other is deleted the same way. The provider's own idle expiry (Salesforce 30 days) finishes the job.
- Removing an app does this for each of its connections.

### 5. A person's own CLI

`canopy cred connection-token <provider>` with no `--agent` opens the connection on this machine:
1. It resolves the person's connection, getting the envelopes with a device proof.
2. It opens them with the device's ECDH key, then follows §4.2.
3. It writes the same cache file as today.

This needs the laptop **enrolled** (`canopy vault device --enroll`, approved with a passkey) and **Use on my computers** turned on for that connection. Otherwise it refuses with that sentence and the command to run. A PAT-only laptop can no longer get a token.

### 6. Turns

The turn route returns, for each provider the agent may use, the envelopes, their metadata and the app's client, never a token. The cloud runner's existing staging (`_stage_connections`, holders, heartbeat, cleanup, adoption) calls `canopy cred connection-token --runner …`, which now runs §4.2 locally. The staged file, its path and its format are unchanged, and so are the agent's usage `canopy cred connection-token <provider> --agent <slug>` and `$CANOPY_CONNECTIONS`.

### 7. Provider code splits in two

- **canopy-web** keeps the connect-time half: authorize URL, params, scopes, prompts, option validation, code exchange, identity lookup, the identity policy (`check_owner`, `allowed_domains`), and revoking a refused Salesforce grant at connect.
- **The CLI** (canopy repo) gains `orchestrator/connections/providers/{salesforce,google}.py`: token URL from the provider's rules, refresh, revoke, and `rotates_refresh_token`.

A test in each repo pins the shared names (provider key, required scopes, rotation).

### 8. Audit and logging

The `connections.*` vocabulary stays. The new verbs are `shared` (the statement and its seal, with version), `refreshed` (with machine, rotated yes/no), `resealed`, `revoked` / `revoke_unconfirmed`, and `lease_expired_unwritten` (the crash case). `token_issued` now records the runner or machine that read the material. The secret-scan test extends to sealed payloads: no plaintext token may appear in any event, log or response. The app client secret appears only in material responses to an authenticated machine.

### 9. Moving over

- Existing `Connection` rows (one Salesforce connection today) are not migrated: the server-side plaintext is exactly what we are removing.
- A migration deletes the rows, then drops `refresh_token_enc`, `access_token_enc` and the access-token columns. `ConnectionApp.client_secret_enc` stays.
- After deploy, each person reconnects once and shares on the confirmation screen.

## Existing Google paths

**Connect Google mailbox** (`gog-token`, `google_mint.py`) and **lending your own Google account to an agent** (`google-delegated-*`) fold into Connections as a follow-up after this ships (Jonathan, 2026-11-01). They are runner-minted and sealed already, so they stay as they are until then.

## Order of work

1. canopy-web: sealed columns, seal-to-owner at connect, the share route, recipients, the material, lease and write-back routes, keeper work, the migration. The server never stores or refreshes a token; tests assert it.
2. canopy CLI: provider modules, local refresh with lease and write-back, keeper for connections, the person path on an enrolled laptop.
3. cloud runner: staging through the new CLI path.
4. Frontend: the confirmation screen ("Connected as …", share), "use on my computers", waiting states.
5. Docs: vault.md (replace "OAuth connections are server-held"), api-surface.md, deploy.md.

Releases go together: the CLI first (cloud boxes pick it up), then canopy-web with the migration.

## Accepted trust at the start

The guarantee holds from a connection's first honest seal onward. These limits remain:

- **At the moment of connecting.** A server compromised at that moment can substitute that one connection.
- **A fresh box's first read.** A box with no pin for a connection can be handed a self-consistent forged set. That gives it a substitute token and never the real one. It also pins the attacker's owner key on that box, so the real set is refused there until the box's pin entry is removed or the box is replaced.
- **An old endorsement.** A device with an old endorsement by the owner or a listed endorser, for an agent still in the statement, can be re-listed by the server. Removing such a device from the statement or revoking its device key therefore does not retire it. Retiring a device whose key may be compromised means ending the grant at the provider by disconnecting and connecting again.

## Downsides accepted

- A connection made while canopy-web is compromised is exposed, as is an owner who loads its pages then.
- Re-sealing and revoking need a holder runner online (or the owner's browser). Turns need one anyway.
- A crash between refresh and write-back can force a reconnect on rotating providers.
- Cloud runners become the target. A box exposes the connections of the agents it runs.
- canopy-web cannot inspect a token, so health comes from runner reports.
- The CLI needs an enrolled laptop.
- Losing every holder and the owner's vault key means reconnecting, which is cheap for OAuth.

## Left to the builder

Model and field names beyond those above, lease timings within the bounds given, how the keeper batches connection work, and the exact refusal wording (plain sentences, one action each).
