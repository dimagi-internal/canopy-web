# Narrative storyboards, first-class feedback, and one writer per field

**Status:** design, approved 2026-07-26
**Spans:** `canopy` (the DDD runtime, `scripts/ddd/`), `canopy-web` (this repo), and the
`ace:narrative-iteration-review` skill.

## Why

Two problems arrived at the same time and turned out to be one problem.

**1. A single narrative is no longer the unit of the story.** The supply work is
authoring four narratives against one external audience (Ellyn / ECF), explicitly ordered
as an arc — procurement integrity → the command centre → what the money bought — whose
closing beat calls back to `nutrition-demo`, a *fifth* narrative in a different app that
the same person already watched. Told linearly this is far too long; told as acts with
scenes it is a product tour. canopy-web has no object above `narrative`, and `narrative`
is not even a table — `apps/runs/aggregate.py` infers it at read time from a `run_id`
slug prefix. There is nothing to share, and nothing for feedback to attach to.

**2. Narrative identity is unstable, and the local↔cloud seam is losing data.** The DDD
system was built local-first, deliberately not depending on canopy-web. That premise has
been retired in practice — the human reviews on canopy-web, so canopy-web is where the
narrative lives — but the code still hedges, and the hedge is a content-hash merge
algorithm between two writers of one file. It has real defects (below), and they land
hardest on exactly the surface this work needs to be good: the plain-language vN→vN+1
diff a domain expert reads.

So: fix identity, split the ownership, then build the sharing surface on top. In that
order, because each step shrinks the next.

## Standing constraint: history is disposable

**Only the latest version of each narrative matters.** Prior versions are not an asset
worth carrying migration complexity for. Where preserving lineage is free, keep it; the
moment it costs a merge step, a compatibility shim, or a careful backfill, drop it and
take the latest version as ground truth.

This is load-bearing in three places:
- the L0 backfill wants ids that line up with stored history, but if they diverge the
  only casualty is the before/after view of *old* versions, which nobody reads;
- the L1 lockfile does **not** need to reproduce old versions, so it is justified by PR
  legibility and offline renders alone;
- the L1 migration may **regenerate** a narrative from its current version rather than
  carefully splitting the existing file, and a narrative that resists migration may
  simply be reset.

## Current state (verified)

### Confirmed defects

**D1 — the join key between the two domains is mutable and web-owned.**
`merge_narrative_into_spec` (`canopy/scripts/ddd/narrative.py:1047`) matches local scenes
to web scenes by `_title_slug(ps["title"])`. `title` is in `_NARRATIVE_SCENE_FIELDS`
(`:908`), i.e. web-owned and editable in the review UI. Reword a title, the lookup misses,
`base = {}`, and `base.setdefault("show", "")` (`:1049`) writes an empty render recipe.
Every hard-won selector is one wording tweak from silent deletion.

**D2 — the pull path writes the voiceover into the wrong field.**
The v2 roundtrip is correct in two of three directions: the review shows
`scene.narrative` (`_scene_text_for_review:131`, resolution order 1) and
`apply_narrative_edits` writes edits back to `scene.narrative`, explicitly leaving
`concept_claim` alone (`:645`). But `web_narrative_to_spec_parts:994` maps the web's
`narration[].text` into **`concept_claim`**. A pull therefore leaves: stale
`scene.narrative` (still the old VO — and still what the next push presents as "current"),
a destroyed `concept_claim` (overwritten with narration prose), and a `spec.narrative`
paragraph agreeing with neither. This is data loss, not churn.

**D3 — the vN→vN+1 diff is broken by its own producer.**
`frontend/src/components/ddd/narrativeScenePairing.ts:18` — the domain-expert before/after
view shipped in PR #294 — states: *"Scenes reused across versions keep a stable `id` —
match on it so a diff is robust to reordering."* They do not.
`build_narrative_review_request` (`narrative.py:278`) sets
`NarrationItem.id = _title_slug(scene.title)`. Titles are precisely what a language
reviewer edits, so every reworded title renders that scene as **removed + added** rather
than changed — on the one surface built so a non-engineer can see what changed.

