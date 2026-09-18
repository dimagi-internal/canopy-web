# Who is asking: initiator identity and access, end to end

**Status:** proposed — decisions D1–D5 open (see the end).
**Spans:** canopy-web (identity, tool authorization), the canopy agent framework
(`agent-core`, the agent factory, `gating_guard`), and each embedding host's MCP
server (connect-labs first).

## The problem, in one example

Alice uses connect-labs. She has limited access there, and she has a canopy
account. She opens the canopy widget and asks the agent *"which opportunities
are behind on payments?"*

Today three things are true, and none of them is what we want:

1. **She arrives as a contact**, not as her canopy account, because the widget's
   only token path mints contacts (`/api/auth/contact-token`). canopy's
   permissions for Alice are never consulted.
2. **The agent does not know it is Alice.** `Turn.enqueued_by` is the only "who"
   field; it is null for email, schedule and dispatched turns, and an email
   sender lives untyped in `origin_ref["from"]`. There is no single place that
   says who started a turn and how sure we are.
3. **The agent's tool calls run as the agent's runner**, not as Alice. canopy's
   MCP server is reached with a static token on the runner box
   (`~/.claude/canopy/workbench-token` / `CANOPY_WEB_PAT`), and a host's MCP
   server with whatever credential the runner holds for it. The ids on Alice's
   page narrow what the agent looks at; they do not limit what it *could* look
   at. She can ask it for data she cannot see herself.

Point 3 is the one that breaks the rule this whole system was built on —
everything resolves to the person's own login and permissions. It is safe today
only because the one person using it is the owner.

## What we want

1. **One arrival path, resolved by canopy.** A visitor arrives; if they have a
   canopy account, they are that account; if not, they are a contact. The host
   does not choose.
2. **Every turn knows who asked, and how sure we are** — the widget visitor, the
   emailer, the person on the phone, the schedule's owner — in one shape, told to
   the agent at the top of the turn and re-readable during it.
3. **Every tool call runs as `agent ∩ initiator`.** The agent can do only what
   BOTH it and the person who asked may do. Not "as Alice" alone — an agent
   deliberately scoped to read-only must stay read-only even for an admin.
4. **Explicit, per-agent access rules by who is asking**, versioned in the agent's
   repo, enforced by canopy for tools and by the agent framework for local
   actions, and shown to the agent as plain instructions.

## 1. The principal: one shape for "who asked"

Every `Turn` gets an **initiator**, replacing the partial signals scattered today:

| field | meaning |
| --- | --- |
| `initiator_kind` | `user` · `contact` · `system` · `agent` |
| `initiator_user` | FK, when `user` |
| `initiator_contact` | FK, when `contact` |
| `initiator_assurance` | how we know — see below |
| `initiator_via` | the channel: `widget:<app>`, `chat`, `email`, `schedule:<id>`, `dispatch:<agent>`, `slack` |

**Assurance** reuses the ladder contacts already grade email on
(`apps/contacts/email_auth.py`), extended to every channel, so "how sure are we"
is one vocabulary:

| assurance | meaning | example |
| --- | --- | --- |
| `session` | signed in to canopy in this request | phone composer, `/w/:ws/chat` |
| `host_signed` | a registered host signed a statement about this visitor | the widget on connect-labs |
| `dmarc` / `dkim` / `spf` / `none` | as today for email | an inbound email |
| `internal` | canopy itself started it | a schedule firing, one agent dispatching to another |

**Multiplayer sessions:** the initiator is per TURN — whoever sent *that*
message — not the session's creator. Two people in one chat are two principals,
and the agent should act for whichever one it is answering. (D2.)

**System turns** carry the human behind them where there is one: a schedule's
initiator is `system` via `schedule:<id>`, with the schedule's creator recorded
as the accountable user, and that person's permissions bound the turn. (D3.)

`enqueued_by` and `origin_ref["from"]` become derived/compat reads of the new
fields rather than a second source of truth.

## 2. Arrival: canopy resolves user vs contact

The host keeps doing exactly what it does now — its server signs a short-lived
statement about the visitor (`sub` = the host's own id, `aud`, `exp` ≤ 120 s,
single-use `jti`). Two claims are added: `email` and `email_verified`.

canopy then resolves, in order:

1. **An existing link** `(app, host sub) → canopy user` — use it.
2. **Otherwise, a verified email the host may speak for** — if `email_verified`
   is true, the email's domain is one this site's owner has explicitly allowed it
   to resolve (a new, visible setting on Connected sites, bounded as before to a
   domain the owner is in and canopy admits at login), and a canopy user with that
   verified email exists → record the link and resolve to that user.
