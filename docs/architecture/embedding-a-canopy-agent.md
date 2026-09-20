# Embedding a canopy agent in your product

**This document is the whole task.** Do the six steps in order, then run the
checks in [§6](#6-check-that-it-works). You should not need anything else — if
you do, that is a bug in this document, and saying so is more useful than
working around it.

**What you are building:** a chat panel on one of your pages, talking to a
canopy agent that can read the page state you choose to hand it. The user is
your already-signed-in user, and the agent can only ever see what that user can
see.

**Two things to know before you start, because they change what you build:**

1. **Agent actions work, and they are MCP tools.** What you declare with
   `registerAction` appears in the agent's tool list with your JSON-Schema —
   you do not implement MCP, canopy translates. They run **only while the page
   is open**; see [§7](#7-how-actions-reach-the-agent-and-what-they-cannot-do) for what that rules out.
2. **Register your local dev origin too** (`http://localhost:8000`, or whatever
   you serve on), or the widget will not load on your machine. See
   [§1](#1-register-your-app-in-canopy).
3. **Your visitor talks to the agent as a *contact*, not as their canopy
   account — and the agent's own tool calls run as the agent, not as your
   visitor.** Both matter for what data the agent can reach. Read
   [§8a](#8a-who-the-agent-acts-as) before you give an agent a tool that reads
   anything sensitive.

Design rationale lives in
[`../superpowers/specs/2026-09-12-embedded-agent-widget-v2-design.md`](../superpowers/specs/2026-09-12-embedded-agent-widget-v2-design.md).
You do not need it to do this.

---

## The six steps

| # | Step | Where it happens |
| --- | --- | --- |
| [1](#1-register-your-app-in-canopy) | Register the app: credential, frame origins, allowed agents | canopy admin |
| [2](#2-keep-the-private-key-private) | Generate a signing key; the private half goes in your secret store | your infra |
| [3](#3-add-one-backend-endpoint) | One backend endpoint that mints a per-user token | your repo |
| [4](#4-add-the-script-tag) | The script tag and `canopy.init` | your repo |
| [5](#5-hand-the-agent-the-page-state) | `setPageState` with what the user is looking at, `onInvalidate` to refresh | your repo |
| [6](#6-check-that-it-works) | Check it works | a browser |

Steps 1–2 are one-off per environment. Steps 3–5 are the only code you write.

---

## 1. Register your app in canopy

Canopy has to know your app exists, which origins may frame the widget, and
which agents it may offer. **All three fail closed** — which is why "nothing
happens" almost always means one of them is missing rather than something being
broken.

Go to **Connected sites** in your workspace —
**`/w/<workspace>/settings/connected-apps`** — as a workspace owner:

| Field | What it is | If you get it wrong |
| --- | --- | --- |
| **Name** | The `app` value you pass to `canopy.init`, e.g. `connect-labs` | Mismatch ⇒ the widget's frame 404s |
| **Site URLs** | The origins allowed to frame the widget. **Include every environment** — production, staging, and your local dev origin | Empty ⇒ the frame 404s by design; an exempt page with no `frame-ancestors` would be frameable by any site |
| **Agents it may offer** | Which of this workspace's agents this site may offer | None ⇒ the picker offers nothing |
| **Signing key** | The PUBLIC half of a key pair your server signs visitor statements with (§3). Several may be pasted during a rotation | Missing ⇒ your site can vouch for nobody, so the widget never gets a token |

Creating a site also shows a **secret**, once. The signing-key flow in §3 does
not use it — it exists for an older server-to-server exchange (§3, last part).
canopy keeps only a hash, so it cannot be recovered; **New secret** issues
another and invalidates the old one immediately.

URLs are validated on save. A wildcard, a path, or anything containing `;` is
refused with an explanation — a wildcard in particular would undo the entire
framing protection. A trailing slash is accepted and trimmed, because that is
what you get from an address bar.

> **Why a signing key and not "vouch for our email domain".** This page used to
> offer that: a checkbox letting whoever held the site's secret mint a token as
> *any* canopy user in your domain. A static string that can speak for anybody
> is the weakest possible credential, so the page no longer grants it. A signed
> statement names one visitor, expires in two minutes, works once, and cannot be
> forged by canopy or by anyone who steals canopy's database — it holds only the
> public half.

> **Why not the Django admin?** It used to be the only way, and it is now
> read-only. It is staff-only, so the person who wants to embed an agent could
> not do it; there is no shell on a deployment to run the management commands
> in either; and every field here fails closed and silently, so a bare form
> produces a widget that never appears with nothing to say why. See §11 for
> canopy's own pages, where the whole thing is one button.

**Alternatively, by command** (local or scripted setup only — a deployment has
no shell to run these in, since `EnableExecuteCommand` is off on the service and
the database is VPC-internal). Signing keys are set on the page:

```bash
uv run python manage.py create_app_credential --name connect-labs
uv run python manage.py grant_app_frame_origin --name connect-labs \
    --origin https://labs.connect.dimagi.com
uv run python manage.py grant_app_frame_origin --name connect-labs \
    --origin http://localhost:8000
uv run python manage.py grant_app_agent --name connect-labs --agent labs-helper
uv run python manage.py grant_app_agent --name connect-labs --list
```

---

## 2. Keep the private key private

Generate a key pair on your side (Ed25519 is the simplest):

```bash
openssl genpkey -algorithm ed25519 -out canopy-signing.pem
openssl pkey -in canopy-signing.pem -pubout      # paste THIS into Signing key
```

The **private** key goes in your secret store and stays on your server. It lets
its holder vouch for visitors to your site — treat it like a database password,
and never send it to a browser. The public half is not secret; it is what canopy
checks signatures against.

To rotate: paste the new public key alongside the old one, deploy the new
private key, then remove the old public key. Editing the site never changes
anything else, so you can add URLs and agents freely without breaking a
deployment.

---

## 3. Add one backend endpoint

Your server vouches for the person looking at the page, and canopy hands back a
**short-lived token for them**. This is the one piece that cannot live in a
browser, which is why "one script tag" is really "one script tag plus one
endpoint."

You vouch by **signing a statement**, not by presenting a shared secret. Keep
the private key on your server; canopy holds only the public half, which you
pasted in step 1. That is the difference that matters: a secret canopy stores
can be stolen from canopy, and one static string can name anybody — a signature
proves *this* claim, about *this* visitor, at *this* moment, and canopy could
not forge one if it wanted to.

```python
# your_app/canopy.py
import json, urllib.request, uuid
from datetime import datetime, timedelta, timezone

import jwt  # pyjwt[crypto]
from django.conf import settings


def vouch_for(user) -> dict:
    """Ask canopy for a token for one of our signed-in people."""
    now = datetime.now(timezone.utc)
    assertion = jwt.encode(
        {
            "iss": settings.CANOPY_APP_NAME,       # the Name from step 1
            "sub": str(user.pk),                   # YOUR id for them, opaque to canopy
            "aud": settings.CANOPY_BASE_URL,       # this canopy, and no other
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=60)).timestamp()),
            "jti": str(uuid.uuid4()),              # single use
            # Optional and descriptive only — canopy records them, and matches
            # on neither.
            "name": user.get_full_name(),
            "email": user.email,
        },
        settings.CANOPY_SIGNING_KEY,               # the PRIVATE half. Never leaves here.
        algorithm="EdDSA",
    )
    req = urllib.request.Request(
        f"{settings.CANOPY_BASE_URL}/api/auth/contact-token",
        data=json.dumps({"assertion": assertion}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())
```

```python
# POST /your-app/canopy/token
@login_required
@require_POST
def canopy_token(request):
    vouched = vouch_for(request.user)
    # Pick fields explicitly rather than passing canopy's dict through — a new
    # field on canopy's side should not break your endpoint.
    return JsonResponse({
        "token": vouched["token"],
        "expires_at": vouched["expires_at"],
    })
```

> **`sub` must be your own id for the signed-in user and nothing else.**
> You are asserting "this is a real person on my site", and canopy believes you
> because it verified your signature. Taking `sub` from the request body would
> let any caller be anybody.

**What the visitor becomes.** A `Contact` in the workspace that owns your app —
somebody canopy knows about, who is *not* a member of anything. They can talk to
the agents you were allowed to offer and see their own conversations with your
site. They cannot reach the workspace, its other agents, or anyone else's
threads, and recording them grants nothing: `/api/contact/` is the entire
surface a contact token reaches.

Your ids live in your own namespace — `(your app, your id)` — so they cannot
collide with a canopy user or with another site's people. That is why this is a
smaller grant than the email-domain vouching it replaced, which reached into
canopy's own user population.

**Requirements canopy enforces**, so it is worth knowing before you debug a
401: `EdDSA`, `ES256` or `RS256` only (never HMAC — the key is public, so a
symmetric algorithm would let anyone sign); `aud` must match; `exp` at most 120
seconds out; and each `jti` works exactly once.

**If your visitors already have canopy accounts** — Dimagi staff on a Dimagi
product, say — they still arrive as contacts through this flow, not as their
canopy selves. That is deliberate: a contact can reach only the agents you
offer and their own conversations, so a mistake on your side cannot hand a
visitor your workspace. There is a second, older kind of token that *does*
resolve to the canopy user — `POST /api/auth/token-exchange`, with the site's
secret and the visitor's email, as ace-web uses — but it depends on the
email-domain grant this page no longer issues, and can only be set up by
command, which a deployment cannot run. If your product needs visitors to act
as their canopy accounts, raise it rather than working around it; the widget
accepts either kind of token unchanged, so nothing else would move.

The widget calls this endpoint with `credentials: 'same-origin'` and sends an
`X-CSRFToken` header if you set a `csrftoken` cookie, so your normal session
auth and CSRF protection apply unchanged. **If you renamed that cookie** — which
a Django app served under a path prefix on a shared host has to do, or it
collides with its siblings — pass the real name as `csrfCookieName` in step 4.
Leave it wrong and the header is simply absent: the mint 403s and the widget
never starts, with nothing on the page to say why.

---

## 4. Add the script tag

```html
<script src="https://labs.connect.dimagi.com/canopy/embed/widget.js"></script>
<script>
  var widget = canopy.init({
    baseUrl: 'https://labs.connect.dimagi.com/canopy',
    app: 'connect-labs',              // the Name from step 1
    tokenUrl: '/labs/canopy/token',   // the endpoint from step 3
    mode: 'docked',
    launcherLabel: 'Canopy AI',        // what YOUR people call it
    // dismissible: false,             // default true — see below
    // Only if your CSRF cookie is not named `csrftoken`:
    // csrfCookieName: 'csrftoken_labs',
  })
</script>
```

No npm, no React, no build step, no framework. The loader is ~6 kB and imports
nothing; the chat UI runs inside an iframe with its own React, so it cannot
collide with anything your page already loads. That is what makes it work on a
page built from non-module scripts sharing a global scope.

**The launcher is yours to name.** `launcherLabel` is the text on the bubble,
because it appears on *your* page and only you know what your people call this
thing. It defaults to "Ask Canopy".

**And your visitors can move it or put it away.** Dragging the bubble relocates
it, and where they left it is remembered per site (`localStorage`; pass
`storage: null` to opt out, and it degrades to "starts in the corner" wherever
storage is blocked). A position is always pinned back inside the window, so one
saved on a desktop cannot strand the bubble off-screen on a phone.

 The bubble is fixed to a corner of your
page, and on a phone it lands on whatever is already in that corner — so it
carries an × that hides it for the rest of that page load. It comes back on the
next load, deliberately: a widget that stays hidden with no way back is a
support ticket. Pass `dismissible: false` if you have laid out around it, or
call `widget.dismiss()` to offer your own way.

**Pick a mode:**

| mode | shape | use when |
| --- | --- | --- |
| `overlay` | floating bubble + panel | you cannot change the page's layout |
| `docked` | full-height right rail | the page can give up a column |
| `inline` | fills a `target` element, no launcher | you want it inside your own layout |

`inline` requires `target` (a selector or element) and is always open — there is
no launcher to reopen it with.

**Other options:** `agent: 'labs-helper'` skips the picker; `metadata: {…}`
stamps opaque data on sessions the widget creates; `onInvalidate(resource)` is
called when data your page shows has changed (§5a); `dismissible`, `storage`,
`csrfCookieName` as above; plus `width`, `title`, `open: true`.

The returned object has `open()`, `close()`, `toggle()`, `isOpen()`,
`dismiss()`, `isDismissed()`, `setPageState()`, `registerAction()`,
`unregisterAction()`, `destroy()`, and the older `provideContext()` (§5).

---

## 5. Hand the agent the page state

**This changed on 2026-09-16.** `provideContext` still works, but it is the old
model and it is worse: read once when a conversation opened, delivered as prose,
attached to the first message only. Filter your page after opening the chat and
the agent was reasoning about a screen that had moved.

Declare your page instead. Two lines:

```js
widget.setPageState({
  resource: 'stock://',                 // WHAT you are showing (an MCP resource URI)
  backing_tool: 'stock_on_hand',        // the tool that resolves these rows
  visible_ids: [4821, 4822, 4823],      // WHICH rows are on screen
  filters: { period, commodity },       // what the user narrowed to
})
```

Push it whenever the view changes — it replaces wholesale, so re-sending costs a
round trip and nothing else.

### Send the selection, not the data

The single rule. `visible_ids` plus `backing_tool` says *which rows* and *where
to read them*; the agent then calls that tool itself, live, with the caller's own
permissions applied.

Serialising the rows instead means you have duplicated your own API, the copy can
go stale between render and send, and you now have a second place to get access
control wrong. **canopy refuses a state larger than 8 KiB** with a message
telling you this — the cap is generous for several hundred ids and deliberately
too small to hold the rows behind them.

### What the agent does with it

Two paths, deliberately, and the redundancy is the point:

1. **It rides the first message.** Your declared state is folded into the
   context block sent with the opening question, so the agent has your selection
   from the very first turn.
2. **The MCP tool `current_page`** returns every attached page's state, so the
   agent re-reads your screen *whenever it needs to* — "close the ones I'm
   looking at" is answerable on turn nine.

Path 1 exists because path 2 can be unavailable: MCP servers connect
asynchronously, and a conversation starts a fresh agent process, so the first
turn can race that connection. Measured live on 2026-09-16 — the agent replied
"the canopy-web MCP server is still connecting, its tools aren't loaded" and was
blind to twenty rows it had been sent. The first turn carries the user's actual
question, so it must not be the one that depends on the flakiest link.

Also:

- **Include identifiers, not labels.** "Kano warehouse" lets the agent talk
  about it; `4821` lets it act on it.
- **Never include secrets.** It lands in a transcript that persists.
- **Compute it from what this user can see.** It runs in their browser, in their
  session — if your RBAC hides a row, keep it out. "The agent sees exactly what
  the user sees" is the whole access-control story, and keeping it true is yours.

Declare nothing and the agent simply starts without page state.

### AG-UI: one call instead of two

If you already speak AG-UI, send its own object and skip canopy's two endpoints:

```
PUT /api/canopy-sessions/{id}/run-input     ← AG-UI RunAgentInput
```

`state` becomes your page state, `tools` become your page actions. Fields canopy
cannot honour (`messages`, `run_id`, `resume`, `forwarded_props`) are accepted
and ignored, so a conforming client sends the whole object unchanged.

The stream coming back is AG-UI too. Connect the session socket with
`?protocol=ag-ui` and every frame arrives as an AG-UI event — which is what
canopy's own chat page and widget do, via `canopy-ui` ≥ 0.9
(`useSessionSocket({ protocol: 'ag-ui' })`). What AG-UI has no word for rides its
designed extension points rather than being dropped:

- canopy-only events (co-edited drafts, presence, page actions, "did my stop
  land") are `CUSTOM` events named `canopy.<event>`;
- where AG-UI's spelling is lossy for canopy — the connect snapshot, a stream
  error, a blocked agent's dialog — the original frame rides alongside under
  `metadata.canopy.frame`. It is legal AG-UI 1.0 metadata, so a 1.0 client's
  enforcement keeps it (CI checks every event canopy emits against
  `@ag-ui/client`'s own `enforceEvents`).

Ask for nothing and you get canopy's native frames, unchanged.

One limit, so nobody discovers it the hard way: the EVENTS are AG-UI 1.0, the
TRANSPORT is canopy's. AG-UI's own clients expect one HTTP request per run,
streamed over SSE and opening with `RUN_STARTED`; canopy's socket is a
long-lived multiplayer session. So point `canopy-ui` (or your own reader) at the
socket — not a stock AG-UI `HttpAgent`, which will reject the stream.

---

## 5a. Being told when your data changes

A page is a cache. Tell the widget how to re-read it and canopy will say when
the data behind your declared `resource` moves — **whoever moved it**:

```js
var widget = canopy.init({
  // …the options from step 4…
  onInvalidate: function (resource) {
    if (resource === 'stock://') refetchStock()
  },
})
```

It is only ever called for a `resource` your page declared with `setPageState`,
so a page that declared nothing is told nothing.

This fires when the agent changes something, *and* when a scheduled job does,
*and* when your own fleet does, *and* when the same page is open in another tab.
Without it, the agent can delete rows you are still displaying — which is the
bug this replaced.

The notification carries the resource URI and nothing else. That is MCP's
`notifications/resources/updated` shape: you re-read through the path you
already use, where your authorization already applies. A diff would be a second
source of truth for data you already know how to load.

---

## 5b. Reading back a user's history

The token your endpoint mints also lists that user's earlier conversations —
**on your host, and only yours**:

```
GET /api/canopy-sessions/                          ← this user's conversations here
GET /api/canopy-sessions/?resource=stock://        ← ...had while showing that resource
GET /api/canopy-sessions/?page_path=/warehouses    ← ...or on that path
```

The host is taken from the token, exactly as it is when a conversation is
created. An `embed_app` you pass is ignored rather than honoured, so connect-labs
cannot list the same person's conversations on canopy-web — the same human, but
across a boundary they did not cross. (A signed-in canopy user or a personal
token sees everything that person can, because there the person is asking, not
an app on their behalf.)

The page filters match what the page itself declared with `setPageState`, never
anything canopy infers. A conversation whose page declared nothing matches no
page filter: it was not had on any page canopy knows about, and guessing from
its title would be inventing where it happened.

---

## 6. Check that it works

In a browser, on your page, signed in as an ordinary user:

| Check | Expected |
| --- | --- |
| The launcher appears | a button, or your docked/inline panel |
| Clicking opens a panel | a chat, not a blank rectangle |
| An agent is offered | the picker, or straight into a conversation |
| Sending a message gets a reply | the agent responds |
| The agent knows your page | ask it "what am I looking at?" |
| Reopening later shows history | your prior conversations with that agent — on this host only (§5b) |

### If something does not work

| Symptom | Cause |
| --- | --- |
| Blank panel; console shows a frame refused to load | your origin is not in **Allowed frame origins** |
| Panel says "not configured correctly" | same cause — the shell rendered with no origins |
| "No agent is available here yet" | no **Allowed agents** row, or this user is not a member of that agent's workspace |
| A token error in the panel | your endpoint 403'd, 500'd, or returned no `token` |
| 404 on `/embed/chat` | app name mismatch, credential revoked, or no frame origins |
| 503 on `/embed/widget.js` | canopy's frontend is not built |
| Agent replies but knows nothing about the page | no `setPageState` call, or it ran after the first message was sent |
| Agent acts on rows the user cannot see | you sent rows instead of ids — send `visible_ids` + `backing_tool` |
| Page shows rows the agent already deleted | no `onInvalidate` in `canopy.init`, or the `resource` it checks differs from the one in `setPageState` |
| Message sends but no reply ever arrives | no runner is online for that agent — a canopy-side operational issue, not yours |

---

## 7. How actions reach the agent, and what they cannot do

### First: is it actually a page action?

Three doors, and picking the wrong one is the commonest mistake:

| the thing | door | why |
| --- | --- | --- |
| **reading** data | your server / MCP tool | live, ACL-correct, works with no tab open. The page supplies only the *selection* |
| **writing** server data | your server / MCP tool | audited, rate-limited, revocable, survives the tab closing |
| something that **only exists in a browser** | page action | scroll to a row, open a drawer, fill a form, apply a filter |

canopy got this wrong itself and it is worth learning from: `dismissInsights`
was a page action until 2026-09-16. It was a *data mutation wearing a page
action's clothes* — unaudited, dead the moment the tab closed, capped by a
20-second wait, and a second implementation of a delete the REST API already
had. It existed only because nothing could tell the page its data had changed.
Once pages could be told their data changed (§5a), the reason was gone; it is
now the server tool `dismiss_insights(ids)` and the page action is deleted.

**If your action's last line is an HTTP call to your own backend, it is not a
page action.** Put it in your MCP server and let §5a refresh the page.

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
| 8 | Frame calls canopy | **Every** request re-resolves the token against the database, with expiry checked, and records which app is acting |
| 9 | `GET /api/embed/agents` | The app comes from the **token**, not a parameter. Returns allowlisted agents **∩** workspaces this user belongs to. Both required |
| 10 | Session create | Workspace from the user's memberships; the agent must belong to it; the user becomes the owner; the acting app is stamped server-side |
| 11 | Reading a session | Workspace membership **and** (you created it, or you are a participant, or it is runner-discovered). A co-tenant holding the id cannot read your chat |
| 12 | WebSocket | The same token, on the query string, since a WS handshake carries no headers |
| 13 | Page state | Computed in the user's browser, in their session, so it names only what that user can see. It carries a SELECTION (ids + the tool that resolves them); the agent re-reads the rows through that tool — as the **agent**, see §8a |
| 14 | Invalidation | Sent only to sessions whose declared `resource` matches, and carries the URI alone — never row data. A page that declared nothing is told nothing |

**The property that matters:** nothing is frozen into the token. Tokens are
opaque random strings stored as hashes, and memberships are re-read from the
database on every request — revoke a membership and the next call reflects it.
A JWT would have frozen the ACL at mint time, which is weaker.

**What the agent never gets:** any credential for your product from this
chain. It reaches your page only through the bridge, only while the page is
open.

## 8a. Who the agent acts as

The chain above governs **who can talk to the agent**. It does not govern what
the agent can *read* — and the two are different people.

When the agent calls a tool, the call runs as **the agent's own identity**: the
credentials configured on the machine that runs it. Nothing in a turn passes
your visitor's identity through to the agent's tools. So:

- `backing_tool` re-reads your rows **as the agent**. If the agent can see
  more than your visitor, the ids in your page state narrow what it looks at,
  but they do not *limit* what it could look at.
- For canopy's own MCP tools, the agent reads with whatever token its runner
  holds — not the visitor's permissions.
- For **your** MCP tools, the agent needs its own credential for your product,
  set up on the runner. That credential is what bounds it.

**What this means in practice:** give an embedded agent tool access that is fine
for *every* visitor who can reach it, because in effect every visitor can ask it
to use that access. Per-visitor tool permissions — the agent acting *as* your
visitor — do not exist yet. They are the next piece of work, and if your data
needs them, the right move is to wait for it rather than scope around it in your
page.

---

## 9. Reference: known limits

- **Page actions need the page open** (§7). There is no server-side path for an
  agent to act on your *page* later — which is a reason to put data mutations in
  your MCP server, where they work regardless.
- **Invalidation reaches attached pages only.** Nothing is queued for a tab that
  is closed; it reads fresh data when it next opens, so this is correct rather
  than a gap.
- **The agent's tools run as the agent, not your visitor** (§8a). The largest
  limit on what an embedded agent should be allowed to do.
- **Visitors are contacts, not canopy users** (§3). Any visitor your server
  vouches for can chat — there is no domain allowlist on this path — but none of
  them acts as a canopy account.
- ~~**Context is a snapshot.**~~ **Fixed 2026-09-16.** Page state is pushed on
  every change and re-read by the agent on demand via `current_page`, so
  changing the page mid-conversation is now reflected. The page is also told
  when its data changes (§5a). `provideContext` still behaves the old way; use
  `setPageState`.
- **The agent does not remember previous conversations.** A user can see and
  resume their prior chats, but each is its own transcript; a new conversation
  starts cold.
- **canopy's own pages are the only production host so far** (§11). That
  exercises everything except the cross-origin boundary, so the first
  third-party host is still the first real test of that. `scripts/e2e_embed_widget.py`
  walks the live chain against a deployment.

---

## 10. Reference: relationship to CopilotKit

CopilotKit is the closest off-the-shelf equivalent, and knowing the difference
sets expectations.

| CopilotKit | Here | Difference |
| --- | --- | --- |
| `useCopilotReadable` | `setPageState` | Both push on change. Ours carries a selection (ids + the tool that resolves them) rather than data, and is capped at 8 KiB to keep it that way |
| `useCopilotAction` | `registerAction` | Both take a JSON-Schema. Theirs adds in-chat rendering and human-in-the-loop confirmation; ours surfaces as an MCP tool, so any MCP agent can call it |
| `CopilotPopup` / `CopilotSidebar` | `mode: 'overlay' / 'docked'` | Equivalent, but framework-free rather than React components |
| `CopilotRuntime` → an LLM | a canopy `Agent` on a runner | **The big one.** Theirs orchestrates a model call. Ours routes to a persistent agent with its own identity, mailbox, repo, credentials and tools |

If you want a page-scoped copilot in a React app, CopilotKit is more complete
and you should probably use it. The reason to use this instead is that the thing
on the other end is a *canopy agent* — the same one that runs scheduled work and
has its own inbox — not a chat completion bound to your page.

---

## 11. Reference: canopy embedding its own pages

canopy is also a host, and **not a special one**. The reason to talk to an agent
is usually about what is in front of you, and `/w/:ws/chat` is not in front of
you — so the widget mounts on canopy's own authenticated pages, where two real
cases live: an agent inbox that has gone stale, and a feature set you decide to
deprecate while looking at it and have forgotten a minute later.

**Setup is the ordinary setup, plus one tick.** Connect a site as in step 1 and
tick **"Show this panel on canopy's own pages"**. There is no separate flow, no
reserved name and no deployment setting: canopy's own panel is whichever
connected site an owner ticked that box on, and usually that site is canopy
itself. Only one app may have it at a time, and the refusal names the one that
already does.

Ticking it also adds canopy's own origin to that app's URL list, because the two
are not independent: `frame-ancestors` is built from that list, so a ticked app
without it would be on by every visible measure and dead in the browser. The
origin comes from the request rather than a field — it is the address you are
looking at, and the one value that cannot be typed wrong.

Steps 2 and 3 still do not apply when the site *is* canopy. A third-party host
signs an assertion because canopy cannot see who its visitor is; here the two
are one process, so `POST /api/embed/token` is session-authenticated and mints
for `request.user` directly. No secret, no signing key, no backend endpoint.

**What this does and does not prove.** The frame is same-origin here, so none of
the origin discipline is exercised — not `targetOrigin`, not `event.origin`
rejection, not storage partitioning. What it does exercise is everything above
that: the handshake, the agent picker, session creation and history, page
state, invalidation, page actions, and whether the panel is pleasant to use. A real
third-party host is still the only test of the boundary.

---

## 12. Reference: building your own UI instead of the widget

The widget is the supported path, and almost always the right one: it needs no
install, no build step and no framework, and it isolates canopy's UI in an
iframe. Build your own only if a chat panel in an iframe genuinely cannot work
for your page.

| package | what it is | needs |
| --- | --- | --- |
| [`canopy-client`](https://www.npmjs.com/package/canopy-client) | the transport: token cache, REST calls, the session socket URL, the host bridge | nothing — zero dependencies, ships JavaScript (use ≥ 0.2.0) |
| [`canopy-ui`](https://www.npmjs.com/package/canopy-ui) | React components: the chat panel, `useSessionSocket` (AG-UI on the wire), design tokens | React 19 as a peer, and a bundler that compiles TypeScript in `node_modules` (Vite does; webpack + babel with `exclude: /node_modules/` does not) |

Neither imports the other. A React 18 or non-React host uses `canopy-client`
and renders its own messages; a React 19 + Vite host can use both. Everything
in §1–§3 still applies — the token endpoint, the registered origins and agents
— and sessions are still created by your backend, never by the browser.