**D4 — cloud identity cached in a git-tracked file.**
`narrative_synced_version` / `_hash` / `_at` are written into the spec
(`narrative.py:1230-1232`, `:1538-1540`), so every sync churns a versioned file and
reconciliation is manual.

### Not a defect (previously reported, now stale)

"The gate reviews `concept_claim` but the video speaks `scene.narrative`" describes
pre-v2 behaviour. The push and apply paths agree on `scene.narrative` today. Only the
*pull* path was left behind — that is D2.

### Root cause

D1, D2 and D3 are all the same thing: **more than one writer per field, and an identity
derived from a mutable, human-edited string.** The ownership model in the code is already
right (`narrative.py:900-908` — story is web-owned, recipe is disk-only and regenerated),
but it is enforced by a content hash over a subset of one shared YAML document rather than
by structure. That choice is what requires `narrative_content_hash`,
`decide_narrative_sync` (five outcomes, including `refuse_conflict`), and
`merge_narrative_into_spec` with rules like *"local scenes absent from web are dropped."*

**The rule going forward: for every field, exactly one writer. If a field can be written
from two places, you have chosen to implement a merge algorithm — so either pay that cost
deliberately or move the field.**

## L0 — Stable identity (the keystone)

**Every scene carries an explicit `id:`**, authored once and never derived. `_title_slug`
survives only as a migration fallback for specs that predate this.

Consequences, all three from one change:
- `merge_narrative_into_spec` matches on id → recipes survive rewording (D1)
- `NarrationItem.id` is stable → the diff pairs correctly (D3)
- the recipe file becomes keyable by scene → L1 is possible at all

**Fix the pull path** (D2): `web_narrative_to_spec_parts` maps `narration[].text` →
`scene.narrative`, and `_NARRATIVE_SCENE_FIELDS` becomes
`("title", "persona", "provenance", "narrative", "features")`.

`concept_claim` **leaves the web-owned set entirely.** `NarrationItem` has no
`concept_claim` field (`scripts/narrative/models.py:794-812`), so it is never transmitted
to canopy-web — the pull was *reconstructing* it from `text`, which is precisely why D2
destroys it. Under "one writer per field" it is local: the dev-facing falsifiable claim
`ddd-concept-eval` judges, never seen or edited by a reviewer. Reclassifying it is the
fix; keeping it nominally web-owned while no writer on that side exists is the bug.

**Migration is a freebie, but only until someone rewords a title.** canopy-web's stored
narration ids for all 13 existing narratives *already are* the current title slugs, so
backfilling `id: <current-title-slug>` into each local spec makes history line up rather
than breaking it. Do this first.

Validation: `spec_qa` fails a spec with a missing, duplicate, or non-slug `id`, and fails
a spec whose `id` changed relative to the narrative lock (ids are permanent; renaming one
is deleting a scene and adding another, and should read that way in the diff).

## L1 — Split the document along the seam that already exists

Three files, one writer each.

**`docs/walkthroughs/<slug>.recipe.yaml`** — git-tracked, PR-reviewed, keyed by scene id.
Holds `show`, `url`, `viewport`, `pace`, `must_succeed`, `design_intent`, plus
`base_url`/auth. This is code: it is coupled to selectors in a worktree, it changes when
the app changes, and it belongs in the PR that changes the app.

**The narrative lives on canopy-web only.** Immutable per version; vN is vN forever.

**`docs/walkthroughs/<slug>.narrative.lock.json`** — generated by `narrative pull`,
committed, never hand-edited. Carries `{slug, version, fetched_at, narrative, personas,
build_order, scenes: [{id, title, persona, provenance, concept_claim, narrative,
features}]}`. CI checks it is byte-identical to a fresh fetch of the recorded version.
This is the lockfile pattern, and it buys three things a git-ignored cache does not: PR
diffs that show the story which drove the build, offline renders, and reproducible
re-renders of old versions.

