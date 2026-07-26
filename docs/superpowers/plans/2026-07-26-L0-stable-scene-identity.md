# L0 — Stable scene identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every DDD scene a stable explicit `id` so a reworded scene title can no longer delete its render recipe, break the domain-expert vN→vN+1 diff, or corrupt `concept_claim` on pull.

**Architecture:** One helper — `_scene_id(scene)` — returns an explicit `Scene.id` when authored and falls back to the legacy title slug when not. Every scene-identity call site in `scripts/ddd/narrative.py` switches to it, `NarrationItem.id` becomes stable, the pull path carries the web's `id` through and writes narration into `scene.narrative` instead of `concept_claim`, and `spec_qa` starts requiring ids so the fallback stays a migration path rather than a second supported mode.

**Tech Stack:** Python 3.12, Pydantic v2, pytest, PyYAML.

**Repos:** implementation is in **`canopy`** (`~/emdash/repositories/canopy`). Task 8 also touches **`connect-labs`**. This plan and its spec live in `canopy-web`.

**Spec:** `canopy-web/docs/superpowers/specs/2026-07-26-narrative-storyboard-and-ownership-design.md`, section "L0 — Stable identity (the keystone)".

## Global Constraints

- Work in a fresh git worktree off `origin/main` in the `canopy` repo. Never commit to `main` directly.
- Run tests from the canopy repo root: `uv run pytest tests/ddd/test_narrative.py -v`.
- `_title_slug` is **never deleted** — it remains the migration fallback and the backfill's slug generator.
- Scene ids are permanent. Renaming an id is deleting a scene and adding another, and must read that way in the diff. (The lock-based enforcement of this lands in L1; L0 only enforces presence, uniqueness, and shape.)
- `concept_claim` is **local-owned** as of this plan. It is never transmitted to canopy-web (`NarrationItem` has no such field) and must never again be written from a pull.
- Existing canopy-web narration ids for all 13 live narratives already equal the current title slugs, so backfilling `id: <title-slug>` makes history line up for free. **Take the freebie, don't defend it** — per the spec's standing constraint, only the latest version of each narrative matters, so if an id diverges from what an old version stored, the only casualty is the before/after view of a version nobody reads.

---

### Task 1: `Scene.id` and the `_scene_id` helper

**Files:**
- Create: `scripts/ddd/identity.py`
- Modify: `scripts/narrative/models.py:490-580` (the `Scene` model)
- Modify: `scripts/ddd/narrative.py:187-195` (re-export from `identity`)
- Test: `tests/ddd/test_narrative.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `slugify(text: str) -> str` and `scene_id(scene: "Scene | dict") -> str` in a **dependency-free** `scripts/ddd/identity.py`; `Scene.id: str` (defaults `""`); `narrative.py` re-exports both as `_title_slug` / `_scene_id` so existing call sites and tests keep working.

**Why a new module rather than putting these in `narrative.py`:** `validate.py` needs `scene_id` (Task 3b), and `narrative.py` imports `scripts.ddd.review`, which does network. Making the validator depend on the network layer to learn what a scene is called would be a real coupling for no reason. `identity.py` imports nothing.

- [ ] **Step 1: Write the failing test**

Append to `tests/ddd/test_narrative.py`:

```python
from scripts.ddd.narrative import _scene_id


def test_scene_id_prefers_explicit_id_over_title():
    scene = Scene(
        id="the-goal",
        persona="alice",
        title="A title that will be reworded later",
        show="Open the dashboard.",
        concept_claim="The dashboard loads in under two seconds.",
        provenance="S1",
    )
    assert _scene_id(scene) == "the-goal"


def test_scene_id_falls_back_to_title_slug_when_unset():
    scene = Scene(
        persona="alice",
        title="Area Selection",
        show="Draw a boundary.",
        concept_claim="A boundary can be drawn in under 30 seconds.",
        provenance="S1",
    )
    assert _scene_id(scene) == "area-selection"


def test_scene_id_accepts_a_raw_dict():
    assert _scene_id({"id": "the-goal", "title": "Anything"}) == "the-goal"
    assert _scene_id({"title": "Area Selection"}) == "area-selection"


def test_scene_id_treats_whitespace_only_id_as_absent():
    assert _scene_id({"id": "   ", "title": "Area Selection"}) == "area-selection"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ddd/test_narrative.py -k scene_id -v`
Expected: FAIL — `ImportError: cannot import name '_scene_id'`

- [ ] **Step 3: Add the `id` field to `Scene`**

In `scripts/narrative/models.py`, immediately above `persona: str` in `class Scene`:

```python
    id: str = ""
    """Stable, permanent identity for this scene.

    The join key between the web-owned narrative and the local render recipe,
    and the key canopy-web's vN→vN+1 diff pairs on. Authored once and never
    changed: renaming an id is deleting a scene and adding another, and reads
    that way in every diff.

    Defaults to "" only so pre-L0 specs still load; `spec_qa` rejects a spec
    whose scenes have no ids, and `_scene_id` falls back to the title slug for
    exactly as long as it takes the backfill to run.
    """
```

- [ ] **Step 4: Create `scripts/ddd/identity.py` and re-export from `narrative.py`**

Create `scripts/ddd/identity.py`:

```python
"""Scene identity — the one place a DDD scene's name is derived.

Deliberately dependency-free: the validator, the narrative gate, the spec
composer and the renderer all need to agree on what a scene is called, and
none of them should have to import the network layer to find out.

Before this module the same slug expression was written out three times
(narrative.py twice, validate.py once), which is how `build_order` came to be
validated against title-derived slugs while it was being GENERATED from scene
ids — two spellings of "identity" that silently disagreed.
"""
from __future__ import annotations

