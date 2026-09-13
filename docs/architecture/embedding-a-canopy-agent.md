# Embedding a canopy agent in your product

How to put a canopy agent into a host application, what the host has to build,
and exactly where the access-control boundaries are.

Design rationale lives in
[`../superpowers/specs/2026-09-12-embedded-agent-widget-v2-design.md`](../superpowers/specs/2026-09-12-embedded-agent-widget-v2-design.md).
This document is the integration contract.

**Status:** the read path works end to end. **The agent cannot yet call a host
action** — see [Known gaps](#known-gaps) before planning around it.

---

## 1. What the host builds

Two things. That is the whole integration.

### 1a. One backend endpoint

A registered `AppCredential` is a **secret**, so only your server can exchange
it. There is no way around this: a browser-only embed is possible for anonymous
chat, not for anything that resolves to a user's identity.

Your endpoint must be session-authenticated (your own login), and it returns a
short-lived canopy token for **the signed-in user**:

```python
# your_app/canopy.py  — modelled on ace-web's apps/canopy/client.py
import json, urllib.request
from django.conf import settings

def exchange_token(email: str, ttl: int = 3600) -> dict:
    req = urllib.request.Request(
        f"{settings.CANOPY_BASE_URL}/api/auth/token-exchange",
        data=json.dumps({"acting_as_email": email, "ttl_seconds": ttl}).encode(),
        headers={
            "Content-Type": "application/json",
            # The app credential. Server-side only, never sent to a browser.
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
    # Pick fields explicitly. Passing canopy's raw dict through a strict
    # response schema means canopy adding a field breaks your endpoint.
    return JsonResponse({
        "token": exchanged["token"],
        "expires_at": exchanged["expires_at"],
    })
```

**`acting_as_email` is the trust hinge.** You are asserting "this is an
authenticated user of mine," and canopy believes you because you hold the
secret. So this endpoint must never accept an email from the request — always
`request.user.email`.

### 1b. One script tag

```html
<script src="https://labs.connect.dimagi.com/canopy/embed/widget.js"></script>
<script>
  var widget = canopy.init({
    baseUrl: 'https://labs.connect.dimagi.com/canopy',
    app: 'connect-labs',              // your registered AppCredential name
    tokenUrl: '/labs/canopy/token',   // the endpoint above
    mode: 'docked',                   // 'overlay' | 'docked' | 'inline'
  })
</script>
```

No npm, no React, no build step, no framework. The loader is 5.8 kB and imports
nothing; the chat UI runs inside an iframe with its own bundled React, so it
cannot collide with anything your page already loads.

**Display modes**

| mode | shape | use when |
| --- | --- | --- |
| `overlay` | floating bubble + panel | you cannot change the page's layout |
| `docked` | full-height right rail | the page can give up a column |
| `inline` | fills `target` element, no launcher | you want it in your own layout |

`inline` requires `target` (a selector or element) and is always open — there is
no launcher to reopen it with.

---

## 2. Registering the app (canopy side, one-off)

This is the security step. Canopy has to know the app exists, which origins may
frame it, and which agents it may offer — and it fails **closed** on all three.

### In the admin (the normal way)

`/admin/tokens/appcredential/add/`, as a staff user. One page:

| Field | What it does | Getting it wrong |
| --- | --- | --- |
| **Name** | The `app` value in `canopy.init` | Mismatch ⇒ the embed shell 404s |
| **Allowed delegation domains** | Email domains this app may vouch for, e.g. `["dimagi.com"]` | Empty ⇒ the app can mint for nobody |
| **Allowed frame origins** | Origins that may frame the widget, e.g. `["https://labs.connect.dimagi.com"]` | Empty ⇒ the shell 404s, deliberately — an `X-Frame-Options`-exempt page with no `frame-ancestors` is frameable by any site |
| **Allowed agents** (inline) | Which agents this app may offer | Empty ⇒ the picker offers nothing |
| **Provision workspace / role** | Optional: tenant a brand-new user lands in | Leave blank unless every user of this host is trusted in that tenant |

**The raw credential is shown once, in a banner, on save.** It is stored only as
a hash and cannot be recovered — copy it into the host's secret store
immediately. Editing the app later never re-mints it (a second token would
silently invalidate whatever the host already has deployed).

Origins are validated on save: a wildcard, a path, or anything with a `;` is
refused with an explanation. A wildcard is rejected on purpose — it would
restore exactly the exposure `X-Frame-Options: DENY` was preventing.

**Revoking** is a changelist action. It takes effect immediately: the embed
shell 404s and existing delegated tokens stop working on their next request.

### Or by command (scripted setup, needs DB access)

```bash
uv run python manage.py create_app_credential --name connect-labs --domains dimagi.com
uv run python manage.py grant_app_frame_origin --name connect-labs \
    --origin https://labs.connect.dimagi.com
uv run python manage.py grant_app_agent --name connect-labs --agent labs-helper
uv run python manage.py grant_app_agent --name connect-labs --list
```

Note these need a shell with database access, which a deployment does **not**
currently provide — `EnableExecuteCommand` is off on the ECS service and the RDS
instance is VPC-internal. Use the admin on a deployment; the commands are for
local and scripted setup.

### Registering every environment

Each origin that embeds the widget must be listed — production, staging, and
`http://localhost:8000` for local development. They can all live on one
credential, or you can register separate apps per environment if you want their
agent grants to differ.

## 2a. Where the agent's KNOWLEDGE comes from — not here

Worth separating, because it is a different system. Nothing in §2 teaches an
agent anything. Registration answers *"may this agent be offered on this host,
to this user?"* — a permission question.

What the agent **knows** lives with the agent:

- its **persona and skills**, in its own git repo (`Agent.repo_url`, and the
  skills under it);
- its **credentials** for other systems, as named slots (`AgentCredential`) it
  declares in `runtime.yaml` (`Agent.runtime_secrets`);
- its **tools**, including any MCP servers it is configured against — which is
  how an agent would query connect-labs' own data directly rather than through
  the page.

So "make the agent good at supply" is work in that agent's repo: give it skills
about supply concepts, and MCP access to the data it should be able to look up.
The widget only decides *whether it shows up*.

## 3. Designing the host page for this

### Give the agent the state the user is looking at

```js
widget.provideContext(() => ({
  supplyPoint: currentSupplyPoint,     // ids the agent can look up
  stockOnHand: visibleRows,            // what is on screen
  filters: { period, commodity },      // what the user narrowed to
}))
```

This is **pulled once, when a conversation opens** — a snapshot, not a
subscription. Return whatever describes the current view; it is serialised to
JSON and handed to the agent as a preamble on the user's first message.

Guidance that matters in practice:

- **Include identifiers, not just labels.** "Kano warehouse" lets the agent talk
  about it; `supply_point_id: 4821` lets it look things up.
- **Send what is on screen, not the whole dataset.** The snapshot is capped at
  8 000 characters and truncates. If you are near that, you are sending a
  database, not a context.
- **Do not send secrets.** It goes into a conversation transcript that persists.
- **It is read at request time**, so a plain closure over current state is
  correct — no need to re-register when things change.

### Keep the page's own permissions in front of it

The context callback runs **in the user's browser, in their session**. So
compute it from what that user can already see — if a row is hidden from them
by your RBAC, do not put it in the snapshot. The agent's view being exactly the
user's view is what makes the ACL story hold (§4), and it is the host's job to
keep it true.

### Offer actions (plumbing ready, agent side not — see gaps)

```js
widget.registerAction('recordStockCount', async (args) => {
  // Throw to refuse. A thrown error reaches the agent as a refusal it can
  // read; returning something falsy looks like success.
  if (!canEdit()) throw new Error('this count is closed')
  return await recordCount(args)
})
```

Re-registering a name replaces it, so calling this from a component that
re-renders is safe. `unregisterAction(name)` when the thing it acts on leaves
the screen — an action that mutates something off-screen is worse than a
missing one.

---

## 4. How access control actually resolves

Walking a real request, from a signed-in user on a host page.

| # | Step | What is checked, and by whom |
| --- | --- | --- |
| 1 | User is signed in to the host | The **host's** login. canopy is not involved |
| 2 | Page loads `widget.js`, calls `canopy.init` | Nothing — a public script |
| 3 | Frame loads `/embed/chat?app=…` | canopy returns `Content-Security-Policy: frame-ancestors <registered origins>`. **The browser** then refuses to render the frame on any other site |
| 4 | Frame posts `ready` | Carries no data |
| 5 | Host mints a token | Host's own session auth. Host sends its `AppCredential` + `request.user.email` |
| 6 | canopy `token-exchange` | Credential valid and unrevoked; rate limit; email domain in **that credential's** `allowed_delegation_domains` *and* canopy's login allowlist; account not deactivated. Then finds-or-creates the canopy user, applies `provision_workspace` if set (create-only — it can never change an existing member's role), and issues an opaque `DelegatedToken` |
| 7 | Host posts the token into the frame | `targetOrigin` is canopy's origin, never `*`. The token is never in a URL |
| 8 | Frame calls canopy | Every request: `DelegatedToken.lookup` — a **live DB read** with expiry checked — resolves the real user, and records which app is acting |
| 9 | `GET /api/embed/agents` | The app comes from the **token**, not a parameter. Returns allowlisted agents **∩** workspaces the user is a member of. Both required |
| 10 | `POST /api/canopy-sessions/` | Workspace resolved from the user's memberships; the agent must belong to it; `created_by` is the user, who becomes an OWNER participant; `embed_app` stamped from the token and any client-supplied value discarded |
| 11 | Reading a session | Workspace membership **and** (you created it, or you are a participant, or it is runner-discovered). A co-tenant with the UUID cannot read your chat |
| 12 | WebSocket | Same `DelegatedToken`, passed as `?token=` since a WS handshake carries no headers |
| 13 | Context / actions | Run **in the user's browser, in their session**. The agent gets what the user can see, and can do what the user could do — never more |

**The property that matters:** nothing is frozen into the token. Tokens are
opaque random strings stored as hashes, and memberships are re-read from the
database on every single request. Revoke a membership and the next request
reflects it. This is deliberate — `DelegatedToken`'s own docstring says
*"DB-backed (not JWT) so it is revocable and the table is the audit trail."*
A JWT would freeze the ACL at mint time, which is weaker.

**What the agent never gets:** any credential for your product. It runs on a
runner under its own identity, and it reaches your page only through the bridge,
only while the page is open.

---

## 5. Known gaps

Read these before designing around the widget.

### The agent cannot call a host action yet

`registerAction` works, and the host↔frame plumbing is complete and tested. But
the in-frame app never tells the **agent** which actions exist and never invokes
one — so an action registered today is reachable by nothing.

Closing it needs two pieces that do not exist:

1. **A parameter schema.** The bridge passes action *names* only. An agent
   cannot call `recordStockCount` without knowing it takes
   `{supply_point_id, commodity, quantity}`. CopilotKit's `useCopilotAction`
   takes a `parameters` array for exactly this reason, and the equivalent is
   needed here.
2. **An agent-facing surface.** The agent must be able to emit a call and
   receive a result — an MCP tool scoped to the session, or a structured
   convention in the conversation.

Until then: treat context as the feature, and actions as a host-side API with no
caller.

### Actions and context need the page open

Both execute in the user's browser. Close the tab and the agent can neither read
nor act — there is no server-side path. Good for access control, and a real
constraint: an agent cannot do something for you after you leave.

### The domain allowlist is narrow today

`token-exchange` requires the email's domain in both the credential's
`allowed_delegation_domains` **and** canopy's own login allowlist. The second is
currently Dimagi-only, so a partner user on another domain gets a 403 and the
widget never opens. Widening it is a deliberate policy change.

### Context is a snapshot, not a live feed

Pulled when a conversation opens. If the user changes the page mid-conversation,
the agent still has the old snapshot. Continuous sync (CopilotKit's
`useCopilotReadable` is the reference shape) is future work.

### The agent does not remember previous conversations

A user can see and resume their prior chats, but each session is its own
transcript. A new conversation starts cold.

### None of this has run in a browser

Every test is in-process. Expect real fixes on first embedding.

---

## 6. Relationship to CopilotKit

CopilotKit is the closest off-the-shelf equivalent, and the comparison is useful
for knowing what to expect.

| CopilotKit | Here | Difference |
| --- | --- | --- |
| `useCopilotReadable` | `provideContext` | Theirs is per-component and continuously synced; ours is one page-level snapshot at open |
| `useCopilotAction` | `registerAction` | Theirs has parameter schemas, in-chat rendering, and human-in-the-loop confirmation. Ours has names and a function — **and no agent-facing caller yet** |
| `CopilotPopup` / `CopilotSidebar` | `mode: 'overlay' / 'docked'` | Equivalent, but ours is framework-free rather than React components |
| `CopilotRuntime` → an LLM | a canopy `Agent` on a runner | **The big one.** Theirs orchestrates a model call. Ours routes to a persistent agent with its own identity, mailbox, repo, credentials and tools |

If you want a page-scoped copilot in a React app, CopilotKit is more complete
and you should probably use it. The reason to use this instead is that the thing
on the other end is a *canopy agent* — the same Echo or Hal that runs
scheduled work, has its own inbox, and can be handed a task — not a chat
completion bound to the page.
