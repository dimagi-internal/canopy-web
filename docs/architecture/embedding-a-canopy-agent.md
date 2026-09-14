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

Design rationale lives in
[`../superpowers/specs/2026-09-12-embedded-agent-widget-v2-design.md`](../superpowers/specs/2026-09-12-embedded-agent-widget-v2-design.md).
You do not need it to do this.

---

## The six steps

| # | Step | Where it happens |
| --- | --- | --- |
| [1](#1-register-your-app-in-canopy) | Register the app: credential, frame origins, allowed agents | canopy admin |
| [2](#2-store-the-credential) | Put the raw credential in your secret store | your infra |
| [3](#3-add-one-backend-endpoint) | One backend endpoint that mints a per-user token | your repo |
| [4](#4-add-the-script-tag) | The script tag and `canopy.init` | your repo |
| [5](#5-hand-the-agent-the-page-state) | `provideContext` with what the user is looking at | your repo |
| [6](#6-check-that-it-works) | Check it works | a browser |

Steps 1–2 are one-off per environment. Steps 3–5 are the only code you write.

---

## 1. Register your app in canopy

Canopy has to know your app exists, which origins may frame the widget, and
which agents it may offer. **All three fail closed** — which is why "nothing
happens" almost always means one of them is missing rather than something being
broken.

Go to **`/admin/tokens/appcredential/add/`** on your canopy deployment, as a
staff user:

| Field | What it is | If you get it wrong |
| --- | --- | --- |
| **Name** | The `app` value you pass to `canopy.init`, e.g. `connect-labs` | Mismatch ⇒ the widget's frame 404s |
| **Allowed delegation domains** | Email domains this app may vouch for, e.g. `["dimagi.com"]` | Empty ⇒ the app can mint tokens for nobody |
| **Allowed frame origins** | Origins that may frame the widget. **Include every environment** — production, staging, and your local dev origin | Empty ⇒ the frame 404s by design; an exempt page with no `frame-ancestors` would be frameable by any site |
| **Allowed agents** (inline) | Which agents this app may offer | Empty ⇒ the picker offers nothing |
| **Provision workspace / role** | Optional: the tenant a brand-new user lands in | Leave blank unless *every* user of your host should be trusted in that tenant |

Origins are validated on save. A wildcard, a path, or anything containing `;` is
refused with an explanation — a wildcard in particular would undo the entire
framing protection.

**Alternatively, by command** (local or scripted setup — note a deployment has
no shell to run these in, since `EnableExecuteCommand` is off on the service and
the database is VPC-internal):

```bash
uv run python manage.py create_app_credential --name connect-labs --domains dimagi.com
uv run python manage.py grant_app_frame_origin --name connect-labs \
    --origin https://labs.connect.dimagi.com
uv run python manage.py grant_app_frame_origin --name connect-labs \
    --origin http://localhost:8000
uv run python manage.py grant_app_agent --name connect-labs --agent labs-helper
uv run python manage.py grant_app_agent --name connect-labs --list
```

---

## 2. Store the credential

**The raw credential is shown once, in a banner, when you save.** It is stored
only as a hash and cannot be recovered — if you lose it you must create a new
app. Copy it straight into your secret store.

It is a **secret with real power**: it lets its holder mint a working canopy
token for any user in its allowed domains. Treat it like a database password,
and never send it to a browser.

Editing the app later never re-mints it, so you can add origins and agents
freely without breaking a deployment.

---

## 3. Add one backend endpoint

Your server holds the credential and exchanges it for a **short-lived token
scoped to the signed-in user**. This is the one piece that cannot live in a
browser, which is why "one script tag" is really "one script tag plus one
endpoint."

```python
# your_app/canopy.py
import json, urllib.request
from django.conf import settings

def exchange_token(email: str, ttl: int = 3600) -> dict:
    req = urllib.request.Request(
        f"{settings.CANOPY_BASE_URL}/api/auth/token-exchange",
        data=json.dumps({"acting_as_email": email, "ttl_seconds": ttl}).encode(),
        headers={
            "Content-Type": "application/json",
            # The app credential. Server-side only.
            "Authorization": f"Bearer {settings.CANOPY_APP_CREDENTIAL}",
        },
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
    exchanged = exchange_token(request.user.email)
    # Pick fields explicitly rather than passing canopy's dict through — a new
    # field on canopy's side should not break your endpoint.
    return JsonResponse({
        "token": exchanged["token"],
        "expires_at": exchanged["expires_at"],
    })
```

> **`acting_as_email` must be `request.user.email` and nothing else.**
> You are asserting "this is an authenticated user of mine," and canopy believes
> you because you hold the secret. Accepting an email from the request body
> would let any caller impersonate any user in your allowed domains.

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
    // Only if your CSRF cookie is not named `csrftoken`:
    // csrfCookieName: 'csrftoken_labs',
  })
</script>
```

No npm, no React, no build step, no framework. The loader is ~6 kB and imports
nothing; the chat UI runs inside an iframe with its own React, so it cannot
collide with anything your page already loads. That is what makes it work on a
page built from non-module scripts sharing a global scope.

**Pick a mode:**

| mode | shape | use when |
| --- | --- | --- |
| `overlay` | floating bubble + panel | you cannot change the page's layout |
| `docked` | full-height right rail | the page can give up a column |
| `inline` | fills a `target` element, no launcher | you want it inside your own layout |

`inline` requires `target` (a selector or element) and is always open — there is
no launcher to reopen it with.

**Other options:** `agent: 'labs-helper'` skips the picker; `metadata: {…}`
stamps opaque data on sessions the widget creates; plus `width`,
`launcherLabel`, `title`, `open: true`. The returned object has `open()`,
`close()`, `toggle()`, `isOpen()`, `provideContext()`, `registerAction()`,
`unregisterAction()` and `destroy()`.

---

## 5. Hand the agent the page state

```js
widget.provideContext(() => ({
  supplyPoint: currentSupplyPoint,     // ids the agent can look things up by
  stockOnHand: visibleRows,            // what is on screen
  filters: { period, commodity },      // what the user narrowed to
}))
```

Called **once, when a conversation opens** — a snapshot, not a subscription. It
is read at request time, so a plain closure over your current state is correct;
you do not need to re-register when things change.

What actually matters here:

- **Include identifiers, not just labels.** "Kano warehouse" lets the agent talk
  about it. `supply_point_id: 4821` lets it look it up.
- **Send what is on screen, not the whole dataset.** The snapshot is capped at
  8 000 characters and truncates. If you are near that, you are sending a
  database rather than a context.
- **Never include secrets.** It lands in a conversation transcript that
  persists.
- **Compute it from what this user can see.** The callback runs in their
  browser, in their session — so if your RBAC hides a row from them, do not put
  it in the snapshot. "The agent sees exactly what the user sees" is the whole
  access-control story, and keeping it true is the host's job.

Register nothing and the agent simply starts without page context. If your
callback throws, the same — it will not break the conversation.

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
| Reopening later shows history | your prior conversations with that agent |

### If something does not work

| Symptom | Cause |
| --- | --- |
| Blank panel; console shows a frame refused to load | your origin is not in **Allowed frame origins** |
| Panel says "not configured correctly" | same cause — the shell rendered with no origins |
| "No agent is available here yet" | no **Allowed agents** row, or this user is not a member of that agent's workspace |
| A token error in the panel | your endpoint 403'd, 500'd, or returned no `token` |
| 404 on `/embed/chat` | app name mismatch, credential revoked, or no frame origins |
| 503 on `/embed/widget.js` | canopy's frontend is not built |
| Agent replies but knows nothing about the page | `provideContext` not registered, or registered after the conversation opened |
| Message sends but no reply ever arrives | no runner is online for that agent — a canopy-side operational issue, not yours |

---

## 7. How actions reach the agent, and what they cannot do

You declare an action once, in JS:

```js
widget.registerAction('dismissInsights', async ({ ids }) => { … }, {
  description: 'Dismiss insights from the list the user is viewing',
  parameters: {
    type: 'object',
    properties: { ids: { type: 'array', items: { type: 'integer' } } },
    required: ['ids'],
  },
})
```

canopy turns that into an **MCP tool** (`page_dismissInsights`) on its own MCP
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
| 5 | Your page mints a token | Your own session auth. You send your credential plus `request.user.email` |
| 6 | Canopy `token-exchange` | Credential live and unrevoked; rate limit; email domain allowed; account active. Then finds-or-creates the canopy user, applies provisioning if configured, and issues an opaque token |
| 7 | Token is posted into the frame | `targetOrigin` is canopy's origin, never `*`. The token never appears in a URL |
| 8 | Frame calls canopy | **Every** request re-resolves the token against the database, with expiry checked, and records which app is acting |
| 9 | `GET /api/embed/agents` | The app comes from the **token**, not a parameter. Returns allowlisted agents **∩** workspaces this user belongs to. Both required |
| 10 | Session create | Workspace from the user's memberships; the agent must belong to it; the user becomes the owner; the acting app is stamped server-side |
| 11 | Reading a session | Workspace membership **and** (you created it, or you are a participant, or it is runner-discovered). A co-tenant holding the id cannot read your chat |
| 12 | WebSocket | The same token, on the query string, since a WS handshake carries no headers |
| 13 | Context | Runs in the user's browser, in their session — so the agent sees what that user sees, never more |

**The property that matters:** nothing is frozen into the token. Tokens are
opaque random strings stored as hashes, and memberships are re-read from the
database on every request — revoke a membership and the next call reflects it.
A JWT would have frozen the ACL at mint time, which is weaker.

**What the agent never gets:** any credential for your product. It runs under
its own identity and reaches your page only through the bridge, only while the
page is open.

---

## 9. Reference: known limits

- **Actions and context both need the page open** (§7). There is no
  server-side path for an agent to act on your page later.
- **The domain allowlist is narrow today.** Token exchange requires the email's
  domain in both your credential's allowed domains *and* canopy's own login
  allowlist, which is currently Dimagi-only. A partner user on another domain
  gets a 403 and the widget never opens. Widening it is a deliberate policy
  change.
- **Context is a snapshot.** If the user changes the page mid-conversation, the
  agent still holds the state from when it opened.
- **The agent does not remember previous conversations.** A user can see and
  resume their prior chats, but each is its own transcript; a new conversation
  starts cold.
- **None of this has run in a browser yet.** Every test is in-process. Expect
  real fixes on the first embedding — yours may well be it.

---

## 10. Reference: relationship to CopilotKit

CopilotKit is the closest off-the-shelf equivalent, and knowing the difference
sets expectations.

| CopilotKit | Here | Difference |
| --- | --- | --- |
| `useCopilotReadable` | `provideContext` | Theirs is per-component and continuously synced; ours is one page-level snapshot at open |
| `useCopilotAction` | `registerAction` | Both take a JSON-Schema. Theirs adds in-chat rendering and human-in-the-loop confirmation; ours surfaces as an MCP tool, so any MCP agent can call it |
| `CopilotPopup` / `CopilotSidebar` | `mode: 'overlay' / 'docked'` | Equivalent, but framework-free rather than React components |
| `CopilotRuntime` → an LLM | a canopy `Agent` on a runner | **The big one.** Theirs orchestrates a model call. Ours routes to a persistent agent with its own identity, mailbox, repo, credentials and tools |

If you want a page-scoped copilot in a React app, CopilotKit is more complete
and you should probably use it. The reason to use this instead is that the thing
on the other end is a *canopy agent* — the same one that runs scheduled work and
has its own inbox — not a chat completion bound to your page.