import re


def slugify(text: str) -> str:
    """Lowercase, hyphen-separated, alphanumeric-only.

    Examples:
        "Area Selection"   -> "area-selection"
        "Sample Gen (v2)"  -> "sample-gen-v2"
    """
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def scene_id(scene) -> str:
    """The stable identity of a scene — explicit ``id``, else the title slug.

    Accepts either a ``Scene`` model or the raw dict form used by the
    apply/merge paths, because both halves of the roundtrip need the SAME
    identity function. The title-slug fallback is a migration path for pre-L0
    specs, not a supported authoring mode: it is exactly what canopy-web
    already stored as ``NarrationItem.id`` for every existing narrative, so a
    spec that has not been backfilled still matches its own history.
    """
    if isinstance(scene, dict):
        explicit = (scene.get("id") or "").strip()
        title = scene.get("title") or ""
    else:
        explicit = (getattr(scene, "id", "") or "").strip()
        title = getattr(scene, "title", "") or ""
    return explicit or slugify(title)
```

Then in `scripts/ddd/narrative.py`, replace the body of `_title_slug` (`:187-195`) with a re-export so every existing caller and test keeps working:

```python
from scripts.ddd.identity import scene_id as _scene_id, slugify as _title_slug  # noqa: F401
```

Place it with the other imports and delete the old `_title_slug` definition. Keep the module-level docstring reference intact.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/ddd/test_narrative.py -k scene_id -v`
Expected: 4 passed

- [ ] **Step 6: Run the full narrative suite for regressions**

Run: `uv run pytest tests/ddd/ -v`
Expected: all pass (adding an optional field with a default breaks nothing)

- [ ] **Step 7: Commit**

```bash
git add scripts/narrative/models.py scripts/ddd/narrative.py tests/ddd/test_narrative.py
git commit -m "feat(ddd): explicit stable Scene.id with title-slug migration fallback"
```

---

### Task 2: Push path emits stable narration ids

**Files:**
- Modify: `scripts/ddd/narrative.py:278` and `:293` (inside `build_narrative_review_request`)
- Test: `tests/ddd/test_narrative.py`

**Interfaces:**
- Consumes: `_scene_id` from Task 1.
- Produces: `ReviewRequest.narration[].id` is now the scene's explicit id; `ReviewRequest.build_order` defaults to explicit ids.

- [ ] **Step 1: Write the failing test**

```python
def test_review_request_narration_id_is_stable_across_a_title_reword():
    def spec_with_title(title: str) -> UnifiedSpec:
        return _make_spec([
            Scene(
                id="the-goal",
                persona="alice",
                title=title,
                show="Open the dashboard.",
                concept_claim="The dashboard loads in under two seconds.",
                provenance="S1",
            )
        ])

    before = build_narrative_review_request(spec_with_title("Original wording"), "demo-2026-07-26-001")
    after = build_narrative_review_request(spec_with_title("Completely different wording"), "demo-2026-07-26-002")

    assert before.narration[0].id == "the-goal"
    assert after.narration[0].id == "the-goal"
    assert before.narration[0].id == after.narration[0].id


def test_build_order_defaults_to_explicit_scene_ids():
    spec = _make_spec([
        Scene(id="the-goal", persona="alice", title="Some Title", show="x",
              concept_claim="A claim with at least five words.", provenance="S1"),
        Scene(id="the-proof", persona="alice", title="Another Title", show="y",
              concept_claim="Another claim with at least five words.", provenance="S2"),
    ])
    req = build_narrative_review_request(spec, "demo-2026-07-26-001")
    assert req.build_order == ["the-goal", "the-proof"]
```

Ensure `build_narrative_review_request` is imported at the top of the test file (it already is if other tests use it; otherwise add it to the existing `from scripts.ddd.narrative import ...` line).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ddd/test_narrative.py -k "stable_across_a_title_reword or build_order_defaults_to_explicit" -v`
Expected: FAIL — ids come back as `original-wording` / `completely-different-wording`

- [ ] **Step 3: Switch both call sites to `_scene_id`**

`scripts/ddd/narrative.py:278`, inside the `narration = [...]` comprehension:

```python
            id=_scene_id(scene),
