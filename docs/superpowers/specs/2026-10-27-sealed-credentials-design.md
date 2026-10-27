# Sealed credentials: an agent's turn never holds the password

**Status:** design 2026-10-27, not built. Jonathan asked whether there was "an even better way to make the agents even less able to read/accidentally log passwords than we are currently doing". Of three options (mask known values in transcripts, give each turn its own OS user, or keep the secret off the agent entirely and add it on the wire) he chose the last one and asked for it to be designed in full. Running each turn as its own OS user turned out to be a prerequisite, so it is Part 1 here.

## The idea in one paragraph

Today a full turn on a runner gets each vault value as a plain environment variable and in a per-turn env file. Anything the agent runs can print it, and the printout lands in the model's context, in canopy's transcript, and in the provider's logs. With this design the turn gets a **stand-in** instead, a string like `canopy-sealed-v1.ghp.k3J9…` that is worthless off the box. The box's egress proxy already sees every connection a locked turn makes. When a request leaves for a host the secret is **bound** to, the proxy puts the real value into the one header (or form field) the binding names. The agent can still *use* the GitHub token, the labs token and the Google login. It can never *read* them. Sending the stand-in anywhere else does nothing, because the proxy substitutes only on the bound host. The proxy runs as a user the turn is not, holding values the turn cannot reach.

## Glossary

- **Sealed credential.** A vault value a turn gets only as a stand-in. The box's proxy puts the real value on the wire, only to the hosts its binding names. The agent can use it but cannot read it.
- **Held credential.** A vault value a turn gets in plain text, as every value is today. Each held credential is shown as such, and the aim is to have none.
- **Stand-in.** The string a turn sees in place of a sealed value. It is per turn, means nothing after the turn ends, and the proxy honours it only for that turn's own connections.
- **Binding.** What a sealed credential may be used for: which hosts, which place in the request (a header, HTTP basic auth, a form field on one path), and in what format. It is written by a vault member and sealed with the value, so canopy-web cannot change where a secret goes.
- **Turn user.** The OS user one turn runs as on a cloud box. It is never the runner's own user, and never another live turn's.

## Intent

Jonathan's question, in his words: *"is there an even better way to make the agents even less able to read/accidentally log passwords than we are currently doing?"* — and, of the proxy option, *"option 1 sounds pretty good, design it fully out."*

His worry covers two different things, and this design treats both:

1. **Accidental logging.** An agent runs `env`, prints a config object, or echoes a failing curl command, and a password ends up in a transcript that is stored, shared, or exported. Masking after the fact is regex-based today (`apps/session_sharing/redact.py`), runs only on shared pages and the ledger export, and cannot recognise a plain password at all.
2. **Reading.** Prompt injection in an email or web page persuades the agent to read its credentials and send them somewhere. Today nothing stops a full turn reading its env file. On an egress-locked box the agent cannot reach an attacker's host directly, but it can still put the value in a GitHub gist, an email, or a labs record.

The current defences are about *which* turns get secrets (confined turns get none, `page_site` withholds a site login, delegated entries are sealed to machines). Once a turn has a secret, nothing protects it. This design protects it.

## Goals

1. On a cloud box running this design, an agent's turn never holds the plaintext of any sealed credential: not in its environment, its files, its process memory, or anything it can read.
2. The agent's tools keep working unchanged for credentials sent in a request header, in HTTP basic auth, or in OAuth token-endpoint bodies. That covers `gh`, git over HTTPS, the labs/OCS/Nova/Kindora API keys, gog's Google login and Claude's own login.
3. A stand-in sent anywhere but its bound hosts and place is never substituted. Where the proxy can see it (a header or query on a host it intercepts), it reports the attempt as a signal of misuse; elsewhere (inside a tunnel to another host, in a body) it cannot see it, and the stand-in is harmless there.
4. Where a secret may go is decided by a vault member and sealed with the value. canopy-web, which holds no vault key, cannot widen a binding.
5. Each vault entry shows whether it is sealed or held, and the agent's health page counts the held ones. Owners can mark an entry **sealed only**: a turn that cannot be given it sealed does not get it at all.
6. Each turn runs as its own OS user on a cloud box, so a turn cannot read the runner's files, another turn's files, or another agent's credential files. This also closes the "Known limit" in `docs/architecture/access.md`.
7. Nothing breaks during rollout: an entry stays held until it has a binding, and a box without the proxy keeps working as today.

## Non-goals

