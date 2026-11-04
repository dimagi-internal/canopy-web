# Access: confinement and acting on someone's behalf

How a confined turn is enforced (what a caller's turn can and cannot reach), how an agent acts on behalf of the person who asked, and the two act-as-you doors: canopy-web's own `ActAsGrant` in the agent's turn, and the passkey-confirmed host grants on a connected site. The words and the one rule are defined in [access.md](access.md).

**See also:** [access.md](access.md) (glossary, "Whose authority an agent acts with"), [access-direct-use-and-admission.md](access-direct-use-and-admission.md), [embedding-internals.md](embedding-internals.md) (host grants).

## How confinement is enforced

Who asked is recorded on every turn (`initiator_*`), and the caller envelope
([access-direct-use-and-admission.md](access-direct-use-and-admission.md) "The caller envelope") reaches the agent three ways: in the claim response, which the runner
writes to `~/.canopy/caller/<turn_id>.json`; through the `who_is_asking` MCP
tool; and beside every delivered prompt — the runner leaves
`~/.canopy/caller/pending/<task>.json` before the send and the canopy plugin's
`caller_context` UserPromptSubmit hook adds a summary next to the person's words
(cloud: `CANOPY_CALLER`).

* **Default deny.** An agent with no capability published and no `write` line is reached only by
  its owner and agent admins; every read_only, capability-line, contact or unidentified
  caller is refused (row 5). The interface is live state on canopy-web —
  never a file in the agent's repo; the owner or an agent admin edits it on the agent. It
  confines callers who reach the agent through canopy (web chat, Slack, email,
  embedded sites). canopy's MCP server offers NO per-agent tools: someone who
  wants to use an agent installs its plugin and runs it in their own Claude
  Code, as themselves (`/get-started`).
* **An agent's own login is the agent.** `Agent.user` grades as `system`;
  another agent's login grades like any person, or agent A could steer all of B.
* **Three layers, each failing closed.** (1) canopy stamps `Turn.capability` at
  enqueue (`harness.services._apply_capability`) or refuses the turn; (2) only a
  runner reporting `profiles` (and `envelope >= 2`) may claim such a turn, and it
  runs it in its own `cx-` session with the profile written to
  `~/.canopy/profiles/<task>.json` first; (3) the plugin-wide `profile_guard`
  PreToolUse hook refuses anything the profile does not list — Bash
  argument-for-argument, `{thread_id}`-pinned, paths realpath'd.
* **Native deny rules under the guard.** The runner also writes the capability as
  Claude Code's own DENY rules (`runner/canopy_runner/canopy_runner/native_permissions.py`
  → the `cx-` worktree's `.claude/settings.json`; `--settings` on cloud). Fleet
  sessions run with `--dangerously-skip-permissions`, so only deny rules take
  effect natively: whole tools, MCP servers and high-risk programs the capability
  never grants. Exact-argument scoping stays the hook's job.
* **Confined sessions have `Read`; a cloud runner has no `Grep` or `Glob`.** The cloud box
  installs the current `@anthropic-ai/claude-code` from npm (`runner/ec2/runner.cfn.yaml`,
  `bootstrap_agents.sh`), and that build answers "No such tool available" to both. A profile that
  lists them grants nothing there and sends the model reaching for a tool that does not exist, so
  canopy's default read profile (`interface.default_read_profile`) does not list them. File search
  in a confined session needs an explicit bash pattern in the profile. The default
  carries no network tool (no `curl`: the session's cwd is a worktree of the agent's own repo, and
  `Read` covers it). It does carry `ToolSearch`, which only loads a deferred MCP tool's schema (the
  guard still judges each real call), so `who_is_asking` can be reached among canopy-web's hundreds
  of tools. It may write to two scratch folders, `.<slug>-read/` and the legacy fleet name
  `.<slug>-ask/` (the fleet's `answer-caller` skills draft there); nothing else is writable. Laptop runners run whatever
  Claude Code the person installed, and the runner reports no per-tool inventory; the runner's
  native rules still scope `Grep`/`Glob` by `read_paths` when a published profile lists them
  (`native_permissions.py`).
* **A confined session never holds the runner owner's PAT.** It reaches canopy's
  MCP with a **caller token** (`apps/harness/caller_tokens.py`, issued by
  `harness.claim_payload.issue_credentials`, `cct_…`), minted per confined turn at claim
  and bound to its conversation; the canopy plugin's `headersHelper` sends it from
  the session's profile. It resolves to the conversation's CURRENT turn: tools run as
  that turn's caller (no canopy user at all for an outside contact), and only the
  tools the capability lists exist (`apps/mcp/turn_scope.py`, `agent ∩ caller`).
  Expired, long-finished, or a conversation whose current turn is not confined → it
  authenticates nothing; `who_is_asking` answers only about its own conversation.
* **A confined turn holds no credentials it does not need.** canopy refuses it
  the GitHub token delegated to the agent (`agents.credentials.delegations.github_token_for_turn` → 409).
  The cloud runner builds its env from an allowlist (`_confined_env` in
  `runner/ec2/cloud_runner.py`: Claude's login, locale/paths/TLS, which agent and
  turn) — no `CANOPY_TOKEN`, no agent `.env`, no vault key (it never calls
  `credentials/resolve`), no chat key; gog's keyring password only when the
  capability's own bash rules run the mail path (`canopy email …` /
  `bin/<slug>-email`).
