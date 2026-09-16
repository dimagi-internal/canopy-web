# The page is told when its data changes

**Status:** design, not built.
**Prerequisite:** shipped — FastMCP 4 / mcp SDK 2.2 (#812), which is where the
subscription machinery lives.

## The bug this exists to remove

Ask canopy "close everything" on `/insights` today and what you see depends on
which tool the agent happens to pick, in ways it has no reason to reason about:

| it calls | what you see |
|---|---|
| `page_dismissInsights` | rows vanish in front of you |
| `clear_insights` | the page keeps showing twenty rows that no longer exist, until you reload |

Same sentence, two outcomes. The second is worse than it looks: `clear_insights`
with no filters clears **everything the user can see**, not the twenty on screen
(its own docstring says so — "intended, so be careful"), and the page goes on
displaying them. Clicking one 404s.

This is not an agent problem. The same staleness arrives when the fleet dismisses
an insight while you are looking at the feed, when a scheduled turn fires, when
you have the page open in two tabs, or when a colleague acts. The agent just made
it easy to notice.

## The invariant

> A page declares what it is showing. If that data changes — **by any actor,
> through any door** — the page is told.

Everything below is mechanism for that one sentence. Note what it does not say:
it says nothing about agents, and nothing about insights.

## Three doors, and which one a thing goes through

The first draft of this design said "prefer page actions, they keep the screen in
sync." That was wrong, and worth recording as wrong: it optimises for one symptom
and makes the architecture worse. Routing a data mutation through a browser tab
means it is unaudited, unavailable when the tab is closed, bounded by a 20-second
timeout, and implemented twice.

| kind of thing | door | why |
|---|---|---|
| **read** | server tool | live, ACL-correct, works headless. The page supplies only a SELECTION |
| **write to server data** | server tool | audited, rate-limited, revocable, survives the tab closing |
| **write that exists only in a browser** | page action | scroll-to, open a drawer, fill a form, apply a filter — there is no server equivalent |

**Consequence: `dismissInsights` stops being a page action.** It is a data
mutation wearing a page action's clothes, and it only exists because we had no
way to tell a page its data changed. Remove that constraint and it collapses into
`clear_insights`. `apps/canopy_sessions/page_actions.py` stays — the durable row,
the doorbell, the bounded wait that refuses out loud are all still needed for the
browser-only cases.

## The vocabulary is MCP's, not ours

MCP already specifies this and we should not invent a parallel concept. From the
2026-07-28 spec:

```json
{"capabilities": {"resources": {"listChanged": true, "subscribe": true}}}
```

A client opens `subscriptions/listen` naming resource URIs; the server delivers
`notifications/resources/updated` carrying only the `uri`; the client re-reads.
The spec's own flow diagram is precisely the mechanism below — notify, then
refetch, deliberately **not** a diff, because re-reading goes through the normal
path where authorization applies.

So the key is a **resource URI** (`insight://`, `insight://61`), not the
per-tool tag an earlier draft of this invented. URIs address instances as well as
collections, they are what a subscribing client already understands, and
subscription is capability-gated so a server that cannot do it simply says so.

## Where invalidation is raised: the model layer

The tempting version is "every mutating tool publishes an invalidation." That is
N sites and it rots. This repo's own evidence: `page_tools.py` shipped with ten
passing tests and no import; six tenancy predicates each independently grew a
`NULL means allow` leg. **Anything that requires every future author to remember
will be forgotten.**

The shape that does not rot already exists here, in `apps/push/signals.py`:

> "An Item is a real row, so one receiver covers everything — no per-producer
> hops, no Drive-backed staleness, and nothing to keep in sync."

Same move. One `post_save`/`post_delete` receiver per resource, and
`transaction.on_commit` to coalesce — a fleet audit that writes 200 rows in one
transaction must produce one notification, not 200. `apps/push` already
demonstrates that exact coalescing.

## Two consumers, one vocabulary

The subscriber MCP designs for is the **agent**. The consumer we actually need to
reach is the **page**, and a browser page is not an MCP client and never will be.
Nobody has joined those ends — AG-UI carries agent→app state within a run and has
no opinion on data changing outside one (searched; nothing).

So:

| consumer | transport |
|---|---|
| agent | `SubscriptionBus.publish(ResourceUpdated(uri=...))` — the SDK's own seam |
| page | an AG-UI `CUSTOM` event over the session socket that already exists |

Both carry the same thing: a URI and nothing else.

The SDK's bus is a `Protocol` whose docstring anticipates our deployment shape:

> "Implement this over an external pub/sub backend (Redis, NATS, ...) to fan
> events out across replicas."

canopy runs on ECS with Redis already present (`channels-redis`,
`PRESENCE_REDIS_URL`), so the multi-task case has a known answer rather than a
surprise.

## What the page does when told

**Refetch.** Not a patch, not a diff. It re-reads through the same tool it
declared in `backing_tool`, which is the one path with the right ACL. A diff
would be a second source of truth for data the page already knows how to load,
and the page is a cache — the rule `page_state.py` already states.

## The contract, for canopy and for every other product

Three things, none of which mention insights, agents, or canopy:

1. **A page declares its selection and the resource it is showing.** Already
   shipped as `usePageState` + `describeSelection` (#807); gains a resource URI
   beside `backing_tool`.
2. **A tool declares the resource it touches.** One decoration, in your own code,
   where you already know what the tool reads.
3. **The framework invalidates on change.** Written once, in the framework.

Cost to add this to connect-labs: one decoration per MCP tool, one
`usePageState` line per page. That smallness is the point — it is what makes
"an AI can implement this in my other products" true, and it is only small
because the hard part lives in one place.

## What this does not resolve

- **A page that is not open.** Invalidation reaches attached sessions. A tab
  opened later reads fresh data anyway, so this is correct rather than a gap, but
  it is worth stating that nothing is queued for absent pages.
- **FastMCP has no server-side subscription API.** SDK 2.2 has the bus and
  `send_resource_updated`; FastMCP 4.0.4's own subscription code is client-side
  only. We reach through to the SDK, in one seam module, the way `agui.py`
  already does for AG-UI. When FastMCP grows the high-level API, that module is
  the only thing that changes.
- **Ordering against a turn in flight.** An agent that reads, then acts, then is
  told the data changed has no way to know whether its own write caused the
  notification. Self-notification suppression is out of scope here; the cost of
  getting it wrong is a redundant re-read.
- **Whether a contact may be told.** `/api/contact/` is the whole contact surface
  and this adds nothing to it, so contacts get no invalidation today. That is the
  safe default and is consistent with the disjoint-predicate design.

## Why this is worth building rather than papering over

The narrow fix — make the agent prefer the page action — leaves the page lying
whenever anything else mutates the data, which is most of the time in a system
with a fleet. The general fix costs one receiver per resource and makes the page
honest no matter who changed what, including actors that do not exist yet.
