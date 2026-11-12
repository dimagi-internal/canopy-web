# Embedding a canopy agent: actions and access

How actions reach the agent and what they cannot do (7), how access control resolves (8), whom the agent acts as (8a), and returning facts plus a `resource_link` through standard MCP (8b). Read 8a before you give an agent a tool that reads anything sensitive.

**See also:** [embedding-a-canopy-agent.md](embedding-a-canopy-agent.md), [embedding-advanced.md](embedding-advanced.md), [embedding-reference.md](embedding-reference.md), [embedding-internals.md](embedding-internals.md) (host grants), [access.md](access.md).

## 7. How actions reach the agent, and what they cannot do

### First: is it actually a page action?

Three routes, and picking the wrong one is the commonest mistake:

| the thing | route | why |
| --- | --- | --- |
| **reading** data | your server / MCP tool | live, ACL-correct, works with no tab open. The page supplies only the *selection* |
| **writing** server data | your server / MCP tool | audited, rate-limited, revocable, survives the tab closing |
| something that **only exists in a browser** | page action | scroll to a row, open a drawer, fill a form, apply a filter |

The classic wrong one is a "dismiss these rows" action on a feed. It is a *data
mutation wearing a page action's clothes* — unaudited, dead the moment the tab
closes, capped by a wait (`page_actions.DEFAULT_TIMEOUT_SECONDS`), and a second implementation of a delete the
REST API already has. The only reason to build one is a page that cannot be told
its data changed, and [§5a](embedding-advanced.md#5a-being-told-when-your-data-changes) removes that reason. The Tasks page shows the right
shape: declining is the server tool `act_on_task`, and the page hears about it
through `task://` ([§5a](embedding-advanced.md#5a-being-told-when-your-data-changes)).

**If your action's last line is an HTTP call to your own backend, it is not a
page action.** Put it in your MCP server and let [§5a](embedding-advanced.md#5a-being-told-when-your-data-changes) refresh the page.

### Declaring a real one

```js
widget.registerAction('scrollToRow', async ({ id }) => { … }, {
  description: 'Scroll the table to a row and highlight it',
  parameters: {
    type: 'object',
    properties: { id: { type: 'integer' } },
    required: ['id'],
  },
})
```

canopy turns that into an **MCP tool** (`page_scrollToRow`) on its own MCP
server, scoped to that user, with your schema as the tool's `inputSchema`. The
agent discovers and calls it like any other tool. **You never implement MCP** —
and because it is MCP, any MCP-speaking agent gets it, not only canopy's.

A browser page cannot be an MCP server (it cannot accept an inbound
connection), so canopy is the server and your page is the executor behind it:
canopy advertises, the agent calls, canopy rings the session's socket, your
callback runs in the user's tab, and the result is POSTed back.

**Throw to refuse.** A thrown error reaches the agent as a readable refusal;
returning something falsy reads as success, and it will carry on as though the
page changed.

### What this rules out

- **Actions need the page open.** They run in the user's tab, as that user —
  which is what makes the access story airtight, and means an agent cannot act
  after they navigate away. An action on a closed page fails with `no_page` or
  `timeout`; it is **never queued**, because a tab may never return and a
  deferred action would fire into a different screen.
- **Tool names are namespaced** `page_*`, so a page cannot shadow a canopy tool.
- **Two tabs declaring the same action** expose one tool — the newest wins, and
  the chosen session is named in the tool description so a wrong guess is
  visible rather than silent.
- **A confined agent sees only its own conversation's page.** A visitor's
  conversation runs the agent in a host site (an entry with `sites:`), and that profile gets
  the `page_*` tools automatically — in a sensitive conversation too, since an
  action runs in that person's tab and its answer goes only to the turn. They are
  canopy's tools, not your MCP server's: an agent that sends `page_showQuery`
  through `site_call` is refused with the name of the tool to call instead, and
  your server never sees it.

## 8. Reference: how access control resolves

One request, from a signed-in user on your page.

| # | Step | What is checked, and by whom |
| --- | --- | --- |
| 1 | User is signed in to your product | **Your** login. Canopy is not involved |
| 2 | Page loads `widget.js`, calls `canopy.init` | Nothing — a public script |
| 3 | Frame loads `/embed/chat?app=…` | Canopy returns `Content-Security-Policy: frame-ancestors <your registered origins>`. **The browser** then refuses to render that frame on any other site |
| 4 | Frame posts `ready` | Carries no data |
| 5 | Your page asks your endpoint for a token | Your own session auth and CSRF. Your server signs a statement about `request.user` |
| 6 | Canopy `contact-token` | Signature against a registered public key; `aud` is this canopy; `exp` ≤ 120 s; `jti` unused (single-use, fails closed); rate limit. Then finds-or-creates the **contact** `(your app, your id)` and issues an opaque, revocable token |
| 7 | Token is posted into the frame | `targetOrigin` is canopy's origin, never `*`. The token never appears in a URL |
| 8 | Frame calls canopy | **Every** request re-resolves the token against the database, with expiry checked, and records which app is acting. The token reaches only **your site ∩ the user** (below): the chat routes and a read-only view of your agents' turns and runners — anything else is a 403 `not_delegable` |
| 9 | `GET /api/embed/agents` | The app comes from the **token**, not a parameter. Returns allowlisted agents **∩** workspaces this user belongs to. Both required |
| 10 | Session create | Workspace from the user's memberships; the agent must belong to it; the user becomes the owner; the acting app is stamped server-side |
| 11 | Reading a session | Workspace membership **and** (you created it, or you are a participant, or it is runner-discovered on a box you own). A co-tenant holding the id cannot read your chat |
| 12 | WebSocket | The same token, on the query string, since a WS handshake carries no headers. It opens only a chat stream, and only for an agent your site offers |
| 13 | Page state | Computed in the user's browser, in their session, so it names only what that user can see. It carries a SELECTION (ids + the tool that resolves them); the agent re-reads the rows through that tool — as the **agent**, or as the visitor when your site issued a grant ([§8a](#8a-who-the-agent-acts-as)) |
| 14 | Invalidation | Sent only to sessions whose declared `resource` matches, and carries the URI alone — never row data. A page that declared nothing is told nothing |

**What a user's token can reach: your site ∩ the user.** When your visitor is
an existing canopy user, the token you hold for them acts *as* them — but only
inside what your site is for: the chat API, `/api/embed/…`, the chat websocket and a
read-only view of harness turns, sessions and runners. The surface list is
`apps/embedding/delegation.py` (`_SURFACE`; ask for a route by name rather than
widening it). Within that, **only the agents your site offers** in the workspace that
registered it (`offered_agent_ids`): the same person's chats with any other agent are a
404 to your token, exactly as to a stranger. Every other canopy route answers
`403 not_delegable`; the token is never the user's whole canopy account.

**The property that matters:** nothing is frozen into the token. Tokens are
opaque random strings stored as hashes, and memberships are re-read from the
database on every request — revoke a membership and the next call reflects it.
A JWT would have frozen the ACL at mint time, which is weaker.

**canopy's own panel on a workspace app's page** (`/w/<ws>/apps/<key>/…`) is the same chain with canopy as
the host: the panel names the app when it mints (`?app=`, `frontend/src/widget/grantPage.ts::tokenUrlFor`),
canopy keeps it on the token only if the person may open that app (a guest included,
`embed_api.embed_self_token`), offers the agents with an entry for it, and stamps the conversations it creates
`metadata.workspace_app`, which selects that entry and grants nothing ([host-sites.md](host-sites.md) § App
entries). The app's page offers the agent its own actions as a view does on its page (`AppFrame` → `pagePanel`).

**What the agent never gets:** any credential for your product from this
chain. It reaches your page only through the bridge, only while the page is
open.

## 8a. Who the agent acts as

The chain above governs **who can talk to the agent**. What the agent can *read*
is a second question, and there are two answers depending on whether your
site issues grants.

**Without a grant** — the default, and every Slack or email caller — a tool call
runs as **the agent's own identity**: the credentials configured on the runner.
`backing_tool` then re-reads your rows *as the agent*: the ids in your page state
narrow what it looks at, but do not *limit* what it could look at. Give such an
agent only access that is fine for *every* visitor who can reach it.

**With a grant, the agent calls your tools AS YOUR VISITOR.** Per-visitor tool
permissions exist, and they are yours to enforce, because you signed the
visitor in. The wire contract is
[`host-grant-contract.md`](host-grant-contract.md); the shape is MCP's
Enterprise-Managed Authorization (an ID-JAG redeemed with the RFC 7523
jwt-bearer grant), and the principle is: **the site that authenticated the user
issues the grant; canopy only redeems.** canopy never holds a key you trust to
assert who a user is.

**Don't hand-write your half.** A Python host installs the SDK that implements it
— `dimagi-canopy` ([`sdk/python`](../../sdk/python/README.md), import name
`canopy_sdk`): signing the assertion and ID-JAG, the page registry, the
jwt-bearer token endpoint, the DPoP gate for your MCP, Django wiring, and
conformance checks you can run against your deployment. canopy-web's CI runs its
host half against canopy's real redemption code, so it cannot drift from what
canopy accepts.

The steps — every claim, lifetime, header and refusal — are in
[the SDK README § Host grant contract v1](../../sdk/python/README.md#host-grant-contract-v1).
The parts a host must not get wrong:

1. **Arrival — only once the person asks.** Beside `assertion` ([§3](embedding-a-canopy-agent.md#3-add-one-backend-endpoint)), send `id_jag` (an ID-JAG,
   `typ: oauth-id-jag+jwt`, for your own MCP server, scoped to what THIS page offers)
   **only on a mint whose JSON body is `{"grant": true}`**. This is the person's
   **act-as-you permission**. The widget's mint on page load is always `{}`; it sends
   `{"grant": true}` once the person sends in the panel (or opens a conversation whose
   live card needs to read your site as them), and on every mint of that tab after,
   until they press **Stop**. `canopy_sdk.django`'s `panel_token` already does this.
   canopy holds the grant unusable until the person confirms it with their canopy
   passkey in the panel (30 minutes from that touch). Only a visitor who arrives as a
   canopy user can: an ID-JAG sent with a contact's arrival is ignored, and an agent that
   serves your site on pages is not offered to a contact at all.
2. **Redeem.** canopy (client id `{canopy base}/oauth/client.json`, keys at
   `{canopy base}/oauth/jwks.json`) redeems it at your token endpoint with
   `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`, `private_key_jwt` and DPoP;
   you answer a short-lived, DPoP-bound access token for the visitor — no refresh token.
3. **Use.** canopy's `site_tools` / `site_call` / `site_read` call your MCP with
   `Authorization: DPoP <token>`, a fresh proof and an informational `Canopy-Actor`
   header; run the tool as `sub`, within the token's scopes.
4. **Sensitive data? Verify `Canopy-Execution`.** On an agent's turn every request also
   carries canopy's signed statement about the call (the agent, its host-site
   `data: sensitive` declaration, the runner's flags, the conversation's requirements).
   `canopy_sdk.django.execution.verify_execution(request)` checks it;
   `execution.sensitive_clearance(agents={...})` says whether to release sensitive data.
   canopy's gateway itself never calls you for a sensitive conversation whose host site
   does not handle sensitive data. Claims and refusal codes:
   [SDK README § `Canopy-Execution`](../../sdk/python/README.md#canopy-execution--what-canopy-vouches-for-per-request).
5. **The person's own View? Verify `Canopy-View`, never infer it.** A View's
   `resources/read`, and a View's own `tools/call` of a tool whose `_meta.ui.resourceUri`
   is that View, carry canopy's signed `Canopy-View`; an agent's own tool call never does.
   It rides only when the principal behind the View is a person acting as themselves
   (`apps/harness/acting_person.py`) in a conversation they are in
   (`mcp_apps_views.view_facts`). If you give a View more than an agent, give it on
   `canopy_sdk.django.execution.verify_view(request, uri=, subject=)` alone: a delegated
   call with **no** statement is no one you may trust. Check it live with
   `manage.py view_read_probe --session <id> --email <person>` in canopy-web's container.

**What canopy adds on its side.** In your site's host-site row
(`/w/<workspace>/settings/host-sites`), set your **sign-in issuer** and **MCP
server** URL. The agent's owner gives the host site that serves your site
`sites: [<your site's name>]`. **What a call may reach is yours to decide**: your
page's scopes and your ACL for that person, enforced by your MCP server (it lists
and runs only the tools the token's scopes map to). canopy holds no list of your
tools. An owner MAY add a site tool limit, `ceiling:` (tool-name globs) to keep an agent off part
of your site; it only ever narrows what you granted. Your page's `backing_tool`
tells the agent where to read the rows on screen — it is a hint, not a filter,
so a page's other tools need not be re-listed in its state.

**No presence, no access, never a fallback.** Your access token lives at most
30 minutes (`ACCESS_TOKEN_MAX_LIFETIME`). While the person's tab holds a grant
the widget re-mints about every five minutes, so you issue a fresh ID-JAG on
that cadence. A conversation reads ONLY through the grant of the page that last
acted in it as that person (sent, declared its page, or made a View call), and
only once they confirmed it with their passkey —
never another of their pages' grants, however recent, and never the agent's own
credential. Once that grant runs out (or they press Stop, in the panel or on
canopy's Settings), a call in their turn is REFUSED and the agent tells them:
"Your permission for <agent> to act as you on this page has ended — send again
to grant it for another 30 minutes." A failed redemption never fails the
arrival: the chat opens, and the agent simply cannot reach your tools as them.

**What a compromise of canopy can do**: use unexpired tokens of visitors who sent
from your page in the last half hour, within that page's scope. It cannot mint a
grant for anyone (there is no user-signing key to steal), reach a visitor who
never sent from the panel, or use a token anywhere but your MCP server (`aud`).
A token copied from a log is useless without canopy's DPoP key, and no runner
ever holds either.

(canopy does not sign assertions about your visitor for you to trust: whoever
held that key could sign for anyone at every host site.)

For **canopy's own MCP tools**: a visitor who is not the agent's owner
or an admin talks to a CONFINED session, which reaches canopy's MCP with a
caller token scoped to **agent ∩ visitor** — the visitor's own canopy
permissions (none, for a contact), limited to the tools its profile (that host
site, or read mode) lists. The owner's token is never used there.

## 8b. Return facts + a `resource_link` to a result you keep, using standard MCP

A tool that produces data — a query's rows, a chart, a draft edit — should not put
the data in its result. Return **facts** for the model (how many rows, the columns,
a few samples, what the query was) and a standard MCP **`resource_link`** (MCP
2025-06-18 tool-result content) naming a **result your host keeps**, which your MCP
server answers `resources/read` for. Keeping it is your host's own MCP business —
canopy needs only the link, and a declared scheme:

```json
{"content": [
  {"type": "text", "text": "{\"row_count\": 1840, \"columns\": [\"worker\", \"visits\"], ...}"},
  {"type": "resource_link", "uri": "labs-result://Zk3...", "name": "Visits by worker",
   "mimeType": "application/json", "description": "1840 rows"}
]}
```

**Why.** Your host owns what its tools produce; canopy carries the conversation and
the wiring, never the data. The person's View (your MCP Apps card in the chat) reads
the link back from you with `resources/read` — through canopy's View routes and its
gateway, **as the viewer**, with their delegated token — and pages, sorts and
re-reads it there. The agent reads it with **`site_read`** (beside `site_call`), as
the visitor, only when it needs more than the facts — so a large result never has to
be in its context. Your ACL and your audit apply to every read.

**What canopy keeps of a tool result** is `sensitivity.strip_result`
(`apps/harness/sensitivity.py`). In a sensitive conversation: the call's id, error flag and
status, `withheld: true`, none of your text, and each link as `{type, uri, mimeType}` with
`name` set to the URI's **scheme**. Ordinary conversations keep your text (bounded) and
each link whole.

A link is kept only when **your own tool returned it** — `site_call` to your site, or
one of your tools a runner reached directly — never from another tool's output (link-shaped
JSON a shell or a file read prints is the agent's words, and goes). It must be a
well-formed `resource_link` whose URI is **`<scheme>://<id>`** with a scheme your
`site.json` declares in `site.resource_schemes` (never `http(s)`, `data`, `ui` or `file`),
a url-safe id, no path, query or fragment; the id length (`site.resource_id_lengths`),
the allowed `mimeType`s and the per-result cap are `canopy_sdk/resources.py`
(`validate_links`). Its `description`, `_meta` and `annotations` are never kept, and in a
sensitive conversation neither is its `name`: those links rest in plaintext, so nothing in
them may be data — make the id opaque (`labs-result://Zk3…`), never a query, a name or a
filter. A link is pulled out of your result **before** the text is cut, so a long answer
still reaches its card. canopy reads the scheme declaration live from your signed
document, with no owner step.

The agent gets your answer through `site_call` as `{site, tool, is_error, links,
content, structured}`: your links first, then the rest, with MCP's text copy of
`structuredContent` left out so the facts are carried once. What a View receives: in an
ordinary conversation, your tool result as it was, with the kept links first. In a sensitive one, `ui/notifications/tool-result` is
`{content: [the links], structuredContent: null, _meta: {"canopy/withheld": true}}`, and
the View reads each link as its viewer. canopy refuses any answer from your MCP server
over **4 MB** (`result too large; use a smaller page_size`), so page what you return.

**A minimal host** (a tool returning facts and a link, and a `resources/read` handler) is
in [SDK README § Host-held results](../../sdk/python/README.md#host-held-results-return-facts--a-link),
with `CANOPY_HOST["RESOURCE_SCHEMES"]` / `["RESOURCE_ID_LENGTHS"]` to declare the scheme.
The resource handler must apply **the same sensitive-data rule as the tool**: a result is a
second route to the same rows, so a host that clears only `tools/call` lets an agent
`site_read` back what its tool withheld. One rule, asked by every handler that returns rows:

```python
from canopy_sdk.django import execution

def cleared(request, uri: str | None = None) -> bool:
    """A delegated request with neither statement is no one: never cleared."""
    if request.headers.get("Canopy-Execution"):                 # an agent's turn (§8a step 4)
        try:
            call = execution.verify_execution(request)
        except execution.ExecutionRefused:
            return False
        return call.sensitive_clearance(agents={"labsdata"}) is None
    if uri is not None:                                         # the person's own View (§8a step 5)
        try:
            execution.verify_view(request, uri=uri, subject=current_subject())
        except execution.ViewRefused:
            return False
        return True
    return False
```

**Live updates.** A View may `resources/subscribe` to a link. canopy holds the
subscription (its gateway's MCP sessions last one request), so your host tells canopy
when a result changes — a POST to `/api/sites/resources-updated` with a JWT signed by
your site key (signed with your site key; the wire is `canopy_sdk/resources.py`, canopy's check `apps/host_grants/resources_updated.py`);
`canopy_sdk.resources.notify_updated(canopy_url, site_name, site_key, [uri],
workspace=...)` builds and sends it. canopy sends each subscribed View
`notifications/resources/updated` — the URI, never data — and the View re-reads it
as its viewer.

---