* **A colleague's FULL turn reads as the colleague.** A full-profile turn not
  asked by the agent's owner, an agent admin or canopy (an editor line, a
  writer in an agent admin's session — `caller_tokens.is_caller_scoped`)
  gets a **scoped** caller token (`CallerToken.scoped`): every READ tool exists, plus
  driving the asker's open page (`all_tools` + `read_only`; a write is refused — the
  asker granted no act-as-you permission), and each runs as the conversation's current asker, so
  `turn_access.can_read_turn_content` and the session ACL decide what
  `harness_list_turns` / `read_turn_messages` / `read_turn_transcript` return.
  The cloud runner leaves it at `~/.canopy/scoped/turn/<turn id>.token` and sets
  `CANOPY_SCOPED_TURN` (`_scoped_token_env`); the laptop runner leaves
  `~/.canopy/scoped/task/<emdash task>.token` from `execute._confine`
  (`scoped_token.py`) and clears it for a later turn with none. The plugin's
  `canopy-web-mcp-headers.js` sends it and never falls back to a PAT once it has
  one; a write failure refuses the turn. A scoped token does not serve a
  confined turn's conversation, and a confined token does not serve a full turn's.
  **REST takes a scoped token too, but only where MCP would** (`tokens.middleware._authenticate_caller_token`): re-resolved on every request
  as above, it signs in as the asker on a route the MCP server serves as a tool
  (`api_tools.is_tool_route`) and is refused (403) on every other one. The routes
  MCP leaves out (minting a PAT, the person's own memory switches and grants, the
  runner protocol) stay out of reach of a session acting for someone. A confined
  token is refused on REST (401): its capability's tool list is an MCP filter, and
  REST has no equivalent to apply. This is what lets the canopy CLI in a scoped
  session read as the asker instead of failing for want of a PAT; like MCP, REST
  refuses an agent turn's token every write but driving the page (non-GET → 403);
  a repo chat's token keeps its writes.
