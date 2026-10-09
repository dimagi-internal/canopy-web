"""Read-time aggregation of DDD runs.

A DDD *run* (one ``run_id``) packages a hero video, an HTML walkthrough/deck, a
narrative, and companion links — but those live across two tables
(``Walkthrough`` and ``ReviewRequest``) with no FK. These pure functions join
them on ``run_id`` and roll runs up under their *narrative* (the run_id slug).

Everything here is queryset-in / plain-dict-out so it can be unit-tested without
the HTTP layer, and so the Ninja handlers stay thin.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from apps.runs.ddd import (
    RUN_CHILD_GATES,
    is_run_child_gate,
    narrative_slug_from_run_id,
)
from apps.reviews.models import ReviewRequest
from apps.reviews.titles import narrative_title, phase_words
from apps.walkthroughs import pinned
from apps.walkthroughs.models import Walkthrough
from apps.workspaces import services as wsvc

# ---------------------------------------------------------------------------
# Workspace scoping
#
# Every base ``Walkthrough`` / ``ReviewRequest`` queryset reached from an API
# handler is narrowed to the caller's workspaces. ``workspace_slugs`` is a set of
# ``Workspace`` slugs (the FK value is the slug string, stored in
# ``workspace_id``). ``None`` means "no scoping" — a safe no-op kept so the pure
# aggregation functions stay unit-testable without a tenant and so non-handler
# callers (e.g. the walkthrough upload path) are unaffected.
# ---------------------------------------------------------------------------


def _scope(qs, workspace_slugs: set[str] | None):
    """Narrow a Walkthrough/ReviewRequest queryset to ``workspace_slugs``.

    A member of workspace A must never read (or, via the delete helpers, mutate)
    workspace B's rows — even a pinned single-id lookup is filtered so cross-tenant
    ids resolve to nothing.
    """
    if workspace_slugs is None:
        return qs
    return qs.filter(workspace_id__in=workspace_slugs)


# ---------------------------------------------------------------------------
# Small predicates
# ---------------------------------------------------------------------------


def _is_video(w: Walkthrough) -> bool:
    return w.role in (Walkthrough.ROLE_HERO_VIDEO, Walkthrough.ROLE_CLIP) or (
        w.kind == Walkthrough.KIND_VIDEO
    )


def _is_deck(w: Walkthrough) -> bool:
    return w.role in (Walkthrough.ROLE_DOCS, Walkthrough.ROLE_DECK) or (
        w.kind == Walkthrough.KIND_HTML
    )


def narrative_of_walkthrough(w: Walkthrough) -> str:
    return (w.narrative_slug or "").strip() or narrative_slug_from_run_id(w.run_id or "")


def narrative_of_review(r: ReviewRequest) -> str:
    return (getattr(r, "narrative_slug", None) or "").strip() or narrative_slug_from_run_id(r.run_id)


#: Gates that carry a one-line narration but are NOT a narrative version — they hang
#: off the run, not the narrative timeline. ``external_release`` is an approval gate;
#: ``product_findings`` is a run-child findings review (its narration is a degradation
#: mirror). Including either pollutes the version list and lets it drive the narrative's
#: title/phase (the "v0 findings review" bug).
#:
#: A superset of ``RUN_CHILD_GATES``: every run-child gate is non-narrative, but
#: ``external_release`` is non-narrative while still *belonging* to a narrative — so it
#: may create one, and a run-child gate may not.
_NON_NARRATIVE_GATES = ("external_release", *RUN_CHILD_GATES)


def _is_narrative_version(r: ReviewRequest) -> bool:
    """A narrative version = a story-bearing review on a narrative gate (not an
    external-release approval or a run-child product-findings review)."""
    return _review_has_narrative(r) and r.gate not in _NON_NARRATIVE_GATES


def _narrative_versions_for(
    narrative_slug: str, workspace_slugs: set[str] | None = None
) -> list[ReviewRequest]:
    """Narrative-version reviews for a narrative_slug, oldest version first."""
    out = [
        r
        for r in _scope(ReviewRequest.objects.all(), workspace_slugs)
        if narrative_of_review(r) == narrative_slug and _is_narrative_version(r)
    ]
    out.sort(key=lambda r: (r.version, r.created_at))
    return out


def has_narrative_version(
    narrative_slug: str, workspace_slugs: set[str] | None = None
) -> bool:
    """True iff ``narrative_slug`` has at least one story-bearing narrative version.

    A narrative version is a ``concept_change`` review carrying a story (see
    :func:`_is_narrative_version`) — i.e. the ``ddd-narrative-review`` gate ran
    for this narrative. When this is False, any run uploaded under ``narrative_slug``
    renders as **"no narrative"**, so the upload path refuses to publish it.
    """
    return bool(
        _narrative_versions_for((narrative_slug or "").strip(), workspace_slugs)
    )


def _narrative_payload(r: ReviewRequest | None) -> dict | None:
    if r is None:
        return None
    rj = r.request_json if isinstance(r.request_json, dict) else {}
    return {
        "review_id": str(r.id),
        "version": r.version,
        "run_id": r.run_id,
        "gate": r.gate,
        "title": _title_from_review(r),
        "story": (rj.get("narrative") or "").strip() or None,
        "narration": rj.get("narration") or [],
        "personas": rj.get("personas") or {},
        "why_brief": rj.get("why_brief"),
    }


def narrative_for_run_id(run_id: str, feature_map: dict[str, str] | None = None) -> str:
    """Narrative slug for a run.

    The explicitly-uploaded ``narrative_slug`` of any walkthrough in the run is the
    source of truth (the plugin sends it). Parsing the run_id slug
    (:func:`narrative_slug_from_run_id`) is only a last-resort fallback for runs that
    have no walkthrough carrying an explicit narrative_slug (e.g. a review-only run).
    """
    if feature_map and feature_map.get(run_id):
        return feature_map[run_id]
    return narrative_slug_from_run_id(run_id)


def _narrative_slug_map(walkthroughs) -> dict[str, str]:
    """run_id -> explicit narrative_slug, from walkthroughs that carry both."""
    m: dict[str, str] = {}
    for w in walkthroughs:
        feat = (getattr(w, "narrative_slug", None) or "").strip()
        if w.run_id and feat:
            m.setdefault(w.run_id, feat)
    return m


def _tok(base: str, w: Walkthrough) -> str:
    """Append ?t=<share_token> when the artifact is public + tokened, so an
    anonymous browser can stream it (each artifact self-checks its OWN token in
    Walkthrough.readable_by). Members stream tokenlessly via session; the token
    is harmless for them. A no-op for private/member artifacts (no token minted)."""
    if w.visibility == Walkthrough.VISIBILITY_LINK and w.share_token:
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}t={w.share_token}"
    return base


def _content_url(w: Walkthrough) -> str:
    """In-app viewer stream. Session auth covers private artifacts; public
    (visibility=link) artifacts carry ?t=<share_token> so they stream to anyone
    with the URL — parity with the release page, and it keeps the DDD console's
    <iframe> embed readable even when the artifact's workspace differs from the
    viewer's active one."""
    return _tok(wsvc.scoped_path(w.workspace_id, f"/walkthrough/{w.id}/content"), w)


