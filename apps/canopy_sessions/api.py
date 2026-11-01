"""Django Ninja router for /api/canopy-sessions — live chat sessions.

The one router and the request helpers every session route shares. The routes
live beside it by area, each registering on this router; they are imported at the
bottom, after the helpers they use.
"""
from __future__ import annotations

import datetime as dt
import uuid

from django.db.models import Q
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.embedding import delegation
from apps.harness import sensitivity
from apps.workspaces import services as wsvc

from . import (
    access,
    services,
    staleness,
    transcript_read,
    transfer_requests,
)
from . import activity as session_activity
from .models import Session

#: Reserved, server-owned metadata key: the embedding app a session was created
#: through. Named once so the writer (create) and the reader (list filter)
#: cannot drift onto different spellings.
EMBED_APP_KEY = services.EMBED_APP_KEY

router = Router(auth=session_auth, tags=["chat"])

#: User-level views (`/api/me/…`), mounted at `/me`.
me_router = Router(auth=session_auth, tags=["chat"])


def _visible_slugs(request: HttpRequest, *, listing: bool = False, bulk_write: bool = False) -> set[str]:
    """The tenants a session read may reach. A by-id read (the default) takes
    every membership, or the pin; a LIST or search (`listing=True`) takes the
    read rule — a caller whose workspaces sit in more than one tenant tree must
    pin one, or gets a 422 rather than two organisations' sessions; a pinned list
    covers the caller's workspaces below the pin. A bulk reset (`bulk_write=True`)
    takes the WRITE scope: pinned, exactly that workspace, never the ones below."""
    if bulk_write:
        return wsvc.request_write_slugs(request)
    if listing:
        return wsvc.request_workspace_slugs(request)
    return wsvc.request_member_slugs(request)


def _site_scoped(request: HttpRequest, qs):
    """`site ∩ user`: when a connected site is acting, only its own agents'
    sessions exist (apps/embedding/delegation.py). No-op for the person themself."""
    offered = delegation.offered_for(request)
    return qs if offered is None else qs.filter(agent_id__in=offered)


def readable_sessions(request: HttpRequest):
    """The sessions a by-id request may name: `access.readable_sessions` over the
    caller's tenants, narrowed to the acting site's agents. Every route that
    takes a session by id reads this — the Slack routes too (apps/slack/session_api.py)."""
    return _site_scoped(request, access.readable_sessions(request.user,
                                                          workspace_slugs=_visible_slugs(request)))


def _session_or_404(request: HttpRequest, session_id: uuid.UUID, *, write: bool = False) -> Session:
    # One authority (`access`): the tenant, then who within it — the same rule
    # the list, the chat socket and attachments apply. `write=True` also needs
    # an owner/editor role; a viewer gets 403, since they can already see it.
    session = get_object_or_404(
        readable_sessions(request)
        .select_related("agent", "created_by", "runner_binding", "runner_binding__runner")
        .annotate(_last_msg_at=transcript_read.last_message_at()),
        pk=session_id,
    )
    if write and not access.can_write(request.user, session):
        raise HttpError(403, "you can read this session but not act in it")
    return session


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    """A query-string datetime, read as UTC when it names no zone."""
    if value is None or timezone.is_aware(value):
        return value
    return timezone.make_aware(value, dt.timezone.utc)


