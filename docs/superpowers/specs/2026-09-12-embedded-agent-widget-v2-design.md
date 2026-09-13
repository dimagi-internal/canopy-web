# Embedded agent widget v2 — canopy as a drop-in agent surface for any product

**Status:** Draft for review · **Date:** 2026-09-12 · **Author:** Jonathan + Claude

> **Supersedes `2026-09-12-embedded-agent-widget-design.md`** (merged as #745).
> Three of that spec's five sections turned out to be wrong once the target
> codebase was read: §2 (one agent per host), §3 (`npm install canopy-ui`), §5
> (non-React embed deferred). This replaces it rather than patching it.

## What changed, and why

v1 designed a **React component integration** — `npm install canopy-ui`, mount
`<ChatPanel>`, host wires a token endpoint — and deferred a generic embed on the
grounds that "both current hosts are React apps." Every clause of that is
load-bearing and two are false:

- **connect-labs is not a React SPA.** It is Django templates plus `alpinejs`
  and `htmx.org` with **13 `createRoot` islands** and 63 `.tsx` files. A
  script-tag widget drops onto any labs page; a React component reaches only the
  islands.
- **`canopy-ui` cannot be installed there.** It declares `react: ^19.0.0`
  (also `lucide-react ^1.8.0`, `tailwind-merge ^3.5.0`); labs is on React
  `^18.2.0`, `lucide-react ^0.263.1`, `tailwind-merge ^2.6.0`.
- **One agent per host was never a design, just a default.** ace-web picks its
  agent in its own env (`CANOPY_AGENT_SLUG = env(..., default="ace")`). canopy
  has no record of which agents may operate in which host, so it cannot answer
  "which agents do I have here" — the question a widget's picker must answer.

The goal is therefore restated: **a self-contained widget any product can embed
with one script tag, serving any canopy user — not a React integration recipe.**
Dimagi is not a design input; it is a deployment policy (see §5).

## 1. Four layers, so "go native" is supported rather than a fallback

| Layer | What | Status |
| --- | --- | --- |
| **0 — wire** | REST + WS protocol, any language | exists (`docs/architecture/api-surface.md`) |
| **1 — `@canopy/client`** | framework-free TS: token cache + refresh, REST, WS reconnect | **new** |
| **2 — `canopy-ui/chat`** | React 19 components | exists, published |
| **3 — `widget.js`** | drop-in IIFE, own bundled React, iframe-isolated | **new** |

Layer 1 is the enabling piece and it is mostly a **move, not a write**:
ace-web's `frontend/src/canopy/{token,api,ws}.ts` is ~390 lines with **zero
React imports** — already framework-free, just trapped inside one host. Both the
widget and any native host want it, and today only ace-web has it.

The context/action contract (§6) is defined **once** as plain interfaces at
layer 1. The widget's `postMessage` transport is one implementation of it; a
native host calls the same functions directly. That is what keeps the native
path first-class instead of a downgrade.

## 2. One bundle, three display modes

An iframe cannot paint outside its own box, so the launcher and panel chrome are
host-DOM elements (in a shadow root, so host CSS cannot bleed in) wrapping the
iframe — the standard approach for this class of widget.

```js
canopy.init({ mode: 'overlay' })                  // launcher bubble + floating panel
canopy.init({ mode: 'inline', target: '#pane' })  // fills a host element, no launcher
canopy.init({ mode: 'docked' })                   // right-hand rail
```

Same iframe in all three; only the container's positioning differs. `docked` is
already proven in labs — `workflow-runner.tsx` docks its own edit-mode chat with
`mr-96`.

## 3. Credential path: issuance unchanged, delivery and refresh new

Unchanged: the host backend holds the `AppCredential` and calls
`POST /api/auth/token-exchange`. That cannot move into the browser — the
credential is a secret — so "one script tag" always means *plus one backend
endpoint* for authenticated users. A genuinely zero-backend embed is only
possible for anonymous sessions, which is out of scope here.

What the origin boundary changes:

- **Delivery is `postMessage`, never the iframe URL.** A `#token=` fragment
  lands in `document.location`, history, and potentially a referrer. The host
  fetches the token same-origin (cookie + CSRF, unchanged) and posts it in with
  an explicit `targetOrigin` — never `*`.
- **Refresh becomes a bridge round-trip.** The iframe has no cookies and cannot
  mint. ace-web's `getCanopyToken(true)` force-refresh becomes a request
  message: the widget reports 401/near-expiry, the host re-fetches and posts a
  fresh token.
- **Origin validation is load-bearing in both directions** — a surface that does
  not exist in the npm-component model.

**Framing must be opened first, narrowly.** `config/settings/base.py:121`
enables `XFrameOptionsMiddleware` with no `X_FRAME_OPTIONS` override, so
Django's default `DENY` applies and *every canopy page currently refuses to be
framed*. The embed route gets `@xframe_options_exempt` **plus**
`Content-Security-Policy: frame-ancestors` built from origins registered on the
`AppCredential`. Done that way it is stricter than a blanket exempt: only
registered hosts can frame it, and the allowlist is server-side data rather than
a setting someone edits.

The embed route serves a **minimal shell**, not the app bundle — no service
worker, no PWA shell. Per the PWA navigate-fallback rule (fail-safe allowlist),
an unknown route already goes to the network, so the embed route is excluded by
construction; it must not be added to `NAVIGATE_FALLBACK_ALLOWLIST`.

## 4. No JWT

`DelegatedToken`'s own docstring already settled this: *"DB-backed (not JWT) so
it is revocable and the table is the audit trail."* A JWT freezes its claims at
mint time, so a membership revoked mid-life still passes until expiry. The
current design re-resolves against the live DB on every request —
`secrets.token_urlsafe(32)` opaque, stored as sha256, `lookup()` filtering
`expires_at__gt=now` with `select_related("user")`.

So "everything resolves to the user's login/ACL" is already true *per request*
rather than per mint, and introducing JWT would weaken exactly that property.
A signed host assertion (HMAC/JWS) would only earn its keep for a host that
cannot make a server-to-server call; neither host needs that.

## 5. Identity for any user — the host is the identity provider

A widget user **never logs into canopy-web.** They authenticate to the host
(labs users via Connect/CommCareHQ OAuth), and the host — which holds the
`AppCredential` secret — vouches for them via `acting_as_email`. canopy already
supports this end to end: JIT creation is domain-agnostic
(`User.objects.create_user(username=email, email=email)`, plus a verified
allauth `EmailAddress` so a later real login connects to the same user instead
of forking).

**One conjunct is all that makes this Dimagi-only:**

```python
if domain not in app_domains or domain not in _allowed_login_domains():
    raise HttpError(403, "delegation not allowed for this domain")
```

The second clause is canopy's *login* allowlist — it answers "who may log into
canopy-web directly," a different question from "whom may a registered app vouch
for." Delegated exchange should be governed by the **per-credential**
`allowed_delegation_domains` only, which an admin grants explicitly per app.

The fail-closed property the original docstring was protecting survives intact:
an app with `allowed_delegation_domains: []` is still denied by the *first*
clause, so an unconfigured credential grants nothing. For a host that genuinely
serves any domain, a wildcard must be **explicit and opt-in** (`["*"]`) — never
"empty means any", repeating the `provision_role` lesson that this is an
allowlist, not a denylist.

This is independent of removing canopy-web's own login gate.
`BearerTokenAuthMiddleware` resolves delegated tokens upstream of
`LoginRequiredMiddleware`, so widget users work whether that gate stays or goes.

## 5a. Workspace membership implies trust

**A workspace must not contain untrusted co-tenants.** Anyone with membership is
trusted by everyone else in it. This is an invariant, not a default, and several
things follow from it that pulled the opposite way while it was unstated:

- **Runner-discovered sessions stay visible to the whole tenant** (§9 leg 3),
  and must not be narrowed by role. An agent's own emdash sessions land in
  `agent.workspace` (`harness/services.py`: `workspace=workspace or
  (agent.workspace ...)`) — the same workspace a user must join to see that
  agent at all. Under the invariant that is fine; a draft of this spec proposed
  gating leg 3 on role to defend against a co-tenant the invariant says cannot
  exist.
- **An open host must NOT set `provision_workspace`.** Auto-provisioning every
  user of a host into a tenant is precisely how untrusted co-tenants would get
  created. Membership is granted deliberately (invite), not as a side effect of
  opening a widget.
- **A user with no membership correctly sees nothing.** `current_workspace`
  raising for a user with zero memberships is the intended state, not an error
  to design around — they have not been given an agent to talk to. The widget
  should say so plainly rather than treating it as a failure.

`provision_workspace` remains right for a host whose users are *all* trusted for
that tenant — ace-web's internal Workbench is that case. It is the wrong tool
for open signup.

**Untrusted usage is therefore a different capability, not a weaker membership.**
If canopy needs to serve users who are not trusted co-tenants, that path runs
*without* workspace membership — no tenant data, against an agent explicitly
published for it — rather than by adding a lower-privileged member role. v1 of
this spec serves trusted users only.

**Open question this leaves (deliberately unanswered here): dynamic tenants.**
If each untrusted-or-separate user needs their own tenant, note that
`Agent.workspace` is a single NOT NULL FK with **no shared, template, or global
agent concept anywhere** — so a freshly created tenant starts with zero agents
and nothing can lend it one. Dynamic tenant creation therefore needs either
per-tenant agent instances or a new cross-tenant sharing concept. The NOT NULL
is itself a deliberate security invariant (six tenancy predicates independently
grew `workspace_id IS NULL`-means-allow legs while it was nullable), so it
should not be loosened to get there.

## 6. Tenancy: user-driven, with provisioning only where users are trusted

Two things were conflated in early drafting and must stay separate:

- **Which tenant a session lives in** follows the **user's** memberships.
  `_visible_slugs(request)` → `user_workspace_slugs(request.user)` is computed
  live per request, so a user in three tenants sees across all three and nothing
  in that path is product-specific. A host must **not** pin a workspace: that
  would override the user's own tenancy with the product's.
- **A brand-new user has no memberships at all**, and `current_workspace` raises
  `ValueError("no unambiguous workspace for user; specify one")` for both 0 and
  2+. So onboarding needs an answer or the first session cannot be created.

`AppCredential.provision_workspace` answers it **only for a host whose users
are all trusted for that tenant** (§5a). It is additive — `ensure_member` is
`get_or_create` and *"an existing member's role is never raised or lowered by an
app"* — so it cannot repin someone who already has their own tenancy. But
additive is not the same as safe: for an open host it would manufacture exactly
the untrusted co-tenants §5a forbids, so an open host leaves it unset and a
user without membership sees nothing.

Resolution order for a new session, therefore: the user's sole membership if
they have exactly one → **ask them** if they have several (`GET /api/workspaces/`
already exists to populate the picker) → the provisioned landing tenant if they
have none. Never a host-pinned constant.

## 7. Agent permissioning is a triple, and none of it exists yet

"Which agents do I have that are permissioned to operate here" is three-way:
**agent × host × user**. Nothing models it. `AppCredential` covers app×tenant
(`name`, `token_hash`, `allowed_delegation_domains`, `provision_workspace`,
`provision_role`, …) with **no agent relation at all**; `Agent.workspace`
(NOT NULL) covers agent×tenant.

v2 adds the missing edge as **explicit server-side rows on the `AppCredential`**
— which agents this app may target. Host-fixed, never client input, following
`provision_workspace`'s discipline and `RunnerAssignment`'s lesson that explicit
rows beat a self-declared capability string.

The widget's picker then shows the **intersection** of that allowlist with the
user's workspace memberships — both conditions, neither sufficient alone, which
is exactly the question as posed. Surfaced as `GET /api/embed/agents`.

## 8. Context comes over the bridge, deliberately

The agent does **not** run as you — it runs on a runner under its own identity
(its own `AgentCredential` slots). So "the agent knows my context" has three
possible answers:

| | How | Verdict |
| --- | --- | --- |
| **a** | Host pushes it: the browser holds the user's host session, so the host reads with **the user's** ACL and hands the agent a snapshot over the bridge | **v1 of this spec.** Correct ACL by construction |
| **b** | Agent holds a per-user delegated host credential | The general answer. `2026-07-31-server-side-gmail-watch-custody` already worked this shape out (per-mailbox grant, weakest sufficient scope, DWD rejected outright) |
| **c** | Host mints a user-scoped token *for* the agent — exchange in reverse | Symmetric, but makes the host an identity provider to canopy: a second auth direction to secure |

**(a) is chosen on its merits, not as a compromise:** the agent's view of your
context is exactly what you can see on the page you are looking at. "The agent
sees what you see" is an ACL story that cannot leak, because nothing is resolved
outside your own session.

Canopy holds **no host context** and should not: it would become a stale mirror
of every host's permission model. This follows the references-not-values
instinct already in `Agent.runtime_secrets` ("secret-reference NAMES … never
values") and `AgentCredential`.

For connect-labs the bridge has a real seam to attach to. Workflow render code
receives `{definition, instance, workers, pipelines, links, actions,
onUpdateState, view}` as live props — including in-progress state no saved run
has frozen — so reads and one gated write both already exist as props. The host
adapter is called from `workflow-runner.tsx` with those props:

```js
canopy.provideContext(() => ({ instance, definition, state: view.state }))
canopy.registerAction('updateState', (patch) => onUpdateState(patch))
```

Template authors opt in per template (a `context_manifest`, in the spirit of
saved-runs' `snapshot_inputs`) — a snapshot at session-open, not a continuously
synced channel. Continuous sync (CopilotKit's `useCopilotReadable` is the
reference shape) is future work and is not needed to prove this.

## 9. ACL prerequisite: by-id reads must agree with the list (shipped, #749)

`_session_or_404` gated on workspace membership alone, so any co-tenant holding
a session UUID could read a conversation the **list** already refused to show
them — across all 14 endpoints that resolve through that one helper.

An earlier draft of this section said "enforce `SessionParticipant` on by-id
reads." **Implementing it proved that wrong**, and it would have broken the
product: `harness/services.py` creates runner-discovered sessions with no
`created_by` and no participant row, so requiring participation makes every
emdash-discovered session unreachable by *everyone*.

Worse, the list's own predicate was wrong in a way that directly defeats this
spec. It keyed co-tenant visibility on having a `RunnerBinding` — and a **web**
session acquires a binding the moment a runner picks it up. So **every widget
session would have become co-tenant-readable as soon as it started running.**
That was confirmed by a failing test, not reasoned about.

`origin` is the property that actually separates "nobody in-app created this"
from "someone did". Three ways in, no fourth: you created it; you are a
`SessionParticipant`; or it is a runner-*discovered* session a runner is
actually reporting. Both callers now read one predicate
(`apps/canopy_sessions/access.py`) with a parity test asserting they agree —
the guard shape `test_claim_schedule_parity` uses.

Known trade-off, documented at the predicate: `created_by` is `SET_NULL`, so
deleting a user drops their web sessions out of the API rather than opening them
to every co-tenant. Losing them from a list is the cheaper mistake than
publishing a departed colleague's private chats.

## 10. Honest limits of v1

These are consequences of the design, and should be documented on the embed
surface rather than discovered:

- **Actions require the page to be open.** The bridge executes in your browser
  with your host session — excellent for ACL, but close the tab and the agent
  cannot act. Action is scoped to the life of your visit. Server-side action is
  case (b)/(c) above.
- **A turn needs a runner online and assigned.** `claim_next_turn` gates on the
  **pairing human's** workspaces (`user_workspace_slugs(runner.paired_by)`), so
  the runner must be paired by someone in the agent's workspace or the turn sits
  QUEUED forever. This took prod down once (4 of 5 agents wedged).
- **The agent does not remember prior conversations.** You can see and resume
  them — `Q(created_by=request.user)` + `origin_key` + agent is already exactly
  that query — but canopy has no cross-session memory concept, and each session
  is its own Claude Code session with its own transcript. A fresh conversation
  starts cold unless the agent carries its own memory.

## 11. Non-goals

- **Cross-system bridging** (one session reasoning across two external systems)
  — unchanged from v1: no concrete second consumer, and the credential resolver
  keyed by tenant+system has no standard to lean on.
- **Continuous context sync** — snapshot at session-open only (§8).
- **Anonymous / zero-backend embed** — authenticated users need the host-side
  token endpoint (§3).
- **Replacing labs' `workflow_*` MCP tools** — those remain right for persisted
  and historical workflow data. This adds a live, in-session surface for the
  instance currently open.
- **Replacing labs' edit-mode workflow chat** (`workflow_agent.py`) — that
  authors workflow JSX; this reasons about a live run. They coexist.

## 12. Open questions

- Whether `frame-ancestors` origins live on `AppCredential` as a list column or
  a related table — a host with many embedding domains argues for the latter.
- Whether the agent allowlist should support a per-agent default so a host can
  express "this one unless the user picks otherwise", avoiding a picker in the
  common single-agent case.
- Whether `GET /api/embed/agents` belongs on a new `embed` router or extends the
  existing agents router with an `app=` scope.
