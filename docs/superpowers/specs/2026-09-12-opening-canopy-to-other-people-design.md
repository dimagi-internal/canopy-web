# Opening canopy to other people — first run, a self-documenting app, and a public explainer

**Date:** 2026-09-12
**Status:** Design — not implemented
**Companion:** `2026-09-12-github-backed-agent-creation-design.md` — agent creation that
produces a real repo. Deliberately a separate spec; see § "Why agent creation is not in here".
**Builds on:** `2026-09-05-agent-credentials-design.md` (provisioning an agent from a
browser instead of a terminal — this spec is the same thesis applied to the humans)

## Problem

> *"We're going from an entire platform I built myself to one that I want to start
> opening up to Dimagi power users."* — Jonathan, 2026-09-12

Three things were asked for: a public explainer of what canopy is and how it works
(a component view, good enough to send to someone outside Dimagi), end-user docs for
canopy-web itself, and an explanation of how the agent system works. The docs were
asked to be "self-documenting and dynamic as much as possible", because everything
static in this repo goes stale — the canopy plugin's own README still describes the
system as a self-improvement loop over Claude Code sessions, which is now a small
fraction of what it does.

**The finding that reorders the work: this is a product problem before it is a
documentation problem.** Four dead ends were confirmed by reading the code, not by
speculating:

| # | Gap | Evidence |
|---|---|---|
| 1 | **A new user sees a blank page.** A signed-in user with no workspace membership lands on `/`, `RootRedirect` renders `null`, and nothing appears inside the app shell. Not an error, not an empty state. `TenantRedirect` does the same for every legacy flat path. | `frontend/src/router.tsx:126`, `:135` |
| 2 | **No way to create a workspace.** `POST /api/workspaces/` exists and works. Nothing in the frontend calls it — the only workspace writes the UI makes are invite-preview and invite-accept. | `apps/workspaces/api.py:102` vs. `frontend/src/api/workspaces.ts` |
| 3 | **No way to create an agent.** `POST /api/agents/` (upsert by slug) exists. No frontend caller; agents exist only because the plugin POSTs them. | `apps/agents/api.py:115` |
| 4 | **No way to see, mint, or revoke a Personal Access Token.** `POST /api/tokens/` and `DELETE /api/tokens/{pk}/` exist with zero UI. `/settings` offers the AI backend switch, Claude subscription auth, a presence toggle and debug-session minting — and nothing else. | `apps/tokens/api.py`, `frontend/src/pages/SettingsPage.tsx` |

Compounding (1): the header workspace switcher returns `null` at
`workspaces.length <= 1` (`frontend/src/components/AppLayout/AppLayout.tsx:188`), so
a user with zero workspaces has neither a switcher nor a create affordance anywhere
on the page.

**The pattern, which matters more than the four bugs:** *every write that turns a
person into a tenant, an agent owner, or an authenticated machine caller exists in
the API and is missing from the UI.* Those are the first three things a new power
user needs. They are missing for one reason — they have only ever been done from a
shell or from the plugin, by the person who wrote them. That is the signature of a
platform built for one operator, and it is the whole of what "opening it up" means.

### A note on method, so the next person doesn't over-trust it

The four gaps above were found by a mechanical diff: every `@router.post|put|patch|delete`
across `apps/*/api.py` against every write call in `frontend/src`. **Roughly two thirds
of that output is noise** and it must not be treated as an audit. Machine-only contracts
(`runners/{id}/heartbeat`, `turns/{id}/finish`, `inbound/gmail/{workspace}/`) correctly
have no UI caller, and the diff mis-attributes any app whose mount prefix differs from its
label (`canopy_sessions` mounts at `/api/canopy-sessions`, `runs` at `/api/ddd`,
`session_sharing` at `/api/sessions`). To become a real instrument it needs the mount map
from `apps/api/api.py` plus an explicit machine-only classification. It is a hint
generator. A genuine cold-start audit — a fresh account walked through every path — is
**deliberately deferred** until the known gaps are closed, so the expensive discovery
runs against a system that already handles the obvious cases.

## Decisions

1. **Fix the known gaps before documenting them.** Documentation for a path a power
   user cannot walk is worse than none: it converts a missing feature into a broken
   promise.
2. **Enforce coverage; do not attempt to enforce accuracy.** A fast test proves every
   surface is described. Proving a description is *true* means driving the live app
   (canopy already has the machinery, via `canopy:walkthrough`), which costs minutes
   per claim. Rejected for now on speed grounds — the loop through CI is moving too
   fast to spend that. Revisit for the first-run path alone, where a wrong doc loses
   a user permanently.
