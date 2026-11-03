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
3. **Your visitor talks to the agent as a *contact*, not as their canopy
   account — and the agent's calls into your product run as the agent unless
   your site issues a grant for the visitor** (`id_jag`, host grant contract
   v1), in which case they run as the visitor. Both matter for what data the
   agent can reach. Read [§8a](embedding-actions-and-access.md#8a-who-the-agent-acts-as) before you give an
   agent a tool that reads anything sensitive.

Beyond the six steps: [embedding-advanced.md](embedding-advanced.md) (5a-5e), [embedding-actions-and-access.md](embedding-actions-and-access.md) (7, 8, 8a, 8b) and [embedding-reference.md](embedding-reference.md) (9-12). canopy's side is [embedding-internals.md](embedding-internals.md).

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
**`/w/<workspace>/settings/connected-apps`** — as a workspace owner.

**Type your site's address and click "Fill from the site".** A site on
`dimagi-canopy` ≥ 0.16 publishes its own facts — the origins that frame the
panel, where its keys are, its sign-in issuer and its MCP server — in the `site`
block of its signed `site.json`. canopy looks for it beside the address you typed
(`…/site.json`), at `/.well-known/canopy-site.json` on its origin, and at
`…/canopy/site.json` (the SDK's default mount), checks the signature against the
keys the document names, and fills the form; the name comes from the document
too. You check the values and click **Connect**. Nothing is saved until you do —
a site must never be able to set its own trusted origins. On an already
connected site, **Fill from the site's own documents** shows what the site says
beside what canopy holds, marks what would change, and saves only on **Save these
values**, through the same audited update as a hand edit.

The fields, whether filled or typed:

| Field | What it is | If you get it wrong |
| --- | --- | --- |
| **Name** | The `app` value you pass to `canopy.init`, e.g. `connect-labs` | Mismatch ⇒ the widget's frame 404s |
| **Site URLs** | The origins allowed to frame the widget. **Include every environment** — production, staging, and your local dev origin | Empty ⇒ the frame 404s by design; an exempt page with no `frame-ancestors` would be frameable by any site |
| **Agents it may offer** | Which of this workspace's agents this site may offer | None ⇒ the picker offers nothing |
| **Opens on** | The site's default agent, one of those it offers (`default_agent` on `PATCH …/connected-apps/{id}`). See [§5c](embedding-advanced.md#5c-a-page-that-chooses-its-own-agent) for what outranks it | None ⇒ your `AGENT_SLUG`, then the only agent, then the picker |
| **Signing key** | The PUBLIC half of a key pair your server signs visitor statements with ([§3](#3-add-one-backend-endpoint)). Several may be pasted during a rotation | Missing ⇒ your site can vouch for nobody, so the widget never gets a token |

There is **no secret** to copy. A site proves itself by signing ([§3](#3-add-one-backend-endpoint)), so canopy
holds nothing that could impersonate it.

URLs are validated on save. A wildcard, a path, or anything containing `;` is
refused with an explanation — a wildcard in particular would undo the entire
framing protection. A trailing slash is accepted and trimmed, because that is
what you get from an address bar.

> **Why a signing key and not "vouch for our email domain".** A checkbox letting
> whoever held the site's secret mint a token as *any* canopy user in your domain
> would make a static string that can speak for anybody — the weakest possible
> credential — so the page does not offer one. A signed
> statement names one visitor, expires in two minutes, works once, and cannot be
> forged by canopy or by anyone who steals canopy's database — it holds only the
> public half.

> **Why not the Django admin?** It is read-only for this. It is staff-only, so the person who wants to embed an agent could
> not do it; and every field here fails closed and silently, so a bare form
> produces a widget that never appears with nothing to say why. See [§11](embedding-reference.md#11-reference-canopy-embedding-its-own-pages) for
> canopy's own pages, where the whole thing is one button.

**Alternatively, by API or MCP**: the page is two routes,
`POST /api/workspaces/<ws>/connected-apps` (MCP `connect_app`) and
`PATCH /api/workspaces/<ws>/connected-apps/<id>` (MCP `update_connected_app`), which take
the same fields — `origins`, `agents`, `public_keys` or `jwks_url` — with the
same validation; `GET /api/workspaces/<ws>/connected-apps/discover?address=…`
(MCP `discover_site`, add `app_id` for a connected site) reads the site's
declared facts for you to pass to them, and record each change in the settings audit
(`settings-audit.md`). Registering a site, and changing what it may sign (its
`public_keys`, `jwks_url` or `host_issuer`), need a person at a browser who just
confirmed with their passkey: a site's signed email names a canopy member, so a
token (a PAT, an MCP call) is refused those and may change only the rest. There is
no management command for any of it: a command would skip both. For local setup,
sign in and use the page.

### Serving more than one canopy workspace

**Each workspace registers your site itself**, on its own Connected sites page:
its own name for it, your JWKS URL, the URLs that may frame it, and which of
*its* agents it may offer. There is no shared registration and no workspace
that "owns" your site for the others.

So nothing one workspace does can reach another's integration: editing its
origins, disconnecting, or deleting the workspace touches its registration
alone. Give every workspace the same JWKS URL and rotating your key needs no
change in any of them.

**Name the workspace.** A site's name is unique only *within* a workspace —
two workspaces may each have a `connect-labs` — so the name alone does not say
which registration verifies your assertion. Set `CANOPY_HOST["WORKSPACE"]` to
the workspace's slug (`dimagi-canopy` ≥ 0.16): the SDK signs it as
`canopy_workspace` into every visitor assertion and site agents request, and
canopy selects that workspace's registration (`?workspace=` does the same on the
embed shell's URL). Without the SDK, add the claim to what you sign.

Naming an agent does it too — `agent_slug` on `POST /api/auth/contact-token`
(`AGENT_SLUG`), `agent` in `canopy.init` — because an agent belongs to exactly
one workspace. But it breaks the moment that agent is deleted or moved to
another workspace: canopy then cannot find your site, and the site agents API
answers nothing. Name the workspace and keep sending the agent for its other
jobs; when both are sent they must agree, and canopy refuses a disagreement with
`409 workspace_mismatch` rather than pick one.

Naming neither still works **while only one workspace has a live site by that
name**. The moment a second workspace registers the same name, an unnamed mint
gets a `409 ambiguous_issuer` and an unnamed shell a 404 rather than a guess:
picking one would route your visitor into a tenant you never meant.

One consequence worth expecting: a visitor of yours who talks to agents in two
workspaces is **two contacts**, one per workspace, and you mint a token for
each. That is deliberate — merging what two tenants know about somebody would
leak one's dealings into the other — and it is why the tenant is named at mint
time rather than inferred.

Canopy does record that the two are **the same person**, keyed on the `sub` you
sign (your own id for them, in your own namespace) *and on your keys* — two
workspaces' registrations are the same system exactly when they trust the same
JWKS URL (or the same pasted keys), since only you can sign for them. Register
one workspace by URL and another by pasted key and canopy will not link them;
that miss is the safe direction. Nothing reads across that
link today and neither tenant can see the other's record; it exists so that
combining a person's context across tenants can later be offered as a
deliberate act, rather than being impossible because nobody wrote the
connection down while it was knowable.

---

## 2. Keep the private key private

Generate a key pair on your side (Ed25519 is the simplest):

```bash
openssl genpkey -algorithm ed25519 -out canopy-signing.pem
openssl pkey -in canopy-signing.pem -pubout
```

The **private** key goes in your secret store and stays on your server. It lets
its holder vouch for visitors to your site — treat it like a database password,
and never send it to a browser. The public half is not secret; it is what canopy
checks signatures against.

### Give canopy a URL, not a key

**Publish a JWKS and paste its URL into "Where your site publishes its keys".**
It is the ordinary document every OIDC provider already serves, and most
languages have a one-liner for it:

```json
{ "keys": [ { "kty": "OKP", "crv": "Ed25519", "x": "…", "use": "sig",
              "alg": "EdDSA", "kid": "2026-09-a" } ] }
```

Then **rotation costs you nothing here**: publish the new key beside the old,
switch what you sign with, and drop the old one a day later. Canopy follows by
`kid` — including refetching the moment it meets a `kid` it has not seen, so
there is no window where your new key is refused. Nobody has to remember to
come back to canopy, which is the point: a key that can only be rotated by
somebody re-pasting it is a key that never gets rotated.

Requirements, all of which canopy tells you at the moment you save rather than
at the next failed assertion: **https**, reachable from the public internet (a
VPC-internal or localhost URL is refused — canopy would otherwise be fetching
inside its own network), no redirects, and under 64 KiB.

**Pasting a key still works** and is a perfectly good answer if you have no
JWKS to publish — that is what the "…or paste a signing key" box is. It behaves
identically; the only difference is that each rotation means coming back here.
Paste several during one, and all of them verify until you remove the old.

Whichever you use, editing the site never changes anything else, so you can add
URLs and agents freely without breaking a deployment.

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
            # Optional. `name` is descriptive. `email` + `email_verified` let a
            # visitor who ALREADY has a canopy account arrive as that account —
            # only at a domain your site's owner allowed it to resolve (Connected
            # sites → "Visitors with a canopy account arrive as themselves"), and
            # only when YOU verified the address. Never creates an account.
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
    # Pick fields explicitly rather than passing canopy's dict through — a new
    # field on canopy's side should not break your endpoint.
    return JsonResponse({
        "token": vouched["token"],
        "expires_at": vouched["expires_at"],
        # "user" (a visitor with a canopy account, arriving as themselves) or
        # "contact". Hand it to canopy-client's fetchToken as `kind`, and every
        # call routes to what that visitor can reach. Both are first-class.
        "kind": vouched.get("kind", "contact"),
    })
```

> **`sub` must be your own id for the signed-in user and nothing else.**
> You are asserting "this is a real person on my site", and canopy believes you
> because it verified your signature. Taking `sub` from the request body would
> let any caller be anybody.

**What the visitor becomes.** If they already have a canopy account you may
resolve (see the `email_verified` note above), **that user**. Otherwise a
`Contact` in the workspace that owns your app —
somebody canopy knows about, who is *not* a member of anything. They can talk to
the agents you were allowed to offer and see their own conversations with your
site. They cannot reach the workspace, its other agents, or anyone else's
threads, and recording them grants nothing: `/api/contact/` is the entire
surface a contact token reaches.

**Either way, [§5](#5-hand-the-agent-the-page-state)'s page state works.** A contact declares theirs on that same
prefix, so nothing in this guide is a privilege of visitors who happen to hold a
canopy account — do not try to make visitors arrive as users just to make the
panel page-aware.

Your ids live in your own namespace — `(your app, your id)` — so they cannot
collide with a canopy user or with another site's people. That makes this a
smaller grant than vouching for an email domain, which would reach into
canopy's own user population.

**Requirements canopy enforces**, so it is worth knowing before you debug a
401: `EdDSA`, `ES256` or `RS256` only (never HMAC — the key is public, so a
symmetric algorithm would let anyone sign); `aud` must match; `exp` at most 120
seconds out; and each `jti` works exactly once.

**If your visitors already have canopy accounts** — Dimagi staff on a Dimagi
product, say — they can arrive as *themselves*, with their own canopy
permissions, instead of as contacts. It is one grant, made by a workspace owner
on **Connected sites → "Visitors with a canopy account arrive as themselves"**,
and it is bounded twice: only a domain that owner is in, and only one canopy
admits at login. Your assertion then has to carry `email` and
`email_verified: true` for that visitor, and the person must ALREADY have a
canopy account at that address — arrival never creates one. Anyone else, at any
other domain, is a contact, which is the safe default: a contact reaches only
the agents you offer and their own conversations, so a mistake on your side
cannot hand a visitor your workspace. The token comes back with `kind` saying
which happened, and nothing else about your integration changes.

The widget calls this endpoint with `credentials: 'same-origin'` and sends an
`X-CSRFToken` header if you set a `csrftoken` cookie, so your normal session
auth and CSRF protection apply unchanged. **If you renamed that cookie** — which
a Django app served under a path prefix on a shared host has to do, or it
collides with its siblings — pass the real name as `csrfCookieName` in step 4.
Leave it wrong and the header is simply absent: the mint 403s and the widget
never starts, with nothing on the page to say why.

**If JavaScript cannot read your CSRF cookie at all, pass the token instead.**
`CSRF_COOKIE_HTTPONLY = True` (connect-labs sets it) hides the cookie from
`document.cookie` at *every* name, so no `csrfCookieName` can rescue it. Django
treats the DOM as a first-class source of the token — its own middleware says
"depending on whether the client obtained the token from the DOM or the cookie"
— so render it into the page and hand it over:

```js
canopy.init({
  // …
  csrfToken: () => document.querySelector('[name=csrfmiddlewaretoken]').value,
})
```

Pass a **function**, not a string, wherever the value can change within a page's
life: Django rotates the token on login, and a string captured at `init` is then
stale for every later mint. It is read afresh on each one. `csrfToken` wins over
`csrfCookieName`, and anything that can produce the value your backend expects
works here — the scheme does not have to be Django's.

Do **not** reach for `CSRF_COOKIE_HTTPONLY = False` to make the cookie route
work. Django's own docs note the setting buys little (CSRF defends cross-origin;
an attacker who can read cookies from JS already has XSS), so it is a defensible
change — but it weakens a global setting to accommodate one widget, and this
option exists precisely so you do not have to.

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
    // dismissible: false,             // default true — see below
    // Only if your CSRF cookie is not named `csrftoken`:
    // csrfCookieName: 'csrftoken_labs',
    // Or, if JS cannot read the cookie at all (CSRF_COOKIE_HTTPONLY):
    // csrfToken: () => document.querySelector('[name=csrfmiddlewaretoken]').value,
  })
</script>
```

No npm, no React, no build step, no framework. The loader is ~15 kB (under 6 kB gzipped) and imports
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

**The panel never shows tool calls, and there is no option to.** A visitor
asked a question and wants the answer, not the MCP calls behind it. canopy
decides this from the widget's token (a site's delegated token or a contact
token), not from anything the page can ask for, and withholds the calls on
every path the panel reads a conversation through: the live socket, the
snapshot on reconnect, and older history over REST. They are never sent to the
browser. The transcript keeps every call, so canopy's own chat page shows
them. While tools run the panel still shows that the agent is working.
Earlier conversations are listed by date and by the start of what was first
asked, not by the session's title.

### Match it to your brand

By default the launcher is an orange pill and the panel is dark, which will not
suit every page. There are three ways to change it, and which one you need
depends on **which half** you are styling:

| Half | Where it lives | What reaches it |
| --- | --- | --- |
| **Launcher** (the bubble, its ×, the panel's frame) | your page, inside a shadow root | CSS variables, `::part()`, or `theme` |
| **Panel contents** (messages, composer, Send) | canopy's own document, in the iframe | **only** `theme` — no CSS of yours can reach it |

The shadow root is deliberate: a global `button { … }` or `* { … }` on your
page must not be able to deform the launcher by accident. Everything below is
explicit, so that promise still holds.

**1. `theme` in `canopy.init`: one option, both halves.**

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

`mode` is rendered by the server, so the panel paints in the right mode from
its first frame instead of flashing dark on a light page. The accent recolours
canopy's own `--primary` token, which every primary control inside the panel
reads, so it reaches all of them rather than whichever someone remembered.

If your page has its own light/dark toggle, keep the widget in step:

```js
myThemeToggle.onChange(function (mode) { widget.setTheme({ mode: mode, accent: '#2563eb' }) })
```

`setTheme()` **replaces** the whole theme, so pass everything you still want.

Every value is validated, twice: once in the loader and again inside canopy's
document, which does not take your page's word for it. A value that is not a
colour, length or font-family, or that contains `;`, `{`, `}`, `url(` and the
like, is dropped with a `console.warn` naming it. If your accent does not show
up, look in the console.

**The font has to exist inside the iframe too.** A webfont your page loads is
not visible inside canopy's document. Name a system stack, or a font canopy also
serves, or the panel falls back to its default while the launcher uses yours.

**2. CSS variables: the launcher, from your own stylesheet, no JS.** Custom
properties are the one thing that crosses a shadow boundary:

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

These style the launcher only. A `theme` passed to `canopy.init` wins over the
same variable set here.

**3. `::part()`: anything else about the launcher.** For size, shadow,
letter-spacing and so on:

```css
[data-canopy-widget]::part(launcher) { height: 40px; box-shadow: none; }
[data-canopy-widget]::part(dismiss)  { display: none; }  /* prefer dismissible: false */
[data-canopy-widget]::part(panel)    { border-radius: 16px; }
[data-canopy-widget]::part(resize)   { display: none; }  /* no corner resizing */
[data-canopy-widget]::part(divider)  { display: none; }  /* no resizing a pinned rail */
```

The parts are `launcher`, `dismiss`, `panel`, `resize` and `divider` (a pinned rail's left edge). Only rules you write against
them reach in.

**Pick a mode:**

| mode | shape | use when |
| --- | --- | --- |
| `overlay` | floating bubble + panel | you cannot change the page's layout |
| `docked` | full-height right rail | the page can give up a column |
| `inline` | fills a `target` element, no launcher | you want it inside your own layout |

`inline` requires `target` (a selector or element) and is always open — there is
no launcher to reopen it with.

**Other options:** `theme` (above); `agent: 'labs-helper'` opens on that agent when canopy has no default for the visitor ([§5c](embedding-advanced.md#5c-a-page-that-chooses-its-own-agent)); `metadata: {…}`
stamps opaque data on sessions the widget creates; `onInvalidate(resource)` is
called when data your page shows has changed ([§5a](embedding-advanced.md#5a-being-told-when-your-data-changes)); `onSession(session)` is
called when the conversation open in the panel changes ([§5e](embedding-advanced.md#5e-keeping-a-pointer-to-a-conversation-and-opening-it-again)); `dismissible`, `storage`,
`csrfCookieName`, `csrfToken` as above; `onResize`, `snappable`, `snapTarget` (below); plus `width`, `title`, `open: true`.

The returned object has `open()`, `close()`, `toggle()`, `isOpen()`,
`dismiss()`, `isDismissed()`, `expand()`, `collapse()`, `isExpanded()`,
`snap()`, `unsnap()`, `isSnapped()`,
`setPageState()`, `registerAction()`, `unregisterAction()`, `setTheme()`,
`setLauncherVisible()`, `ask()`, `openSession()`, `currentSession()` ([§5e](embedding-advanced.md#5e-keeping-a-pointer-to-a-conversation-and-opening-it-again)), `destroy()`, and the older `provideContext()` ([§5](#5-hand-the-agent-the-page-state)).

### A bigger panel for data work

A visitor can resize the panel by dragging its top-left corner. In `overlay` it
grows up and to the left, staying attached to the bubble. In `docked` the rail
stays on the window's bottom-right: the corner changes its width and height, and
dragged back to the top it is a full-height rail again. Either way it
stays between a usable minimum and the window's edge, is remembered per site in
`storage`, and once set wins over the height the panel would pick for itself.

A docked rail is drawn over your page. Its header has a **snap** control (the
pin) that snaps it into the page instead: the rail reserves its width as
`padding-right` on the root element, so the page reflows beside it, follows the
width as the visitor drags it, and gives the room back when the panel closes or
expands. The visitor's choice is remembered per site. `snappable: false` removes
the control; `snapTarget` (a selector or element) pads something other than the
root — for a page that scrolls inside a fixed-height container; `snap()`,
`unsnap()` and `isSnapped()` drive it from your page.

If you would rather lay out around the rail yourself, pass `onResize` to follow
its width (it is also called once at load with a remembered size):

```js
canopy.init({ /* … */ mode: 'docked',
  onResize: ({ width }) => { main.style.marginRight = width + 'px' } })
```

In `overlay` and `docked` the panel header has an expand control: the panel grows
to fill the window bar a 16px margin (all of it on a phone) and Esc or the control
puts it back in the mode it came from. In it, a View (an MCP Apps results table or
chart) uses the panel's full width and most of its height, and a View may also ask
to go full screen over the panel. Wide markdown tables scroll sideways with their
header pinned, and code blocks have a Copy button.

```js
canopy.init({ /* … */
  expandable: true,             // default true; false hides the control
  initialDisplay: 'expanded',   // or 'normal' (default) — the visitor's own
                                // remembered choice on this site wins
})
```

The visitor's choice is remembered per site (`storage`, as for a dragged
launcher). `widget.expand()` / `collapse()` let the page do it itself — a
"work with this data" button, say — without overriding what the visitor chose
last time. `inline` never expands: there your page owns the box.

`setLauncherVisible(false)` hides the bubble on a page where it is wrong — it
covers that page's own controls, or duplicates what the page already is — and
`true` brings it back on the next route. It is yours, not the visitor's: unlike
`dismiss()` it is reversible, and it is what canopy uses to keep the bubble off
its own chat pages, where it sat on the Send button.

`ask(text)` opens the panel and starts a new conversation with `text` as the
visitor's first message — for a page control such as "Explain this answer", so
the words about a page come from the agent rather than being written into it.
The visitor sees the question exactly as sent, and the agent reads the page
state as for any first message. Asked before the panel has loaded, the question
waits and is sent once it has. Text is trimmed and capped at 2,000 characters;
an empty one does nothing. Older widgets have no `ask`, so feature-detect it
(`typeof widget.ask === 'function'`) and fall back to `open()`.

---

## 5. Hand the agent the page state

`provideContext` still works, but it is a snapshot and it is worse: read once
when a conversation opens, delivered as prose, attached to the first message
only. Filter your page after opening the chat and the agent reasons about a
screen that has moved.

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

1. **It is in the agent's context from the first turn.** canopy puts your
   declared selection (resource, ids, filters) into the caller envelope, and the
   canopy plugin hands it to the session as context alongside every message —
   NOT inside the person's message, so the transcript shows exactly what they
   typed.
2. **The MCP tool `current_page`** returns every attached page's state, so the
   agent re-reads your screen *whenever it needs to* — "close the ones I'm
   looking at" is answerable on turn nine.

Path 1 exists because path 2 can be unavailable: MCP servers connect
asynchronously, and a conversation starts a fresh agent process, so the first
turn can race that connection. **It is also not something the agent has to think
of** — an agent whose MCP tools arrive deferred will not go looking for
`current_page` unprompted, and one whose MCP server is still connecting cannot
call it at all — with path 2 alone, the first reply is "I can't see your screen"
with no tool call. The first turn carries the user's actual question, so it must
not be the one that depends on the flakiest link.

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

## 6. Check that it works

In a browser, on your page, signed in as an ordinary user:

| Check | Expected |
| --- | --- |
| The launcher appears | a button, or your docked/inline panel |
| Clicking opens a panel | a chat, not a blank rectangle |
| An agent is offered | the picker, or straight into a conversation |
| Sending a message gets a reply | the agent responds |
| The agent knows your page | ask it "what am I looking at?" |
| Reopening later shows history | your prior conversations with that agent — on this host only ([§5b](embedding-advanced.md#5b-reading-back-a-users-history)) |

### If something does not work

| Symptom | Cause |
| --- | --- |
| Blank panel; console shows a frame refused to load | your origin is not in **Allowed frame origins** |
| Panel says "not configured correctly" | same cause — the shell rendered with no origins |
| "No agent is available here yet" | no **Allowed agents** row, or this user is not a member of that agent's workspace |
| A token error in the panel | your endpoint 403'd, 500'd, or returned no `token` |
| The mint 403s and no `X-CSRFToken` was sent | your CSRF cookie is renamed (`csrfCookieName`) or unreadable from JS because it is `HttpOnly` (`csrfToken`) — [§3](#3-add-one-backend-endpoint) |
| 404 on `/embed/chat` | app name mismatch, credential revoked, or no frame origins |
| 503 on `/embed/widget.js` | canopy's frontend is not built |
| Agent replies but knows nothing about the page | no `setPageState` call, or it ran after the first message was sent, or the runner's canopy plugin predates the caller-context hook |
| Agent acts on rows the user cannot see | you sent rows instead of ids — send `visible_ids` + `backing_tool` |
| Page shows rows the agent already deleted | no `onInvalidate` in `canopy.init`, or the `resource` it checks differs from the one in `setPageState` |
| Message sends but no reply ever arrives | no runner is online for that agent — a canopy-side operational issue, not yours |

---