**First drafts** author into `<slug>.draft.yaml`, which exists only until the first push
mints v1 and is then deleted. `ddd-spec` keeps working unchanged.

Deleted outright: `narrative_content_hash`, `decide_narrative_sync`,
`merge_narrative_into_spec`, the `narrative_synced_*` fields (D4), and `refuse_conflict`
as a concept. You cannot diverge from something you do not duplicate.

The only surviving cross-boundary operation is **resolve `<slug>@vN`** — a read.
Run state stays local scratch during a run (it is the resume mechanism and must survive
the cloud going down mid-render) and publishes once, terminally, on convergence. One-way
plus immutable is not a sync problem.

## L2 — `Feedback` as a first-class object

**Framework tier** (`apps/feedback`), generic over its target, importing no product app —
the same discipline `Item` follows: it carries its own text rather than resolving a
subject, so nothing needs a registry and nothing drifts.

Feedback arrives from **several channels, not just the web page**. People will comment in
email and in Google Docs, and that is legitimate. The durable object is "feedback on
target X at version V, anchored at Y, from person P, via channel C" — the page, an email
thread, and a doc all POST into the same store.

```
Feedback
  target_kind      "narrative" | "storyboard"
  target_ref       slug
  target_version   int | null      # the version the feedback was left against
  anchor_id        str | null      # scene id, or act id; null = the whole thing
  kind             "comment" | "suggestion"
  body             text            # the comment
  suggested_text   text            # proposed replacement narration (kind=suggestion)
  author_name      str             # free text — externals have no accounts
  author_email     str
  submitted_by     FK(User) | null
  channel          "web" | "email" | "gdoc" | "manual" | "api"
  source_ref       str             # message-id, doc-id + comment-id — for idempotent re-ingest
  state            "new" | "triaged" | "answered" | "declined"
  disposition_note text            # written by the decision turn
  resolved_in_version int | null
```

- `POST /api/feedback/` — batch, idempotent per `(channel, source_ref)`. Share-token-gated
  for web submits; PAT for agent ingestion of email and doc comments.
- `GET /api/feedback/?target=&state=&channel=` — the pool.
- `POST /api/feedback/{id}/resolve` — how a decision turn records what it did.

**Deliberately emits no signal.** No `Item`, no push, no nag. Feedback is not work; it is
input to a decision. The pool sits until a turn is fired that reads it, clusters it across
channels, decides a disposition for each piece, drafts the resulting vN+1, and comes back
with *"here is what they said, what I did with each, and what I declined and why."* That is
the loop that already worked with Sophie, now with a real inbox behind it.

This also retires an anti-pattern rather than enforcing it. `ace:narrative-iteration-review`
currently labels the Google-Doc bridge *"disposable, NOT this skill's durable mechanism,
the fix belongs in canopy."* Under this model the doc is a supported **channel**. What was
wrong was the doc being a parallel *source of truth*, not the doc existing.

## L3 — Two surfaces, both outside the operator console

### The reviewer surface

One narrative, scene by scene, with before/after when a prior version exists. Reuses
`pairNarrationScenes` and the existing narration-edit round-trip. Shows the story and
nothing else — no gates, features, provenance, actionability scores, or findings.

Per scene: a comment box always; inline-editable text when the link grants `suggest`.
A submitted suggestion is a `Feedback` row carrying proposed text — it reaches the
narrative only through a turn the owner fires.

**This becomes the default narrative-agreement surface for everyone**, with the operator
console reachable behind a "show the build view" link — the inverse of today.
`ReviewPage.tsx` (1,874 lines) keeps gates and findings but stops being the front door.
The complaint in canopy-web#290 was *"something only I understand"*; the fix should not be
a second-class copy for outsiders.

**canopy-web#290 Gap 2 stays closed.** No anonymous writes to a `ReviewRequest`. The
token grants comment/suggest on `Feedback`, which is a different object with a different
blast radius.

### The storyboard

**Product tier** (`apps/storyboards`) — it curates DDD narratives, which are product.

