# Give an agent a secret: a zero-knowledge request card

**Status:** design, 2026-10-19. The owner (Jonathan) asked for this on 2026-10-19 and asked Hal to take it from design through to shipped PRs. The recipient-trust decision (§4) is Hal's call, recorded here.

## Intent

Standing up Bailey (the Marshellis family agent) needed a GitHub token, and there was no good way in the IDE to hand it over. The chat's **Share secret** stores the value with Fernet on the server (`SessionSecret.value_enc`), so canopy-web can read it, and the session reads it back through `canopy secret exec`. That masks the value. It is not a boundary. The agent's **Credentials** page is zero-knowledge, but only an agent admin can write there, and at kv > 0 the writer has to unlock the vault key (VK). Anyone who can write can also read everything.

What the owner wants: from the IDE, an easy way to put a secret into an agent's vault, encrypted so that **no session and no server ever sees the plaintext**. The same primitive should work from the setup checklist ("Add the missing secrets"), from the new-agent flow and from the CLI.

## Goals

- An agent's session asks for a declared secret by name, and a card appears inline in the chat.
- The card has one password field. The **browser** seals the value to `zks1` (spec §6 of `docs/vault/zk-v1/zk_v1_reference.py`) before anything leaves the page. Only ciphertext travels.
- The writer never needs the vault key, so filling a secret never grants the ability to read one.
- The card verifies **who** it is sealing to (§4). The server's word about the recipients is not enough.
- The session learns only "`GITHUB_TOKEN` stored for bailey". It never learns the value.
- One component, `SecretSealCard`, used by the chat, the agent's setup checklist, its Credentials page and (through the checklist) the new-agent flow. The CLI has `canopy vault zk set --sealed`.

## Non-goals

