# Who is asking: initiator identity and access, end to end

**Status:** proposed, revised 2026-09-18 around **owner + admins** and a **declared interface** for everyone else. D1 and D7 decided; D2–D6 open. Phase 1a: #846.
**Channels in scope:** the widget, canopy chat (web + phone), email, **Slack** (`apps/slack`, being brought back up in parallel — see §1a), schedules and dispatch.
**Spans:** canopy-web (identity, admins, the interface, tool authorization),
the runner, the canopy agent framework (`agent-core`, the agent factory,
`gating_guard`), and each embedding host's MCP server (connect-labs first).

**Terms.** *Owner*: the one human accountable for an agent (`Agent.owner`).
*Admins*: people the owner grants full access to that agent. *Callers*:
everyone else who asks it for something.

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
4. **Two relationships, not one agent restricted per caller.** An agent's owner
   and admins reach its full working session; everyone else reaches only the
   capabilities it declares, over MCP, with who they are and what they are
   looking at carried as structured context. Every run is still a turn on a
   canopy runner under the routing rules.

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
| `slack_linked` | a Slack user linked by signing in to canopy (emails matched), re-checked for membership per message | a mention of the bot in Slack |
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

## 1a. Slack: already the strongest identity, and per-message by nature

`apps/slack` (#838, being brought back up now) already resolves a sender the way
this spec wants every channel to:

- **A Slack user is a canopy user only through `SlackUserLink`** — made by that
  person signing in to canopy, and only when the two email addresses agree — and
  must ALSO be a member of the installation's workspace at send time. Being in the
  Slack workspace grants nothing.
- **An unlinked or non-member sender is refused** with words and a link, and never
  becomes a contact. That differs from the widget on purpose: a Slack workspace is
  a population canopy did not vouch for, and while canopy is not public there is
  nothing a stranger there should be able to reach.
- **Every message is authorized on its own** (`services.authorize`), and a thread
  is one `Session` that several people can post into. So in Slack the initiator is
  unavoidably per MESSAGE — which is D2's recommendation, arrived at by a channel
  that already works that way.

What this spec adds for Slack is small, deliberately, so it does not collide with
the Slack work in flight:

- the initiator is set from the link: `kind=user`, `assurance=slack_linked`,
  `via=slack:<team>` (`enqueued_by` already carries the linked user — the same
  value, now in the common shape);
- **the channel context Slack feeds a turn is DATA, not the initiator.** The bot
  reads the channel's recent history and the mentioning thread into the prompt;
  those messages were written by other people, and none of them is who the agent
  acts for. The caller context (§5) names the sender of the triggering message
  only;
- resolving a call to its turn through the session (§7) covers Slack for free,
  since a Slack thread IS a session.

## 2. Arrival: canopy resolves user vs contact

The host keeps doing exactly what it does now — its server signs a short-lived
statement about the visitor (`sub` = the host's own id, `aud`, `exp` ≤ 120 s,
single-use `jti`). Two claims are added: `email` and `email_verified`.

**Arrival never creates a canopy account.** A visitor becomes a canopy user only
if one already exists; otherwise they are a contact. canopy resolves, in order:

1. **An existing link** — the visitor's contact `(app, host sub)` already has a
   `user` (`Contact.user`, set by `contacts.services.promote_to_user`) → resolve
   to that user. The link is the contact row itself; there is no second table.
2. **Otherwise, an existing account the host may speak for** — if
   `email_verified` is true, the email's domain is one this site's owner has
   explicitly allowed it to resolve (a new, visible setting on Connected sites,
   bounded as before to a domain the owner is in and canopy admits at login), and
   a canopy user with that verified email ALREADY exists → link the contact to it
   and resolve to that user.
3. **Otherwise → contact**, exactly as today. No user is created, whatever the
   email says.

### Deferred: a contact becoming a user

canopy is not public yet, so **the widget offers no sign-up and no "sign in to
link" step** for now. A visitor with no matching account is a contact, and stays
one. When canopy opens up, the upgrade is: the person creates an account through
canopy's normal sign-up, signs in once from the widget, and is linked with the
existing `contacts.services.promote_to_user` (which grants no membership), their
contact history following them. Slack's `SlackUserLink` sign-in flow (§1a) is the
working precedent to copy.

### A path that breaks this rule today

`POST /api/auth/token-exchange` — the older server-to-server exchange ace-web
uses — **does** create users on the fly (`User.objects.create_user` plus an
auto-verified `EmailAddress`), and can provision a workspace membership. That
contradicts the rule above. It stays working for ace-web until ace-web moves onto
this arrival path, and is then removed; until then no new site may be given it
(Connected sites already refuses to grant the email-domain vouching it depends
on).

The token issued is the kind that matches: a `DelegatedToken` for a user (their
full canopy ACL applies on every request), a `ContactToken` for a contact (only
`/api/contact/`). The widget already accepts either unchanged.

**The honest trade in step 2.** A host allowed to resolve a domain can assert any
verified address in it, so a compromised host signing key could speak for
EXISTING canopy users in that domain (never create new ones). That is the old email-domain vouching risk, made much
smaller: it is per-visitor and signed rather than one static secret; it is opt-in
per domain on a page the owner sees; every resolution is audited with
`assurance=host_signed`; and an agent's interface (§4) can offer a `host_signed`
user less than a signed-in one. A site owner who does not want
host-asserted matching simply leaves step 2 off, and every visitor is a contact.

## 3. Two relationships to an agent: its owner and admins, and everyone else

Restricting a fully-powered agent per caller — instructions, a policy file, a
hook — is a denylist on something powerful, and denylists leak. So the model is
split by relationship instead:

| | **Owner and admins** | **Callers** — everyone else |
| --- | --- | --- |
| who | `Agent.owner` (exists today) and an explicit per-agent **admins** list | workspace members who are not admins, contacts, Slack users, other agents |
| how they reach it | its **working session**: full chat, emdash jump-in, steering | its **declared interface** (§4), over MCP |
| what they can make it do | anything the agent can | only the capabilities the agent offers to their kind of caller |
| what it runs as | the agent's full profile | the invoked capability's restricted profile (§6) |

**Owner** is the one accountable human, and is always an admin. **Admins** are
granted by the owner, per agent. They reshape the agent (edit, schedule, route,
publish) and get its full working session.

**Why an explicit list rather than a workspace role.** Today "who may change an
agent" is workspace `editor` or `owner` (`apps/agents/api.py::_agent_for_write`),
and that code says plainly that `editor` "is not a deliberate grant": self-join
hands it to anyone from an allowed domain who clicks join. Being in a workspace
and being trusted with an agent's full session are different things.

**Credentials follow the same line (D7, decided).** The agent's owner or any
admin may set up all of its credentials. Today that gate is `_agent_for_admin`,
which — confusingly — means "WORKSPACE owner"; it becomes "the agent's owner or
an admin", so the name finally means what it says.

## 4. The declared interface: what an agent offers callers

An agent declares what it offers, in its repo — `config/interface.yaml`, grown
out of `config/allowlist.txt` and published to canopy on the same path as the
skill catalog (the definition lives in the repo; canopy holds the deployed
version and enforces it):

```yaml
# config/interface.yaml — what callers can ask this agent for
capabilities:
  ask:                                   # free-form conversation: how chat stays chat
    description: Ask Echo about the portfolio.
    callers: [member, contact:app_signed]
    tools: [canopy.list_insights, canopy.current_page, page.*]
    actions: [reply_in_thread]

  summarise_opportunity:
    description: Summarise one opportunity's delivery and payment status.
    input: { opportunity_id: integer }
    callers: [member]
    tools: [connect_labs.get_opportunity, connect_labs.list_payments]
    actions: []

callers_default: none                    # anything not listed here: not offered
```

- **Allowlist by construction.** A caller can reach only a capability listed
  for them, and while it runs, only that capability's tools and actions.
- **`ask` is a capability like any other.** People in the widget, Slack or email
  will not pick from a menu; `ask` is the free-form door, with its own scope.
  Everything else is a specific door with a narrower one.
- **Served as MCP, per caller.** canopy exposes each agent's interface as MCP
  tools (`echo.ask`, `echo.summarise_opportunity`), computing the list for the
  caller in front of it — the same thing `page_tools` already does for a page's
  actions. The call carries the caller's token, so who is asking arrives with
  the request rather than being inferred afterwards.
- **Channels become thin clients.** The widget, Slack, email and canopy chat for
  a non-admin all invoke a capability (usually `ask`) with the caller context
  (§5). They stop being ways into the agent's working session.
- **Prior art, borrowed rather than adopted:** MCP for tools and per-request
  identity; the Agent2Agent protocol's "agent card" (a published list of an
  agent's skills and how to authenticate to it) is close to what
  `interface.yaml` describes.

## 5. The caller context: one structured envelope

Every invocation carries one envelope, as data — the lesson of the page-state
work, where prose pasted into a first message went stale and structured state
re-read through a tool did not:

| part | source |
| --- | --- |
| **who** — kind, user/contact, assurance, channel | the turn's initiator (phase 1a, shipped in #846) |
| **relationship** — owner, admin, or caller, and their workspace role | owner + admins (§3) |
| **what they are looking at** — `resource`, `backing_tool`, `visible_ids`, filters | page state (shipped) |
| **the conversation** — session, thread | the session the turn belongs to |
| **what was invoked** — the capability and its granted scope | the interface (§4) |

The agent reads it from a file the runner writes beside the turn, and re-reads it
mid-turn through `who_is_asking()`. It is **never prepended to the prompt**: a
chat turn's prompt is the person's own words and becomes the transcript, so it
would appear as something they typed. The envelope informs the agent's
judgement; enforcement is §6 and §7.

## 6. Execution: every run is still a turn on a canopy runner, under routing

There is no second execution path. **Every run — admin or caller — is a `Turn`,
claimed by a canopy runner under the routing rules** (the `RunnerAssignment`
cascade, source-aware rules, pinning, session stickiness), and those rules keep
getting more sophisticated independently of this work.

What changes is **how the runner executes a turn**, decided by the turn itself:

- **Admin turns** run in the agent's full profile — today's behaviour.
- **Caller turns** run in the invoked capability's profile: only its declared
  tools; canopy's MCP scoped to `agent ∩ caller` (§7); none of the admins'
  credentials; local actions (email, shell, deploys) only as the capability
  allows, enforced by `gating_guard` reading the envelope. Claude Code can
  restrict a session's tools, so this is configuration of an ordinary session,
  not a new executor.
- **A profile is fixed per session.** Each caller conversation (a widget chat, a
  Slack thread, an email thread) is already its own session, so an admin's
  working session and a caller's conversation never share one — and a session's
  profile cannot drift mid-conversation.
- **Routing gains inputs, not a new mechanism.** Caller class and capability are
  on the turn, so a rule like "contact invocations go to the cloud runner" is a
  natural extension of source-aware routing when it is wanted. Not needed on day
  one.

## 7. Tools the agent calls downstream: `agent ∩ caller`

The interface decides what a caller may ASK for. This decides what data the
agent reaches while answering — both are needed.

- **canopy's own tools.** canopy resolves the call to its turn through the
  session (one running turn per session is a database constraint, so the lookup
  is exact; the per-agent constraint does not cover session turns, which is why
  resolving by agent alone is ambiguous), and scopes every tool to the
  intersection of the agent's grants and the caller's.
- **Host tools (connect-labs).** canopy forwards each call with a signed
  on-behalf-of assertion — `sub` = the caller's id at the host (known from their
  arrival, §2), `act` = the agent, short expiry — which the host verifies against
  canopy's public key and runs as that person (D4).