* **Projects run as the person who asked.** Agents and projects are both first
  class, and both act FOR the person who asked; they differ only in WHO the
  session is. An agent's turn acts as the agent, bounded by the asker (the rule
  in [access.md](access.md)). A project turn or project chat (a repo, no agent) has no agent to act
  as, so it acts as its asker: a verified, active human initiator gets the same
  scoped caller token (`caller_tokens._project_asker`), so its session acts on
  canopy with exactly that person's access — nothing more, and never the box's (the
  cloud runner strips its own `CANOPY_TOKEN` from the turn). Unlike an agent's caller
  token it may write: a repo chat is the person's own tool.
  The token follows the conversation's current asker and voids when a later turn
  has none verified. A project turn with no person behind it (a schedule, a
  drill) gets no token and no canopy identity. GitHub is the one thing a project
  borrows from an agent: its repo's `RepoIdentity` agent, for project dispatches
  and project chats alike (`delegations.turn_agent`; `runner-credentials.md` → "The GitHub
  identity a turn gets"). The cloud runner deletes the token when the turn ends.
* **Known limit.** On a laptop every session is the same OS user and emdash
  spawns it, so the runner cannot strip its environment; on any box, credential
  FILES (agent `.env`s, gog's keyring, ace's Drive key) stay readable to an
  escaped session. The guard and the native deny layer are what keep a confined
  session off them.

Spec: `docs/superpowers/specs/2026-09-18-who-is-asking-initiator-identity-and-access-design.md`.

## Acting on behalf of the person who asked

An agent's turn does the work of the person who started it, and canopy-web carries that
person without being told (`apps/harness/turn_asker.py`). A request authenticated with
the login canopy-web issued for a turn (`TurnAgentToken`) works for that turn's person;
the bearer middleware works them out on every such request, and nothing the agent sends
can add or remove them. The person is found only when the turn that login serves now
(its own, or a session login's newest turn) is executing, unconfined and runs for this
agent, and was started by a verified, active human who is not an agent — or was itself
started on such a person's behalf, so a chain of agents keeps them. The request must not
have lowered itself (`acts_as`), and the person's own turn cap applies to them. Anything
else — a stored PAT, a turn canopy itself started, an unverified asker — is the agent
acting for itself.

    X-Canopy-On-Behalf-Of: <the person's email>

still works as an assertion. It must name that same person; any mismatch, and any other
credential sending it (a browser session included), is refused with a 403 that says why.

On someone's behalf, these are judged as that person:

| Decision | Where | Their access needed |
|---|---|---|
| Pin a turn, or place a chat, on a runner | `access.may_pin_runner` (dispatch pins, chat placement, moving queued turns) | the target agent admin, or the runner's administrator |
| Ask for `turn_mode: auto` | `turn_gates.check_requested_turn_mode`; the claim re-checks `requested_turn_mode_by`, which is them | the target agent admin |

Reads are unchanged: a turn already reads as its asker where that matters (scoped caller
tokens, above). Each decision made on someone's behalf is recorded as an
`agent.on_behalf.decided` event. Every turn the agent starts from there, through any entry point
(a dispatch, a send into another session, a new session), carries
`origin_ref.on_behalf_of` (`{user_id, email, turn, agent, decided}`), stamped by
`services.enqueue_turn` alone; a client's own is dropped. Its initiator stays the agent:
the agent made the call, for that person. The receiving agent's envelope says so in
`who.on_behalf_of` (`{user, relationship, via_agent, turn}`, the relationship being the
person's to the receiving agent), which every turn an agent's login started carries:
`null` means the agent acted on its own.

## Acting as the person on canopy-web (act-as-you, in your own turn)

An agent working in a turn you started can ask to act **as you** on canopy-web itself,
for 30 minutes, and does so only once you approve with your passkey
(`apps/harness/act_as.py`, `harness.models.ActAsGrant`; rules pinned by
`tests/test_act_as_you_on_canopy.py`):

- **Asked by the turn, for the turn's own person.** The agent calls
  `POST /api/me/act-as-requests/` with the login canopy-web issued for its turn; canopy-web
  works out who you are from that login exactly as "Acting on behalf of the person who
  asked" does (`turn_asker.check`): an executing, unconfined turn for this agent, started
  by a verified, active human who is not an agent (or a chain on their behalf). Nothing it
  sends names the person, so it can never ask to act as anyone else. A stored token, a
  confined turn, a contact's, canopy's own or an unverified asker's turn asks no one (403
  saying why); so does a person with no passkey.
- **It belongs to the conversation, not the one turn.** In a chat the reply carrying the
  approve link reaches the person only when the asking turn ends, and each message after is
  a new turn with a new login. So a request waits 30 minutes, and both the request and the
  permission live while the turn runs **or**, when it was asked in a conversation (a chat
  session, or a laptop's session-scoped turn login), while that conversation is active and
  its newest turn is still the person's (`act_as.held`). Any turn login of the same agent
  in that conversation working for the same person reads the status, collects the token
  (still once) and stops it (`act_as.agent_may`); with a turn login,
  `GET /api/me/act-as-requests/` lists that agent's waiting and live requests there. A
  colleague's message, or the conversation closing, ends it. A push goes to the person's
  devices at once (how many it reached is kept: `push_sent`, and `pushed` in the event).
- **Approved with a fresh passkey touch, for that request, in your browser.** A push, a
  card on your supervisor feed and a row in Settings → Agents acting for you lead to
  `/act-as/<id>`: the agent, its reason verbatim, the conversation, what it asked for and
  the deadline — or, once it is not waiting, what happened and when.
  "Allow read-only" / "Allow read + write" each run a WebAuthn assertion begun for this
  request (`…/approve/begin` + `/finish`); a session cookie, a step-up window or any token
  is not enough, and approve/decline refuse every token.
- **Write is refused to an unattended `auto` turn.** An `auto` turn is unreviewed, and
  write authority as you with no human in it is the one combination refused
  (`act_as.unreviewed_write_refusal`: 403 at the request, 422 at the approval). A `manual`
  turn is unaffected. An `auto` turn **you are attending** may ask for write: your message
  typed in the canopy-web chat (`origin=canopy_web_chat`, asker kind `user` and that very
  person, verified, not a chain on your behalf), not confined to a caller capability, with
  you present in that chat when it asks (`presence.is_present`). The review it lacks is
  yours, at the approval page, which shows the reason and scope and needs your passkey.
  Not attended, so still read-only: a schedule or trigger firing, an inbound email or Slack
  message, a dispatch or another agent's work, a confined turn, and a chat you are not
  present in. Presence is checked when the agent asks; you may then leave for the approval
  page, but the permission still dies with the conversation and every other limit below holds.
- **Read or write, 30 minutes, dies with the turn.** `read` reaches GET routes and
  read-only tools only; `write` reaches every route canopy's MCP server serves as a tool
  except one list, `act_as.EXCLUDED_PREFIXES` / `EXCLUDED_PATTERNS` / `EXCLUDED_TOOLS`:
  tokens and PATs, the person's memory switches and grants, passkeys, vaults, keys and
  secrets, connected sites' signing keys, act-as and host-grant approvals, agent-login
  acceptance, workspace invites and the runner protocol. 30 minutes from the touch, never extended; a new approval needs a
  new request. Every request re-checks it: stopped, run out, or its turn and
  conversation over, and the token signs in as no one.
- **The token is returned once.** The agent's first status read after the approval
  carries it (`cact_…`); only its hash is kept. It signs in as you on REST
  (`tokens.middleware._authenticate_act_as_token`) and MCP (`mcp.act_as_scope`, and
  again inside the in-process request), never mints, never steps up, and is refused where
  only you may act (`kinds.speaks_for_a_turn`). A turn it starts is not a verified asker's,
  so it never chains into another permission.
- **Attributed "<agent> as <person>".** Every request is a row in the MCP call log with
  your email, the acting agent and the grant (`MCPAuditLog.acting_agent`,
  `act_as_grant`); its credential label names the agent in the settings audit and turn
  provenance; `act_as.requested|approved|declined|stopped|expired` events (agent, person,
  turn, scope — never the reason) are in the workspace's event log for its admins.
- **You see and stop it**: Settings → Agents acting for you, beside host grants, and the
  approval page; the agent may stop it too (`POST …/{id}/stop`).

## Acting as the person on a connected site (act-as-you permissions)

The **act-as-you permission** is the person's word for a **host grant**
(`tokens.HostGrant`): a time-limited permission, given in the moment, that lets an
agent call a connected site's tools as that person, within their own access on that
site, for the page they are on. It is the only way an agent acts as a person *on a
host* (the header above is canopy-web's own decisions):

- **Given by acting, never by loading, and only with the person's passkey.** The
  panel asks the site for one only when the person sends from it (or opens a
  conversation whose live card needs it); nothing can use it until they confirm it
  with their passkey in the panel ("Let <agent> act as you on this page for 30
  minutes?"), and the send waits for that. It then shows "<agent> can act as you on
  this page until <time> · Stop". 30 minutes from the passkey: the panel's token
  renewals carry the same end, never extend it.
- **Never a contact's.** Only a canopy user has a passkey, so a contact is never
  given one, and an agent that serves the site on pages is not offered to a contact
  there at all; when no agent is left, the site shows them no panel.
- **Read by one lookup, with no fallback.** `host_grants.lookup`: a turn (gateway) or
  a View in a conversation reads only the grant of the page that last acted in that
  conversation as the person who started the turn / is viewing — expired or stopped
  is a refusal ("…has ended — send again to grant it for another 30 minutes"), and so
  is one not confirmed with the passkey, never
  another page's grant or the agent's own credential.
- **A chat on a page reaches that site only as the person, under their act-as-you
  permission — owners included.** The gateway (`site_tools` / `site_call` /
  `site_read`) serves every turn in a conversation held on a site's page (the
  session's `embed_app`) whose agent is set up for that site — some capability of its
  interface lists it in `sites:` (`interface.page_site_of`) — whatever the turn's
  profile: a confined visitor's (its capability must list the site), an editor's
  (scoped token), the owner's or an agent admin's (found from the chat key the runner gave
  only that session). Every call uses the SENDER's grant for that page. A full-profile
  turn is narrowed by the widest ceiling the owner set on that site
  (`interface.site_ceiling`).
- **The agent's own login at that site is withheld from such a turn.** A capability
  names it per site: `own_login: {connect-labs: [LABS_MCP_TOKEN]}` — credential names,
  never values. The claim carries `page_site: {site, withheld}`
  (`interface.page_turn`); a cloud runner leaves those names out of the turn's
  environment and its per-turn env file and sets `CANOPY_PAGE_SITE`; a laptop runner
  cannot set an emdash session's environment, so it leaves a by-task marker
  (`~/.canopy/page/task/<task>.json`) for the agent's own MCP headers helper — the
  agent's `.env` is still on that disk, so there the boundary is the helper and the
  instructions. The caller envelope's `site` block tells the agent it is on a page,
  reaches the site only through the gateway as the person, and has no account of its
  own there in this conversation.
- **The person sees and stops theirs**: Settings → Agents acting for you
  (`/api/me/host-grants/`), and Stop in the panel (`/api/embed/host-grant`). Stop
  takes effect on the next call.

Mechanics: [`embedding-internals.md`](embedding-internals.md) § Act-as-you
permissions; the wire contract: `sdk/python/README.md` § Host grant contract v1.