def _viewer_url(w: Walkthrough) -> str:
    return _tok(wsvc.scoped_path(w.workspace_id, f"/walkthrough/{w.id}"), w)


def _cut_payload(w: Walkthrough) -> dict:
    """One recorded-narrative cut's video (canopy-web#1288)."""
    return {
        "cut_id": w.cut_id,
        "title": pinned.cut_title(w),
        "scene_ids": list(w.cut_scene_ids or []),
        "walkthrough_id": w.id,
        "video_url": _content_url(w),
        "video_viewer_url": _viewer_url(w),
        "duration_sec": w.duration_sec,
    }


def _artifact_payload(w: Walkthrough | None) -> dict | None:
    if w is None:
        return None
    return {
        "id": w.id,
        "title": w.title,
        "kind": w.kind,
        "role": w.role,
        "content_url": _content_url(w),
        "viewer_url": _viewer_url(w),
        "duration_sec": w.duration_sec,
    }


def _pick(wts: list[Walkthrough], *predicates) -> Walkthrough | None:
    """Return the most-recent walkthrough matching the earliest predicate that
    matches anything. ``wts`` must be newest-first."""
    for pred in predicates:
        for w in wts:
            if pred(w):
                return w
    return None


def _review_has_narrative(r: ReviewRequest) -> bool:
    rj = r.request_json if isinstance(r.request_json, dict) else {}
    return bool((rj.get("narrative") or "").strip() or rj.get("narration"))


def _title_from_review(r: ReviewRequest) -> str | None:
    # The slug the title falls back to is the review's narrative — the stored column,
    # else derived from the run id, exactly as the reviews API resolves it. Passing
    # only the column left legacy rows to the clipped-sentence fallback, which then
    # read as a real title to every consumer that dedupes title against lede.
    slug = (r.narrative_slug or "").strip() or (
        None if is_run_child_gate(r.gate) else narrative_slug_from_run_id(r.run_id)
    )
    return narrative_title(r.request_json, slug)


def _run_record(run_id: str, workspace_slugs: set[str] | None) -> dict | None:
    """The run's ``AgentRun`` document, if the caller may see its agent.

    A run's artifacts are tenanted by THEIR workspace; its record by its owning
    agent's. The record shows only to members of the agent's workspace.
    """
    from apps.agent_runs.models import AgentRun  # noqa: PLC0415

    qs = AgentRun.objects.select_related("agent", "project").filter(ext_id=run_id)
    if workspace_slugs is not None:
        qs = qs.filter(agent__workspace_id__in=workspace_slugs)
    run = qs.first()
    if run is None:
        return None
    return {
        "agent_slug": run.agent.slug,
        "project_ext_id": run.project.ext_id if run.project_id else None,
        "project_name": run.project.name if run.project_id else None,
        "kind": run.kind,
        "status": run.status or "running",
        "current_step": run.current_step,
        "summary": run.summary or {},
        "holder": run.holder,
        "holder_at": run.holder_at,
    }