3. **Descriptors are data, colocated with the routes** — not prose in a wiki, not
   Markdown in `docs/`. Same choice, and the same reasoning, as
   `frontend/src/pages/systemWorkflows.ts`: data can be read by a test and rendered
   by a page; prose in another repo can be neither.
4. **The public page and the authed guide share one registry of user paths**, so the
   public promise cannot drift from the internal documentation. This is the only
   structural coupling between them, and it is deliberate.
5. **No maturity badges.** Labelling each path Solid/Rough/Planned was considered and
   rejected: maturity changes faster than the page does, and a stale "rough" reads as
   a live warning. What doesn't work and is fixable gets fixed instead.
6. **No "ask the docs" chatbot.** It lets bad structure survive by papering over it,
   and it can invent answers about a system whose failure modes matter.
7. **The agents empty state teaches rather than offering a button.** Until the companion
   spec ships, a web-created agent would be a record with no repo, no persona and no
   skills — unable to take a turn. A half-created agent is worse than no button, so the
   empty state explains what an agent is and hands over `/canopy:create-agent`.

## Part A1 — first run works

Four changes, all wiring endpoints that already exist and are already tested.

**The first-run screen** replaces both `return null` sites. A signed-in user with no
membership gets a real page: what a workspace is (one short paragraph — it is the
tenant that owns agents, projects and demos), a **Create workspace** form, and the
alternative path ("if a colleague invited you, open their `/invite/...` link"). This
screen is the single highest-value piece of documentation in the whole spec, because
it is the only one every new user is guaranteed to read.

**Create a workspace** posts to `POST /api/workspaces/` from that screen, and from a
persistent affordance in the header. The switcher's `workspaces.length <= 1` guard is
split in two: hide the *switcher* when there is nothing to switch between, but always
offer *create*. Conflating "nothing to switch to" with "nothing you can do" is what
produced the dead end.

**PAT management** on `/settings`: list tokens with their created/last-used dates,
mint one (shown exactly once, with a copy button — the existing `CopyBlock` component
already does this for debug sessions), and revoke. This is the gate on two of the five
ways in — pointing another tool at canopy's MCP surface, and setting up the plugin —
and it is currently only reachable by running a skill that requires the plugin you are
trying to set up.

**The agents empty state** explains what an agent is, links to `/guide`, and hands over
the `create-agent` command. It becomes a button in the companion spec.

## Part B — the self-documenting app

**One new data file**, `frontend/src/guide/surfaces.ts`: one descriptor per route,
keyed by the exact path string from the route table. Per surface — what it is in one
line, who it is for, what you need before it does anything useful, and what you can do
on it. 43 entries, verified against `router.tsx`: 52 path literals are declared, 12 are
redirects, legacy aliases or the catch-all and need no descriptor, leaving 40 today —
plus the three surfaces this work adds (`/new-workspace`, `/guide`, `/about`).

**One small enabling refactor.** `frontend/src/router.tsx` currently passes its route
array as an inline literal to `createBrowserRouter(guarded([...]))`. Extract it to a
named `export const routeTable`, passed in. Recovering paths from a *built* router is
awkward; reading them from an exported array is three lines in a test. Nothing else
about the router changes.

**`/guide`** renders the descriptors grouped by `nav.ts`'s existing grouping, so the
guide's shape matches the menu people actually look at, with the first-run sequence
pinned at the top as an ordered list rather than a peer group. Anchors are route paths
(`/guide#/w/:workspace/chat`) so anything can deep-link to the explanation of a
specific surface.

**One fast test.** Flatten `routeTable`, drop redirects, legacy aliases and the
catch-all, then assert two directions: every remaining path has a descriptor, and
every descriptor names a real path. The second direction matters as much as the first
— it catches a descriptor orphaned by a deleted route, which is how a generated guide
starts lying. Pure array comparison, no browser, no network, inside the existing
`vitest run` alongside the current 58 test files.

**Empty states deep-link into the guide.** The stuck-user surface and the documentation
surface become the same content, which is what stops the guide from being a thing
nobody opens.

## Part C — the public explainer

A chrome-less public page on `PublicLayout` — the pattern `/storyboard` and
`/ddd-release` already use, mounted outside `AppLayout` so anonymous visitors are not
bounced to login by the shell's authed calls. No Dimagi account, no plugin, no install.

**Content**, in order: what canopy is in two sentences; live counts; the component view;
the five ways in; links to the public artifacts that prove it works (a storyboard, a
walkthrough) and to `/api/docs/` for the API.