```
Storyboard   slug, title, lede, workspace, share_token, capability
  acts[]     ordered: title, prose
    entries[] ordered: narrative_slug, pinned_run_id (optional override)
```

`pinned_run_id` is normally null and should stay that way — see **Freshness** below. It
exists for the one case that needs it: holding an entry on a known-good run while that
narrative is mid-redraft.

Page: lede → act prose → per-narrative card (hero video, one line, "read the scenes" →
the reviewer surface) → feedback affordance at act and narrative level. One
capability-bearing share token per storyboard (`read` / `comment` / `suggest`).

Authored by agents via API from a `storyboard.yaml` in the product repo; reorderable and
retitleable in the canopy-web UI.

**Freshness:** a storyboard *follows* each narrative's current released run rather than
freezing run ids, and every `Feedback` row records `target_version`. So an emailed link
never goes stale, but a comment stays anchored to the text that provoked it, and the UI
can mark a piece of feedback *"left against v3, now v5."* This is the fix for the
"emailed links go stale" learning; the guarantee it needs is that `/ddd/<slug>` is a
mutable pointer to the current hero while per-walkthrough ids stay immutable.

## What this fixes, mapped back

| Symptom | Layer |
|---|---|
| Selectors silently deleted by a title reword | L0 |
| Domain-expert vN→vN+1 diff reading as remove+add | L0 |
| `concept_claim` destroyed on pull | L0 |
| Sync churn, `refuse_conflict`, manual reconciliation | L1 |
| Email and Google-Doc feedback as a labelled anti-pattern | L2 |
| Feedback with nowhere to live and no record of being answered | L2 |
| "The review UI is too complicated for a domain expert" (#290 Gap 1/2) | L3 |
| Emailed links going stale | L3 |
| No object that represents the product as acts and scenes | L3 |

## Out of scope

- No per-piece disposition workflow UI — the decision is a turn, not a queue-grooming
  session.
- No comment threading or replies.
- No notifications or outbound email.
- No capability model beyond the three link grants.
- Acts do not get their own videos.
- Deep-linkable routes in the supply app are a *supply-side* prerequisite for "try it
  live" (the SPA is currently one URL with tab state in React `useState`). Tracked there,
  not here.

## Testing

- **L0** — `spec_qa` rejects missing/duplicate/non-slug/changed ids; a unit test proves a
  title reword preserves the recipe; a pairing test proves a reworded title reads as
  `changed`, not `removed` + `added`; a roundtrip test proves push → edit → pull leaves
  `scene.narrative` correct and `concept_claim` untouched.
- **L1** — lockfile round-trips byte-identically; a CI check fails a stale lock; deleting
  the sync module leaves no callers (grep gate).
- **L2** — idempotent re-ingest on `(channel, source_ref)`; token scope enforced per
  capability; framework-boundary test passes with `feedback` in `FRAMEWORK`.
- **L3** — `storyboards` added to `PRODUCT` in `tests/test_architecture_boundary.py` and
  the `ARCHITECTURE.md` tier table; anonymous token read/comment/suggest paths tested
  including the negative (wrong token 404s, no existence leak — same rule walkthroughs
  follow).

## Sequencing

1. **L0 in `canopy`** — stable ids + backfill + pull-path fix. Small, and it unblocks the
   supply agent, which is holding four unwritten specs on exactly this decision.
2. **L1 in `canopy`** — the split, the lockfile, and the deletions.
3. **L2 in `canopy-web`** — `apps/feedback` + ingestion API.
4. **L3a** — the clean reviewer surface; demote the operator console to a link.
5. **L3b** — `apps/storyboards` + the storyboard page.
6. **`ace:narrative-iteration-review`** — rewrite against the new contract; drop the
   "disposable gdoc bridge" language for real channel ingestion.

Steps 1 and 2 land before anything external is shared, because nothing outward-facing
should be built on a foundation that is actively losing selectors.

## Immediate action, independent of everything else

Tell the supply agent to author its four specs with **explicit stable scene ids** and the
recipe kept separable. That is correct under every option considered here and is the one
decision it is blocked on.