def _phase_label(r: ReviewRequest) -> str:
    return phase_words(r.gate, r.status)


def _scene_count(r: ReviewRequest | None) -> int:
    if r is None:
        return 0
    rj = r.request_json if isinstance(r.request_json, dict) else {}
    narration = rj.get("narration") or []
    return len(narration) if isinstance(narration, list) else 0


# ---------------------------------------------------------------------------
# Single run package
# ---------------------------------------------------------------------------


def build_run(run_id: str, workspace_slugs: set[str] | None = None) -> dict | None:
    """Aggregate one run_id into a package dict, or ``None`` if nothing matches."""
    wts = list(
        _scope(Walkthrough.objects.filter(run_id=run_id), workspace_slugs)
        .select_related("owner")
    )
    revs = list(
        _scope(ReviewRequest.objects.filter(run_id=run_id), workspace_slugs)
    )  # -created_at default
    record = _run_record(run_id, workspace_slugs)
    if not wts and not revs and record is None:
        return None

    video = _pick(
        wts,
        lambda w: w.role == Walkthrough.ROLE_HERO_VIDEO,
        lambda w: w.role == Walkthrough.ROLE_CLIP,
        lambda w: w.kind == Walkthrough.KIND_VIDEO,
    )
    # First-class, single-valued outputs (newest of each role). `slides` is the
    # canopy:walkthrough HTML slideshow (role=deck); `documentation` is the
    # feature docs page (role=docs). An unroled HTML artifact falls back to
    # documentation so legacy uploads still surface somewhere, but a role=deck
    # artifact never leaks into documentation (and vice-versa).
    slides = _pick(wts, lambda w: w.role == Walkthrough.ROLE_DECK)
    documentation = _pick(
        wts,
        lambda w: w.role == Walkthrough.ROLE_DOCS,
        lambda w: w.kind == Walkthrough.KIND_HTML
        and w.role != Walkthrough.ROLE_DECK,
    )

    # Explicit narrative_slug (uploaded by the plugin) wins; run_id parsing is fallback.
    explicit_narrative_slug = next(
        (w.narrative_slug for w in wts if (w.narrative_slug or "").strip()), None
    )
    narrative_slug = explicit_narrative_slug or narrative_slug_from_run_id(run_id)

    # The narrative VERSION this run rendered. Prefer the explicit stamp
    # (narrative_review_id on the run's artifacts); else the run's own
    # story-bearing review; else the narrative's current version (legacy).
    stamped = next((w.narrative_review_id for w in wts if w.narrative_review_id), None)
    narrative_review = None
    if stamped:
        narrative_review = _scope(
            ReviewRequest.objects.filter(pk=stamped), workspace_slugs
        ).first()
    if narrative_review is None:
        narrative_review = next((r for r in revs if _is_narrative_version(r)), None)
    if narrative_review is None:
        versions = _narrative_versions_for(narrative_slug, workspace_slugs)
        narrative_review = versions[-1] if versions else None

    narrative_payload = _narrative_payload(narrative_review)
    phase = _phase_label(revs[0]) if revs else None
    if record is not None:
        # The runner's own account of the run beats a guess from its latest review.
        it = (record["summary"] or {}).get("iteration")
        phase = (
            f"{(record['summary'] or {}).get('phase') or 'running'}"
            + (f" · iteration {it}" if it is not None else "")
            if record["status"] == "running"
            else record["status"]
        )

    # Links: union across the run's walkthroughs, de-duped on (url, kind),
    # oldest-first for a stable order.
    links: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for w in sorted(wts, key=lambda x: x.created_at):
        for link in w.links or []:
            if not isinstance(link, dict):
                continue
            key = (link.get("url", ""), link.get("kind", "reference"))
            if key in seen:
                continue
            seen.add(key)
            links.append(
                {
                    "label": link.get("label", ""),
                    "url": link.get("url", ""),
                    "kind": link.get("kind", "reference"),
                }
            )

    all_artifacts = [
        {
            "id": w.id,
            "title": w.title,
            "kind": w.kind,
            "role": w.role,
            "created_at": w.created_at,
            "viewer_url": _viewer_url(w),
        }
        for w in sorted(wts, key=lambda x: x.created_at)
    ]

    timestamps = [w.created_at for w in wts] + [r.created_at for r in revs]
    created_at = min(timestamps) if timestamps else None
    latest_at = max(timestamps) if timestamps else None

    return {
        "run_id": run_id,
        "narrative_slug": narrative_slug,
        "created_at": created_at,
        "latest_at": latest_at,
        "phase": phase,
        "record": record,
        "video": _artifact_payload(video),
        "slides": _artifact_payload(slides),
        "documentation": _artifact_payload(documentation),
        "narrative": narrative_payload,
        "links": links,
        "all_artifacts": all_artifacts,
    }