- **Laptops.** On a laptop, emdash starts sessions as the person's own OS user with their whole environment (`runner/canopy_runner/canopy_runner/desktop.py` `_cli_env`), and there is no separate user to hold the values. Laptop turns keep held credentials. An entry marked sealed only does not run on a laptop (§5). A later design could use a macOS helper, but none is proposed here.
- **Limiting what an agent can *do* with a credential.** A sealed GitHub token can still push, open PRs and create gists. Sealing stops the agent learning the value. Scope is still the token's own (fine-grained PAT, labs scopes).
- **Hiding values from the box's owner or root.** Root on the box can read the proxy's memory. As today, a machine's owner can extract what the machine decrypts (`2026-10-26-delegated-credentials-in-the-vault-design.md` §1.4).
- **Credentials that cannot be put on the wire by a proxy:** a private key used to sign locally (a Google service-account JSON key, SigV4), a key that decrypts data on the client (1Password's service-account token), a password typed into a web page in a browser. These stay held. §7 says what to do about each, and Part 4 makes the browser case sealable later.
- **Masking held values in transcripts.** This is option 3 from the same conversation. It is cheap and complementary, but it is a separate change.

## Anti-goals (would be a failure even if it technically worked)

- **The proxy becomes a way out.** A stand-in that is substituted into a request body, a URL, or for any host not in its binding would let an agent post the real value anywhere it can post a stand-in. Substitution happens in exactly the place the binding names and nowhere else.
- **canopy-web can redirect a secret.** If the server could edit a binding, a compromised canopy-web could bind the GitHub token to its own host and receive it on the next turn. That would undo the vault's zero-knowledge property.
- **Interception of traffic that needs none.** The proxy decrypts TLS only for hosts some live binding names. Everything else stays a plain CONNECT tunnel, as today.
- **Silent fallback to held.** A sealed-only entry that quietly arrives as a plain value on a box without the proxy would be worse than none, because the owner believes otherwise.
- **Stand-ins that work outside their agent's own live turns.** A stand-in learned from a transcript must be useless to any other agent, on any other box, and when no turn of its agent is running on that box. (A later turn of the *same* agent on the same box may honour it: that turn is given the same credential anyway, §2.4.)

## Today (what the design builds on)

From reading the code on 2026-10-27:

- **Egress proxy.** `cloud_runner.py --egress-proxy` (`egress_proxy_main`, ~2460) is an asyncio CONNECT-only proxy running as system user `canopy-egress`, with one loopback listener per runner on port `20000 + uid % 10000`. It admits port 443 to an allowlist (`api.anthropic.com`, GitHub, canopy-web, plus per-runner extra hosts), re-read on every connection from `/etc/canopy-runner/<id>.egress.allow`, which root syncs from `POST /api/harness/runners/box-egress` every minute. An iptables chain, `CANOPY-EGRESS`, keyed on each locked runner's uid, forces all traffic through it. It does not decrypt TLS or log content. It is off unless the runner is locked.
- **Users.** Each runner on a multi-runner box is `canopy-runner@<id>` running as `cu-<id>`. **Claude and every tool it runs share that uid** (`runner/ec2/README.md` ~799). The KMS broker (`kms_broker.py`, root, Unix socket, `SO_PEERCRED`) does ECDH for whichever process has the runner's uid, so a turn today can do anything the runner can, including decrypting the vault.
- **How values reach a turn.** `_agent_env` (`cloud_runner.py` ~3550) layers the agent's values (`_turn_values`, in memory from `canopy cred env --runner --print-json`, or legacy `~/.<slug>/.env`) into the environment, and `write_turn_env` writes `/run/canopy/<uid>/<turn>/env` (0600, same uid). GitHub comes per turn as `GH_TOKEN`, `GITHUB_TOKEN` and a git credential helper that echoes `$GH_TOKEN`. gog gets `GOG_KEYRING_PASSWORD`, derived from `/etc/canopy-runner/<id>.gog-secret`. Claude's own login is `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`, set in the runner process and inherited.
- **Confined turns** get an allowlisted environment (`_confined_env`, ~10922) with no agent secrets. This design leaves them as they are.
- **Vault entries.** `AgentCredential` and `VaultSecret` both inherit `VaultEntryAccount` (`apps/agents/models.py` ~499): `account`, `provider` (`github|salesforce|anthropic|other`), `identity`, `scopes` (shown, not enforced), probes. Values are `zk1:` (AES-GCM under the vault key) or `zks1:` (sealed to readers). canopy-web stores ciphertext only. No field records where a value may be used. The vault-name→env-var mapping is the agent's `.env.tpl`, applied on the box.
- **The gateway precedent.** For a connected site, canopy-web's gateway adds the host grant on the server (`apps/host_grants/host_gateway.py`) and the turn never sees the token. `page_site` withholds the agent's own login there (`own_login:`). That is the same principle — use without holding — done server-side for one kind of credential. This design does it on the box for all of them.

## Part 1 — Each turn runs as its own OS user (prerequisite)

Without this, a stand-in is cosmetic: the turn shares the runner's uid, so it can ask the KMS broker to decrypt the vault, read the runner's memory through `/proc`, or read `~/.<slug>/.env`.

**Decided (my call):**

- **A pool of turn users per runner.** For runner `<id>`, root creates `ct-<id>-0` … `ct-<id>-<n-1>`, where `n` is the runner's concurrent-turn limit. These are system users with no login shell. Each turn takes a free one and gives it back when it ends. Fixed users, rather than systemd `DynamicUser`, keep home directories and iptables rules simple to reason about.
- **Which boxes (my call).** Only boxes installed with runner units (`--install-units`, each runner its own `cu-<id>`), which are also the only ones with the egress lock. A legacy single-runner box running as `ubuntu` (passwordless sudo on stock EC2) cannot hold a boundary and reports `sealing: false`.
- **Groups.** Each turn user has its **own** group, `ct-<id>-k`, whose only other member is the runner `cu-<id>`. A turn's home is 0750 `ct-<id>-k:ct-<id>-k`, so the runner can create the worktree and read transcripts, and **no other turn user can read it** (a shared group would give every turn read on every other turn's home, and on a runner holding several agents that would expose another agent's files).
- **Loopback.** A locked turn may still reach loopback, since an agent's own local servers listen there. So no control channel may be a loopback TCP port: the launcher, the KMS broker and the proxy's table socket are Unix sockets checked by peer uid. The builder lists the box's loopback TCP listeners and confirms none of them serves anything a turn must not reach.
- **Clean slate per turn.** When a turn ends, the runner kills every process of that turn user (`pkill -U`, then a check that none remain) and empties its home, except `~/.claude` session state for a resumed conversation, which the runner moves with the session as it does today. A turn user that will not empty is retired until the box restarts.
- **Starting the turn as that user.** The runner (`cu-<id>`) must not gain general root. Of two ways, I recommend the first:
  1. **A root launcher on a Unix socket**, on the KMS broker's pattern (`SO_PEERCRED`): the runner asks "start this argv, in this cwd, with this environment, as `ct-<id>-k`". The launcher checks that the caller is `cu-<id>` and the target is one of *that* runner's turn users, then forks, drops to the user and execs. It returns the pid plus stdin/stdout pipes through the socket (`SCM_RIGHTS`), so the runner drives Claude's stdio exactly as now (`subprocess.Popen` at `cloud_runner.py` ~5233 and ~7242, and the ACP executor).
  2. Ambient `CAP_SETUID`/`CAP_SETGID` on the runner unit. That is simpler, but a compromised runner could then become any user on the box.
- **iptables.** The `CANOPY-EGRESS` rule moves from the runner's uid to its turn users (`--uid-owner` for each `ct-<id>-k`), and the runner's own uid keeps the access it needs to reach canopy-web. A locked runner's turns can reach only their own proxy port.
- **The KMS broker** keeps answering only `cu-<id>`. A turn user is refused, so no turn can decrypt the vault.
- **What moves out of the turn's reach:** `~/.<slug>/.env`, the gog keyring file and `/etc/canopy-runner/<id>.gog-secret`, `.sf-creds.json`, the turn env file, the runner's in-memory cache, and Claude's login directory (`CLAUDE_CONFIG_DIR` / `~/.claude-logins`). Anything the turn still needs, the runner copies into the turn user's home for that turn, as a stand-in where Part 2 applies.

Part 1 is useful on its own, because it closes the known limit, and it ships first. A turn whose values are still held keeps getting them as today, now in its own user's environment and files rather than the runner's.

## Part 2 — The sealing proxy

### 2.1 Where it runs

**Decided (my call): the existing egress proxy gains a credential table, rather than a second daemon.** It already runs as its own user (`canopy-egress`), already sees every connection a locked turn makes, and is already the only way out (iptables). A second hop would add a second place to configure hosts and a second failure mode.

So sealing requires the runner to be **egress-locked**. A sealed binding's hosts are added to the effective allowlist automatically (they are shown on the egress card as "added by a sealed credential", not typed in by hand).

### 2.2 How a turn's stand-ins are loaded

1. The runner opens the agent's values as today: `canopy cred env --runner`, ciphertext from `credentials/resolve`, decrypted with the device key through the KMS broker. **This stays in the runner's uid**, which turns no longer share.
2. For each value whose sealed payload carries a binding (§3), the runner makes a stand-in (§2.4) and gives it to the turn in place of the value: in the environment variable the agent's `.env.tpl` names, in the turn env file, and anywhere else the value used to go (the git credential helper, gog's keyring, Claude's login).
3. Before starting the turn, the runner sends the proxy the turn's table over a Unix socket at `/run/canopy-egress/<id>.sock`: `{turn_uid, turn_id, entries: [{stand_in, value, binding}]}`. The socket is owned by `canopy-egress`, mode 0660, group `cu-<id>`. The proxy checks `SO_PEERCRED` is `cu-<id>` and refuses a table for a uid that is not that runner's turn user. One runner can never load secrets for another runner's turns.
4. When the turn ends, the runner sends `drop {turn_id}` and the proxy forgets the table. The proxy also drops a table when its turn user has no processes left, so a runner crash does not leave stand-ins live. The stand-ins die with the table.

The proxy keeps tables only in memory. It writes no value to disk or to its log.

**When the proxy restarts (my call: turns survive).** The proxy restarts when a runner is added or removed (`apply_egress`) and on a crash (`Restart=always`). Killing every sealed turn each time would be unacceptable, so:

- The runner watches its table socket. When the proxy comes back, the runner reloads the table of every live turn it holds; it still has the values.
- The CA key is kept for the box's lifetime, not the process's (§2.7). Clients that loaded the CA bundle at start keep trusting new leaf certificates.
- The unit gets `RuntimeDirectory=canopy-egress` (for the sockets) and `StateDirectory=canopy-egress` (for the CA), so it can keep `ProtectSystem=strict`. The CA certificate is published from the state directory to the turns' bundle by root at start.

Requests that fail during the gap fail as any network blip does.

### 2.3 Which connection belongs to which turn

The proxy must know which turn user opened each connection, so it applies only that turn's stand-ins. On loopback this is exact: the proxy looks the client's address and port up in `/proc/net/tcp{,6}`, which gives the socket's owning uid. A connection from a uid with no table gets no substitution, only plain CONNECT as today. This is what makes a stand-in useless to any other agent's turn and outside its own agent's live turns.

### 2.4 The stand-in

- **Format:** `<provider prefix>cnpysealed<random url-safe chars>`. The provider prefix is empty by default and set per preset only where a client checks a token's shape (for example `ghp_` for a GitHub PAT, `sk-ant-oat01-` for Claude's OAuth token). The marker `cnpysealed` lets anyone reading a transcript see it is a stand-in. The random part says nothing about the value. The builder checks each client; a client that rejects every shape is listed as held until fixed. If a redaction regex masks a stand-in in a shared page, nothing is lost.
- **Length:** at least as long as the shortest real value its client accepts.
- **Lifetime (my call): stable per agent, entry and box; honoured only during that agent's live turns.** Worktrees and resumed conversations persist across turns. A token in a git remote URL in `.git/config`, in a generated `.mcp.json`, or quoted earlier in the conversation would break the next turn if stand-ins changed every turn. So the runner derives a stand-in once per (agent, entry, box) and keeps it until the value changes or the box restarts. The proxy honours it only on connections from a turn user currently running a turn of that agent with a loaded table. Across boxes, agents, or time with no turn running, it is worthless.