def _filtered_sessions(
    request: HttpRequest, *, state: str, source: str = "", opp_slug: str = "",
    opp_run_id: str = "", origin_key: str = "", embed_app: str = "",
    resource: str = "", page_path: str = "", session_key: str = "",
    q: str = "", repo: str = "", branch: str = "", pr: str = "",
    since: dt.datetime | None = None,
    until: dt.datetime | None = None, runner: str = "", of_user: int | None = None,
    slack_channel: str = "",
):
    """Every session the caller may see, narrowed by the list filters, with
    `_last_msg_at`, `_activity` (= `last_activity_at`, computed in SQL) and
    `_opening` annotated. Unordered — each route orders it its own way. The
    list and the search route share it so a filter (and, above all, the
    delegated-app scoping) cannot exist on one and not the other."""
    if state not in ("active", "archived", "all"):
        raise HttpError(422, "state must be one of: active, archived, all")

    slugs = _visible_slugs(request, listing=True)
    rows = (
        # The same authority every other session surface reads — see
        # apps/canopy_sessions/access.py — narrowed to the acting site's agents.
        _site_scoped(request, access.readable_sessions(request.user, workspace_slugs=slugs))
        .select_related("agent", "created_by", "runner_binding", "runner_binding__runner")
    )
    # Embedder filters: an embedder (e.g. ace-web) narrows the shared
    # session list to the sessions it cares about, keyed on the opaque
    # `metadata` bag a session carries (never interpreted elsewhere in this
    # app). Empty string = no filter, so the default call is unaffected.
    #
    # `origin_key` is the generic one: an embedder whose own product is
    # multi-tenant stamps ITS tenant into metadata.origin_key at create time and
    # filters on it here, so two of its tenants sharing one canopy workspace do
    # not see each other's sessions in the list. Deliberately opaque — canopy
    # never parses it. (Note the residual: this scopes the LIST; canopy's own
    # tenancy still lets any member of the canopy workspace open a session by id.
    # An embedder that needs hard isolation maps its tenants onto separate canopy
    # workspaces instead.)
    rows = services.host_filters(rows, source=source, origin_key=origin_key, opp_slug=opp_slug,
                                 opp_run_id=opp_run_id, resource=resource, page_path=page_path)
    # Which embedding app created the session — stamped server-side from the
    # delegated token at create (see create_session), so unlike `origin_key`
    # this one cannot have been chosen by whoever wrote the row.
    #
    # For a DELEGATED caller it is FORCED, not a filter: a caller-supplied
    # `embed_app` would let a token issued to connect-labs pass
    # `embed_app=canopy-web` and enumerate that user's conversations from
    # another host — same user, but across the host boundary, titles included.
    # A host sees its own conversations HERE and never asks for someone else's
    # THERE (tests/test_embed_session_provenance.py).
    #
    # A browser/PAT caller is unchanged: it is the human themself, not an app
    # acting for them, so an unfiltered list stays unfiltered and canopy's own
    # UI does not quietly narrow.
    acting_app = getattr(request, "delegated_app", None)
    if acting_app is not None:
        rows = rows.filter(**{f"metadata__{EMBED_APP_KEY}": acting_app.name})
    elif embed_app:
        rows = rows.filter(**{f"metadata__{EMBED_APP_KEY}": embed_app})

    # The name an Open Sessions card shows for a runner session (an emdash task
    # name, or a cloud session's Claude UUID) — so it can be looked up by what
    # a person reads off the card, not only by the session id they never see.
    if session_key:
        rows = rows.filter(runner_binding__session_key=session_key)
    # The box it is on, by id or name — what to move off a runner (`/transfer-all`).
    if runner:
        box = transfer_requests.resolve_runner(runner)
        rows = rows.filter(runner_binding__runner=box) if box is not None else rows.none()
    # One person's own sessions (`access.sessions_of_q`) — what a coach an agent's
    # person let review them lists (`/api/people/session-reviewers/granted-to-me/`).
    # Only narrows what the caller may already read.
    if of_user is not None:
        rows = rows.filter(access.sessions_of_q([of_user]))
    # The sessions that live in one Slack channel (its id), as the session's metadata names it.
    # Only narrows what the caller may already read — a channel's reviewer lists its sessions so.
    if slack_channel:
        rows = rows.filter(metadata__slack_channel=slack_channel)
    # Free text over what a person reads off a card: the title, or the runner's
    # session_key (an emdash task name, or a cloud session's Claude UUID).
    if q:
        rows = rows.filter(Q(title__icontains=q) | Q(runner_binding__session_key__icontains=q))
    # The repo a session ran in — or worked on. An agentless repo chat names it
    # in `project`, a runner session's binding carries the emdash project its
    # worktree lives under (`RunnerBinding.emdash_project`), and Session.activity
    # (folded at ingest, activity.py) holds every repo the session cd'd into,
    # edited, pushed to or opened a PR on — so an agent session that built
    # connect-labs matches `repo=connect-labs` too. `repo` takes a bare name or
    # owner/name; `branch` any branch the transcript saw; `pr` a number,
    # owner/name#N or a PR URL.
    if repo:
        name = repo.strip().lower().removesuffix(".git").split("/")[-1]
        rows = rows.filter(Q(project__iexact=name) | Q(runner_binding__emdash_project__iexact=name)
                           | session_activity.filter_q(repo=repo))
    if branch or pr:
        try:
            rows = rows.filter(session_activity.filter_q(branch=branch, pr=pr))
        except ValueError as exc:
            raise HttpError(422, str(exc)) from exc
    # `_activity` is transcript_read.last_activity_at's rule in SQL, so a window and a
    # cursor can be applied to it.
    rows = transcript_read.with_activity(transcript_read.with_opening(rows)).distinct()
    if since is not None:
        rows = rows.filter(_activity__gte=_aware(since))
    if until is not None:
        rows = rows.filter(_activity__lt=_aware(until))
    if state == "active":
        rows = staleness.active(rows)
    elif state == "archived":
        rows = rows.filter(Q(status=Session.ARCHIVED) | staleness.unseen_q())
    return rows


def _session_by_key_or_404(request: HttpRequest, key: uuid.UUID) -> Session:
    """The fallback for a UUID that is not a session id: the runner's
    `session_key` (a cloud session's key is a Claude session UUID, and it is
    what the Open Sessions card shows). Read through the SAME authority as the
    id lookup, so it finds nothing the caller could not already open by id."""
    matches = list(
        _site_scoped(request, access.readable_sessions(request.user,
                                                       workspace_slugs=_visible_slugs(request)))
        .filter(runner_binding__session_key=str(key))
        .values_list("pk", flat=True)[:5]
    )
    if not matches:
        raise HttpError(
            404,
            "no session with this id or session_key is visible to you. A session is "
            "visible to its creator, its participants, the agent's admins (for the "
            "agent's own threads) and, for an emdash-discovered session, its workspace",
        )
    if len(matches) > 1:
        raise HttpError(
            409,
            "this session_key names more than one session; open one by id: "
            + ", ".join(str(m) for m in matches),
        )
    return _session_or_404(request, matches[0])


def _in_it_or_403(request: HttpRequest, session) -> None:
    # Bringing someone into a sensitive conversation, or copying it whole (a fork),
    # hands its words on: only its own people may, on its own chat surfaces. Without
    # this an agent's admin — an owner of a visitor's thread who is not IN it — could
    # add themselves, or fork it, and read it back.
    if not sensitivity.reads_content(session, user=request.user,
                                     chat_surface=sensitivity.request_is_chat_surface(request)):
        raise HttpError(403, sensitivity.NOT_IN_IT)


# The routes, by area. Each registers on `router` above, so the order here is the
# order paths are matched in: every module declaring a one-segment path (`/reset`,
# `/my-keys`, `/transfer-all`, …) comes before `api_read`, whose `/{session_id}`
# would otherwise take that segment as a session id.
from . import (  # noqa: E402,F401
    api_lifecycle,
    api_people,
    api_transfer,
    api_read,
    api_attachments,
    api_page,
    api_secrets,
)