# ---------------------------------------------------------------------------
# Clean, shareable run RELEASE page
#
# A curated, outside-the-app-shell view of ONE run: title + video + narrative
# story + the live product URLs the run used. Unlike build_run (the operator
# console, session-gated), this is reachable anonymously via a ?t=<share_token>
# link, so its stream URLs must carry each artifact's own token. Access is the
# same gate the walkthrough viewer uses: a workspace MEMBER, or anyone holding a
# matching share token for the run's primary (public) artifact.
# ---------------------------------------------------------------------------


def _is_member(w: Walkthrough, request) -> bool:
    """True iff the caller is a member of the walkthrough's workspace (the
    non-token half of Walkthrough.readable_by)."""
    from apps.workspaces import services as wsvc

    # A null-workspace walkthrough has no members (see Walkthrough.readable_by).
    if w.workspace_id is None:
        return False
    return w.workspace_id in wsvc.request_workspace_slugs(request)


def _humanize_slug(slug: str) -> str:
    return (slug or "").replace("-", " ").replace("_", " ").strip().title()


def _clean_links(raw: list[dict]) -> list[dict]:
    """Curate a run's link list into clean, visitable destinations.

    Upstream link capture (the DDD upload harvesting scene targets) can emit
    junk the release page must not show: an unexpanded ``${var}`` template that
    never resolved, a bare origin with no path ("App"), a host-less relative
    URL, and the same page repeated per scene. Drop template leaks + bare
    origins, absolutize relative URLs against the run's own host, and de-dupe —
    so "Try it live" is a short list of real pages, not a scene-by-scene dump.
    """
    # Derive the run's host from the first CLEAN absolute url — never a
    # template-polluted one, or the leaked ${var} would pollute the host and
    # get prefixed onto every relative link.
    host = ""
    for link in raw:
        u = (link.get("url") or "").strip()
        if "${" in u:
            continue
        m = re.match(r"^(https?://[^/]+)", u)
        if m:
            host = m.group(1)
            break
    out: list[dict] = []
    seen: set[str] = set()
    for link in raw:
        if not isinstance(link, dict):
            continue
        url = (link.get("url") or "").strip()
        if not url or "${" in url:  # empty or an unresolved template variable
            continue
        if url.startswith("/") and host:  # host-less relative → absolute
            url = host + url
        if "${" in url:  # defence: absolutization must never reintroduce a leak
            continue
        m = re.match(r"^https?://[^/]+(.*)$", url)
        path = m.group(1) if m else url
        if path in ("", "/"):  # bare origin, no meaningful destination
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(
            {
                "label": link.get("label", ""),
                "url": url,
                "kind": link.get("kind", "reference"),
            }
        )
    return out


def _lede_from_story(story: str | None, title: str | None) -> str | None:
    """A one-line hook for the hero: the first sentence of the story that isn't
    the title line (the story's first line is what _title_from_review returns)."""
    if not story:
        return None
    lines = [ln.strip() for ln in story.splitlines() if ln.strip()]
    body = [ln for ln in lines if ln != (title or "").strip()]
    if not body:
        return None
    first = body[0]
    # Trim to the first sentence, capped so the hero stays one line.
    for end in (". ", "? ", "! "):
        if end in first:
            first = first.split(end, 1)[0] + end.strip()
            break
    return first[:240]


