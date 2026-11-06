# Embedding a canopy agent: advanced steps (5a-5e)

Optional steps after the six in [embedding-a-canopy-agent.md](embedding-a-canopy-agent.md): being told when your data changes (5a), reading back a user's history (5b), a page that chooses its own agent (5c), choosing agents from the site agents API (5d), and keeping a pointer to a conversation (5e).

**See also:** [embedding-a-canopy-agent.md](embedding-a-canopy-agent.md), [embedding-actions-and-access.md](embedding-actions-and-access.md), [embedding-reference.md](embedding-reference.md), [embedding-internals.md](embedding-internals.md).

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
Without it, the agent can delete rows you are still displaying.

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

## 5c. A page that chooses its own agent

A site may offer more than one agent and want a different one on some pages —
connect-labs keeps ACE on the marketplace and puts `labsdata`, a read-only data
agent that runs only on sensitive-data runners, on the pages over real visit data. The
page chooses; the rest of the site keeps its default.

With the Python SDK (`dimagi-canopy` ≥ 0.11):

```python
# CANOPY_HOST: AGENT_SLUG="ace", and no site-wide RUNNER_REQUIREMENTS
ctx = panel_context(request, resource="labs-data://visits",
                    agent="labsdata", runner_requirements=["sensitive_data"])
```

What canopy does with it:

- The page's choice is signed into its page token (or, for a single-page app,
  read from `CANOPY_HOST["PAGE_AGENTS"]`), so a browser cannot keep a data
  page's scopes and swap in another agent.
- Your mint signs it into the visitor assertion as `canopy_agent` beside
  `agent_slug`. canopy refuses the arrival unless the two are the same agent
  (`agent_mismatch`) and that agent is on your host site's allow-list.
- The token it returns is **bound** to that agent: the widget offers only it,
  and no conversation with any other agent can be started, listed, opened or
  sent into with that token — on REST, the chat socket, the contact surface and
  MCP Apps Views alike. The host grant redeemed on that arrival is usable only
  by that agent: an ACE turn never spends the grant a data page minted for
  `labsdata`, however recently it was minted. And it is that PAGE's grant: a
  conversation reaches your site through the grant of the page it is on (the
  last page to send or declare its state in it), never another tab's, so you
  may hold each page's grant to what that page shows.
- The page's `runner_requirements` are added to the site's. Every conversation
  the token starts or sends into carries them, and routing runs it only on a
  runner that meets them.

A token without `canopy_agent` behaves exactly as before: any agent the site
offers. Hosts not on the SDK add the claim to their own assertion — see
`sdk/python/README.md` § Host grant contract v1.

### Which agent the panel opens on

When more than one agent is on offer, the panel opens on the first of these that
names one of them:

1. **The page's choice** — a bound token (above). It is the only agent listed.
2. **The visitor's own default** — set from the panel ("Make this my default",
   `PUT /api/embed/default-agent`) or in canopy (`PUT /api/people/me/default-agent/`).
   One per person, for every site and canopy's own pages.
3. **The site's default** — "Opens on" on the host sites page
   (`default_agent` on `PATCH /api/workspaces/{slug}/host-sites/{id}`).
4. **The agent you named** — `agent` in `canopy.init`, which the SDK fills from
   `AGENT_SLUG`.
5. **The only agent**, else the picker.

canopy decides 1–3 and marks the winner `default: true` in `GET /api/embed/agents`
(and `GET /api/contact/me`, where a contact has no default of their own); each
counts only when the agent is on that visitor's list, so a default never grants
an agent. The `agent` you send is therefore a fallback, not a choice: to make a
page open on an agent whatever the visitor prefers, choose it with
`panel_context(agent=...)` so the token is bound. With several agents on offer,
the panel's header names the agent as a switch back to the picker.

## 5d. Choosing your agents from canopy's site agents API

Don't type canopy's facts into your settings. Ask for them (`dimagi-canopy` ≥ 0.14;
the workspace, links and `last_error` need ≥ 0.16):

```python
from canopy_sdk.django import site_agents

current = site_agents.current()     # None = show no agent
error = site_agents.last_error()    # why not, when it is None or stale
site_agents.refresh()               # "re-read now" from your own settings page
```

