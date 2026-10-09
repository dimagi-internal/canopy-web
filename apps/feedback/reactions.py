"""The REACTION signal on an artifact: who commented, and whether its human looked.

Why (board task hal/T76, 2026-10-09): a "useful artifact" is one the agent judged
good at the time OR one the human responded to and implied was good. Canopy could
show neither — artifacts carried no reaction at all. This module answers, for a
batch of artifacts, four numbers a list can render without an N+1:

* ``comment_count``   — Feedback rows aimed at the artifact (any channel).
* ``commenter_count`` — distinct people among them (email, else name, else the
  signed-in submitter).
* ``viewer_count``    — distinct HUMANS who opened it while signed in. An agent's
  own login (``Agent.user``) is not a human; reading back your own upload is not
  a reaction (``bin/hal-verify-link --as-hal`` does exactly that).
* ``owner_viewed_at`` — when the creator's human last opened it. For an upload
  made with an agent's PAT that human is the agent's OWNER (Jonathan, for hal);
  for an upload made by a person it is that person.

Framework tier, generic over its target by string — the caller (a product app)
says which feedback targets and view targets stand for each of its artifacts.
Every function is batch-shaped: a constant number of queries per list.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import ArtifactView, Feedback

log = logging.getLogger(__name__)

#: target kinds a view is recorded under (the same vocabulary Feedback uses).
WALKTHROUGH = "walkthrough"
NARRATIVE = "narrative"
STORYBOARD = "storyboard"

#: Re-reads closer together than this are the same visit (see record_view).
VISIT_WINDOW = dt.timedelta(minutes=5)


# ---------------------------------------------------------------------- write


def record_view(request, target_kind: str, target_ref) -> None:
    """Upsert one view of ``(target_kind, target_ref)`` by the signed-in caller.

    A no-op for an anonymous reader. NEVER fails the read it rides on: a view
    record is a signal, and a page that 500s because its signal could not be
    written is worse than a missing signal."""
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return
    ref = str(target_ref or "")[:200]
    if not ref:
        return
    now = timezone.now()
    try:
        rows = ArtifactView.objects.filter(target_kind=target_kind, target_ref=ref, user=user)
        # Throttled: an orchestrator POLLING a review (every few seconds, for
        # hours) is one visit, not thousands of writes. A re-read inside the
        # window costs one indexed SELECT and writes nothing.
        if rows.filter(last_viewed_at__lt=now - VISIT_WINDOW).update(
            view_count=F("view_count") + 1, last_viewed_at=now
        ):
            return
        if rows.exists():
            return
        try:
            # Savepoint: SESSION_SAVE_EVERY_REQUEST means a poisoned outer
            # transaction would take the session write down with it.
            with transaction.atomic():
                ArtifactView.objects.create(
                    target_kind=target_kind, target_ref=ref, user=user, last_viewed_at=now
                )
        except IntegrityError:  # a concurrent first view won the insert
            rows.update(view_count=F("view_count") + 1, last_viewed_at=now)
    except Exception:  # noqa: BLE001 — a signal must never fail a read
        log.exception("could not record a view of %s:%s", target_kind, ref)


# ----------------------------------------------------------------------- read


@dataclass
class Target:
    """One artifact, described in this module's generic terms.

    ``feedback`` — ``(target_kind, target_ref, target_version | None)`` tuples
    whose Feedback counts as a reaction to this artifact (a version of None
    matches every version). ``views`` — ``(target_kind, target_ref)`` tuples
    whose views count. ``creator_id`` — the user who made it (an agent's login
    or a person); None when unknown."""

    key: str
    feedback: list[tuple[str, str, int | None]] = field(default_factory=list)
    views: list[tuple[str, str]] = field(default_factory=list)
    creator_id: int | None = None
    extra_commenters: list[str] = field(default_factory=list)
    """Identities of reactions held outside Feedback (a review's external
    suggestions) — counted as comments by the same identity rule."""


def empty() -> dict:
    return {"comment_count": 0, "commenter_count": 0, "viewer_count": 0, "owner_viewed_at": None}


def _identity(email: str, name: str, submitted_by_id) -> str:
    email = (email or "").strip().lower()
    if email:
        return f"email:{email}"
    name = (name or "").strip().lower()
    if name:
        return f"name:{name}"
    return f"user:{submitted_by_id}" if submitted_by_id else "anonymous"


def suggestion_identity(entry) -> str:
    """The commenter identity of one ``ReviewRequest.suggestions_json`` entry."""
    entry = entry if isinstance(entry, dict) else {}
    return _identity(entry.get("email") or "", entry.get("name") or "", None)


def humans_of(user_ids: Iterable[int]) -> tuple[dict[int, int], set[int]]:
    """``({user_id: the human behind it}, {user ids that are agent logins})``.

    An agent's login maps to the agent's owner; anyone else maps to themselves.
    One query."""
    from apps.agents.models import Agent

    ids = {u for u in user_ids if u is not None}
    human = {u: u for u in ids}
    agent_users: set[int] = set()
    if not ids:
        return human, agent_users
    for user_id, owner_id in Agent.objects.filter(user_id__in=ids).values_list("user_id", "owner_id"):
        agent_users.add(user_id)
        human[user_id] = owner_id  # may be None: an ownerless agent has no human
    return human, agent_users


def summarize(targets: Iterable[Target]) -> dict[str, dict]:
    """``{target.key: {comment_count, commenter_count, viewer_count, owner_viewed_at}}``.

    Three queries for the whole batch, whatever its size."""
    targets = list(targets)
    out = {t.key: empty() for t in targets}
    if not targets:
        return out

    # -- comments: one query over every (kind, ref) any target names.
    by_kind_ref: dict[tuple[str, str], list[tuple[Target, int | None]]] = {}
    for t in targets:
        for kind, ref, version in t.feedback:
            by_kind_ref.setdefault((kind, str(ref)), []).append((t, version))
    commenters: dict[str, set[str]] = {t.key: set(t.extra_commenters) for t in targets}
    counts: dict[str, int] = {t.key: len(t.extra_commenters) for t in targets}
    if by_kind_ref:
        q = Q()
        kinds: dict[str, set[str]] = {}
        for kind, ref in by_kind_ref:
            kinds.setdefault(kind, set()).add(ref)
        for kind, refs in kinds.items():
            q |= Q(target_kind=kind, target_ref__in=refs)
        rows = Feedback.objects.filter(q).values_list(
            "target_kind", "target_ref", "target_version", "author_email", "author_name",
            "submitted_by_id",
        )
        for kind, ref, version, email, name, by in rows:
            for t, want in by_kind_ref.get((kind, ref), ()):
                if want is not None and version != want:
                    continue
                counts[t.key] += 1
                commenters[t.key].add(_identity(email, name, by))
    for t in targets:
        out[t.key]["comment_count"] = counts[t.key]
        out[t.key]["commenter_count"] = len(commenters[t.key])

    # -- views: one query for the rows, one for who is an agent.
    view_keys: dict[tuple[str, str], list[Target]] = {}
    for t in targets:
        for kind, ref in t.views:
            view_keys.setdefault((kind, str(ref)), []).append(t)
    views: list[tuple[str, str, int, dt.datetime]] = []
    if view_keys:
        q = Q()
        kinds = {}
        for kind, ref in view_keys:
            kinds.setdefault(kind, set()).add(ref)
        for kind, refs in kinds.items():
            q |= Q(target_kind=kind, target_ref__in=refs)
        views = list(
            ArtifactView.objects.filter(q).values_list(
                "target_kind", "target_ref", "user_id", "last_viewed_at"
            )
        )
    human, agent_users = humans_of(
        {v[2] for v in views} | {t.creator_id for t in targets if t.creator_id}
    )
    viewers: dict[str, set[int]] = {t.key: set() for t in targets}
    for kind, ref, user_id, at in views:
        for t in view_keys.get((kind, ref), ()):
            if user_id not in agent_users:
                viewers[t.key].add(user_id)
            owner_human = human.get(t.creator_id) if t.creator_id else None
            if owner_human is not None and user_id == owner_human:
                prev = out[t.key]["owner_viewed_at"]
                out[t.key]["owner_viewed_at"] = at if prev is None or at > prev else prev
    for t in targets:
        out[t.key]["viewer_count"] = len(viewers[t.key])
    return out