def build_release(run_id: str, request, ws: str = "") -> dict | None:
    """Curated, token-aware package for the clean release page.

    Returns ``None`` (→ 404) when the run doesn't exist OR the caller is neither
    a workspace member nor holding a valid share token — never leaking existence.
    NOT workspace-scoped at query time: the share token itself is the capability;
    the primary artifact's ``readable_by`` gate is the authority.

    ``ws`` is the workspace the page's URL names
    (``/w/<ws>/ddd-release/<narrative>/<run>``): a run whose primary artifact
    lives elsewhere is not at that address (``None`` → 404).
    """
    wts = list(Walkthrough.objects.filter(run_id=run_id).select_related("owner"))
    revs = list(ReviewRequest.objects.filter(run_id=run_id))
    if not wts and not revs:
        return None

    # The primary artifact anchors both the access gate and the shareable token.
    primary = _pick(
        wts,
        lambda w: w.role == Walkthrough.ROLE_HERO_VIDEO,
        lambda w: w.kind == Walkthrough.KIND_VIDEO,
        lambda w: w.role == Walkthrough.ROLE_DECK,
        lambda w: w.role == Walkthrough.ROLE_DOCS,
        lambda w: True,
    )
    if primary is None or not primary.readable_by(request):
        return None
    if ws and ws != primary.workspace_id:
        return None

    is_member = _is_member(primary, request)
    is_public = bool(
        primary.visibility == Walkthrough.VISIBILITY_LINK and primary.share_token
    )

    video = _pick(
        wts,
        lambda w: w.role == Walkthrough.ROLE_HERO_VIDEO,
        lambda w: w.role == Walkthrough.ROLE_CLIP,
        lambda w: w.kind == Walkthrough.KIND_VIDEO,
    )
    documentation = _pick(
        wts,
        lambda w: w.role == Walkthrough.ROLE_DOCS,
        lambda w: w.kind == Walkthrough.KIND_HTML and w.role != Walkthrough.ROLE_DECK,
    )

    explicit_narrative_slug = next(
        (w.narrative_slug for w in wts if (w.narrative_slug or "").strip()), None
    )
    narrative_slug = explicit_narrative_slug or narrative_slug_from_run_id(run_id)

    stamped = next((w.narrative_review_id for w in wts if w.narrative_review_id), None)
    narrative_review = None
    if stamped:
        narrative_review = ReviewRequest.objects.filter(pk=stamped).first()
    if narrative_review is None:
        narrative_review = next((r for r in revs if _is_narrative_version(r)), None)
    if narrative_review is None:
        versions = _narrative_versions_for(narrative_slug, None)
        narrative_review = versions[-1] if versions else None
    narrative_payload = _narrative_payload(narrative_review)

    # Title: a short, human headline. The narrative's derived title is the
    # story's first LINE, which is often a full scene-setting sentence — too
    # long for an H1. When it is, use the humanized slug and let that long
    # sentence be the lede instead.
    story = (narrative_payload or {}).get("story")
    derived_title = (narrative_payload or {}).get("title")
    if derived_title and len(derived_title) <= 70:
        title = derived_title
        lede = _lede_from_story(story, derived_title)
    else:
        title = _humanize_slug(narrative_slug)
        lede = _lede_from_story(story, None)

    # Product URLs the run used (reference) vs sibling artifacts (narrative /
    # companion), each curated (template leaks + bare origins dropped, relative
    # absolutized, de-duped) so the page shows real, visitable destinations.
    raw_product: list[dict] = []
    raw_related: list[dict] = []
    for w in sorted(wts, key=lambda x: x.created_at):
        for link in w.links or []:
            if not isinstance(link, dict):
                continue
            kind = link.get("kind", "reference")
            (raw_product if kind == "reference" else raw_related).append(link)
    product_links = _clean_links(raw_product)
    related_links = _clean_links(raw_related)

    return {
        "run_id": run_id,
        "narrative_slug": narrative_slug,
        "title": title,
        "lede": lede,
        "video": _artifact_payload(video),
        "documentation": _artifact_payload(documentation),
        "narrative": narrative_payload,
        "product_links": product_links,
        "related_links": related_links,
        "is_public": is_public,
        "is_member": is_member,
        # The shareable handle: the primary artifact's token (only when public).
        "share_token": primary.share_token if is_public else None,
        # The page's own address, absolute and token-bearing — what you send
        # someone. Under the run's workspace like every artifact
        # (canopy-web#1337); the flat /ddd-release/ is a 404.
        "share_url": (
            wsvc.scoped_url_or_none(
                primary.workspace_id,
                f"/ddd-release/{narrative_slug}/{run_id}?t={primary.share_token}",
            )
            if is_public
            else None
        ),
        # Internal-only link to the full operator console, under the run's
        # workspace. The clean page shows it only to members.
        "build_url": wsvc.scoped_path_or_none(primary.workspace_id, f"/ddd/{narrative_slug}/{run_id}"),
    }


# ---------------------------------------------------------------------------
# Narrative list + detail
# ---------------------------------------------------------------------------


def _blank_narrative(slug: str) -> dict[str, Any]:
    return {
        "slug": slug,
        "title": None,
        "story": None,
        "phase": None,
        "project_slug": None,
        "run_ids": set(),
        "project_slugs": set(),
        "owner_ids": set(),
        "visibilities": set(),
        "latest_at": None,
        "has_video": False,
        "has_deck": False,
        "has_narrative": False,
        "_latest_rev_at": None,
        "_latest_narr_at": None,
        # Origin (T76): the newest artifact/version that carries one wins.
        "agent_project_id": None,
        "_project_at": None,
        "session_id": None,
        "turn_id": None,
        "_origin_at": None,
        "session_ids": set(),
        "creator_id": None,
    }


def _note_origin(a: dict, obj) -> None:
    """Fold one walkthrough's / review's origin into its narrative (newest wins)."""
    at = obj.created_at
    if obj.agent_project_id and (a["_project_at"] is None or at > a["_project_at"]):
        a["agent_project_id"], a["_project_at"] = obj.agent_project_id, at
    if obj.source_session_id:
        a["session_ids"].add(obj.source_session_id)
    if (obj.source_session_id or obj.source_turn_id) and (
        a["_origin_at"] is None or at > a["_origin_at"]
    ):
        a["session_id"], a["turn_id"] = obj.source_session_id, obj.source_turn_id
        a["_origin_at"] = at