### 2.5 When the proxy decrypts TLS

- For a CONNECT to a host that **some live binding in that turn's table** names, the proxy terminates TLS with a certificate for that host, signed by the box's CA (§2.7). It reads the HTTP request, substitutes (§2.6), and sends it on over a fresh TLS connection to the real host, verifying the real host's certificate against the system trust store.
- For every other CONNECT, including a host bound only in another turn's table, it is a plain tunnel as today. It never decrypts traffic it has no reason to.
- **The request must be for the host it claims.** On an intercepted connection the proxy refuses (and does not substitute into) any request whose `Host` header, or a client SNI that disagrees, is not the CONNECT host. Otherwise a shared front end (Google's, a CDN in front of GitHub) could carry a substituted header to another tenant behind the same address.
- **Parse, then rebuild.** The proxy parses each request, substitutes in the parsed form, and sends a **freshly serialised** request upstream. It never forwards the client's raw bytes after an edit. It refuses ambiguous framing (both `Content-Length` and `Transfer-Encoding`, duplicate `Content-Length`, obsolete line folding) and normalises the path (`//`, percent-encoded `/`, `.`/`..`) before matching a `form` or `query` binding. It refuses `TRACE` and `CONNECT` inside an intercepted connection.
- **Protocol:** the proxy offers only `http/1.1` in ALPN on intercepted connections, so clients fall back from HTTP/2. Supporting HTTP/2 is left to the builder if a client needs it. Upgraded connections (WebSocket) are passed through after the substitution in the upgrade request.
- **Library choice is left to the builder.** The proxy is plain Python today, and it should stay something a reviewer can read. A careful asyncio + `h11` implementation fits; so would using mitmproxy's core as a library. Constraints: no request or response content in any log, bounded memory per connection, and streaming bodies, since git pushes can be large.

### 2.6 Substitution rules

Applied only on intercepted connections, only to requests from a turn user with a table, and only where a binding allows:

- **Host:** exact match against the binding's hosts. A leading-dot suffix (`.githubusercontent.com`) is allowed in a binding only if a vault member writes it explicitly. Presets use exact hosts where possible.
- **Place:** exactly one of:
  - `header` — a named header (`Authorization`, `x-api-key`, `X-Labs-Token`…). The stand-in may appear inside a format (`Bearer {}`, `token {}`). The proxy replaces the stand-in substring within that header only.
  - `basic` — HTTP basic auth. The proxy decodes `Authorization: Basic …`, replaces a stand-in found as the username or password, and re-encodes. This is how git over HTTPS works (`x-access-token:<token>`).
  - `form` — one field of an `application/x-www-form-urlencoded` or JSON body, on one method and path. This is used for OAuth token endpoints (`refresh_token`, `client_secret`) and, in Part 4, login forms. The proxy buffers that request's body (bounded, 64 KB), replaces the field's value if it is exactly the stand-in, and fixes `Content-Length`.
  - `query` — one query parameter, on given paths, for APIs that take a key in the URL. It is allowed in a binding, but no preset uses it.
- **Nowhere else.** A stand-in anywhere else is **left as it is**. The request is not blocked: blocking would tell an attacker which hosts work, and the stand-in is harmless where it lands.
- **Misdirected use (my call: headers and query only).** On an intercepted connection, a stand-in seen in a header or the query string where its binding does not allow it is recorded as a **misdirected use** (§6). Bodies are not scanned for this. Once Claude's own traffic is intercepted (§5.1), every model request carries the conversation, so after one accidental `env` printout every later request would contain the stand-ins and a body scan would raise an alert per request on exactly the harmless case. Streaming scans of git packs and uploads would also cost far more than the signal is worth.
- **Stripping.** The proxy removes any request header whose name is `X-Canopy-*`, so a turn cannot speak for the proxy upstream.

### 2.7 The box's CA

- On the box's first start, the proxy generates a CA key and keeps it in its state directory (`/var/lib/canopy-egress/`, owner `canopy-egress`, 0600). No other user, and no turn, can read it. It lives as long as the box: a restart of the proxy reuses it (§2.2), and a new box makes a new one. Only the certificate is published, world-readable, to the bundle turns trust.
- Leaf certificates are made on demand, per host, short-lived (24 h), cached in memory.
- **The CA is trusted only inside turns**, not system-wide: the runner sets `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` to a bundle of the system store plus the box CA, `NODE_EXTRA_CA_CERTS`, `GIT_SSL_CAINFO`, and `CURL_CA_BUNDLE` in the turn's environment. The builder checks each client the fleet uses (Claude Code itself, `gh`, git, gog, Python `requests`/`httpx`, Node MCP servers) and adds whatever else each needs.
- A turn knowing the CA certificate is harmless. The private key is what matters, and only the proxy has it.

### 2.8 Responses

Some APIs echo the credential (an error that repeats the header, a token-introspection endpoint). On intercepted connections the proxy scans response **headers** and **bodies up to 1 MB** for every real value in that turn's table, **and for the encoded forms it put on the wire**: the base64 of each `basic` pair it built, and the URL-encoded and JSON-escaped forms of each `form` and `query` value. It replaces each with the matching encoding of the stand-in before the turn sees it. Larger bodies are streamed unscanned: the bound hosts are APIs we choose, and none is known to echo a credential in a large body.

### 2.9 Token endpoints: credentials minted during the turn

gog, Salesforce and Claude's OAuth login exchange a long-lived refresh token for a short-lived access token mid-turn. If the access token came back in plain text, the agent would hold a one-hour credential. A binding can therefore say **`seal_response: [access_token, refresh_token, id_token]`** for its `form` place on a token endpoint (for example `POST https://oauth2.googleapis.com/token`). When the proxy sees such a JSON response:

1. It replaces each named field's value with a fresh stand-in.
2. It adds the pair to the turn's table, bound to the hosts the binding lists under `minted_hosts` (for Google: `www.googleapis.com`, `gmail.googleapis.com`, `.googleapis.com` by explicit choice), in `header` place `Authorization: Bearer {}`.

The agent's tools see a normal token response and use the stand-in like any token.

### 2.10 Failure behaviour

- **Proxy down.** A locked turn's traffic cannot leave the box (iptables), so sealed turns fail closed. The runner reports `sealing: down` in its heartbeat, stops claiming turns that carry a sealed credential, and the existing readiness path alerts.
- **Table load refused.** The turn does not start. It is returned to the queue with the reason, as other claim failures are.
- **A client refuses the box CA.** The tool fails with a TLS error. The fix is to add that client's trust variable. Until then the entry can be set back to held by a vault member.
- **The real host's certificate is bad.** The proxy refuses, exactly as the client would have.

## Part 3 — Bindings in the vault

### 3.1 Sealed with the value

**Decided (my call): the binding is inside the sealed payload, not a server column.** Today a `zk1`/`zks1` value seals a string. A sealed credential's plaintext becomes a small JSON document:

```json
{"v": 2, "value": "<secret>", "bind": {
  "hosts": ["api.github.com", "github.com", "codeload.github.com"],
  "place": "basic|header|form|query",
  "header": "Authorization", "format": "Bearer {}",
  "method": "POST", "path": "/token", "field": "refresh_token",
  "seal_response": ["access_token"], "minted_hosts": [".googleapis.com"],
  "stand_in_prefix": "ghp_",
  "sealed_only": true
}}
```

A payload with no `bind` is a held credential, as every value is today, and old CLIs that open the value see a string they can still use, since the box CLI decides the format (§3.4). Because the binding is under the vault key, canopy-web can neither read nor change it. A runner's resolve checks the payload's version and refuses a `bind` it does not understand rather than treat the value as held.

canopy-web also keeps a **display copy** of the binding in plain columns on `VaultEntryAccount` (`sealing` = `held|sealed`, `sealed_only`, `bind_hosts`, `bind_place`) so the vault page, health page and runner routing can show and use it without the key. The box never trusts the display copy: it acts only on the sealed one. If they disagree (a forged display copy), the box reports `binding_mismatch` and the vault page shows it.

### 3.2 Who sets a binding

- A **vault member's browser or CLI**, which can open the value, writes the binding when it writes the value, and the browser reseals both. This is the existing value-write path (`set_agent_zk_values`, `set_workspace_vault_values`) with a richer payload. canopy-web additionally gates **changing a binding's hosts or place** as a setting (`@gate(..., setting=True)`, audited) behind a **passkey step-up**, since it changes where a secret may go.
- A delegated credential (`account = delegated`) is sealed to machines, not to the vault key. The person it came from sets its binding when they give or replace it. Their browser, or canopy-web at the moment of giving as today (`2026-10-26` design, "Sealed by canopy-web at the moment it is given"), seals the binding with it.
- **The rule for who may write a binding (my call): whoever can already read the value, and no one else.** A binding's job is to stop the value reaching a host it should not. Someone who can already read the value gains nothing by misbinding it. So:
  - A **zero-knowledge vault value** (only members and machines can open it): a member's browser or CLI writes the binding, as above. canopy-web cannot.
  - A **value canopy-web can already read**, or reads at the moment it is given: the GitHub token the old `AgentDelegation` path still decrypts and hands out in plaintext (`/turns/{id}/github-token`), and a delegated GitHub token, which canopy-web probes and seals when it is given. canopy-web may write the binding, but only a **preset's** binding (`github`), never a custom one. For the old path the runner applies the `github` preset itself, since there is no sealed payload. Once these move fully into the vault, the old path goes away.
  - **The box's own Claude login**, which the box seals after a sign-in: the box writes the `anthropic-*` preset binding. Its hosts are fixed by the preset and cannot be widened by the box.
- **The agent never sets its own binding.** Nothing in the agent's repo (`.env.tpl`, interface, capability) can bind or widen a credential. The agent's repo may *suggest* a preset per name (an optional `# canopy:bind github` comment in `.env.tpl`), which the vault page offers to apply. Only a member applies it.

### 3.3 Presets

Most bindings come from a preset the vault page offers per provider. Each preset fixes hosts, place, format and stand-in prefix. The initial set, from what the fleet uses today:

| Preset | Hosts | Place | Notes |
|---|---|---|---|
| `github` | `github.com`, `api.github.com`, `codeload.github.com`, `uploads.github.com` | `header` `Authorization` (`token {}` / `Bearer {}`) **and** `basic` | One binding may list two places for GitHub only; git uses basic, `gh` uses the header. Stand-in prefix `ghp_` / `github_pat_` to match the value. |
| `anthropic-api-key` | `api.anthropic.com` | `header` `x-api-key` | The box's own Claude key (runner vault, §5.1). |
| `anthropic-oauth` | `api.anthropic.com`, `console.anthropic.com` | `header` `Authorization: Bearer {}`, plus `form` on the OAuth refresh with `seal_response` | Claude Code's subscription login. Builder verifies Claude Code's refresh endpoint and token-shape checks. |
| `google-oauth` | `oauth2.googleapis.com` | `form` `POST /token` field `refresh_token` (and `client_secret`), `seal_response: [access_token, id_token]`, `minted_hosts: [.googleapis.com]` | gog. |
| `salesforce-oauth` | the instance's login host | `form` on its token endpoint, `seal_response` | chrome-sales. |
| `bearer` | one host, typed by the member | `header` `Authorization: Bearer {}` | Labs (`LABS_MCP_TOKEN`), Nova, Kindora and similar. |
| `api-key-header` | one host, typed | `header` named by the member | OCS, ElevenLabs, OpenAI. |
| custom | typed | typed | Advanced; same validation. |

Hosts are validated as public DNS names, as egress hosts are today (`apps/harness/egress.py`). A member cannot bind to canopy-web's own host (that would let a turn spend a sealed credential *as* canopy) or to a host with a wildcard broader than a registrable domain.

### 3.4 How names map onto stand-ins

The agent's `.env.tpl` still decides which vault name becomes which environment variable, and the box CLI (`canopy cred env --runner`) still produces the map. Its output gains, per name, `{sealed: true, binding}` alongside the value. The runner, not the CLI, makes stand-ins and talks to the proxy. The `--print-json` contract changes in a version-gated way: an old runner that reads a v2 payload it does not understand refuses the entry (fail closed), rather than handing out the JSON as a value.

## Part 4 — Browser logins (later)

ACE logs in to CommCare HQ and OCS with a username and password typed into a page by Playwright (`ACE_HQ_PASSWORD`, `OCS_PASSWORD`). A browser on the box can be pointed at the same proxy, and it trusts the box CA if the runner adds it to the turn user's NSS database (`~/.pki/nssdb`) before the turn. The login form posts the stand-in, and a `form` binding on that one host, method, path and field substitutes the password. Session cookies that come back are the site's own and are not sealed; they are short-lived and are what the agent was always going to hold.

This is Part 4 because each site's login form must be checked by hand (fields, CSRF, JavaScript-side hashing, which would defeat substitution). Until then those passwords stay held, and the vault page says so.

## 5. Routing, readiness and rollout

- **A box says whether it can seal.** The runner's heartbeat reports `sealing: {version, ready}`; `ready` is true when the proxy is up, the runner is locked, and turn users are in place. Trust follows the existing rule for data mode: believed only from a cloud runner's beat signed with its own device key (`runner_protocol.data_mode_trusted`), never from a laptop or a person's token.
- **Sealed-only entries need a sealing box.** If any of an agent's declared names is `sealed_only`, its full turns get a runner requirement `sealing`, through the existing runner-requirements floor (`docs/architecture/runners.md` § Runner flags and host requirements). A turn nothing can serve says so and waits. It never falls back to a box that would hand the value out in plain text (anti-goal: silent fallback).
- **Entries that are sealed but not sealed-only** are given as stand-ins on a box that can seal, and as plain values elsewhere (a laptop). The vault page shows where each one was last given held.
- **Rollout order:**
  1. Part 1 (turn users), all cloud boxes. Held values unchanged.
  2. Part 2 proxy, with GitHub (header + basic) and the `bearer` / `api-key-header` presets. Turn it on for one agent's GitHub token on one locked box, then widen.
  3. Token endpoints (§2.9): gog/Google, then Salesforce.
  4. The box's own Claude login (§5.1 below).
  5. Part 4, browser logins, per site.
  6. When an agent has no held credentials left, offer "seal everything by default": new values for it are sealed only unless a member says otherwise.

### 5.1 The box's own Claude login

`CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_API_KEY` live in the runner's vault (`RunnerLogin`, `VALUE_KINDS`) and are the most valuable secret on the box: they work from anywhere. They become sealed with the `anthropic-*` presets. Claude Code then runs with a stand-in and its traffic to `api.anthropic.com` is intercepted. This is the step that most needs the builder to check Claude Code's behaviour: token-shape checks, its own refresh flow, and whether it pins certificates. If it cannot be done for the OAuth login, the API-key login is sealed first.

## 6. Audit and alerts

- **Uses, not reads.** At turn end the proxy reports, through the runner, per sealed entry: hosts used and request count. canopy-web records these as `AgentCredentialRead` rows with `via = "sealed"` (and the turn id). A sealed entry's history then shows where it was *used*, which is more useful than "the box read it".
- **Misdirected uses.** A stand-in seen in a header or query string on an intercepted host or place its binding does not allow (§2.6) is reported with the host, the place, and the turn. It raises an `AgentCredentialAlert` of new kind `misdirected` to the agent's owners (email and push, debounced as other kinds). This is the signal that a prompt injection tried to spend or leak a credential.
- **Held credentials.** The agent's health page (`/w/:ws/health`) counts held credentials per agent, and each held entry says why ("no binding", "client cannot use a stand-in", "laptop"). An `Alert` is raised only for an entry marked sealed only that some turn was refused for want of a sealing box.
- **Every binding change** is a `SettingChange` with before and after hosts and place (values never), through `audit.note_change`.

## 7. Credentials this design cannot seal, and what to do about each

| Credential | Why it cannot be sealed | Recommendation |
|---|---|---|
| 1Password service-account token (`OP_SERVICE_ACCOUNT_TOKEN`) | `op` uses it to decrypt vault items on the client | Move the agent's values into canopy's vault (the direction already taken), then drop `op` from its turns. |
| Google service-account JSON key (`GOOGLE_APPLICATION_CREDENTIALS`, ACE's Drive) | Signs a JWT locally with the private key | Replace it with an OAuth refresh token (sealable through §2.9), or have the runner mint a short-lived access token per turn and seal that with `minted_hosts`. |
| gog keyring password | Decrypts a local file | With §2.9 the keyring the turn sees holds a **stand-in** refresh token. The runner writes it into the turn user's home under a throwaway password, so the password protects nothing of value. |
| Playwright form logins | Typed into a page | Part 4. |
| `ECHO_GMAIL_PASSWORD` and other IMAP/SMTP passwords | Not HTTPS | Move to the Gmail API (OAuth, sealable). |

## 8. What changes, by component

- **Runner (`runner/ec2/cloud_runner.py`):** the turn-user pool and launcher (Part 1); the CA, interception, credential table, substitution, response scan, token-endpoint sealing and usage reporting in the egress proxy (Part 2); stand-in creation and table load per turn; CA trust variables in the turn environment; `sealing` in the heartbeat.
- **Root launcher:** a small new root service beside the KMS broker, with the same `SO_PEERCRED` pattern and its own tests.
- **canopy CLI (`canopy cred env --runner`, value writes):** payload v2 with `bind`; a version-gated JSON output.
- **canopy-web:**
  - `VaultEntryAccount`: `sealing`, `sealed_only`, `bind_hosts`, `bind_place` (display copy).
  - Value write routes accept the display copy, record a `SettingChange` and require step-up when hosts or place change.
  - Runner requirement `sealing`; readiness and routing honour it.
  - `AgentCredentialRead.via = "sealed"`; `AgentCredentialAlert` kind `misdirected`; usage-report route for the runner (`POST /api/harness/runners/{id}/turns/{turn_id}/sealed-uses`).
  - Egress: binding hosts join the effective allowlist (`box-egress` response), marked as such.
  - Vault page: sealed/held badge, preset picker, `sealed only` switch, last-used hosts, mismatch warning. Health page: held count.
  - Docs: `docs/architecture/vault.md` (sealed credentials), `runners.md` (turn users, the sealing proxy), `access.md` (replace the Known limit).
- **Agents' repos:** nothing required. Optional `# canopy:bind <preset>` hints in `.env.tpl`.

## 9. Tests that must exist

- A turn user cannot: connect to the KMS broker; read `~/.<slug>/.env`, the runner's home, `/etc/canopy-runner/*.gog-secret`, or another turn user's home; read `/proc/<runner pid>/environ` or memory.
- A stand-in is substituted on its bound host and place, and **not**: on another host; in another header; in a body outside a `form` binding; for a connection from another turn user; after its turn's table is dropped.
- A misdirected stand-in reaches its destination unchanged and raises one alert.
- A response echoing the real value reaches the turn with the stand-in.
- A token-endpoint response's `access_token` reaches the turn as a stand-in, and that stand-in works on `minted_hosts` only.
- An unbound host is tunnelled without interception (the client sees the real certificate).
- A request whose `Host` or SNI differs from the CONNECT host gets no substitution; `TRACE`, ambiguous framing and path tricks (`//token`, `%2Ftoken`) do not place a value anywhere a binding did not name.
- A `basic` echo in a response (base64 of the real pair) reaches the turn re-encoded with the stand-in.
- One turn user cannot read another turn user's home.
- Restarting the proxy mid-turn: the runner reloads the table, the CA is unchanged, and the turn's next request succeeds.
- A display copy that disagrees with the sealed binding is ignored by the box and reported.
- A `sealed_only` entry is never given held: a turn needing it waits when no sealing box is online.
- An old runner given a v2 payload refuses the entry.
- End to end on a real locked box: `gh pr list`, `git push`, a labs MCP call and a gog Gmail read all succeed with stand-ins, and `env` and the turn env file show no real value.

## Left to the builder

- The proxy's HTTP implementation (asyncio + `h11`, mitmproxy core, or other), within §2.5's constraints.
- The exact launcher protocol and how stdio pipes are passed.
- The size of each turn-user pool and how resumed sessions' `~/.claude` state moves between turn users.
- Which extra CA-trust variables each client needs.
- Stand-in prefixes per provider, after checking what each client accepts.
- Whether HTTP/2 is needed for any bound host.
- Storage of the display-copy columns (on `VaultEntryAccount` or a side table).

## Open question for Jonathan

**Should sealing also block endpoints that turn a sealed credential into a new one?** Some bound hosts let a credential create another that comes back in plain text: a GitHub token registering a deploy or SSH key, or creating another token; an Anthropic console login creating an API key; a Google login calling `iamcredentials` `generateAccessToken`. The response scan (§2.8) replaces only values already in the turn's table, so none of these is caught today. The recommendation is to **block them**: each preset carries a deny-list of known credential-creating method+path pairs, and the proxy refuses to substitute on them, so "the agent never holds a credential" stays true. An agent that legitimately needs a new key asks a person. The alternatives are to substitute but alert the owners, or to treat it as out of scope (limited only by the token's own scopes). Until this is answered, the builder plans for the deny-list.