**The component view** is the piece that was actually missing. Five components and the
one sentence that connects them:

| Component | What it is |
|---|---|
| **canopy-web** | The system of record and every human surface. Owns workspaces, agents, turns, items, schedules, sessions and published artifacts |
| **The canopy plugin** | The capability library — skills, agents and commands, installed into Claude Code. What an agent knows how to *do* |
| **Runners** | The execution substrate. A paired laptop (emdash + CDP) or a cloud box (ACP). Claims turns and runs Claude Code |
| **Agents** | Persona repos stamped from the factory — identity, domain skills, hooks. `echo`, `eva`, `hal`, `ada`, `ace` |
| **The harness** | The control plane joining them: turn lifecycle, claim and lease, the event ledger, routing |

> **A turn is the unit of work.** canopy-web owns the record of turns, runners execute
> them wherever your compute happens to live, and the plugin is the library of what an
> agent knows how to do.

**Live counts** come from a new `GET /api/system/public-stats`, `auth=None`, aggregates
only: number of agents, skills in the catalog, runners currently online, turns executed,
published demo packages (DDD run releases). **No names, no slugs, no workspace or
tenant data, no content, and nothing per-agent** — a count cannot leak what a count is of. Cached briefly so the page
is not a free load generator. The reason to build this rather than write numbers into
prose is that prose is wrong within a month, and "we run a fleet of agents" reads as a
claim while `2 runners online` reads as a fact.

**The five ways in** is generated from a shared `frontend/src/guide/paths.ts`, consumed
by both this page and `/guide`:

| Path | Who you are | Where you start |
|---|---|---|
| **You were sent a link** | An audience. No login, no install | `/storyboard/:slug`, `/narrative/:slug`, `/walkthrough/:id`, `/share/:token` |
| **You want work done by an agent** | An operator | `/w/:ws/chat` — plus the choice of *where it runs*: a cloud runner, or your own laptop so you can jump into the terminal |
| **You keep agents unblocked** | A supervisor | `/supervisor`, installable as a phone PWA, pushes when an agent needs you |
| **You want canopy's skills in your own sessions** | A Claude Code user | Install the plugin; `/canopy:*`. No fleet, no runner |
| **You want to build an agent** | A builder | `/canopy:create-agent` (a button, once the companion spec ships) |

Two notes on that taxonomy, since it differs from the one we started with. "Watch a turn
in the browser" and "watch a turn locally" were originally separate paths; they are one
job with a deployment choice inside it, and splitting them hid the fact that the *choice*
is the interesting part. And "you were sent a link" was missing entirely despite almost
certainly being the highest-volume interaction anyone has with canopy.

## Why agent creation is not in here

Creating an agent properly means creating its GitHub repo and linking it — a bare record
is meaningless. That needs a GitHub App, a per-user OAuth grant, an owner picker, and the
agent factory extracted from the canopy orchestrator into a shared package. It is larger
than A1, B and C combined and it needs a GitHub App registered before a line of it can be
tested. Folding it in would stall the documentation work behind an app registration —
the same trap avoided by deferring the cold-start audit.

The seam is clean and nothing gets rewritten: this spec's agents empty state teaches the
flow, and the companion spec upgrades that same empty state into a button.

## Testing

- **Route/descriptor coverage**, both directions, in the existing vitest suite.
- **The first-run screen renders for a zero-workspace user** — the case that currently
  renders `null`. A unit test on the redirect components with an empty membership list.
- **Workspace create, PAT mint and PAT revoke** each get a happy path plus the error the
  API actually returns (duplicate slug on create; 404 on revoking someone else's token).
- **`public-stats` is anonymous and leaks nothing**: asserts 200 without auth, and
  asserts the response body contains no names, slugs or ids. The second assertion is the
  load-bearing one — an anonymous endpoint on a multi-tenant system is exactly where a
  field gets added later without anyone re-asking whether it should be public.
- **No new CI workflow.** Everything above runs inside the existing backend and frontend
  test commands.

## Out of scope

- **The cold-start audit.** Deferred on purpose until the known gaps are closed.
- **Accuracy verification of descriptors.** Coverage only; see Decision 2.
- **GitHub-backed agent creation.** The companion spec.
- **Rewriting the canopy plugin's README.** It is stale in the same direction, and it is a
  different repo. Worth doing; not this.
- **Anything about how the agent *system* works beyond the component view.** The public
  page explains the five components and the turn. The deep version — routing cascades,
  the item ledger, transcript custody — stays in `ARCHITECTURE.md` and the specs, because
  its audience is someone changing the code, not someone deciding whether to try it.
