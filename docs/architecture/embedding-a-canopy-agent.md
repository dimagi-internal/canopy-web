# Embedding a canopy agent in your product

**This document is the whole task.** Do the six steps in order, then run the
checks in [§6](#6-check-that-it-works). You should not need anything else — if
you do, that is a bug in this document, and saying so is more useful than
working around it.

**What you are building:** a chat panel on one of your pages, talking to a
canopy agent that can read the page state you choose to hand it. The user is
your already-signed-in user, and the agent can only ever see what that user can
see.

(Need an agent's answer as data — your backend sends it a task and gets JSON that
validates against your schema, with no panel? That is an agent task:
[agent-tasks.md](agent-tasks.md).)

**Three things to know before you start, because they change what you build:**

1. **Agent actions work, and they are MCP tools.** What you declare with
   `registerAction` appears in the agent's tool list with your JSON-Schema —
   you do not implement MCP, canopy translates. They run **only while the page
   is open**; see [§7](embedding-actions-and-access.md#7-how-actions-reach-the-agent-and-what-they-cannot-do) for what that rules out.
2. **Register your local dev origin too** (`http://localhost:8000`, or whatever
   you serve on), or the widget will not load on your machine. See
   [§1](#1-register-your-app-in-canopy).
3. **Your visitor is a *contact* unless they already have a canopy account in
   the site's workspace ([§3](#3-add-one-backend-endpoint)), and the agent's calls
   into your product run as the agent unless your site issues a grant for the
   visitor** (`id_jag`, host grant contract v1), in which case they run as the
   visitor. Both matter for what data the agent can reach. Read
   [§8a](embedding-actions-and-access.md#8a-who-the-agent-acts-as) before you give
   an agent a tool that reads anything sensitive.

Beyond the six steps: [embedding-advanced.md](embedding-advanced.md) (5a-5e), [embedding-actions-and-access.md](embedding-actions-and-access.md) (7, 8, 8a, 8b) and [embedding-reference.md](embedding-reference.md) (9-12). canopy's side is [embedding-internals.md](embedding-internals.md).

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

**On canopy's side, step 1 needs a second record:** the agent's own owner or admin adds your site under the
agent's Read mode and host sites (what a visitor there may do). Without it the site-agents API answers
`ready: false` ("is not connected to <site>"). The two records, and how they combine, are
[host-sites.md](host-sites.md).

---

## 1. Register your app in canopy

Canopy has to know your app exists, which origins may frame the widget, and
which agents it may offer. **All three fail closed**: "nothing happens" almost
always means one of them is missing.

Go to **Host sites** in your workspace, **`/w/<workspace>/settings/host-sites`**,
as a workspace owner (`frontend/src/pages/HostSitesPage.tsx`). Type your site's
address and click **Fill from the site**: canopy reads the site's signed
`site.json` (origins, key location, sign-in issuer, MCP server), checks the
signature against the keys the document names, and fills the form. You check the
values and click **Connect**; nothing is saved until you do, because a site must
never set its own trusted origins. Where canopy looks, and what it accepts:
`apps/host_grants/api.py::discover_site` and `apps/host_grants/site_needs.py`.

The fields are `CreateIn` / `UpdateIn` in `apps/embedding/host_sites_api.py`.
What to get right, because each fails closed and silently:

- **Name** is the `app` you pass to `canopy.init`; a mismatch 404s the frame.
- **Site URLs** are the origins allowed to frame the widget. Include every
  environment, including your local dev origin (for example
  `http://localhost:8000`). Empty means the frame 404s by design: an exempt page
  with no `frame-ancestors` would be frameable by any site. A wildcard, a path
  or a `;` is refused on save (a wildcard would undo the framing protection); a
  trailing slash is trimmed.
- **Agents it may offer**: none means the picker offers nothing. The site's
  default agent (`default_agent`) must be one of them; what outranks it is
  [§5c](embedding-advanced.md#5c-a-page-that-chooses-its-own-agent).
- **Signing keys**: the PUBLIC half of a key pair your server signs visitor
  statements with ([§3](#3-add-one-backend-endpoint)); several may be pasted
  during a rotation. Missing means your site can vouch for nobody. There is no
  shared secret to copy: canopy holds nothing that could impersonate your site,
  and a signed statement names one visitor, expires in two minutes, works once,
  and cannot be forged from canopy's database.

On canopy's side, the agent's owner or admin must also add your site under the
agent's Read mode and host sites; the two records are
[host-sites.md](host-sites.md). Canopy's own pages are the same form with one
tick ([§11](embedding-reference.md#11-reference-canopy-embedding-its-own-pages)).

**By API or MCP**: `POST` and `PATCH /api/workspaces/<ws>/host-sites[/<id>]`
(MCP `register_host_site`, `update_host_site`) take the same fields with the same
validation, `GET …/host-sites/discover?address=…` (MCP `discover_site`) reads the
site's declared facts, and each change is recorded in the settings audit
([settings-audit.md](settings-audit.md)). Registering a site, and changing what
it may sign (`public_keys`, `jwks_url`, `host_issuer`), need a person at a
browser who just confirmed with their passkey: a token (a PAT, an MCP call) is
refused those and may change only the rest. There is no management command for
any of it, because a command would skip the validation and the audit.

### Serving more than one canopy workspace

**Each workspace registers your site itself**, on its own host sites page: its
own name for it, your JWKS URL, the URLs that may frame it, which of *its* agents
it may offer. Nothing one workspace does reaches another's registration. Give
every workspace the same JWKS URL and a key rotation needs no change in any of
them.

**Name the workspace.** A site's name is unique only *within* a workspace, so the
name alone does not say which registration verifies your assertion. Set
`CANOPY_HOST["WORKSPACE"]` (the SDK signs it as `canopy_workspace`; `?workspace=`
does the same on the embed shell URL; without the SDK add the claim to what you
sign). Naming an agent also works (`agent_slug` on `POST /api/auth/contact-token`,
`agent` in `canopy.init`) but breaks when that agent is deleted or moved. When
both are sent they must agree, or canopy answers `409 workspace_mismatch`.
Naming neither works only while one workspace has a live site by that name; once
a second registers it, an unnamed mint gets `409 ambiguous_issuer` and an
unnamed shell a 404, rather than a guess that routes a visitor into the wrong
tenant (`apps/embedding/assertions.py`, `apps/contacts/contact_api.py`).

A visitor of yours who talks to agents in two workspaces is **two contacts**, one
per workspace, and you mint a token for each: merging what two tenants know about
somebody would leak one's dealings into the other. Canopy records that the two
are the same person (`contacts.models.Person`, keyed on your `sub` and on your
keys via `AppCredential.signer`; two registrations are the same system exactly
when they trust the same JWKS URL or pasted keys), but nothing reads across that
link and neither tenant can see the other's record.

---

## 2. Keep the private key private

Generate a key pair on your side (Ed25519 is the simplest):

```bash
openssl genpkey -algorithm ed25519 -out canopy-signing.pem
openssl pkey -in canopy-signing.pem -pubout
```

The **private** key goes in your secret store and stays on your server; whoever
holds it can vouch for visitors to your site. Never send it to a browser. The
public half is what canopy checks signatures against.

**Publish a JWKS and give canopy its URL** rather than pasting a key:

```json
{ "keys": [ { "kty": "OKP", "crv": "Ed25519", "x": "…", "use": "sig",
              "alg": "EdDSA", "kid": "2026-09-a" } ] }
```

Rotation then costs you nothing here: publish the new key beside the old, switch
what you sign with, drop the old one later. Canopy follows by `kid` and refetches
the moment it meets one it has not seen (`apps/embedding/jwks.py::keys_for`).
The URL is checked when you save: **https**, publicly routable (a VPC-internal
or localhost URL is refused), no redirects, at most 64 KiB (`jwks.py`).

A pasted key behaves identically, but every rotation means coming back to paste
the new one (all pasted keys verify until you remove the old).

---

## 3. Add one backend endpoint

Your server vouches for the person looking at the page, and canopy hands back a
**short-lived token for them**. This is the one piece that cannot live in a
browser. You vouch by **signing a statement**, not by presenting a shared secret:
a signature proves *this* claim, about *this* visitor, at *this* moment, and
canopy could not forge one.

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
            # Optional. `name` is descriptive. `email` + `email_verified` let a
            # visitor who ALREADY has a canopy account (and is a member of the
            # site's workspace) arrive as that account, only when YOU verified
            # the address. Never creates an account.
            "name": user.get_full_name(),
            "email": user.email,
            "email_verified": True,                 # only if your product verified it
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
    # Pick fields explicitly rather than passing canopy's dict through.
    return JsonResponse({
        "token": vouched["token"],
        "expires_at": vouched["expires_at"],
        # "user" (a visitor with a canopy account, arriving as themselves) or
        # "contact". Hand it to canopy-client's fetchToken as `kind`.
        "kind": vouched.get("kind", "contact"),
    })
```

> **`sub` must be your own id for the signed-in user and nothing else.** You are
> asserting "this is a real person on my site"; taking `sub` from the request
> body would let any caller be anybody.

**Requirements canopy enforces** (the wire contract is
[sdk/python/README.md](../../sdk/python/README.md#host-grant-contract-v1);
canopy's side is `apps/embedding/assertions.py`): `EdDSA`, `ES256` or `RS256`
only (never HMAC: the key is public, so a symmetric algorithm would let anyone
sign); `aud` must match; `exp` at most 120 seconds out; each `jti` works once.

**What the visitor becomes** (`contacts.services.resolve_arrival`). If the
assertion carries `email` with `email_verified: true` for an address that exactly
one active canopy user provably holds, **and that user is a member of the site's
workspace**, they arrive as that user, with their own permissions; arrival never
creates an account. Anyone else is a `Contact` in the workspace that owns your
app: not a member of anything, able to talk to the agents you were allowed to
offer and see their own conversations, and nothing else. `/api/contact/` is the
entire surface a contact token reaches. The token comes back with `kind` saying
which happened. Your ids live in your own namespace, `(your app, your id)`, so
they cannot collide with a canopy user or another site's people.

**Either way, [§5](#5-hand-the-agent-the-page-state)'s page state works**: a
contact declares theirs on the same prefix, so do not try to make visitors arrive
as users just to make the panel page-aware.

The widget calls this endpoint with `credentials: 'same-origin'` and sends an
`X-CSRFToken` header if the cookie is readable, so your normal session auth and
CSRF protection apply unchanged. If your CSRF cookie is not named `csrftoken`
(a Django app under a path prefix on a shared host must rename it), pass
`csrfCookieName` in step 4; if JavaScript cannot read it at all
(`CSRF_COOKIE_HTTPONLY = True`), pass `csrfToken` instead, a **function** wherever
the value can change within a page's life (Django rotates it on login):

```js
canopy.init({
  // …
  csrfToken: () => document.querySelector('[name=csrfmiddlewaretoken]').value,
})
```

`csrfToken` wins over `csrfCookieName`. Leave both wrong and the header is
absent: the mint 403s and the widget never starts with nothing on the page to say
why. Do not set `CSRF_COOKIE_HTTPONLY = False` to make the cookie route work;
that weakens a global setting for one widget.

---

## 4. Add the script tag

```html
<script src="https://canopy.dimagi.com/embed/widget.js"></script>
<script>
  var widget = canopy.init({
    baseUrl: 'https://canopy.dimagi.com',
    app: 'connect-labs',              // the Name from step 1
    tokenUrl: '/labs/canopy/token',   // the endpoint from step 3
    mode: 'docked',
    launcherLabel: 'Canopy AI',        // what YOUR people call it
    // Only if your CSRF cookie is not named `csrftoken`:
    // csrfCookieName: 'csrftoken_labs',
    // Or, if JS cannot read the cookie at all (CSRF_COOKIE_HTTPONLY):
    // csrfToken: () => document.querySelector('[name=csrfmiddlewaretoken]').value,
  })
</script>
```

No npm, no React, no build step: the loader imports nothing and the chat UI runs
inside an iframe with its own React, so it cannot collide with what your page
loads (`frontend/packages/canopy-widget/`). **Every option is defined, with its
default, in `CanopyWidgetOptions` in
`frontend/packages/canopy-widget/src/index.ts`**; `inline` mode requires `target`
and is always open, since there is no launcher to reopen it.

**The panel never shows tool calls, and there is no option to.** canopy decides
this from the widget's token (a site's delegated token or a contact token), never
from anything the page can ask for, and withholds the calls on every path the
panel reads a conversation through (`apps/canopy_sessions/consumers.py::hide_tools`
for the socket). The transcript keeps every call, so canopy's own chat page shows
them.

### Match it to your brand

Two halves, two mechanisms:

| Half | Where it lives | What reaches it |
| --- | --- | --- |
| **Launcher** (the bubble, its ×, the panel's frame) | your page, inside a shadow root | CSS variables, `::part()`, or `theme` |
| **Panel contents** (messages, composer, Send) | canopy's own document, in the iframe | **only** `theme`; no CSS of yours can reach it |

The shadow root is deliberate: a global `button { … }` on your page must not
deform the launcher. Every theme value is validated twice (in the loader and again
inside canopy's document); a value that is not a colour, length or font-family,
or that contains `;`, `{`, `}`, `url(` and the like, is dropped with a
`console.warn` naming it. Fields and rules: `frontend/packages/canopy-widget/src/theme.ts`.

**1. `theme` in `canopy.init`: both halves.**

```js
canopy.init({
  // …
  theme: {
    mode: 'light',          // 'light' | 'dark' (default) | 'auto' (follows the OS)
    accent: '#2563eb',      // launcher, Send button, links, focus rings
    // accentForeground: '#fff',  // text on the accent; derived if you omit it
    radius: 8,              // px, or any CSS length
    font: 'Inter, system-ui, sans-serif',
  },
})
```

`mode` rides the shell URL so the panel paints in the right mode from its first
frame. `widget.setTheme({...})` re-themes later and **replaces** the whole theme.
**The font has to exist inside the iframe too**: a webfont your page loads is not
visible in canopy's document, so name a system stack or a font canopy also serves.

**2. CSS variables: the launcher only, no JS.** Custom properties cross a shadow
boundary; a `theme` passed to `canopy.init` wins over the same variable:

```css
:root {
  --canopy-accent: #2563eb;
  --canopy-accent-foreground: #fff;
  --canopy-radius: 8px;            /* the panel frame AND the bubble */
  --canopy-launcher-radius: 24px;  /* just the bubble, e.g. keep it a pill */
  --canopy-font: Inter, system-ui, sans-serif;
  --canopy-panel-background: #fff; /* the panel frame behind the iframe */
  --canopy-panel-border: #e5e7eb;
  --canopy-focus-ring: #1d4ed8;
}
```

**3. `::part()`: anything else about the launcher.**

```css
[data-canopy-widget]::part(launcher) { height: 40px; box-shadow: none; }
[data-canopy-widget]::part(dismiss)  { display: none; }  /* prefer dismissible: false */
[data-canopy-widget]::part(panel)    { border-radius: 16px; }
[data-canopy-widget]::part(resize)   { display: none; }  /* no corner resizing */
[data-canopy-widget]::part(divider)  { display: none; }  /* no resizing a pinned rail */
```

**Pick a mode:** `overlay` (default; floating bubble and panel), `docked`
(full-height right rail) or `inline` (fills a `target`, no launcher).

The returned object has `open()`, `close()`, `toggle()`, `isOpen()`,
`dismiss()`, `isDismissed()`, `expand()`, `collapse()`, `isExpanded()`,
`snap()`, `unsnap()`, `isSnapped()`,
`setPageState()`, `registerAction()`, `unregisterAction()`, `setTheme()`,
`setLauncherVisible()`, `ask()`, `openSession()`, `currentSession()` ([§5e](embedding-advanced.md#5e-keeping-a-pointer-to-a-conversation-and-opening-it-again)), `destroy()`, and `provideContext()` ([§5](#5-hand-the-agent-the-page-state)); each is documented on the `CanopyWidget` interface in `index.ts`. Callbacks: `onInvalidate`
([§5a](embedding-advanced.md#5a-being-told-when-your-data-changes)), `onSession`
([§5e](embedding-advanced.md#5e-keeping-a-pointer-to-a-conversation-and-opening-it-again)),
`onResize`; the agent a page prefers is `agent` ([§5c](embedding-advanced.md#5c-a-page-that-chooses-its-own-agent)).

Two behaviours worth knowing: `setLauncherVisible(false)` is the **host's**
reversible way to hide the bubble on a page where it is wrong, unlike
`dismiss()` (the visitor's choice for the page load); and `ask(text)` opens the
panel and sends `text` as the first message of a new conversation (trimmed,
capped at `MAX_ASK_CHARS` in `protocol.ts`), so feature-detect it
(`typeof widget.ask === 'function'`) and fall back to `open()` for older widgets.

---

## 5. Hand the agent the page state

`provideContext` still works, but it is a snapshot: read once when a conversation
opens, delivered as prose, attached to the first message only. Declare your page
instead:

```js
widget.setPageState({
  resource: 'stock://',                 // WHAT you are showing (an MCP resource URI)
  backing_tool: 'stock_on_hand',        // the tool that resolves these rows
  visible_ids: [4821, 4822, 4823],      // WHICH rows are on screen
  filters: { period, commodity },       // what the user narrowed to
})
```

Push it whenever the view changes; it replaces wholesale. `resource` is what
canopy keys invalidation on ([§5a](embedding-advanced.md#5a-being-told-when-your-data-changes),
`apps/canopy_sessions/invalidation.py`), so spell it the same in `onInvalidate`.

### Send the selection, not the data

`visible_ids` plus `backing_tool` says *which rows* and *where to read them*; the
agent then calls that tool itself, live, with the caller's own permissions
applied. Serialising the rows duplicates your API, goes stale between render and
send, and gives you a second place to get access control wrong. **canopy refuses
a state larger than 8 KiB** (`apps/canopy_sessions/page_state.py::MAX_STATE_BYTES`):
generous for several hundred ids, deliberately too small for the rows behind them.

### What the agent does with it

Two paths, and the redundancy is the point:

1. **It is in the agent's context from the first turn.** canopy puts your
   declared selection into the caller envelope and the canopy plugin hands it to
   the session as context alongside every message, not inside the person's
   message, so the transcript shows exactly what they typed.
2. **The MCP tool `current_page`** returns every attached page's state, so the
   agent re-reads your screen whenever it needs to.

Path 1 exists because path 2 can be unavailable: MCP servers connect
asynchronously and an agent whose MCP tools arrive deferred will not go looking
for `current_page` unprompted, so with path 2 alone the first reply is "I can't
see your screen".

Also:

- **Include identifiers, not labels**: `4821` lets the agent act on a row.
- **Never include secrets.** It lands in a transcript that persists.
- **Compute it from what this user can see.** It runs in their browser and
  session; if your RBAC hides a row, keep it out. "The agent sees exactly what the
  user sees" is the whole access-control story, and keeping it true is yours.

Declare nothing and the agent starts without page state.

### AG-UI: one call instead of two

If you already speak AG-UI, send its own object:

```
PUT /api/canopy-sessions/{id}/run-input     <- AG-UI RunAgentInput
```

`state` becomes your page state, `tools` become your page actions. Fields canopy
cannot honour (`messages`, `run_id`, `resume`, `forwarded_props`) are accepted
and ignored (`apps/canopy_sessions/api_page.py::declare_run_input`).

The stream coming back can be AG-UI too: connect the session socket with
`?protocol=ag-ui` (what canopy's own chat page and widget do, via `canopy-ui`
≥ 0.9 `useSessionSocket({ protocol: 'ag-ui' })`). Canopy-only events (co-edited
drafts, presence, page actions, "did my stop land") are `CUSTOM` events named
`canopy.<event>`; where AG-UI's spelling is lossy (the connect snapshot, a
stream error, a blocked agent's dialog) the original frame rides alongside under
`metadata.canopy.frame` (`apps/canopy_sessions/agui.py`). Ask for nothing and you
get canopy's native frames.

The EVENTS are AG-UI 1.0 but the TRANSPORT is canopy's: AG-UI's own clients
expect one HTTP request per run over SSE, and canopy's socket is a long-lived
multiplayer session. Point `canopy-ui` (or your own reader) at the socket, not a
stock AG-UI `HttpAgent`, which will reject the stream.

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
| Reopening later shows history | your prior conversations with that agent, on this host only ([§5b](embedding-advanced.md#5b-reading-back-a-users-history)) |

`scripts/e2e_embed_widget.py` walks the same chain against a deployment.

### If something does not work

| Symptom | Cause |
| --- | --- |
| Blank panel; console shows a frame refused to load | your origin is not in the site's URLs |
| Panel says "not configured correctly" | same cause: the shell rendered with no origins |
| "No agent is available here yet" | the site offers no agent, or this user is not a member of that agent's workspace |
| A token error in the panel | your endpoint 403'd, 500'd, or returned no `token` |
| The mint 403s and no `X-CSRFToken` was sent | your CSRF cookie is renamed (`csrfCookieName`) or `HttpOnly` (`csrfToken`) ([§3](#3-add-one-backend-endpoint)) |
| 404 on `/embed/chat` | app name mismatch, site revoked, or no frame origins (`apps/embedding/views_embed.py::_shell`) |
| 503 on `/embed/widget.js` | canopy's frontend is not built |
| Agent replies but knows nothing about the page | no `setPageState` call, or it ran after the first message was sent |
| Agent acts on rows the user cannot see | you sent rows instead of ids: send `visible_ids` + `backing_tool` |
| Page shows rows the agent already deleted | no `onInvalidate` in `canopy.init`, or its `resource` differs from the one in `setPageState` |
| Message sends but no reply ever arrives | no runner is online for that agent (canopy-side) |

---
