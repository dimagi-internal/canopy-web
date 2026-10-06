# Canopy's public site and new mark

Status: DRAFT for owner sign-off (2026-10-05)

## What this is

canopy moved to its own host, `https://canopy.dimagi.com/` (#1166). A signed-out
visitor there today gets bounced to Google sign-in, which says nothing about what
canopy is. This adds:

1. **A new mark** replacing the bare-branch tree everywhere it is drawn.
2. **A public product site** at the root of `canopy.dimagi.com`, built like its
   Dimagi siblings (Connect, SureAdhere, Open Chat Studio), but kept in its own
   folder so it can move to separate hosting later without a rewrite.
3. **A closed-beta sign-up**: a visitor leaves their email and why they want access,
   and that request is emailed to Jonathan.

canopy is not being sold. The site exists so that a link to canopy explains
canopy — the same full product experience the sibling sites give, and the place the
"open source control plane" story from the IDM AI talk lives.

## 1. The mark: the low dome

Chosen over three rounds on the brand canvas
(https://claude.ai/artifact/DgjhaGr3AX1gJejmXDT833, "G4"). A wide, shallow canopy in
three separate segments over a row of three dots: many parts making one cover, over
the people and agents working beneath it.

It replaced the tree because the tree is a stick figure that gets spindly below 32px.
It replaced the earlier semicircle variant because an even semicircle split in three,
with a dot at its centre, reads as a speedometer.

Geometry, in a 100×100 box (from the exploration generator):

- three arcs on one circle, centre (50, 100), radius 60, stroke 13, butt caps,
  spanning 150°→113°, 107°→73° and 67°→30° (6° gaps);
- three dots, r 7, at x = 33, 50, 67, y = 72.

**One correction on the way in:** at radius 60 the outer arcs reach x ≈ −2 and 102,
so the mark clips when drawn without the app-icon tile. The production geometry is
scaled to fit the box with padding; the shape does not change.

**Where it goes.** `assets/brand/` stays the one source of truth: `tree.py` is
replaced by the new mark's geometry (renamed `mark.py`), and `generate.py`
regenerates every output — `favicon.svg`, the PWA icons (including maskable), the
menu-bar PNGs (still monochrome, still tinted per runner status) and the `.icns`.
The README is updated. The app header uses the same mark, and so does the site.

Colours come from the existing Warm Earth palette (green `#3FA374` on bark
`#26211D` for the tile; deep green `#1F6B4A` / ink on light).

## 2. The site

### Where it lives

- **`site/`** at the repo root: an **Astro** project (the stack the Open Chat
  Studio site and Connect's in-progress split use). Shared `Header`, `Footer` and
  section components, so pages do not copy chrome the way SureAdhere's plain HTML does.
- It builds to static files in `site/dist/`. Nothing in it imports from `frontend/`
  or Django; the one runtime dependency is the beta-request endpoint (§3), called
  by URL.
- **Moving it later** (a Cloudflare Worker, GitHub Pages) means pointing that host
  at `site/`, and pointing the form at canopy's API by absolute URL — a hosting
  change, not a rewrite.

### Routing

- **Signed-out `GET /`** → the site's home page. **Signed-in `GET /`** → the
  workbench, as today. Decided in one Django view ahead of the SPA catch-all.
- Other site pages (e.g. `/how-it-works`) are served only if that path exists in
  `site/dist`, so no app route can be shadowed; the set is read from the build output,
  not hand-listed.
- The site's own assets are built under `/site/…` so they cannot collide with the
  SPA's `/assets/`. Same cache rule as everything else (`config/static_cache.py`):
  hashed names immutable, pages `no-cache`.
- Site paths are added to the login middleware's public allowlist. The PWA's
  navigate-fallback allowlist is NOT widened: a site page is server content, and
  "unknown ⇒ network" already does the right thing.

### Look

Work Sans with JetBrains Mono eyebrows, the shared Dimagi header ("canopy" wordmark
+ "by Dimagi") and the shared Dimagi footer, matching the sibling sites and the IDM
talk deck. The app keeps DM Sans. Dark hero, light body, Warm Earth greens for
accent.

### Home page copy outline

Drawn from the IDM AI Talk deck (Canopy section and its speaker notes), which is
already Jonathan's vetted framing.

| Section | Content |
|---|---|
| Hero | Eyebrow *Open source · by Dimagi*. **"Canopy — the open source control plane for agent oversight."** Give an agent deep expertise, let people use it, and let a person step in to verify before it answers. Buttons: **Request access** (§3), **View on GitHub**, and a quiet **Sign in**. |
| The problem | Most AI tools are built for one person and one chat. The six-step ladder from "me and AI" (use it, learn from it) to "us and AI" (improve it, share it, improve it together); canopy is steps 4–6. |
| How it fits | Context · LLM (used as is) · Harness · Runner (laptop or cloud) · Interactions (email, Slack, existing apps), with two bands across them: guardrails & verification, and the learning loop. The same shape as CI or Kubernetes: canopy decides what runs, when and where; runners execute it. |
| Three-up | **Multiplayer sessions** — several people and an agent in one live conversation; watch, or step in. **Agents that improve as they run** — a correction fixes the agent, not one reply (ACE: 162 skills, 2,480 revisions). **Oversight in code** — manual/auto set by a person, never by the agent; rules enforced by hooks, each pinned by a test; callers untrusted by default. |
| Who it's for | Agent builders and reviewers · people who ask agents for work · learners and viewers. |
| What needs a person | The Supervisor inbox: proposals rather than surprises; one fix rolled out to several agents; works on a phone. |
| Evidence | The fleet at Dimagi, honestly sized: "8 agents and a growing group of people." |
| Close | Request access, or read the code. |

**Claims the copy must not make** (from the deck's own notes): no fleet-wide kill
switch, no spend limits, and manual-mode approval is procedure, not a code-enforced
pop-up. Numbers on the page carry their as-of date.

One inner-page template ships with it ("How it works"), so a new page is content,
not layout.

## 3. Closed-beta sign-up

Every **Request access** button opens the same panel:

> **Canopy is currently in a closed beta.**
> Enter your email to request access.
>
> Email · Why do you want access? · [Request access]

On submit the panel says the request was received and nothing more — no promise of a
reply time.

**Where it goes: Jonathan's inbox.** `POST /api/beta-requests` (public, no login):

- Validates the email and requires a reason (capped at 2,000 chars).
- **Stores a `BetaRequest` row** (email, reason, created_at, user agent, notify
  result) — so a request is never lost to a mail failure, and Django admin can list
  them. The notification is best-effort and its outcome recorded on the row, the same
  rule as workspace access requests.
- **Emails it** through the existing labs SES path (`apps/common/email.py`) to
  `CANOPY_BETA_REQUESTS_TO` (default `jjackson@dimagi.com`), with `Reply-To` set to
  the requester, so answering is just replying.
- Abuse: a honeypot field, a per-IP limit (5/hour), and a per-email idempotency
  window (a repeat within 24h is accepted but not re-mailed).
- Responds the same way whether or not the address was seen before, so the form
  cannot be used to test who has asked.

This is deliberately NOT a workspace access request: those need an account and name
a workspace, and grant something when approved. A beta request grants nothing; it is
a note to a person, who then invites them through the ordinary invite flow.

Lives in a new product-tier app, `apps/site`, beside the view that serves the site
pages; `tests/test_architecture_boundary.py` gets its tier.

## 4. Build and deploy

- `site/` gets its own `package.json`; CI's frontend job builds it, and the
  Dockerfile builds it in the Node stage and copies `site/dist` into the image.
- The route docstring of the beta endpoint is public API text; `generated.ts` is
  regenerated.

## License (decided 2026-10-05)

`dimagi-internal/canopy-web` was public with no LICENSE file — "all rights reserved",
not open source. It now carries **BSD-3-Clause** (owner decision, "for now"), matching
CommCare HQ and Open Chat Studio, canopy's nearest Django siblings (the CommCare mobile
repos are Apache-2.0; `commcare-connect` has none). Apache-2.0's patent grant was
weighed and judged marginal for canopy; relicensing stays cheap while Dimagi holds all
the copyright, and gets harder once outside contributions land.

## Testing

- Routing: signed-out `/` serves the site; signed-in `/` serves the workbench; an
  app deep link is never answered by the site; a missing site path falls through.
- Beta requests: stored and mailed; mail failure still stores and returns success;
  honeypot, rate limit and idempotency; identical response for new and repeat emails.
- The mark: `generate.py` output committed; the favicon actually renders in the
  built app (rendered, not curled — the PWA lesson).
- The live check after deploy: load `https://canopy.dimagi.com/` signed out and
  submit one real request.
