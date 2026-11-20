"""`GET /api/brain/preview` and `GET /api/brain/would-read` (the `/brain` page's, which also takes a preview snapshot as
its `source`): what the brain WOULD do over the sessions you can read, storing nothing.

PERSON-ONLY: your browser session, a PAT you minted by hand, or an act-as-you token you approved with your passkey
for READ (a step-up: an agent reading as you, in front of you). Never an agent's login, a turn's or caller token, a
runner, an OAuth client or a site's token, and not an MCP tool (`apps/mcp/api_tools.py` EXCLUDED). It answers with
the rollout gate off: it reads only sessions you already read (legs 1 to 6). `apps/brain/preview.py`."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router

from apps.api.auth import session_auth
from apps.api.errors import TYPE_FORBIDDEN, TYPE_VALIDATION, ProblemError, refusals
from apps.api.route_gates import gate
from apps.contacts import people
from apps.harness import acting_person
from apps.harness.models import ActAsGrant
from apps.tokens import kinds
from apps.workspaces import services as wsvc

from . import preview as preview_mod
from .schemas import BrainPreviewOut, BrainWouldReadOut

router = Router(auth=session_auth, tags=["brain"])

#: The credentials a person previews with themselves: their browser session or a PAT they minted by hand. An act-as
#: token is the third, and only under a READ grant (`the_person`).
PREVIEW_KINDS = frozenset({kinds.SESSION, kinds.PAT})
#: The longest window one preview spans.
MAX_WINDOW = dt.timedelta(days=7)


def the_person(request: HttpRequest):
    """The person, as themselves or through a read-only act-as-you grant they approved, else a 403."""
    kind = kinds.of_request(request)
    user = getattr(request, "user", None)
    if getattr(request, "via_mcp", False) or kind is None or not people.is_human_account(user):
        raise ProblemError(403, "Only the person may preview the brain", type_=TYPE_FORBIDDEN)
    if kind.name == kinds.ACT_AS:
        grant = getattr(request, "act_as_grant", None)
        if grant is None or grant.scope != ActAsGrant.READ or grant.person_id != user.pk \
                or getattr(request, "runner_token", None) is not None:
            raise ProblemError(403, "Only a read-only act-as-you permission previews the brain", type_=TYPE_FORBIDDEN)
        return user
    if kind.name not in PREVIEW_KINDS or acting_person.refusal(request) is not None:
        raise ProblemError(403, "Only the person may preview the brain", type_=TYPE_FORBIDDEN)
    return user


#: The name `api_graph` (the network graph) imports it by.
_the_person = the_person


def _window(since, until, after: str, session: str = ""):
    until = until or timezone.now()
    since = since or until - dt.timedelta(days=1)
    if since >= until or until - since > MAX_WINDOW:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION,
                           detail="since must be before until, and the window a week at most")
    for name, value in (("after", after), ("session", session)):
        if value:
            try:
                uuid.UUID(value)
            except ValueError as e:
                raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION,
                                   detail=f"{name} is a session id") from e
    return since, until


@router.get("/brain/preview", response={200: BrainPreviewOut, **refusals(403, 422)},
            summary="What learning would do over your sessions (stores nothing)")
@gate("session-acl")  # reads only sessions you already read, through legs 1 to 6; writes nothing
def brain_preview(request: HttpRequest, since: dt.datetime | None = None, until: dt.datetime | None = None,
                  view: Literal["full", "conversation", "userinput", "opening"] = "full", after: str = "",
                  limit: int = 10, session: str = "") -> dict:
    """For each session you can read that was created or active between `since` and `until` (the last day by
    default, a week at most): who is in it, whether each class of learning could read it now and if everyone in it
    agreed, with the reason, the project links canopy would make, and the session as a learner reads it, secrets
    scrubbed. Nothing is stored or recorded. Pass `next_after` back as `after` for the next page, or name one
    `session` of the window."""
    user = the_person(request)
    since, until = _window(since, until, after, session)
    return preview_mod.preview(user, wsvc.request_member_slugs(request), since=since, until=until, view=view,
                               after=after, limit=limit, session=session)


@router.get("/brain/would-read", response={200: BrainWouldReadOut, **refusals(403, 404, 422)},
            summary="Whether each class of learning could read your sessions, and the links canopy would make")
@gate("session-acl")  # reads only sessions you already read, through legs 1 to 6; writes nothing
def brain_would_read(request: HttpRequest, since: dt.datetime | None = None, until: dt.datetime | None = None,
                     after: str = "", limit: int = 10, source: str = "live") -> dict:
    """Per session you read: who is in it, whether each class could read it now and if everyone agreed, with the
    reason, and the links canopy would make; what the page adds up to; its links by project and tier. Live: over
    `since`..`until` (the last day by default, a week at most), computed now. `source=preview:<id>`: what that preview
    computed over its own window. No line of a transcript (ask the preview with `session` for one)."""
    from .api_previews import source_of  # noqa: PLC0415 — api_previews imports this module
    from . import view  # noqa: PLC0415 — as above

    user = the_person(request)
    src = source_of(request, source)
    since, until = _window(since, until, after)
    return view.would_read(user, wsvc.request_member_slugs(request), src, since=since, until=until, after=after,
                           limit=limit)