```

`scripts/ddd/narrative.py:290-294`:

```python
    build_order: list[str] = (
        spec.build_order
        if spec.build_order
        else [_scene_id(scene) for scene in spec.scenes]
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/ddd/test_narrative.py -k "stable_across_a_title_reword or build_order_defaults_to_explicit" -v`
Expected: 2 passed

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest tests/ddd/ -v`
Expected: all pass (existing specs have no `id`, so `_scene_id` returns the same title slug as before)

- [ ] **Step 6: Commit**

```bash
git add scripts/ddd/narrative.py tests/ddd/test_narrative.py
git commit -m "fix(ddd): narration ids and build_order key on stable scene id, not title"
```

---

### Task 3: Apply path keys edits on scene id

**Files:**
- Modify: `scripts/ddd/narrative.py:549-552`, `:613-614`, `:620-623`, `:747-754`, `:806-810`, `:824-828`, `:836-840`
- Test: `tests/ddd/test_narrative.py`

**Interfaces:**
- Consumes: `_scene_id` from Task 1.
- Produces: `apply_narrative_edits` matches incoming `edited_scenes[].id` against `_scene_id(scene_dict)`; newly-added scenes are written with an explicit `id` key.

- [ ] **Step 1: Write the failing test**

```python
def test_apply_edits_matches_on_scene_id_not_title(tmp_path):
    spec_path = tmp_path / "demo.yaml"
    spec_path.write_text(yaml.dump({
        "name": "demo",
        "narrative": "The old spoken line.",
        "base_url": "http://localhost:8000",
        "personas": {"alice": {"name": "Alice", "role": "PM"}},
        "scenes": [{
            "id": "the-goal",
            "persona": "alice",
            "title": "A title nobody edits",
            "show": "css:text=/^Dashboard$/",
            "concept_claim": "The dashboard loads in under two seconds.",
            "provenance": "S1",
            "narrative": "The old spoken line.",
        }],
    }))

    result = apply_narrative_edits(spec_path, {
        "decisions": {"narrative-verdict": "approve"},
        "edited_scenes": [{"id": "the-goal", "narration": "The new spoken line."}],
    })

    assert result["updated"] == 1
    raw = yaml.safe_load(spec_path.read_text())
    assert raw["scenes"][0]["narrative"] == "The new spoken line."
    assert raw["scenes"][0]["show"] == "css:text=/^Dashboard$/"


def test_apply_edits_writes_an_explicit_id_on_a_newly_added_scene(tmp_path):
    spec_path = tmp_path / "demo.yaml"
    spec_path.write_text(yaml.dump({
        "name": "demo",
        "narrative": "The only line.",
        "base_url": "http://localhost:8000",
        "personas": {"alice": {"name": "Alice", "role": "PM"}},
        "scenes": [{
            "id": "the-goal", "persona": "alice", "title": "The goal",
            "show": "x", "concept_claim": "A claim with at least five words.",
            "provenance": "S1", "narrative": "The only line.",
        }],
    }))

    apply_narrative_edits(spec_path, {
        "decisions": {"narrative-verdict": "approve"},
        "edited_scenes": [{
            "id": "new-1", "title": "The proof", "narration": "A brand new beat.",
        }],
    })

    raw = yaml.safe_load(spec_path.read_text())
    added = raw["scenes"][-1]
    assert added["id"] == "the-proof"
    assert added["title"] == "The proof"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ddd/test_narrative.py -k "matches_on_scene_id or explicit_id_on_a_newly_added" -v`
Expected: FAIL — the first test's edit is skipped as an unknown slug (`updated == 0`); the second has no `id` key on the added scene

- [ ] **Step 3: Key the primary slug map on scene id**

`scripts/ddd/narrative.py:549-552` becomes:

```python
        # Build id→index map for existing scenes. Keyed on the STABLE scene id
        # (explicit `id`, else legacy title slug) so an edit that reworded the
        # title still lands on the right scene.
        slug_to_index: dict[str, int] = {}
        for idx, scene in enumerate(scenes):
            slug_to_index[_scene_id(scene)] = idx
```

- [ ] **Step 4: Give newly-added scenes an explicit id**

In the new-scene branch, add `"id": _title_slug(scene_title),` as the first key of the `new_scene` dict (around `:600`), then change `:613-614` and `:620-623`:

```python
                scenes.append(new_scene)
                slug_to_index[_title_slug(scene_title)] = len(scenes) - 1
```

```python
                if scene_feedback:
                    feedback.append({
                        "scope": "scene",
                        "ref": _title_slug(scene_title),
                        "text": scene_feedback,
                    })
```

(These two stay on `_title_slug` deliberately — the id was *just minted* from the title on the line above, so they agree by construction and reading `_scene_id` back off a half-built dict would be indirection for its own sake.)

- [ ] **Step 5: Switch the build_order and legacy sets**

`:747-754`:

```python
        surviving_slugs: set[str] = {_scene_id(s) for s in scenes}
        newly_added_slugs: list[str] = [_title_slug(t) for t in needs_grounding]
```

`:806-810`:

```python
    slug_to_index_legacy: dict[str, int] = {}
    for idx, scene in enumerate(scenes):
        slug_to_index_legacy[_scene_id(scene)] = idx
```

`:824-828`:

```python
    surviving_slugs_legacy: set[str] = {_scene_id(s) for s in scenes}
```

`:836-840`:

```python
        for scene in scenes:
            slug = _scene_id(scene)
            if slug not in listed_legacy:
                build_order_legacy.append(slug)
                listed_legacy.add(slug)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/ddd/test_narrative.py -k "matches_on_scene_id or explicit_id_on_a_newly_added" -v`
Expected: 2 passed

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest tests/ddd/ -v`
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add scripts/ddd/narrative.py tests/ddd/test_narrative.py
git commit -m "fix(ddd): apply_narrative_edits matches scenes on stable id"
```

---

### Task 3b: `validate.py` stops re-deriving scene identity

**Files:**
- Modify: `scripts/ddd/validate.py:137-153`
- Test: `tests/ddd/test_validate_build_order.py` (create)

**Interfaces:**
- Consumes: `scene_id` from `scripts.ddd.identity` (Task 1).
- Produces: `build_order` validated against stable scene ids.

**This is a live break, not a cleanup.** `validate.py:139` builds the set of legal `build_order` entries by re-deriving a slug from each scene's *title*, while Task 2 makes `build_order` **generated from scene ids**. The moment a scene has `id: the-goal` and any title that doesn't slugify to `the-goal`, every spec fails validation with *"build_order references unknown scene slug"*. Without this task, L0 Task 8's verification fails across all 12 live specs.

- [ ] **Step 1: Write the failing test**

Create `tests/ddd/test_validate_build_order.py`:

```python
"""build_order validates against stable scene ids, not title slugs (L0)."""
from __future__ import annotations

import yaml

from scripts.ddd.validate import validate


def _spec(tmp_path, build_order):
    raw = {
        "name": "demo",
        "narrative": "The goal. The proof.",
        "base_url": "http://localhost:8000",
        "personas": {"maya": {"name": "Maya", "role": "PM"}},
        "build_order": build_order,
        "scenes": [
            {"id": "the-goal", "persona": "maya",
             "title": "A title that does not slugify to the id",
             "show": "x", "concept_claim": "The dashboard loads in under two seconds.",
             "provenance": "S1", "role": "overview"},
            {"id": "the-proof", "persona": "maya",
             "title": "Another unrelated title", "show": "y",
             "concept_claim": "Each round shows a confidence interval.",
             "provenance": "S2", "role": "overview"},
        ],
    }
    p = tmp_path / "demo.yaml"
    p.write_text(yaml.dump(raw))
    return p


def test_build_order_of_scene_ids_is_valid(tmp_path):
    ok, problems = validate("unified_spec", _spec(tmp_path, ["the-goal", "the-proof"]))
    assert not [p for p in problems if "build_order" in p], problems


def test_build_order_of_title_slugs_is_now_rejected(tmp_path):
    ok, problems = validate(
        "unified_spec",
        _spec(tmp_path, ["a-title-that-does-not-slugify-to-the-id"]),
    )
    assert [p for p in problems if "build_order" in p]


def test_duplicate_build_order_entries_still_rejected(tmp_path):
    ok, problems = validate("unified_spec", _spec(tmp_path, ["the-goal", "the-goal"]))
    assert [p for p in problems if "duplicate" in p]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ddd/test_validate_build_order.py -v`
Expected: FAIL on the first two — ids are rejected as unknown, title slugs are wrongly accepted

- [ ] **Step 3: Use the shared identity function**

`scripts/ddd/validate.py:137-141`:

```python
    if obj.build_order:
        # Scene identity comes from scripts.ddd.identity — the SAME function
        # that generates build_order in build_narrative_review_request. These
        # were two separate slug expressions and they disagreed the moment a
        # scene carried an explicit id.
        scene_slugs: set[str] = {scene_id(scene) for scene in obj.scenes}
```

Add `from scripts.ddd.identity import scene_id` to the imports, and update the error message at `:151-153`:

```python
                problems.append(
                    f"build_order references unknown scene id '{slug}' "
                    "(no scene declares this id)"
                )
```

- [ ] **Step 4: Remove the now-dead `re` usage if it was only for this**

Run: `grep -n "re\." scripts/ddd/validate.py`
If no uses remain, drop `import re`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/ddd/test_validate_build_order.py -v`
Expected: 3 passed

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest tests/ -v`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add scripts/ddd/validate.py tests/ddd/test_validate_build_order.py
git commit -m "fix(ddd): validate build_order against scene ids, not re-derived title slugs

build_order is GENERATED from scene ids but was VALIDATED against slugs
re-derived from titles — two spellings of identity that agreed only while
no scene carried an explicit id. Both now call scripts.ddd.identity."
```

---

### Task 4: Pull path carries the id and stops corrupting `concept_claim`

**Files:**
- Modify: `scripts/ddd/narrative.py:983-1006` (`web_narrative_to_spec_parts`), `:908` (`_NARRATIVE_SCENE_FIELDS`)
- Test: `tests/ddd/test_narrative.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `web_narrative_to_spec_parts(request_json) -> dict` whose `scenes[]` entries carry keys `id, title, persona, provenance, narrative, features` (note: **no `concept_claim`**). `_NARRATIVE_SCENE_FIELDS == ("id", "title", "persona", "provenance", "narrative", "features")`.

This is the D2 fix. `NarrationItem` has no `concept_claim` field, so canopy-web never receives one; the old code reconstructed it from `text`, destroying the local falsifiable claim on every pull while leaving the actual voiceover field (`scene.narrative`) stale.

- [ ] **Step 1: Write the failing test**

```python
def test_pull_maps_web_narration_text_to_scene_narrative_not_concept_claim():
    parts = web_narrative_to_spec_parts({
        "narrative_slug": "demo",
        "narrative": "The whole story.",
        "personas": {},
        "build_order": [],
        "narration": [{
            "id": "the-goal",
            "title": "The goal",
            "persona": "alice",
            "provenance": "S1",
            "text": "The line the reviewer approved.",
            "features": [],
        }],
    })

    scene = parts["scenes"][0]
    assert scene["id"] == "the-goal"
    assert scene["narrative"] == "The line the reviewer approved."
    assert "concept_claim" not in scene


def test_narrative_scene_fields_excludes_concept_claim_and_includes_id():
    assert "concept_claim" not in _NARRATIVE_SCENE_FIELDS
    assert "id" in _NARRATIVE_SCENE_FIELDS
    assert "narrative" in _NARRATIVE_SCENE_FIELDS
```

Add `web_narrative_to_spec_parts` and `_NARRATIVE_SCENE_FIELDS` to the test file's imports.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ddd/test_narrative.py -k "maps_web_narration_text or scene_fields_excludes" -v`
Expected: FAIL — `KeyError: 'id'`, and `concept_claim` is present

- [ ] **Step 3: Rewrite `web_narrative_to_spec_parts`'s scene mapping**

`scripts/ddd/narrative.py:989-997`:

```python
        scenes.append(
            {
                # The web's narration id IS the scene id — this is the join key
                # the local recipe is matched on. Legacy narratives stored the
                # title slug here, which is exactly what the backfill writes.
                "id": (n.get("id") or "").strip() or _title_slug(n.get("title", "")),
                "title": n.get("title", ""),
                "persona": n.get("persona", ""),
                "provenance": n.get("provenance", ""),
                # The reviewer-approved line is the VOICEOVER. It is NOT the
                # concept_claim, which is local-owned, never transmitted
                # (NarrationItem has no such field), and must not be clobbered.
                "narrative": (n.get("text") or "").strip(),
                "features": n.get("features") or [],
            }
        )
```

- [ ] **Step 4: Update the web-owned field set**

`scripts/ddd/narrative.py:907-908`:

```python
# Web-owned, per-scene narrative fields (everything else on a Scene is recipe).
# `concept_claim` is deliberately ABSENT: it is never sent to canopy-web
# (NarrationItem carries no such field), so it has exactly one writer — local.
_NARRATIVE_SCENE_FIELDS = ("id", "title", "persona", "provenance", "narrative", "features")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/ddd/test_narrative.py -k "maps_web_narration_text or scene_fields_excludes" -v`
Expected: 2 passed

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest tests/ddd/ -v`
Expected: all pass. If a test asserts the old `concept_claim`-from-`text` behaviour, it encoded the bug — update it to assert the new mapping and note why in the commit body.

- [ ] **Step 7: Commit**

```bash
git add scripts/ddd/narrative.py tests/ddd/test_narrative.py
git commit -m "fix(ddd): pull writes reviewer text to scene.narrative; concept_claim is local-owned

NarrationItem has no concept_claim field, so canopy-web never receives one.
The pull path was reconstructing it from narration text, destroying the local
falsifiable claim on every pull while leaving scene.narrative — the field the
push actually reads — stale."
```

---

### Task 5: `merge_narrative_into_spec` matches on id (the recipe-loss fix)

**Files:**
- Modify: `scripts/ddd/narrative.py:1021-1057`
- Test: `tests/ddd/test_narrative.py`

**Interfaces:**
- Consumes: `_scene_id` (Task 1), the `id`-bearing parts from Task 4.
- Produces: `merge_narrative_into_spec(local, parts)` preserving the local recipe across a web-side title reword.

This is the D1 fix — the one that stops selectors being silently deleted.

- [ ] **Step 1: Write the failing test**

```python
def test_merge_preserves_local_recipe_when_web_rewords_the_title():
    local = {
        "name": "demo",
        "narrative": "Old story.",
        "base_url": "http://localhost:8000",
        "personas": {},
        "scenes": [{
            "id": "the-goal",
            "title": "Original title",
            "persona": "alice",
            "provenance": "S1",
            "concept_claim": "A local claim with at least five words.",
            "narrative": "Old line.",
            "show": "css:text=/^Hyperzoomed$/",
            "url": "/plans/3536/review/",
            "viewport": {"width": 1440, "height": 900},
        }],
    }
    parts = {
        "name": "demo",
        "narrative": "New story.",
        "personas": {},
        "build_order": ["the-goal"],
        "scenes": [{
            "id": "the-goal",
            "title": "Completely reworded title",
            "persona": "alice",
            "provenance": "S1",
            "narrative": "New line.",
            "features": [],
        }],
    }

    merged = merge_narrative_into_spec(local, parts)
    scene = merged["scenes"][0]

    # Recipe survived the reword
    assert scene["show"] == "css:text=/^Hyperzoomed$/"
    assert scene["url"] == "/plans/3536/review/"
    assert scene["viewport"] == {"width": 1440, "height": 900}
    # Narrative fields updated from web
    assert scene["title"] == "Completely reworded title"
    assert scene["narrative"] == "New line."
    # Local-owned claim untouched
    assert scene["concept_claim"] == "A local claim with at least five words."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ddd/test_narrative.py -k merge_preserves_local_recipe -v`
Expected: FAIL — `KeyError: 'show'` (the lookup missed and the recipe was replaced with an empty `show`)

- [ ] **Step 3: Match on id in both the map and the lookup**

`scripts/ddd/narrative.py:1040-1050`:

```python
    local_by_id = {
        _scene_id(s): s
        for s in (local.get("scenes") or [])
        if isinstance(s, dict)
    }
    merged_scenes: list[dict] = []
    for ps in parts["scenes"]:
        base = dict(local_by_id.get(ps["id"], {}))  # preserve recipe
        base.update({k: ps[k] for k in _NARRATIVE_SCENE_FIELDS})
        base.setdefault("show", "")
        merged_scenes.append(base)
```

Update the docstring at `:1025-1028`:

```
    Scenes are matched on their stable ``id`` (the same identity
    ``apply_narrative_edits`` uses), so a web-side title reword preserves the
    local render recipe. A web scene with no local match is written with an
    empty ``show`` recipe for the author to fill; local scenes absent from web
    are dropped (web owns the scene list).
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/ddd/test_narrative.py -k merge_preserves_local_recipe -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest tests/ddd/ -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add scripts/ddd/narrative.py tests/ddd/test_narrative.py
git commit -m "fix(ddd): merge matches scenes on stable id so a title reword can't delete a recipe"
```

---

### Task 6: `spec_qa` requires ids

**Files:**
- Modify: `scripts/ddd/spec_qa.py:281-300` (the QA-specific checks block)
- Test: `tests/ddd/test_spec_qa_scene_ids.py` (create)

**Interfaces:**
- Consumes: `UnifiedSpec` with `Scene.id` from Task 1.
- Produces: `spec_qa` violations for missing, duplicate, or malformed scene ids. Verdict shape unchanged.

- [ ] **Step 1: Write the failing test**

Create `tests/ddd/test_spec_qa_scene_ids.py`:

```python
"""spec_qa enforces stable scene ids (L0)."""
from __future__ import annotations

from scripts.ddd.spec_qa import spec_qa


def _spec(scenes: list[dict]) -> dict:
    return {
        "name": "demo",
        "narrative": "A story about a dashboard that loads quickly.",
        "base_url": "http://localhost:8000",
        "personas": {"alice": {"name": "Alice", "role": "PM"}},
        "scenes": scenes,
    }


def _scene(**over) -> dict:
    base = {
        "id": "the-goal",
        "persona": "alice",
        "title": "The goal",
        "show": "Open the dashboard.",
        "concept_claim": "The dashboard loads in under two seconds.",
        "provenance": "S1",
        "role": "overview",
    }
    base.update(over)
    return base


def _violations(spec: dict) -> list[str]:
    v = spec_qa(spec)
    return [v.blocking_reason or ""] + list(getattr(v, "violations", []) or [])


def test_missing_scene_id_is_a_violation():
    text = " ".join(_violations(_spec([_scene(id="")])))
    assert "scene id" in text.lower()


def test_duplicate_scene_ids_are_a_violation():
    spec = _spec([_scene(id="dup"), _scene(id="dup", title="Second", provenance="S2")])
    text = " ".join(_violations(spec))
    assert "duplicate" in text.lower()


def test_malformed_scene_id_is_a_violation():
    text = " ".join(_violations(_spec([_scene(id="The Goal!")])))
    assert "scene id" in text.lower()


def test_well_formed_ids_produce_no_id_violation():
    text = " ".join(_violations(_spec([_scene(id="the-goal")])))
    assert "scene id" not in text.lower()
```

If `Verdict` exposes its problems under a different attribute than `violations`, adapt `_violations` to read the real one — check `scripts/ddd/schemas/models.py::Verdict` first and use whatever field `spec_qa` already populates at `:436-446`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ddd/test_spec_qa_scene_ids.py -v`
Expected: FAIL on the first three tests (no id checks exist yet)

- [ ] **Step 3: Add the checks**

In `scripts/ddd/spec_qa.py`, inside `if spec is not None:` (after line 282, before the existing placeholder check):

```python
        # (0) Stable scene identity (L0). The id is the join key between the
        # web-owned narrative and the local render recipe, and the key
        # canopy-web's vN→vN+1 diff pairs on. A missing id silently falls back
        # to the title slug, which is exactly the mutable-identity bug this
        # check exists to prevent from recurring.
        _ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
        seen_ids: set[str] = set()
        for i, sc in enumerate(spec.scenes, start=1):
            sid = (sc.id or "").strip()
            if not sid:
                violations.append(
                    f"scene {i} ({sc.title!r}) has no scene id — add a stable `id:` "
                    f"(suggested: {re.sub(r'[^a-z0-9]+', '-', sc.title.lower()).strip('-')!r}). "
                    f"Ids are permanent; renaming one is deleting a scene and adding another."
                )
                continue
            if not _ID_RE.match(sid):
                violations.append(
                    f"scene {i} has a malformed scene id {sid!r} — use lowercase "
                    f"alphanumerics separated by single hyphens."
                )
            if sid in seen_ids:
                violations.append(
                    f"scene {i} has a duplicate scene id {sid!r} — ids must be unique "
                    f"within a narrative."
                )
            seen_ids.add(sid)
```

Confirm `re` is imported at the top of `spec_qa.py`; add `import re` if not.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/ddd/test_spec_qa_scene_ids.py -v`
Expected: 4 passed

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest tests/ddd/ tests/skills/ -v`
Expected: all pass. Any fixture spec that now fails on missing ids should gain ids — that's the check working.

- [ ] **Step 6: Commit**

```bash
git add scripts/ddd/spec_qa.py tests/ddd/test_spec_qa_scene_ids.py
git commit -m "feat(ddd): spec_qa requires present, unique, well-formed scene ids"
```

---

### Task 7: Backfill CLI

**Files:**
- Create: `scripts/ddd/backfill_scene_ids.py`
- Test: `tests/ddd/test_backfill_scene_ids.py`

**Interfaces:**
- Consumes: `_title_slug`, `narrative_content_hash` from `scripts.ddd.narrative`.
- Produces: `backfill(spec_path: Path) -> dict` returning `{"added": int, "rehashed": bool, "skipped": bool}`; CLI `python -m scripts.ddd.backfill_scene_ids <spec.yaml> [<spec.yaml> ...]`.

**Why the re-hash matters:** `narrative_content_hash` covers `_NARRATIVE_SCENE_FIELDS`, which Task 4 changed (gained `id` and `narrative`, lost `concept_claim`). Every already-synced spec's stored `narrative_synced_hash` is therefore stale, and the next `pull` would read it as a local edit and return `refuse_local_newer` on all 13 narratives. Re-stamping during the backfill is what stops that.

- [ ] **Step 1: Write the failing test**

Create `tests/ddd/test_backfill_scene_ids.py`:

```python
"""Backfill of stable scene ids onto pre-L0 specs."""
from __future__ import annotations

import yaml

from scripts.ddd.backfill_scene_ids import backfill
from scripts.ddd.narrative import narrative_content_hash


def _write(tmp_path, raw):
    p = tmp_path / "demo.yaml"
    p.write_text(yaml.dump(raw))
    return p


def test_backfill_writes_the_title_slug_as_the_id(tmp_path):
    p = _write(tmp_path, {
        "name": "demo", "narrative": "A story.", "base_url": "http://x",
        "personas": {}, "scenes": [{"title": "Area Selection", "show": "x"}],
    })
    result = backfill(p)
    assert result["added"] == 1
    assert yaml.safe_load(p.read_text())["scenes"][0]["id"] == "area-selection"


def test_backfill_is_idempotent(tmp_path):
    p = _write(tmp_path, {
        "name": "demo", "narrative": "A story.", "base_url": "http://x",
        "personas": {}, "scenes": [{"id": "kept", "title": "Area Selection", "show": "x"}],
    })
    result = backfill(p)
    assert result["added"] == 0
    assert yaml.safe_load(p.read_text())["scenes"][0]["id"] == "kept"


def test_backfill_restamps_a_stale_sync_hash(tmp_path):
    p = _write(tmp_path, {
        "name": "demo", "narrative": "A story.", "base_url": "http://x",
        "personas": {}, "scenes": [{"title": "Area Selection", "show": "x"}],
        "narrative_synced_version": 3,
        "narrative_synced_hash": "stale-value-from-before-L0",
    })
    result = backfill(p)
    assert result["rehashed"] is True
    raw = yaml.safe_load(p.read_text())
    assert raw["narrative_synced_hash"] == narrative_content_hash(raw)
    assert raw["narrative_synced_version"] == 3


def test_backfill_does_not_stamp_a_hash_onto_a_never_synced_spec(tmp_path):
    p = _write(tmp_path, {
        "name": "demo", "narrative": "A story.", "base_url": "http://x",
        "personas": {}, "scenes": [{"title": "Area Selection", "show": "x"}],
    })
    result = backfill(p)
    assert result["rehashed"] is False
    assert "narrative_synced_hash" not in yaml.safe_load(p.read_text())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ddd/test_backfill_scene_ids.py -v`
Expected: FAIL — `ModuleNotFoundError: scripts.ddd.backfill_scene_ids`

- [ ] **Step 3: Write the module**

Create `scripts/ddd/backfill_scene_ids.py`:

```python
"""One-shot backfill: give every scene in a pre-L0 spec a stable explicit id.

The id written is the scene's CURRENT title slug — which is exactly what
canopy-web already stored as ``NarrationItem.id`` for every existing narrative
version. Running this before anyone rewords a title makes local specs and
cloud history line up; running it after does not.

Also re-stamps ``narrative_synced_hash`` when the spec has one, because
``_NARRATIVE_SCENE_FIELDS`` changed shape in L0 and every stored hash is
otherwise stale — which a later ``pull`` would misread as a local edit.

    python -m scripts.ddd.backfill_scene_ids docs/walkthroughs/*.yaml
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

from scripts.ddd.narrative import _title_slug, narrative_content_hash


def backfill(spec_path) -> dict:
    """Add `id:` to every id-less scene. Idempotent. Returns a summary dict."""
    p = Path(spec_path)
    raw = yaml.safe_load(p.read_text()) or {}
    scenes = raw.get("scenes") or []
    if not isinstance(scenes, list):
        return {"added": 0, "rehashed": False, "skipped": True}

    added = 0
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        if (scene.get("id") or "").strip():
            continue
        scene["id"] = _title_slug(scene.get("title", "") or "")
        added += 1

    rehashed = False
    if raw.get("narrative_synced_version") is not None:
        raw["narrative_synced_hash"] = narrative_content_hash(raw)
        rehashed = True

    if added or rehashed:
        p.write_text(yaml.dump(raw, default_flow_style=False, allow_unicode=True, sort_keys=False))

    return {"added": added, "rehashed": rehashed, "skipped": False}


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m scripts.ddd.backfill_scene_ids <spec.yaml> [...]")
        return 2
    for arg in argv:
        result = backfill(arg)
        print(f"{arg}: +{result['added']} ids, rehashed={result['rehashed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/ddd/test_backfill_scene_ids.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/ddd/backfill_scene_ids.py tests/ddd/test_backfill_scene_ids.py
git commit -m "feat(ddd): backfill CLI for stable scene ids + sync-hash re-stamp"
```

---

### Task 8: Run the backfill on the live specs

**Files:**
- Modify: `connect-labs/docs/walkthroughs/*.yaml` (12 unified specs; the `.why_brief.yaml` files have no scenes and are untouched)

**Interfaces:**
- Consumes: the CLI from Task 7.
- Produces: every live spec carries explicit scene ids and a fresh sync hash.

This is a **separate repo and a separate PR**. Do it after Tasks 1-7 have merged in `canopy`, so the CLI the backfill runs is the released one.

- [ ] **Step 1: Create a worktree off origin/main in connect-labs**

```bash
cd ~/emdash/repositories/connect-labs
git fetch origin
git worktree add ../../worktrees/connect-labs-scene-ids -b ddd/backfill-scene-ids origin/main
```

- [ ] **Step 2: Run the backfill**

```bash
cd ~/emdash/worktrees/connect-labs-scene-ids
uv run --project ~/emdash/repositories/canopy python -m scripts.ddd.backfill_scene_ids \
  docs/walkthroughs/*.yaml
```

Expected: one line per spec. `.why_brief.yaml` files report `+0 ids`.

- [ ] **Step 2b: Handle the two drifted specs (MEASURED 2026-07-26 — do not skip)**

A dry run of the backfill against all 12 live specs, compared against canopy-web's
stored narration ids, found the "ids line up for free" assumption holds for most but
**not all** specs:

| narrative | web version | result |
|---|---|---|
| `verified-monitoring` | v17 | ids match exactly |
| `microplans-study-groups` | v14 | ids match exactly |
| `campaign-utility-tool` | v4 | ids match exactly |
| `program-admin-report` | v3 | all 14 web ids match; local carries 1 extra scene |
| `create-survey-solicitation` | v12 | **only 4 of 9 ids overlap — genuinely diverged** |

`create-survey-solicitation`'s local titles and its web narrative have drifted (web:
*"AI drafts the scoring rubric and Maya publishes the call"*; local: *"Maya generates
the scoring criteria with AI"*). Backfilling that spec from local titles mints ids
canopy-web has never seen, so the first pull orphans all eight local scenes and writes
empty `show` recipes — the exact D1 failure this work exists to prevent.

**Fix it by hand before pulling, for that one spec only.** Fetch the current narration
ids and write them onto the matching local scenes:

```bash
TOKEN=$(cat ~/.claude/canopy/workbench-token)
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://labs.connect.dimagi.com/canopy/api/ddd/narratives/create-survey-solicitation/" \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
v=max((v for v in d['versions'] if v.get('narration')), key=lambda x: x['version'])
for n in v['narration']:
    print(n['id'], '|', n.get('title'))
"
```

Then set each local scene's `id:` to the web id for the beat it films. A local scene
with no counterpart on the web keeps a locally-minted id and will be dropped by the
first pull (web owns the scene list) — that is correct, and is why this is a manual
5-minute reconciliation rather than a tool. `program-admin-report`'s extra scene is the
same situation and needs no action.

Deliberately NOT automated: this is a one-time, two-file problem, and a
`--ids-from-web` flag would put a network dependency into a migration tool to save one
manual pass.

- [ ] **Step 3: Verify every scene now has an id**

```bash
python3 -c "
import glob, yaml
for f in sorted(glob.glob('docs/walkthroughs/*.yaml')):
    raw = yaml.safe_load(open(f)) or {}
    scenes = raw.get('scenes') or []
    missing = [i for i, s in enumerate(scenes, 1) if not (s.get('id') or '').strip()]
    dupes = len(scenes) != len({s.get('id') for s in scenes})
    print(f, len(scenes), 'MISSING:' + str(missing) if missing else 'ok', 'DUPES' if dupes else '')
"
```

Expected: every spec reports `ok` with no `DUPES`.

- [ ] **Step 4: Verify spec_qa passes on each spec**

```bash
for f in docs/walkthroughs/*.yaml; do
  uv run --project ~/emdash/repositories/canopy python -c "
import sys
from scripts.ddd.spec_qa import spec_qa
v = spec_qa('$f')
print('$f', v.verdict)
" ; done
```

Expected: no spec fails with a scene-id violation. A spec failing for an unrelated pre-existing reason is fine — note it, don't fix it here.

- [ ] **Step 5: Commit and open the PR**

```bash
git add docs/walkthroughs/
git commit -m "chore(ddd): backfill stable scene ids onto every walkthrough spec

Ids are the current title slugs, which is what canopy-web already stored as
NarrationItem.id for every existing narrative version — so local specs and
cloud history line up. Also re-stamps narrative_synced_hash, stale because
_NARRATIVE_SCENE_FIELDS changed shape in canopy L0."
gh pr create --fill
```

- [ ] **Step 6: Confirm a real pull is clean**

Against one already-synced narrative (`verified-monitoring`), run the existing `narrative pull` path and confirm it reports `noop` or `pull` — **not** `refuse_local_newer`. If it refuses, the re-stamp in Task 7 did not cover a field the hash reads; fix that before merging.

---

## Self-review

**Spec coverage.** L0 has three requirements: explicit stable ids (Tasks 1-3, 5), the pull-path fix (Task 4), and validation plus the migration (Tasks 6-8). All covered. The spec's "spec_qa fails a spec whose `id` changed relative to the narrative lock" is explicitly **deferred to L1** — there is no lockfile to compare against yet, and Task 6's docstring says so rather than half-implementing it.

**Type consistency.** `_scene_id` takes `Scene | dict` and returns `str` in every task. `_NARRATIVE_SCENE_FIELDS` is a 6-tuple from Task 4 onward and both Task 4 and Task 5 use that shape. `backfill` returns `{"added", "rehashed", "skipped"}` in both the test and the implementation.

**Known sharp edge.** Task 4 changes what `narrative_content_hash` covers, which invalidates every stored `narrative_synced_hash`. Task 7 re-stamps and Task 8 Step 6 verifies. If Tasks 1-7 ship without Task 8, the next `pull` on each live narrative returns `refuse_local_newer` — recoverable with `pull --force`, and per the standing constraint force-pulling the latest version is an acceptable outcome, not a data-loss event. Don't build a reconciliation path for it.