`GET /api/sites/agents`, signed with your own key, answers with a JWS signed by
canopy's client key (the `Canopy-Execution` key): exactly the agents this
workspace allows on your site, each with its host sites for your site
(`handles_sensitive` and who set it), which runner flags its runners carry and whether one
is ready now, `ready`, `url` (the agent's page in canopy), `problems` — plain
sentences naming where in canopy to fix each — and `problem_links`
(`[{text, url}]`, SDK `SiteAgent.problem_links`): each problem with the URL of
the canopy page that fixes it, to render as a link. The `site` block carries
`settings_url` (SDK `SiteAgents.site_settings_url`): your host site's own
card in canopy, where its owner allows agents, changes keys and tests it. Never
build a canopy URL yourself. `runner_flags` lists every flag canopy knows;
`version` changes exactly when any of that does, so record it with each settings
save. Cached for 5 minutes (per `WORKSPACE`/`AGENT_SLUG`, so changing either takes
effect at once); when canopy cannot be reached the last good copy is served
marked `stale` for up to 24 hours, then `None`.

**When you get nothing, say why.** `last_error()` is the last refresh's failure,
cleared by the next success: a `SiteAgentsError` with a `code`, a `message` safe
to show your operator, and canopy's HTTP `status` when it answered.

| `code` | Meaning | Fix |
| --- | --- | --- |
| `not_configured` | `SIGNING_KEY`, `CANOPY_BASE_URL` or `APP_NAME` is missing or unusable | your settings |
| `unreachable` | canopy could not be reached (network, DNS, TLS) | retry; check `CANOPY_BASE_URL` |
| `unknown_issuer` | no host site named `APP_NAME` in the workspace you named | register it, or fix `WORKSPACE` / `APP_NAME` |
| `ambiguous_issuer` | several workspaces have a site by that name and you named none | set `WORKSPACE` |
| `workspace_mismatch` | `WORKSPACE` and the agent you sent name different workspaces | fix one (a moved agent) |
| `bad_signature` | the keys canopy holds for the site do not verify yours | the site's JWKS URL in canopy, or your key |
| `rate_limited` | too many requests | wait |
| `refused` | canopy refused without naming a code | `status` and `message` |

Tell canopy what you need back by setting `CANOPY_HOST["SITE_NEEDS"]`: the SDK
serves it, signed, at `canopy/site.json` beside `canopy/jwks.json`, and canopy's
**Host sites** page shows, per agent, whether you offer it, whether you let it
work with your sensitive data and — only for those — whether it meets your
`sensitive_data.requires` and why not.

```python
def site_needs():
    return {"sensitive_data": {"requires": {"handles_sensitive": True, "runner_flag": "sensitive_data"}},
            "uses": {"agents": ["ace", "labsdata"], "sensitive_data_agents": ["labsdata"],
                     "version": current_site_agents_version()},
            "titles": {"labs:workflow:run": "A workflow run"}}
```

`sensitive_data.requires` is what an agent must be to work with your sensitive data
(`handles_sensitive`: a host site for your site says `data: sensitive`;
`runner_flag`: its turns run on a runner with that flag), and
`uses.sensitive_data_agents` names the agents you let near it. **The pages and
scopes in `site.json` are informational for canopy:** it shows them to the site's
owner for reference, but never enforces them or tells an agent. Agents never need to
know your pages — they declare only `sites:` and `data: sensitive`; you enforce your
own page scopes.

The same document carries a **`site` block** — your own facts, filled by the SDK
from settings you already have: `origins` (`CANOPY_HOST["ORIGINS"]`, else the
origin of `ISSUER`, else the address canopy fetched it from), `jwks_url`
(`JWKS_URL`, else the SDK's `jwks.json` route), `issuer` and `mcp_resource`
(`ISSUER` and `RESOURCE`, while the grant is on). canopy's owner adopts them with
"Fill from the site" ([§1](embedding-a-canopy-agent.md#1-register-your-app-in-canopy)) — never automatically. Mount
`canopy_sdk.django.views.site_document` at `/.well-known/canopy-site.json` as well,
so your address alone finds it. Details: `sdk/python/README.md` § The site agents
API and the site's needs.

---

## 5e. Keeping a pointer to a conversation, and opening it again

A page can keep a **pointer** to a conversation had in its panel and bring it back
later — a "saved conversations" list beside a shared workspace, say. It never gets
the conversation itself.

```js
const widget = CanopyWidget.init({
  /* … */
  onSession(session) {
    // { id, resource, private } — or null when the panel is on its start screen
    saveButton.disabled = !session || session.resource !== 'labs-data-space://7'
  },
})

widget.currentSession()        // the last report, or null
widget.openSession(savedId)    // open the panel on that conversation
```

**What the page is told** (`onSession`, `currentSession()`), and nothing else:

- `id` — canopy's session id;
- `resource` — the page the conversation was **started** on: the `resource` its page
  first declared with `setPageState`, as canopy recorded it (`origin_resource` on the
  session; query and fragment cut off; never changed afterwards). `""` if it was not
  started on a page that names a resource. Use it to keep a page's pointers to the
  conversations begun on that page;
- `private` — it is a private (sensitive) conversation.

No title, no opening words, no messages: a private conversation's words are sealed to
its people (canopy itself cannot read them), and your page is not one of them. Name a
saved pointer with words your user types, not with anything from the conversation.

**`openSession(id)`** opens the panel and asks the frame to open that conversation.
The frame reads it **as the visitor**, through the panel's own token, so canopy's
session ACL decides — exactly as if they had opened it from canopy's chat list:

- one they may read opens, and the page hears `onSession` for it;
- a private one they can see but are not in shows canopy's own interstitial, which
  lets them ask its people to add them (the request carries nothing it said);
- anything else — not theirs, gone, with an agent your site does not offer — shows
  "This conversation is not one you can open here".

The page learns which of these happened only from whether `onSession` reports it.
A pointer grants nothing: whoever holds one can open only what canopy would already
let them read. An id that is not a canopy session id (a UUID) is ignored; called
before the panel has loaded, the request waits.

`resource` comes from the visitor's own browser — the page declared it, and the frame
relays canopy's record of it. It is not a signed attestation: treat a pointer saved
from it as the person's own write, checked by your own rules for who may write there.
Older widgets have neither call; feature-detect `typeof widget.openSession === 'function'`.

---