3. **Otherwise → contact**, exactly as today.

The token issued is the kind that matches: a `DelegatedToken` for a user (their
full canopy ACL applies on every request), a `ContactToken` for a contact (only
`/api/contact/`). The widget already accepts either unchanged.

**The honest trade in step 2.** A host allowed to resolve a domain can assert any
verified address in it, so a compromised host signing key could speak for canopy
users in that domain. That is the old email-domain vouching risk, made much
smaller: it is per-visitor and signed rather than one static secret; it is opt-in
per domain on a page the owner sees; every resolution is audited with
`assurance=host_signed`; and (§4) a `host_signed` user can be given less than a
`session` user in the agent's policy. The alternative (D1) is a one-time link: the
first time, the visitor confirms their canopy account in a canopy sign-in popup,
and only the stored link is trusted afterwards. Stronger, one extra click per
person, once.

## 3. The agent knows who asked

At the top of every turn the runner delivers a **turn context block**, built by
canopy from the initiator and the agent's policy (§4):

```
Asked by: Alice Moreno <alice@dimagi.com>
  canopy user · editor in workspace connect · via widget:connect-labs · host-signed
You are acting for this person. You may: read opportunities, read insights,
  scroll/highlight on their page. You may not: send email, change payments.
```

An email turn reads the same way (`contact · dmarc-verified · via email`), and so
does a schedule (`system · schedule "weekly digest" · owner Jonathan`). The same
data is re-readable mid-turn through an MCP tool, `who_is_asking()`, because a
long turn outlives the top of its context.

The prose is a convenience for the agent's judgement. It is **not** the
enforcement — §5 is.

## 4. Access policy: `config/access.yaml` in the agent's repo

`config/allowlist.txt` + the "sender triage" rule in `agent-core/turn.md` are the
seed: a flat list of counterparts the agent may *act* for, everyone else
read-only and surfaced. That grows into an explicit policy, keyed by who is
asking, in the agent's repo (the definition lives in the repo — CLAUDE.md, "an
agent row is an instance"):

```yaml
# config/access.yaml — who may ask this agent for what
default: read_only_and_surface        # anyone not matched below

principals:
  owner:                              # the agent row's owner
    tools: all
    actions: [send, reply, write, deploy]

  workspace:                          # canopy users in the agent's workspace
    roles:
      editor: { tools: [read:*, insights.dismiss, page.*], actions: [reply] }
      viewer: { tools: [read:*, page.*],                  actions: [] }

  contact:                            # people canopy knows but who are not members
    tools: [page.*]
    actions: [reply_in_thread]
    require_assurance: dkim           # an spf-only or unauthenticated contact gets default

  system:
    schedule: { as: creator }         # a schedule acts with its creator's grant
    dispatch: { as: dispatcher }

assurance:
  host_signed: { cap: editor }        # a widget-resolved user never exceeds editor here
```

- **Published to canopy** on the same path as the skill catalog, so the server
  enforces the version that is actually deployed.
- **Absent file = today's behaviour for the owner, `read_only_and_surface` for
  everyone else.** Safe by default; nothing existing breaks.
- `allowlist.txt` is read as the `contact` section's allow-list until an agent
  migrates, so agent repos move one at a time.
- The agent factory (`canopy_agent_factory`) scaffolds a commented `access.yaml`
  for new agents instead of the bare allowlist.

## 5. Enforcement: `agent ∩ initiator`, at the tool boundary

Instructions are not a control. Enforcement happens where the call happens.

### canopy's own tools

The agent keeps authenticating to canopy's MCP server as itself. What changes is
that canopy works out **which turn the call belongs to**, and from that who
asked:

- **Each emdash chat task gets its own MCP URL** — `/api/mcp/?session=<id>` —
  written into that task's MCP config when the runner creates it. A task is one
  session for its whole life, so the URL never needs to change mid-conversation.
- **One running turn per session is already a database constraint**
  (`one_executing_turn_per_session`). So `(agent credential, session)` names
  exactly one turn, exactly one initiator — no guessing, and no turn id the agent
  could substitute. Non-session agent turns use the equivalent
  `one_executing_turn_per_agent`.
  This is why a looser design fails: session turns do not participate in the
  per-agent constraint, so one agent can run Alice's turn and Bob's turn at once,
  and "the turn this agent is running" is ambiguous.
- **Every tool resolves its data scope as the intersection** — e.g.
  `list_insights` sees workspaces that are the agent's AND Alice's; a contact
  initiator sees only what the contact surface allows.
