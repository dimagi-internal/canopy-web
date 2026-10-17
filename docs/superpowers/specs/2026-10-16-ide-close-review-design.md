# /ide close review: the agent proposes, the panel shows, you click

**Status:** design, 2026-10-16. The owner (Jonathan) asked for this and approved its shape on 2026-10-16.

## Intent

/ide lists every chat session you can see, and many of them are finished. The agent's last reply says the work is done, but the session is still open. Closing them one at a time with the right-click menu is slow. What the owner asked for: open the agent panel on /ide and say "close all that you are 100% sure are claiming they are done". The panel then shows a UI listing every session the agent is thinking of closing. Each row carries the same details the left rail shows, plus the line from the last reply that convinced the agent. You review the list and confirm.

## Goals

- From the panel on /ide, an agent can read the sessions on screen, with their last replies, and propose which to close.
- The proposal renders as an interactive UI **inside the panel**, using the MCP Apps View canopy already hosts (spec `2026-10-08-mcp-apps-host-design.md`). Each row shows:
  - the rail's facts: title, project, runner (online or not), state, mode, age, running, waiting on you;
  - the agent's quote and a one-line reason;
  - the full last reply, a click away;
  - a checkbox.
- **Close N** closes the ticked sessions, as the person who clicked, through the same `services.close_session` the right-click Delete uses.

## Non-goals and anti-goals

- **The agent never closes anything itself.** Proposing is the only thing the model can do. Closing is a tool only the View can call (MCP Apps `visibility: ["app"]`), so every close needs a person's click. This holds even when the agent is "100% sure".
- **No verdicts on the rail.** The agent's judgement appears only inside the panel UI, as its own quote and reason. /ide rows keep showing reported facts only.
- No automatic or scheduled closing. No new "done" signal from runners.
- The display rows never come from the agent's copy. canopy re-reads every proposed session at render time, so a row cannot show stale or invented facts.

## How it works

### 1. /ide grants the panel a narrow scope

canopy hosts its own MCP for the panel (`apps/tokens/self_host.py`). A new page key, `ide`, matches `/ide` and `/ide/:id` (`frontend/src/widget/grantPage.ts`). It maps to a new scope, `sessions:close`, which unlocks exactly two tools:

| Tool | Who can call it | What it does |
|---|---|---|
| `propose_session_closes` | model + app | Read-only. Takes `[{session_id, quote, why}]`. Re-reads each session the caller can read, marks any that cannot be closed now and why, and returns the rows. Carries the View `ui://canopy/ide/close-review`. |
| `close_proposed_sessions` | app only | Write. Closes the given ids as the caller and returns an outcome per id. Refused to any credential but a person's host grant. |

The agent reads the candidates through a page action, not a server tool (§2).

This is canopy's first **writable** self-host scope. `self_host.py` says a write scope is "a decision taken on purpose"; the owner took it on 2026-10-16. It is declared through the SDK's `writable_scopes`.

### 2. The page says what is on screen

/ide calls `usePageState` with `resource: 'ide.sessions'`, `backingTool: 'page_reviewCloseCandidates'`, and the visible (filtered) session ids, capped at 150 to fit `page_state`'s 8 KiB limit.

The agent reads the sessions through the page action `reviewCloseCandidates`, which the person's own tab answers: it fetches the list with replies, as the person, and returns the visible rows with each reply's tail (1,200 characters).

**Why a page action and not a server tool (decided during review).** When the agent's owner, a workspace owner or an admin asks, the turn is full-profile. It has no caller token and calls canopy's MCP on the agent's own login (`Agent.user`), which is not a leg of the session ACL, so a server read would return none of that person's sessions. Widening that ACL was the alternative, and it was rejected. The tab already holds the person's own session, so it reads with exactly their access, on every path.

### 3. The View

`ui://canopy/ide/close-review` is a self-contained HTML resource: no network, `connect-src 'none'`, everything through the MCP Apps bridge.

- **Input:** it reads the proposals from the tool-input notification. It then calls `propose_session_closes` again **as the viewer**, so the rows are current and read with the viewer's access. The agent's own result may hold none of them (an owner's turn runs on the agent's login), so it is only the first paint.
- **Rows:** it renders the rows, closable ones ticked, blocked ones greyed out with their reason.
- **Close N:** calls `close_proposed_sessions` through the bridge's `tools/call`. canopy's View path (`mcp_apps_views.call`) runs it under the **viewer's** grant.
- **Afterwards:** it shows each outcome (closed, closing on the runner, or refused with the reason). It then posts `ui/update-model-context` with what was closed, so the agent's next turn knows.

It follows the panel theme (light or dark) from `ui/initialize`'s host context.

### 4. What had to open up

- **Resource reads for delegated tokens** (`apps/mcp/delegation.py`). Today every resource read is refused to a delegated token. The change: allow reading a `ui://` URI that a tool inside the token's grant names as its View, and nothing else.
- **Tool `_meta` and resources in `apps/mcp`.** These are hand-written tools in `apps/mcp/tools/`, allowed there because a tool with a View has no REST shape. FastMCP's `@mcp.tool(meta=…)` and `@mcp.resource(…)` already exist.
- **App-only is not a security boundary** (spec §2), and an agent's own login lists every canopy tool. So `close_proposed_sessions` refuses any caller without a delegated (host-grant) token. A View call carries the viewer's grant, while `site_call` refuses app-only tools, so in practice only the View's button reaches it. It also applies the same per-session write check as `POST /canopy-sessions/{id}/close`.
- **The UI index.** A View renders only for a tool in the site's UI index, and the index fills from gateway listings, which an owner's turn never makes. So a grant minted on a page whose scopes carry Views records them directly (`self_host._index_views`).
- **The agent's interface ceiling.** Gate 5 narrows View calls only for a turn whose capability names `canopy-web` with a `ceiling`, which means a confined member's turn. Owner and admin turns have none. For members, the agent's `canopy` capability ceiling must also allow `propose_session_closes` and `close_proposed_sessions`. That is a data change on the agent's interface.

### 5. Access

- Every tool resolves sessions through `apps/canopy_sessions/access.py`: `readable_sessions` to read, and `can_write` to close.
- An id the caller cannot read is reported as `not_found`, never revealed.
- Closing reuses `services.close_session` unchanged, so the runner-durable close (`close_requested`) and the "unavailable" refusal behave exactly as the right-click Delete does.

## Constraints

- Framework/product boundary: the logic lives in `canopy_sessions` (domain), `mcp` (tools) and `tokens` (registry); all three are framework apps.
- MCP Apps sandbox: no storage and no network, and the bridge is the only channel.
- Changing `canopy-widget` or `canopy-ui` source: `canopy-widget` is served by canopy, not published, so it needs no bump. Nothing here should touch `canopy-ui`'s published source.

## Left to the builder

- **The last-reply tail:** 1,200 characters. *Builder's call.*
- **The page-state id cap:** 150 ids, in rail order. *Builder's call.*
- The View's exact markup, following canopy's dense style.
- **What `propose_session_closes` does with sessions the caller cannot read:** it reports only how many were proposed (`proposed`), never which ones. The View shows "N you cannot see". *Builder's call.*
- **The close re-checks each session first:** one that started running or waiting after the review rendered is reported `blocked`, not closed. *Builder's call.*