- **No reading.** No route added here returns a value, and the card has no reveal.
- **No replacing for non-admins.** A person who is not an agent admin may fill only a declared secret that has **no** value yet, and only in answer to the agent's own open request. Swapping a stored token for one the attacker controls is an integrity attack even when nothing is read, so replacing stays admin-only.
- **No agent self-service.** An agent's own login can ask. It can never answer, not even its own request: otherwise a prompt-injected agent could "fulfil" a request with a token the attacker supplied.
- **Not a defence against a malicious JavaScript bundle.** canopy-web serves the page that does the sealing, so a server that ships hostile JS can read whatever is typed into it. This is the ceiling for every web-delivered zero-knowledge vault (1Password and Bitwarden's web vaults included). §4 defends against the threats below that line: a database or API-layer compromise, a forged or injected key row, and a bug that returns the wrong keys. The CLI path (`--sealed`) runs code the server did not ship, so it closes this gap for people who want that.

## What already exists (and is reused unchanged)

| Piece | Where |
|---|---|
| `zks1` sealed format: one ECDH-ES entry per recipient, no VK needed | `zk_v1_reference.py` §6; `frontend/src/vault/zk.ts::sealValue`; `apps/agents/zk_seal.py` |
| `PUT /vault/zk/values` accepts `zks1` at **any** kv | `apps/agents/zk.py::set_values` |
| Runners open `zks1` with their device ECDH key | canopy `cred_resolve.open_sealed_for_device` |
| A VK holder settles `zks1` → `zk1` | browser `settleSealed` (on every grant), `canopy vault zk settle` |
| Every new agent is born zero-knowledge at kv 0 | `services.born_zk_fields` |
| Recipients = the agent's approved device keys on runners that serve it, plus its admins' vault keys | `zk_seal.recipients_for`; `device_keys.runner_serves_agent` |

**Runner isolation (#1698) already gives per-tree recipients.** A cloud runner is one isolation unit, and `runner_serves_agent` asks `routing.runner_routes_agent`, which includes `unit_refusal`. So a box in another tree, or another agent's unit, is never among an agent's recipients. Sealing targets "only the agent's tree's boxes" with no change here. A new unit's KMS-held ECDH key becomes a recipient once it is approved and routed to the agent, through the same wrap flow that §4 attaches endorsements to.

## How it works

### 1. The agent asks: `request_agent_secret`

`POST /api/agents/{slug}/secret-requests` `{name, reason}`. Like every route, it is also an MCP tool (`request_agent_secret`). Who may call it:

- **the agent's own login** (`agent-login`). This is the normal case: a session realises it needs `GITHUB_TOKEN`;
- **an agent admin.**

`name` must be declared in the agent's `runtime.yaml` (`services.declared_credential_names`). An undeclared name gets 422 `not_declared`, and its message lists the declared names. A runner never resolves an undeclared name, so storing one would be a value nobody can use.

The request is an `AgentSecretRequest` row: the agent, the name, the reason, the session it was asked from (from the `X-Canopy-Turn-Id` the runner already sends, so turn → session), who asked, and a status of `open`, `fulfilled` or `cancelled`. A request expires after 24 hours. A second open request for the same (agent, name, session) returns the existing one. The response is `{id, agent, name, status, has_value, card: true}`. **No value field exists on the object.**

### 2. The card renders: server-decided, first-party

When the session's `tool_result` for that call arrives, canopy annotates the row with `secret_request: {id, agent, name}`. That happens on the live socket frame (`consumers._app_for_result`'s sibling) and on a reload (`serializers`). The server decides; a client never guesses from tool names, the same rule MCP Apps rows follow.

`canopy-ui`'s `MessageList` renders a host-provided component for such a row, through a new `SecretRequestHostContext` that mirrors `AppHostContext`. The canopy-web SPA provides `SecretSealCard`. An older host (ace-web, the embed) provides nothing, and the row renders as an ordinary tool row.

**Why not an MCP Apps View?** A View runs in an opaque-origin `sandbox="allow-scripts"` frame, labelled as not being canopy's own UI. It cannot run WebAuthn, so no step-up and no vault-key unlock, and each of its calls is relayed by the host as a tool call. Sealing inside a View would be possible, but the frame is built for third-party UI. This card has to unlock the person's own key (§4) and is canopy's own UI, so it is first-party React that uses `frontend/src/vault`.

### 3. The card seals and stores

1. `GET /api/agents/{slug}/secret-requests/{id}` gives the request's status and whether the name already has a value. Gate: the request's session ACL (read). Shown: "bailey asks for **GITHUB_TOKEN** — *to push to its repo*".
2. `GET /api/agents/{slug}/vault/zk/recipients?request={id}` gives `{agent_id, kv, recipients: [{kind, target, label, public_jwk, key_jkt}], endorsements: [...]}`. Gate: agent-admin, **or** the caller may answer that open request (below). Public keys are public data. Opening this route below agent-admin exposes nothing secret.
3. The card verifies the recipients (§4), seals with `sealValue(value, agent_id, name, kv, verified)`, clears the field, and sends
   `PUT /api/agents/{slug}/secret-requests/{id}/value` `{kv, value_zk}`.
4. The server checks:
   - the value is `zks1` and nothing else (422 `sealed_only`);
   - the body's `kv` is the agent's current kv (409 `zk_kv_mismatch`);
   - the request is open (409 `request_closed`);
   - the caller may answer it (below);
   - a non-admin is filling a missing value, not replacing one (409 `value_exists`).

   It then stores the value through `zk.set_values`, marks the request fulfilled, and records `fulfilled_by`. A browser caller needs a passkey step-up, as every secret-changing browser write does. A token caller (the CLI) is allowed, as on `/vault/zk/values`.
5. The tool result the session sees never changes. The session learns the outcome by asking `GET …/secret-requests/{id}`, which returns `status: fulfilled, has_value: true`, or from the next user message the card posts: "GITHUB_TOKEN is stored for bailey." The value is never part of either.

**Who may answer a request** (the "secret writer" gate, new label `secret-request` in `route_gates`):

- an **agent admin**, who may fill or replace; or
- a person who can **write to the session the request was asked from** (`canopy_sessions/access.can_write`) and is a current member of the agent's workspace, who may **fill a missing value only**;
- never any agent's own login (`Agent.objects.filter(user=caller)`).

A secret writer may seal and nothing else. They never get VK and never get a reveal. The card does not offer to replace an existing value unless the caller is an admin.

### 4. Recipient trust: endorsements, pinned to people you trust

**The problem.** The server names the recipients. A compromised database or API layer could add its own public key to the list, as a fake "device" or a substituted "member" key, and read everything sealed afterwards. The CLI asks before wrapping, but the browser seals to whatever it is told. Without a fix this is "zero-knowledge unless the server lies".

**The decision.** Every recipient must carry an **endorsement**: a signature by a person's vault key, made in that person's browser when they grant the recipient access. The card seals only to recipients endorsed by a key **the writer already trusts**. The server stores endorsements and cannot forge them.

**Format** (normative, `zk_v1_reference.py` §7, with vectors):

```
msg  = "canopy-vault/v1/endorse|{agent_id}|{kind}|{target}|{key_jkt}"
         kind/target as in §6 (device: target = the ECDH key's jkt; member: target = user_id)
         key_jkt = RFC 7638 thumbprint of the recipient's public JWK
sig  = ECDSA-P256-SHA256(utf8(msg)) by the endorser's person key (the same P-256
       scalar as their §4 keyset), IEEE P1363 r||s, 64 bytes, b64u
endorsement = {"kind", "target", "key_jkt", "by": endorser user_id, "by_jkt": jkt(endorser public key), "sig"}
```

The person key is an ECDH key. Signing with the same scalar is safe here because every signed message carries a fixed `canopy-vault/v1/endorse|` prefix that no ECDH derivation ever processes. WebCrypto imports the scalar as ECDSA for this one use. The key never leaves the browser.

**When endorsements are made.** Endorsements are made at the moments a person already grants access, in their own browser, after unlocking their key:

- **turn on / create the vault key** (`prepareTurnOnZk`, including `giveAccess` on approving a machine): endorse every target it wraps VK to, and self;
- **grant** (`grantPending`): endorse every target it wraps;
- **rotate**: endorse every target it re-wraps;
- **confirm**: an explicit "Confirm who can read this agent" step, which lists unendorsed current holders with name and fingerprint. It backfills agents whose wraps predate this design, and it is the step the card offers an admin who finds an unendorsed device.

An endorsement is the **publicly verifiable twin of a wrap**. It says exactly what a VK wrap already says ("I let this key read this agent"), but anyone can check it without VK. The server checks the signature on write (`POST /vault/zk/endorsements`, agent-admin + step-up, signer must be the caller's own keyset) so that junk cannot be stored. The check that matters is the client's.

**Who the writer trusts** (the card's anchors, strongest first):

1. **Their own key.** A writer with a vault keyset unlocks it in the card, using the same passkey touch as the step-up. Their own public key, derived from the unlocked scalar, is an anchor no server can substitute. For Jonathan giving his own agent a secret, the trust root is his own passkey. No TOFU is involved.
2. **Pinned people.** Endorser keys the writer has accepted before, stored in the browser as `canopy-vault/v1/pins` = `{user_id: {jkt, email, pinned_at}}` and keyed by person, not by agent (like `known_hosts`).
3. **First contact (TOFU).** No anchor endorses the recipients. The card then shows each endorser's email and key fingerprint (the jkt in groups of four) and asks the writer to confirm before pinning. If a pinned person's key **changes**, the card stops with "jonathan@'s vault key changed since you last used it — confirm with them before continuing". It does not silently re-pin.

**The sealing rule.** Seal only to recipients with a valid endorsement from an anchor, whose `key_jkt` matches the public key actually used. Recipients without one are listed as "not confirmed by an admin yet — will get it when an admin next opens the vault" (the next settle re-encrypts for them). If **no** endorsed device remains, the card says so and, for an admin, offers Confirm. It never seals to an unverified key to "make it work".

**What this buys, honestly.** A database or API-layer attacker can no longer add a recipient. A device key needs a real admin's signature, and the endorser is pinned. What the attacker can still do is deny service: drop recipients, drop endorsements, or refuse to store. That direction fails safe. Two residual risks are accepted:

- an attacker who has stolen a previously endorsed device's private key **and** controls the server could keep presenting that device after it is revoked. Rotation plus deleting the endorsement covers the normal case. A hostile server is the case §Non-goals already concedes.
- a hostile JS bundle (the web ceiling above). The CLI path does not have it.

### 5. Reuse

- **Setup checklist.** "Add the missing secrets" lists each missing declared name with **Add**, which opens `SecretSealCard` in admin mode (no request; it writes through `PUT /vault/zk/values` with a `zks1` value, which that route already accepts at any kv).
- **New-agent flow.** It lands on the setup checklist, so it gets the card through the checklist.
- **Credentials page.** Unchanged in this release. Admins keep the VK path for `zk1` writes at kv > 0, and "Save" at kv 0 still seals to `recipientsOf(state)`. Moving it onto the verified path waits until existing agents carry endorsements, which their admins' next grant or Confirm adds. Until then the switch would block Save on every older agent. This is a follow-up.
- **Share secret.** See §6.
- **CLI.** `canopy vault zk set --sealed --agent X NAME` reads `/vault/zk/recipients`, verifies endorsements against `~/.canopy/vault-pins.json` (same TOFU rule, shown with fingerprints and confirmed at the prompt, like `set` already confirms wrap targets), seals `zks1` and writes it without the vault key. `--request ID` answers a request instead.

### 6. Share secret, re-pointed

The chat's **Share secret** stays the session-scoped hand-off for values that are not an agent's declared secret (a token to paste into a repo's Actions secrets, say). Those values remain custodial (Fernet), and the dialog now says so plainly. When the chat is with an agent and the typed name is one of its declared secrets, the dialog switches to `SecretSealCard`: the value goes to the agent's vault, zero-knowledge, and never to the session. Sealing session hand-offs to the claiming runner's device key, so that they too stop being server-readable, is a follow-up. It needs the runner to open a per-session `zks1` in `canopy secret exec`, a separate change in canopy.

## Routes (and their gates)

| Route | operationId | Gate |
|---|---|---|
| `POST /api/agents/{slug}/secret-requests` | `request_agent_secret` | `agent-login`, `agent-admin` |
| `GET /api/agents/{slug}/secret-requests/{id}` | `get_agent_secret_request` | `agent-login`, `session-acl`, `agent-admin` |
| `PUT /api/agents/{slug}/secret-requests/{id}/value` | `fulfil_agent_secret_request` | `secret-request` (admin, or session writer filling a missing value); browser + step-up |
| `GET /api/agents/{slug}/vault/zk/recipients` | `get_agent_zk_recipients` | `agent-admin`, `secret-request` |
| `POST /api/agents/{slug}/vault/zk/endorsements` | `add_agent_zk_endorsements` | `agent-admin`; browser + step-up |

None returns a value. `fulfil` and `add_agent_zk_endorsements` are SETTINGS, like `set_agent_zk_values`, so every call writes a `SettingChange`. The stored row also records `updated_by`, and the request records `fulfilled_by`. `request_agent_secret` is NOT_SETTINGS (`request`). No gate includes plain `member`, so none needs a `VIEWER_MAY_MUTATE` entry.

## Testing

- Reference + vectors: §7 endorsement message and a fixed-key signature that both Python and the browser verify. A tampered `key_jkt`, `target`, `agent_id` or `by` must fail.
- Server: request creation is declared-only and agent-login/admin-only; `fulfil` accepts `zks1` only, the current kv only and open requests only; a non-admin cannot replace; agent logins are refused; browser step-up is enforced; endorsement writes verify the signature and the signer.
- Browser: the card seals only to anchored recipients; it refuses on a changed pinned key; the field is cleared before the network call resolves.
- Live (canopy.dimagi.com, Playwright as hal@): an IDE chat with a test agent calls `request_agent_secret`, the card appears, a value is sealed and stored, the server row is `zks1:` ciphertext, and a runner serving the agent resolves the plaintext.