- **Known limit.** On a laptop runner every session runs as the same OS user, so
  a compromised agent in one caller's session could read another session's
  files and borrow the identity of someone *concurrently* talking to the same
  agent — never anyone who is not. Closing that fully means isolating sessions on
  the box, a much larger change; accepted for now, and the cloud runner can
  isolate per session earlier.

## 8. What changes where

| where | change |
| --- | --- |
| canopy-web `harness` | initiator on every turn (**shipped, #846**); capability on caller turns; envelope in the claim response |
| canopy-web `agents` | `Agent.admins`; reshaping AND credentials gated on owner-or-admin (credentials today: workspace owner only); publish + store `interface.yaml` |
| canopy-web `tokens` | arrival resolution (§2), never creating a user |
| canopy-web `mcp` | each agent's interface served as per-caller MCP tools; `who_is_asking()`; tool scoping `agent ∩ caller`; host on-behalf-of forwarding |
| canopy-web `slack` | the one initiator line (shipped in #846); later, invoke `ask` rather than open a working session |
| runner | write the envelope beside the turn; run caller turns in the capability's profile |
| canopy plugin `agent-core` | read the envelope; `gating_guard` enforces the capability's actions; `turn.md` sender triage becomes "you were invoked through capability X for this caller" |
| canopy plugin agent factory | scaffold `interface.yaml` with an `ask` capability |
| each host (connect-labs) | `email`/`email_verified` in its assertion; verify canopy's on-behalf-of assertions in its MCP server |

## 9. Phases

1. **1a — who asked, on every turn.** Shipped (#846). **1b** — the runner and
   `agent-core` delivering the envelope to the agent.
2. **Arrival resolution** (§2) — existing canopy accounts arrive as themselves,
   everyone else is a contact; then ace-web onto this path and the
   user-creating `token-exchange` removed.
3. **Owner and admins** — `Agent.admins`, the owner's UI to grant it, and
   reshaping, credentials and the full working session gated on owner-or-admin. Useful alone: it is the
   explicit trust grant the workspace `editor` role was standing in for.
4. **The declared interface** — `interface.yaml`, published and served as
   per-caller MCP tools; caller turns carry their capability; channels invoke it.
5. **Restricted execution** — runners run caller turns in the capability's
   profile; canopy's tools scoped `agent ∩ caller`. *This phase closes the gap
   the spec opened with.*
6. **On-behalf-of for host tools** — canopy forwarding, connect-labs verifying.

Until phase 5, the host guide's rule stands: give an embedded agent only access
that is fine for everyone who can reach it.

## Decisions

- **D1 — decided 2026-09-18.** No dynamic user creation; existing accounts only;
  no sign-up or linking in the widget while canopy is private (§2).
- **D2 — Multiplayer.** Who asked is per turn (whoever sent that message).
  *Recommended, and effectively settled by Slack (§1a).*
- **D3 — Scheduled turns.** Bounded by the creator (recorded in #846); a schedule
  created by an admin runs in the admin profile, anyone else's in the capability
  it invokes. *Recommended.*
- **D4 — Host tools.** canopy as gateway with signed on-behalf-of assertions.
  *Recommended.*
- **D5 — Where the interface lives.** `config/interface.yaml` in the agent repo,
  published to canopy, with a read-only view on the agent's page. *Recommended.*
- **D6 — Seeding admins.** When `Agent.admins` ships, who is in it on day one?
  *Recommended: the agent's owner plus the workspace's `owner`-role members, so
  nobody who can change an agent today silently loses it; editors do not carry
  over, since theirs was never a deliberate grant.*
- **D7 — Credentials. DECIDED 2026-09-18:** the agent's **owner or any admin**
  may set up all of its credentials. Today that is workspace owners only
  (`_agent_for_admin`); it moves to the agent's own owner + admins, which is also
  why the admins list must be a deliberate grant rather than a workspace role —
  granting admin now hands over the agent's keys.