def narrative_signals(narr: dict[str, dict]) -> dict[str, dict]:
    """Origin + reaction fields per narrative (board task hal/T76), for the
    whole aggregate in a constant number of queries.

    The agent project falls back to the DDD run doc (``AgentRun`` with this
    narrative as its ``subject``) — the one place a narrative's project was
    already recorded. Comments are Feedback on the narrative (any version);
    views are views of the narrative (its review page, its console, its
    storyboard read); the creator is whoever opened its newest story version."""
    from apps.agent_runs.models import AgentRun
    from apps.agents.models import AgentProject
    from apps.feedback import reactions
    from apps.harness import artifact_origin

    missing = [slug for slug, a in narr.items() if a["agent_project_id"] is None]
    if missing:
        for subject, project_id in (
            AgentRun.objects.filter(subject__in=missing, project__isnull=False)
            .order_by("created_at")
            .values_list("subject", "project_id")
        ):
            narr[subject]["agent_project_id"] = project_id  # newest wins (ascending)
    projects = {
        p.pk: p
        for p in AgentProject.objects.select_related("agent").filter(
            pk__in={a["agent_project_id"] for a in narr.values() if a["agent_project_id"]}
        )
    }
    summary = reactions.summarize(
        reactions.Target(
            key=slug,
            feedback=[(reactions.NARRATIVE, slug, None)],
            views=[(reactions.NARRATIVE, slug)],
            creator_id=a["creator_id"],
            extra_commenters=a.get("_suggesters", []),
        )
        for slug, a in narr.items()
    )
    return {
        slug: {
            "session_id": a["session_id"],
            "turn_id": a["turn_id"],
            "agent_project": artifact_origin.project_out(projects.get(a["agent_project_id"])),
            **summary[slug],
        }
        for slug, a in narr.items()
    }


def _suggestion_identity(entry) -> str:
    from apps.feedback.reactions import suggestion_identity

    return suggestion_identity(entry)


def _project_matches(project: dict | None, wanted: str) -> bool:
    """Does a narrative's resolved agent project match a list filter value?"""
    if project is None:
        return False
    wanted = wanted.strip()
    if wanted.isdigit():
        return project["id"] == int(wanted)
    agent, _, ext = wanted.replace(":", "/").rpartition("/")
    return project["ext_id"] == ext and (not agent or project["agent"] == agent)


