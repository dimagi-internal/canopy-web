"""`GET /api/brain/preview`: what the brain WOULD do over the sessions you can read, storing nothing.

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
from .schemas import BrainPreviewOut

router = Router(auth=session_auth, tags=["brain"])

#: The credentials a person previews with themselves: their browser session or a PAT they minted by hand. An act-as
#: token is the third, and only under a READ grant (`_the_person`).
PREVIEW_KINDS = frozenset({kinds.SESSION, kinds.PAT})
#: The longest window one preview spans.
MAX_WINDOW = dt.timedelta(days=7)


def _the_person(request: HttpRequest):
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


@router.get("/brain/preview", response={200: BrainPreviewOut, **refusals(403, 422)},
            summary="What learning would do over your sessions (stores nothing)")
@gate("session-acl")  # reads only sessions you already read, through legs 1 to 6; writes nothing
def brain_preview(request: HttpRequest, since: dt.datetime | None = None, until: dt.datetime | None = None,
                  view: Literal["full", "conversation", "typed", "opening"] = "full", after: str = "",
                  limit: int = 10) -> dict:
    """For each session you can read that was created or active between `since` and `until` (the last day by
    default, a week at most): who is in it, whether each class of learning could read it now and if everyone in it
    agreed, with the reason, the project links canopy would make, and the session as a learner reads it, secrets
    scrubbed. Nothing is stored or recorded. Pass `next_after` back as `after` for the next page."""
    user = _the_person(request)
    until = until or timezone.now()
    since = since or until - dt.timedelta(days=1)
    if since >= until or until - since > MAX_WINDOW:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION,
                           detail="since must be before until, and the window a week at most")
    if after:
        try:
            uuid.UUID(after)
        except ValueError as e:
            raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail="after is a session id") from e
    return preview_mod.preview(user, wsvc.request_member_slugs(request), since=since, until=until, view=view,
                               after=after, limit=limit)