- **The tool LIST is filtered** by `access.yaml` for this initiator, so a
  disallowed tool is not offered at all rather than offered and refused.
- **No running turn → no data.** A call with no resolvable turn fails closed, the
  same direction `page_visible_q` and the workspace authorizer take.

### Third-party tools (connect-labs)

connect-labs' MCP server needs to know it is Alice too, and must be able to
believe it. Recommended (D4): **canopy as the MCP gateway for embedded products.**
The agent reaches connect-labs' tools through canopy's per-session endpoint;
canopy forwards each call with an **on-behalf-of assertion** it signs — `sub` =
Alice's connect-labs id (known from her arrival, §2), `act` = the agent,
`aud` = connect-labs, `exp` ≤ 120 s. connect-labs verifies it against canopy's
public key and runs the tool as Alice. It is the mirror of what connect-labs
already does toward canopy.

The alternative is introspection — connect-labs receives the agent's call plus a
turn reference and asks canopy who is behind it. Less infrastructure, but every
host must implement the call-back, and a turn reference the agent passes has the
concurrency problem above unless it is itself a signed per-turn token.

### Local actions (email, shell, deploys)

`agent-core`'s `gating_guard` hook already hard-blocks wrong paths at the tool
boundary from `config/gating.json`. It gains one input: the current turn's
initiator (written by the runner beside the turn), and it applies
`access.yaml`'s `actions` for that initiator — so "a viewer asked, and the agent
tried `canopy email send`" is blocked by the same rail that blocks raw
`gog gmail send` today.

## 6. What each piece of the system has to gain

| where | change |
| --- | --- |
| canopy-web `harness` | initiator fields on `Turn`; set on every enqueue path (widget, chat, email, schedule, dispatch, Slack) |
| canopy-web `tokens` | arrival resolution (link → verified email → contact); `email`/`email_verified` claims; the per-domain "resolve visitors to canopy accounts" setting; the link table |
| canopy-web `mcp` | per-session endpoint; turn → initiator resolution; intersection in every tool; tool-list filtering; `who_is_asking()`; the gateway + OBO signing (D4) |
| canopy-web `agents` | publish + store `access.yaml` alongside the skill catalog |
| runner | per-task MCP URL on task creation; turn context block; initiator written beside the turn for the hook |
| canopy plugin `agent-core` | `turn.md`: read the context block, act for that person, "sender triage" becomes "apply access.yaml"; `gating_guard`: enforce `actions` by initiator |
| canopy plugin agent factory | scaffold `access.yaml` |
| each host (connect-labs) | add `email`/`email_verified` to its assertion; verify canopy's OBO assertions in its MCP server |

## 7. Phases

Each ships alone and is useful alone.

1. **Initiator on every turn + the context block.** No behaviour change; the agent
   simply knows who asked, on every channel including email.
2. **Arrival resolution** — widget visitors with canopy accounts arrive as
   themselves.
3. **canopy's tools run as `agent ∩ initiator`** — per-session MCP endpoint.
   *This is the phase that removes the "Alice can ask for more than she can
   see" gap for canopy data.*
4. **`access.yaml`** — publish, tool-list filtering, `gating_guard` enforcement,
   factory scaffold, allowlist compatibility.
5. **On-behalf-of for host tools** — the gateway and connect-labs' verifier.

Until phase 5, the rule in the host guide (§8a) stands: give an embedded agent
only the host-tool access that is fine for everyone who can reach it.

## Decisions

- **D1 — How a visitor becomes their canopy account.** (a) automatically, by a
  host-asserted verified email, opt-in per domain; or (b) a one-time link through
  a canopy sign-in popup. *Recommended: (a) for Dimagi-staff hosts, with
  `host_signed` capped by policy; (b) available for anything more sensitive.*
- **D2 — Multiplayer.** Initiator per turn (whoever sent that message) or per
  session (whoever opened it). *Recommended: per turn.*
- **D3 — Scheduled and dispatched turns.** Act with the creator's/dispatcher's
  grant, or with the agent's own. *Recommended: the creator's — someone set it
  up, and that person should bound it.*
- **D4 — Host tools.** canopy as gateway with signed on-behalf-of assertions, or
  host-side introspection. *Recommended: gateway.*
- **D5 — Where policy lives.** `config/access.yaml` in the agent repo, published
  to canopy, or edited in canopy's UI. *Recommended: the repo, with a read-only
  view in the agent's canopy page — the same split skills already have.*