def _max(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


def _aggregate(
    project: str | None,
    owner_id: int | None,
    workspace_slugs: set[str] | None = None,
) -> dict[str, dict]:
    """Build the per-narrative aggregate map from both tables. Filters are
    applied at the narrative level afterwards by the callers."""
    wts = list(
        _scope(
            Walkthrough.objects.exclude(run_id__isnull=True).exclude(run_id=""),
            workspace_slugs,
        ).select_related("owner")
    )
    revs = list(_scope(ReviewRequest.objects.all(), workspace_slugs))

    feature_map = _narrative_slug_map(wts)

    narr: dict[str, dict] = {}
    for w in wts:
        slug = narrative_of_walkthrough(w)
        a = narr.get(slug) or narr.setdefault(slug, _blank_narrative(slug))
        a["run_ids"].add(w.run_id)
        if w.project_slug:
            a["project_slugs"].add(w.project_slug)
            if a["project_slug"] is None:
                a["project_slug"] = w.project_slug
        _note_origin(a, w)
        if a["creator_id"] is None:
            a["creator_id"] = w.owner_id
        a["owner_ids"].add(w.owner_id)
        a["visibilities"].add(w.visibility)
        a["latest_at"] = _max(a["latest_at"], w.created_at)
        if _is_video(w):
            a["has_video"] = True
        if _is_deck(w):
            a["has_deck"] = True

    for r in revs:
        slug = narrative_for_run_id(r.run_id, feature_map)
        # A run-child review ATTACHES to a narrative but never CREATES one. The
        # gate cannot discriminate here: a DDD findings review and Ada's fleet
        # audit both use `product_findings`, and the DDD one is a genuine child
        # of a real run, so it must keep attaching. What separates them is
        # whether a narrative exists at all — Ada's run_id is not a DDD run id,
        # so nothing else references it. Without this, parsing a slug out of any
        # run_id conjured a phantom narrative into the DDD rail (active, empty,
        # unnavigable). Narrative-version reviews still create: a review-only
        # narrative is legitimate (see narrative_for_run_id).
        a = narr.get(slug)
        if a is None:
            if is_run_child_gate(r.gate):
                continue
            a = narr.setdefault(slug, _blank_narrative(slug))
        a["run_ids"].add(r.run_id)
        if r.project_slug:
            a["project_slugs"].add(r.project_slug)
            if a["project_slug"] is None:
                a["project_slug"] = r.project_slug
        _note_origin(a, r)
        a.setdefault("_suggesters", []).extend(
            _suggestion_identity(e) for e in (r.suggestions_json or [])
        )
        if r.owner_id:
            a["owner_ids"].add(r.owner_id)
        a["visibilities"].add(r.visibility)
        a["latest_at"] = _max(a["latest_at"], r.created_at)
        # Latest review overall drives the phase label.
        if a["_latest_rev_at"] is None or r.created_at > a["_latest_rev_at"]:
            a["_latest_rev_at"] = r.created_at
            a["phase"] = _phase_label(r)
        # Latest review carrying a story drives title/story.
        if _review_has_narrative(r) and (
            a["_latest_narr_at"] is None or r.created_at > a["_latest_narr_at"]
        ):
            a["_latest_narr_at"] = r.created_at
            a["has_narrative"] = True
            if r.owner_id:
                a["creator_id"] = r.owner_id
            a["title"] = _title_from_review(r)
            rj = r.request_json if isinstance(r.request_json, dict) else {}
            a["story"] = (rj.get("narrative") or "").strip() or None

    # Narrative-level filters.
    def _keep(a: dict) -> bool:
        if project is not None and project not in a["project_slugs"]:
            return False
        if owner_id is not None and owner_id not in a["owner_ids"]:
            return False
        return True

    return {slug: a for slug, a in narr.items() if _keep(a)}


def list_narratives(
    project: str | None = None,
    owner_id: int | None = None,
    workspace_slugs: set[str] | None = None,
    session: str | None = None,
    agent_project: str | None = None,
) -> list[dict]:
    """Narrative list items, newest activity first.

    ``session`` keeps narratives with any artifact/version made from that canopy
    session; ``agent_project`` those serving that board project (``hal/P5``,
    ``hal:P5`` or an id) — T76."""
    narr = _aggregate(project, owner_id, workspace_slugs)
    if session:
        narr = {s: a for s, a in narr.items() if session in {str(x) for x in a["session_ids"]}}
    signals = narrative_signals(narr)
    if agent_project:
        narr = {s: a for s, a in narr.items() if _project_matches(signals[s]["agent_project"], agent_project)}
    items = [
        {
            **signals[a["slug"]],
            "slug": a["slug"],
            "title": a["title"],
            "phase": a["phase"],
            "project_slug": a["project_slug"],
            "run_count": len(a["run_ids"]),
            "latest_at": a["latest_at"],
            "has_video": a["has_video"],
            "has_deck": a["has_deck"],
            "has_narrative": a["has_narrative"],
        }
        for a in narr.values()
    ]
    items.sort(key=lambda it: it["latest_at"] or datetime.min, reverse=True)
    return items


def _run_summary(run_id, run_wts, run_revs) -> dict:
    run_revs = sorted(run_revs, key=lambda r: r.created_at, reverse=True)
    latest_rev = run_revs[0] if run_revs else None
    narr_rev = next((r for r in run_revs if _is_narrative_version(r)), latest_rev)
    ts = [w.created_at for w in run_wts] + [r.created_at for r in run_revs]
    return {
        "run_id": run_id,
        "created_at": min(ts) if ts else None,
        "latest_at": max(ts) if ts else None,
        "status": latest_rev.status if latest_rev else None,
        "gate": latest_rev.gate if latest_rev else None,
        "scene_count": _scene_count(narr_rev),
        "has_video": any(_is_video(w) for w in run_wts),
        "has_deck": any(_is_deck(w) for w in run_wts),
    }


def _agg_visibility(visibilities: set[str]) -> str:
    """Collapse a set of row visibilities into the narrative's status."""
    if visibilities == {Walkthrough.VISIBILITY_LINK}:
        return "public"
    if visibilities <= {Walkthrough.VISIBILITY_PRIVATE}:  # all private or empty
        return "private"
    return "mixed"


def build_narrative(
    slug: str, workspace_slugs: set[str] | None = None
) -> dict | None:
    """Narrative landing: version-grouped — each narrative version with its runs
    nested beneath it (newest version first)."""
    narr = _aggregate(project=None, owner_id=None, workspace_slugs=workspace_slugs)
    a = narr.get(slug)
    if a is None:
        return None

    wts = [
        w
        for w in _scope(
            Walkthrough.objects.exclude(run_id__isnull=True).exclude(run_id=""),
            workspace_slugs,
        ).select_related("owner")
        if narrative_of_walkthrough(w) == slug
    ]
    revs = [
        r
        for r in _scope(ReviewRequest.objects.all(), workspace_slugs)
        if narrative_of_review(r) == slug
    ]

    wts_by_run: dict[str, list[Walkthrough]] = {}
    for w in wts:
        wts_by_run.setdefault(w.run_id, []).append(w)
    revs_by_run: dict[str, list[ReviewRequest]] = {}
    for r in revs:
        revs_by_run.setdefault(r.run_id, []).append(r)

    # Narrative versions (story-bearing reviews), oldest first.
    versions = [r for r in revs if _is_narrative_version(r)]
    versions.sort(key=lambda r: (r.version, r.created_at))
    versions_by_id = {str(r.id): r for r in versions}
    current = versions[-1] if versions else None

    # Version-pinned narrative videos. A video walkthrough stamped with a
    # version's review id (``narrative_review_id``) belongs to that exact story
    # version — so a later narration edit can't leave a stale video on a newer
    # version. Queried separately from ``wts`` above (which is run-scoped); a
    # narrative-version video may carry no run_id. A recorded narrative pins one
    # video PER CUT; ``pinned.resolve`` picks the hero and orders the cuts.
    pinned_rows = pinned.pinned_video_rows(
        [r.id for r in versions], _scope(Walkthrough.objects.all(), workspace_slugs)
    )
    pinned_by_review = {
        str(r.id): pinned.resolve(
            pinned_rows.get(str(r.id), []), (r.request_json or {}).get("narration")
        )
        for r in versions
    }

    # Resolve which version each run rendered.
    def _version_review_for(run_id) -> ReviewRequest | None:
        run_wts = wts_by_run.get(run_id, [])
        stamped = next((w.narrative_review_id for w in run_wts if w.narrative_review_id), None)
        if stamped and str(stamped) in versions_by_id:
            return versions_by_id[str(stamped)]
        own = next(
            (r for r in revs_by_run.get(run_id, []) if _is_narrative_version(r)), None
        )
        if own is not None:
            return own
        return current

    # A "run" is a render — it has artifacts. Narrative-version reviews are NOT
    # runs (they're the story), so bucket only artifact-bearing run_ids.
    buckets: dict[str | None, list[dict]] = {}
    for run_id in wts_by_run:
        ver = _version_review_for(run_id)
        key = str(ver.id) if ver is not None else None
        summary = _run_summary(run_id, wts_by_run[run_id], revs_by_run.get(run_id, []))
        buckets.setdefault(key, []).append(summary)

    def _sorted_runs(rs):
        return sorted(rs, key=lambda it: it["latest_at"] or datetime.min, reverse=True)

    versions_payload = []
    for r in reversed(versions):  # newest version first
        np = _narrative_payload(r)
        pv = pinned_by_review[str(r.id)]
        vid = pv.hero
        versions_payload.append(
            {
                "version": r.version,
                "review_id": str(r.id),
                "title": np["title"],
                "story": np["story"],
                "narration": np["narration"],
                "created_at": r.created_at,
                "gate": r.gate,
                "status": r.status,
                "video_url": _content_url(vid) if vid else None,
                "video_viewer_url": _viewer_url(vid) if vid else None,
                "cuts": [_cut_payload(w) for w in pv.cuts],
                "runs": _sorted_runs(buckets.get(str(r.id), [])),
            }
        )
    # Runs with no resolvable version (e.g. narrative has artifacts but no review).
    if buckets.get(None):
        versions_payload.append(
            {
                "version": None,
                "review_id": None,
                "title": None,
                "story": None,
                "narration": [],
                "created_at": None,
                "gate": None,
                "status": None,
                "runs": _sorted_runs(buckets[None]),
            }
        )

    current_payload = None
    if current is not None:
        cp = _narrative_payload(current)
        cpv = pinned_by_review[cp["review_id"]]
        cvid = cpv.hero
        current_payload = {
            "review_id": cp["review_id"],
            "version": cp["version"],
            "title": cp["title"],
            "story": cp["story"],
            "video_url": _content_url(cvid) if cvid else None,
            "video_viewer_url": _viewer_url(cvid) if cvid else None,
            "cuts": [_cut_payload(w) for w in cpv.cuts],
        }

    return {
        **narrative_signals({slug: a})[slug],
        "slug": slug,
        "title": a["title"],
        "story": a["story"],
        "phase": a["phase"],
        "project_slug": a["project_slug"],
        "visibility": _agg_visibility(a["visibilities"]),
        "current_version": current_payload,
        "versions": versions_payload,
    }


def set_narrative_visibility(
    slug: str, visibility: str, workspace_slugs: set[str] | None = None
) -> tuple[int, int]:
    """Set visibility on every walkthrough + review grouped under ``slug``.

    Matches the exact same rows the narrative aggregate displays (explicit
    narrative_slug wins; run_id-derived slug is the fallback). Returns
    (walkthroughs_updated, reviews_updated).
    """
    slug = (slug or "").strip()
    wts = list(
        _scope(
            Walkthrough.objects.exclude(run_id__isnull=True).exclude(run_id=""),
            workspace_slugs,
        )
    )
    # Resolve membership exactly as build_narrative does for the /ddd page:
    # walkthroughs via narrative_of_walkthrough, reviews via narrative_of_review
    # (the review's own narrative_slug wins). Keeps the cascade flipping precisely
    # the rows the narrative page shows.
    wt_pks = [w.pk for w in wts if narrative_of_walkthrough(w) == slug]
    rev_pks = [
        r.pk
        for r in _scope(ReviewRequest.objects.all(), workspace_slugs)
        if narrative_of_review(r) == slug
    ]
    wt_qs = Walkthrough.objects.filter(pk__in=wt_pks)
    wt_n = wt_qs.update(visibility=visibility)
    # Flipping a narrative to public (link) must MINT a share token on each
    # walkthrough, or anonymous ?t= access 404s and there is no shareable link.
    # The bulk update() above bypasses save(), so re-fetch and ensure a token on
    # each — mirrors the per-walkthrough PATCH flip-to-public (apps/walkthroughs
    # /api.py). Reviews stay tokenless by design.
    if visibility == Walkthrough.VISIBILITY_LINK:
        for w in wt_qs:
            w.ensure_share_token()
    rev_n = ReviewRequest.objects.filter(pk__in=rev_pks).update(visibility=visibility)
    return wt_n, rev_n
