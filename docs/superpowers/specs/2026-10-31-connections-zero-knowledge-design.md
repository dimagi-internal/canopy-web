# Connections hold nothing canopy-web can use: tokens are minted, sealed and refreshed on runners

**Status:** design 2026-10-31 (amended by the plan's rulings R1–R17, `docs/superpowers/plans/2026-11-01-connections-zero-knowledge.md`); approved 2026-11-01 with two decisions: the owner signs grants with a passkey touch (§4.3), and the older Google paths fold in later. It replaces the storage and token paths of the server-held Connections feature (#2287, #2311, #2347). The workspace apps, per-workspace callbacks, providers, grants and the Settings screens stay.

## Intent

Jonathan's framing: *"As long as we can still run autonomously, and canopy-web getting hacked gives nothing (although it does let you trigger sessions to the AI), that's a good framework."* The case he cares about most: *"general Salesforce credentials I have agents step up into (or Gmail more likely) … across multiple agents on my behalf, seamlessly."*

Today canopy-web holds every connection's refresh token and every workspace app's client secret, Fernet-encrypted under `FIELD_ENCRYPTION_KEY`, which the running server holds. A compromised server therefore yields standing, portable access to every connected Salesforce and Google account. After this change it yields ciphertext. It can still start turns, which is a separate problem with separate limits: narrow agent permissions, and tokens kept out of the model's context.

The pattern already exists twice in canopy: **Connect Google mailbox**, where the token is made on a runner (`apps/agents/credentials/google_mint.py`), and **delegated credentials**, which are sealed to the machines that run an agent and re-sealed by keepers (`apps/agents/credentials/accounts.py`, `docs/architecture/vault.md`). This design generalizes both to every provider in `apps/connections`.

## Goals

1. **canopy-web never holds a usable token or client secret.** No refresh token, access token or client secret reaches canopy-web in plaintext, at rest or in transit. That covers connecting, refreshing, a turn, the CLI, disconnecting and removing an app.
2. **Autonomous turns work as today.** A cloud runner picks up an agent's turn and the token is at `~/.canopy/connections/<slug>/<provider>.json` in the same format. Agents, skills and the chrome-sales MCP need no changes.
3. **One connection, many agents.** A person connects their Gmail or Salesforce once and lends it to any number of agents they administer. Each agent's machines can open it, and every use is recorded.
4. **Rotating refresh tokens work.** Salesforce rotates the refresh token on every refresh. Exactly one machine refreshes at a time, the new token is written back before it is relied on, and a crash between the two is detected rather than silent.
5. **Same screens, same provider setup.** Workspace admins keep Settings → Connections. The Salesforce External Client App and Google OAuth client already registered keep working, with the same callback URLs.

## Non-goals

- **Stopping a compromised server from starting turns.** Turns are bounded elsewhere: the agent's own provider permissions, tokens kept out of the model's context, and the confined-turn env allowlist.
- **Laptop runners staging connection tokens for turns.** Only cloud runners stage them, as today. A person's own laptop can use their own connection through the CLI (§6).
- **Hiding a token from the owner of a machine that holds it.** As with delegated credentials, a box's owner can extract what the box decrypts.
- **Lending a connection to an agent you don't administer.** Same rule as delegated credentials today.

## Anti-goals

- A fallback where canopy-web refreshes, exchanges or revokes "when no runner is online". The point is that it cannot.
- A runner sealing to recipients only because canopy-web listed them. A compromised server must not be able to add its own key to a seal (§4.3).

## The design

### 1. What is stored

| Thing | Today | After |
|---|---|---|
| App client secret and policy | `ConnectionApp.client_secret_enc` (Fernet); options in plain JSON | One `zk1` value per provider in the workspace's **Connections vault** (§5), named `connection-app:<provider>`, sealed in the admin's browser: `{client_id, client_secret, options}`, where options holds `login_url` and `allowed_domains`. The runner takes every security-relevant setting from this value, which the server cannot forge, never from the server's copy. `ConnectionApp` keeps plain `client_id`, options and scopes for display and for building authorize URLs. |
| Refresh token | `Connection.refresh_token_enc` (Fernet) | `Connection.sealed`: a `zks1` envelope bound to `connection:<uuid>` (the binding id takes the agent-id slot in AAD and HKDF, as workspace vaults use `vault:<uuid>`). The plaintext is a small JSON: `refresh_token`, `client_id`, `client_secret`, `login_host` (Salesforce), `instance_url`, `account_id`, `owner_key` (the thumbprint of the owner's member key, §4.3), `issued_at`. URLs are never taken from it as given. The CLI derives every endpoint from the provider's hard-coded rules and checks `login_host` and `instance_url` against them (Salesforce: `login`/`test.salesforce.com` or `*.my.salesforce.com`). |
| Access token | `Connection.access_token_enc` (Fernet) | `Connection.access_sealed`: a `zks1` envelope to the same recipients, plus a plaintext `access_expires_at` so machines can decide without opening it. Shared so machines reuse one access token instead of each refreshing (§4.2). |
| Who can open | — | `Connection.recipients()`, computed, never stored (§3). |

The client secret travels inside each connection's envelope, so any recipient can refresh **without** being a member of the Connections vault. That matters for a person's laptop. The vault copy is used at connect time and to pick up a rotated secret (§4.4).

Each `Connection` row keeps its non-secret metadata as today: owner, app, provider, `account_id`, `account_label`, `scopes`, `needs_reconnect`, `last_used_at`, plus the new `sealed_digest` (SHA-256 of `sealed`, for compare-and-swap).

### 2. Connecting

The browser legs stay: `/auth/connections/<provider>/<workspace>/start/` and `…/callback/`. What happens between them moves to a runner, on the `AgentGoogleMint` pattern, as a provider-neutral `ConnectionMint` in `apps/connections`:

1. **Start.** canopy-web picks a **minting runner** (below) and creates a `ConnectionMint` (`requested`). The browser waits on a small "Connecting…" page that polls the mint.
2. **Challenge.** On its `mint/claim` poll the runner makes the PKCE verifier, keeps it in memory only, and posts the challenge. canopy-web builds the provider's authorize URL (the provider class still owns authorize params, scopes and prompts) and sends the browser there.
3. **Callback.** The provider returns the code to the same per-workspace callback. canopy-web checks state, workspace and client id as today, and holds the code for the runner. Without the verifier and the client secret the code is useless to canopy-web.
4. **Exchange on the runner.** The runner takes the code (once) and runs `canopy connection mint`. That command:
   - reads `connection-app:<provider>` from the Connections vault
   - exchanges code + verifier at the provider
   - looks up the identity (Salesforce identity URL; Google userinfo or id_token)
   - refuses a missing refresh token or required scope
   - seals the §1 plaintext to the endorsed recipients (§4.3)
   - posts `{sealed, access_sealed, access_expires_at, identity{account_id, label, email, hosted_domain}, scopes}`
5. **Policy on canopy-web.** It applies everything that today runs after the exchange, using the identity the runner reported: `allowed_domains`, an agent's own Google account, "wrong account", required scopes, duplicate accounts. A refusal tells the runner to revoke the grant it just made (it still holds it in memory) before discarding it.

**The runner enforces the account itself.** The server only learns what the runner reports, and it could feed the runner a code from a sign-in of its own (it knows the PKCE challenge). So the runner checks the policy it can verify:
- `allowed_domains`, from the vault value.
- On a reconnect, the account must equal the `account_id` inside the existing envelope.
- For an agent's own Google account, the account must equal the identity in the agent's vault.

What it cannot verify (a first connect, where any account is allowed) is closed by the owner's signed confirmation (§4.3): a connection is lent to no agent until its owner, in the browser, has seen the account it holds and signed it.

**Who mints:** an online **cloud** runner (never a laptop) that can read the Connections vault (§5) and that will be a recipient of the result, so nobody else sees the token.
- For an **agent's own** connection: a cloud box that serves the agent.
- For a **person's** connection lent to an agent: a cloud box that runs one of those agents.
- For a **person's** connection with no agents yet: a cloud box in the workspace that serves an agent the person administers. It is a recipient only until the person lends it or turns on "use on my computers". If the person does neither within the confirmation step, the connection is discarded.
- None online: 409 `no_mint_runner` with the reason in words. There is no fallback.

The minting box declares `capabilities.connection_mint` (cloud runners only, as `google_mint`).

### 3. Who can open a connection

`recipients(connection)` is the single rule, read by every seal, reseal and write-back:

- **Agent-owned** (Eva's own Salesforce user): every ECDH-ready device on a runner that serves the agent and may hold it (`runner_may_hold_agent`), plus the same for every agent it is granted to.
- **Person-owned** (new: today `ConnectionGrant.clean` forbids lending a person's connection, and this design deliberately allows it): for each agent it is granted to, `accounts.delegated_devices(agent, person)` (cloud boxes that run it, or the person's own runners). Plus the person's own enrolled machines if they turned on **Use on my computers**. `may_hold_for` is extended here to allow the `computer` kind, because this use is the person's own CLI, not a turn. A laptop only ever reads a connection: it never mints, keeps (re-seals for others) or revokes. Plus the person's member vault key, as for delegated entries.
- A grant is in force only while its rule holds today: the person still administers the agent, and the agent is still in the app's workspace. A connection with no grants, and with "use on my computers" off, still needs at least one recipient, so connecting it offers those two choices.

Recipients that stop qualifying are pruned from the envelope without decrypting (`zk_seal.keep_only`, as `accounts.prune`). New ones receive it from a holder (§4.1).

### 4. On the runners

**4.1 Re-sealing for new machines and new grants.** The keeper (`canopy vault zk keep`, every 120 s on cloud boxes) gets a `connections` list in its work beside `delegated`. For each connection it holds, it gets the waiting machines and their endorsements, and re-seals the **same** plaintext through `PUT …/vault-keeper/connections/{id}` with `expect = sealed_digest`. This is `keep_delegated` with a different binding id. Granting a connection to a new agent therefore needs no action from the person if any holder is online. With no holder online the grant shows "waiting for a machine that holds it" and fills in when one comes up.

**4.2 Getting an access token** (turn staging, heartbeat re-staging, CLI):
- If `access_expires_at` is more than the needed minimum life away, open `access_sealed` and use it.
- Otherwise take the **refresh lease**: `POST …/connections/{id}/refresh-lease`, one holder at a time, 60 s, renewable. Another machine that wants a token waits for the new `access_sealed`, polling up to 30 s, rather than refreshing in parallel.
- Then refresh at the provider, and immediately write back `PUT …/connections/{id}/sealed {sealed?, access_sealed, access_expires_at, expect}`, with `sealed` only when the refresh token rotated. Only after that succeeds is the token staged.
- If the write-back fails, the lease is kept and the box retries from memory. If the box dies, the lease expires and the next refresher gets `invalid_grant`, which marks the connection `needs_reconnect` (it is not left silently broken). It surfaces exactly as an expired connection does today.

**4.3 What a runner trusts: the owner's signed grant, never the server's list.** The existing keeper trusts a person on first sight, and it reads an endorsement's subject from the server's payload. So the server's word decides who is lent what, which is not good enough here. Connections add one signed document:

- **The grant statement.** The owner signs it in the browser with their vault key (ECDSA P-256, as endorsements are). It covers `{connection, account_id, agents: [agent ids], my_computers: [device ECDH thumbprints], version}`.
  - For an agent-owned connection, the admin who connects it signs it.
  - It is made at connect time, after the browser has opened the envelope (the owner's member key is a recipient) and shown the account. It is re-signed on every lend, withdrawal or "use on my computers" change, which costs one passkey touch each.
  - Its signer's key thumbprint is sealed inside the envelope (`owner_key`), so later runners check the signature against a key the server never supplied.
- **Who a runner will seal to:** a device it may seal to must meet one of two conditions:
  - It is listed in the statement's `my_computers`.
  - It carries a valid endorsement of its exact ECDH key, signed by a person the box trusts, naming an agent that is in the statement's `agents`. The runner also checks the endorsement's subject against the statement itself, not against the server's payload.

  Member keys: only the owner's. canopy-web's recipient list only narrows this set, for example pruning a box that stopped serving. It can never widen it. A request to seal to anything else is refused as `zk_unendorsed_recipient`.
- **Envelopes are authenticated.** Every envelope a runner writes (`sealed`, `access_sealed`) carries, for each recipient, a MAC keyed by ECDH between the writer's and that recipient's keys, covering the envelope and `expect`. It is not a signing-key signature: aws-iam boxes have no signing key, and only canopy-web links a signing key to an ECDH key (plan Part 0.4). A reader checks that the signer is a device the statement allows, or the owner. Anyone can build a `zks1` from public keys, so an unsigned or wrongly signed envelope is refused. This is how a forged access token pointing an agent at a stranger's account is stopped.
- **Accepted trust at the start.** The owner's key comes from canopy-web the first time a runner sees it, as for every zero-knowledge vault in canopy today: keepers pin a person on first sight. So a server compromised at the moment of connecting could substitute keys for that one connection. One compromised later gets nothing from connections made before.

A write-back must also keep every current, still-eligible recipient, as `accounts.reseal` requires.

**4.4 A rotated client secret.** When an admin replaces the secret in Settings, the new one lands in the Connections vault. A refresher that can read the vault prefers the vault's secret and rewrites the envelope at its next write-back. One that can't (a person's laptop) keeps using the old secret until a holder that can read the vault rewrites it. Salesforce and Google both accept the old secret during a staged rotation. If the old one has been revoked first, those connections show `needs_reconnect`.

**4.5 Disconnecting and removing an app.**
- **Revoking at Google ends every token of that account on that OAuth client**, not just one. So a refusal at mint (a duplicate account, the wrong account) discards the new token without revoking. A disconnect revokes at the provider only when no other connection of that account through that app remains; otherwise it only deletes.
- A disconnect marks the row `revoking`: it disappears from every list at once and is never handed out again.
- The first holder online opens it, revokes at the provider, and confirms (`POST …/connections/{id}/revoked`). The row and its ciphertext are then deleted.
- After 7 days with no holder, the ciphertext is deleted anyway, recorded as `revoke_unconfirmed`. The provider's own idle expiry (Salesforce 30 days) finishes the job.
- Removing an app does this for each of its connections.

### 5. The Connections vault

A new vault kind, `Vault.kind = "connections"`, one per workspace, built on the workspace-vault engine with two differences:
- It is **never layered into turns** (`workspace_vaults.layers_for` skips it), so no agent's turn reads a client secret.
- **Members** are the workspace's owners and admins only. **Machines** that may hold its key are cloud boxes that serve an agent of the workspace, whose person is a member. A person's laptop never qualifies.

- **Made** the first time an admin configures an app in a workspace: `<workspace> connections`.
- **Key:** it needs a key before the first secret can be sealed, made in the browser with the admin's recovery code like any workspace vault (`makeWorkspaceVaultKey`).
- **Getting it to machines:** keepers pass the key to cloud boxes as for any workspace vault.
- **The secret:** the Settings → Connections form seals `connection-app:<provider>` in the browser (`PUT /api/vaults/{id}/zk/values`). `ConnectionAppIn` loses `client_secret`, and `has_secret` becomes "the vault holds `connection-app:<provider>`".
- **No fallback:** if the browser can't hold the key (no vault key yet), the form says so and points to the one-time vault-key setup. There is no server-side fallback.

### 6. A person's own CLI

`canopy cred connection-token <provider>` with no `--agent` opens the connection on this machine:
1. It resolves the person's connection, getting the envelopes with a device proof.
2. It opens them with the device's ECDH key, then follows §4.2.
3. It writes the same cache file as today.

This needs the laptop **enrolled** (`canopy vault device --enroll`, approved with a passkey) and **Use on my computers** turned on for that connection. Otherwise it refuses with that sentence and the command to run. A PAT-only laptop can no longer get a token. That is the cost of the server holding nothing.

### 7. Turns

The turn route returns, for each provider the agent may use, the envelopes and their metadata, never a token. The cloud runner's existing staging (`_stage_connections`, holders, heartbeat, cleanup, adoption) calls `canopy cred connection-token --runner …`, which now runs §4.2 locally instead of asking canopy-web. The staged file, its path and its format are unchanged, and so are the agent's usage `canopy cred connection-token <provider> --agent <slug>` and `$CANOPY_CONNECTIONS`.

### 8. Provider code splits in two

- **canopy-web** keeps what needs no secret: authorize URL, params, scopes, prompts, option validation, and the identity policy (`check_owner`, `allowed_domains`).
- **The CLI** (canopy repo) gains `orchestrator/connections/providers/{salesforce,google}.py` with what needs a secret: token URL, code exchange, refresh, revoke, identity lookup, and `rotates_refresh_token`.

Adding a provider becomes two small modules, one in each repo, and the recipe docstring says so. A test in each repo pins the shared names (provider key, required scopes).

### 9. Audit and logging

The `connections.*` vocabulary stays. The new verbs are:
- `minted` (with runner)
- `refreshed` (with runner, rotated yes/no)
- `resealed`
- `revoked` / `revoke_unconfirmed`
- `lease_expired_unwritten` (the crash case)

`token_issued` now records the runner or machine that staged the token. The secret-scan test extends to sealed payloads: no plaintext token may appear in any event, log or response.

### 10. Moving over

- Existing `Connection` rows (one Salesforce connection today) and Fernet client secrets are not migrated: the server-side plaintext is exactly what we are removing.
- A migration deletes the rows and empties `client_secret_enc`, then drops both Fernet columns.
- After deploy, the admin re-enters the Salesforce secret once (now sealed in the browser) and each person reconnects once.
- The Salesforce and Google apps themselves and their callback URLs are unchanged.

## Existing Google paths

Two Google paths already exist beside Connections: **Connect Google mailbox** (an agent's own `gog-token`, `google_mint.py`) and **lending your own Google account to an agent** (`google-delegated-*` entries). **Decided (Jonathan, 2026-11-01): fold both into Connections as a follow-up, after this ships.** They are already runner-minted and sealed, so they stay as they are until then.

## Order of work

1. canopy-web: the Connections vault, `ConnectionMint`, the sealed columns, the lease and write-back routes, keeper work, recipients, policy-on-report, the migration. Every server path that decrypts is removed, and tests assert there is no `decrypt_secret` in `apps/connections`.
2. canopy CLI: `connection mint`, provider modules, local refresh with lease and write-back, keeper for connections, the person path on an enrolled laptop.
3. cloud runner: `connection_mint` claim handling, staging through the new CLI path.
4. Frontend: the "Connecting…" page, browser-sealed app secret, "use on my computers", waiting states.
5. Docs: vault.md (replace "OAuth connections are server-held"), api-surface.md, deploy.md.

Releases go together: the CLI first (cloud boxes pick it up), then canopy-web with the migration.

## Downsides accepted (from the conversation)

- Connecting, revoking and re-sealing need a holder runner online. Turns need one anyway.
- A crash between refresh and write-back can force a reconnect on rotating providers.
- Cloud runners become the target. A box exposes the connections of the agents it runs.
- canopy-web cannot inspect a token, so health comes from runner reports.
- The CLI needs an enrolled laptop.
- Losing every holder and the person's vault key means reconnecting, which is cheap for OAuth.

## Left to the builder

Model and field names beyond those above, the poll interval of the "Connecting…" page, lease timings within the bounds given, how the keeper batches connection work, and the exact refusal wording (plain sentences, one action each).
