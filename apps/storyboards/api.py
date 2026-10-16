"""Django Ninja router for /api/storyboards.

The public read follows ``apps/runs/api.py::get_run_release`` exactly: ``auth=None``
on the route, the handler self-enforces (workspace member OR a matching
``?t=<share_token>``), and the login middleware allowlists the path. A missing or
wrong token **404s, never 403s** — a 403 would confirm the board exists, which is
the existence leak walkthroughs already avoid.

The anonymous FEEDBACK route lives here, not in ``apps/feedback``, and that is
deliberate. ``feedback`` is framework tier; ``storyboards`` is product. Having the
framework app resolve a storyboard token would invert the one-way rule. The
storyboard owns the token, so it owns the route that accepts a token-bearing
write — and calls ``feedback.services.ingest``, which is exactly why that service
layer is request-free.
"""
from __future__ import annotations

import uuid

from django.db import transaction
from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.common.csrf import csrf_rejected
from apps.feedback import reactions
from apps.feedback import services as feedback_services
from apps.harness import artifact_origin
from apps.storyboards import decisions as decision_rows
from apps.storyboards import rounds as board_rounds
from apps.storyboards import services
from apps.storyboards.act_keys import act_key
from apps.storyboards.models import Act, Entry, Storyboard
from apps.storyboards.schemas import (
    AnonFeedbackIn,
    DecisionEditIn,
    DecisionOut,
    NarrativeReadOut,
    NotesOut,
    RoundConsumeIn,
    RoundDetailOut,
    RoundCloseIn,
    RoundOut,
    ShareTokenOut,
    StoryboardIn,
    StoryboardListOut,
    StoryboardOut,
    StoryboardPatchIn,
)
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

router = Router(tags=["storyboards"])


# ---------------------------------------------------------------- access rules


def _member_boards(request: HttpRequest):
    """Boards in workspaces the caller belongs to. Empty for anonymous."""
    if not request.user.is_authenticated:
        return Storyboard.objects.none()
    return Storyboard.objects.filter(workspace_id__in=wsvc.user_workspace_slugs(request.user))


def _listed_boards(request: HttpRequest):
    """Boards a LIST may show: the read rule (`wsvc.request_workspace_slugs`) —
    pinned to one workspace, or every membership when they sit in one tenant
    tree; a caller in two trees who names none gets a 422. The by-slug gates
    above keep `_member_boards`: opening one board you belong to mixes nothing."""
    if not request.user.is_authenticated:
        return Storyboard.objects.none()
    return Storyboard.objects.filter(workspace_id__in=wsvc.request_workspace_slugs(request))


def _is_member(request: HttpRequest, board: Storyboard) -> bool:
    """One of the people who SENT the link, rather than one it was sent to."""
    return bool(
        request.user.is_authenticated
        and board.workspace_id in wsvc.user_workspace_slugs(request.user)
    )


def _readable_or_404(request: HttpRequest, slug: str, ws: str = "") -> Storyboard:
    """A board the caller may READ, or 404.

    404 rather than 403 on a bad token is the point: the response for "no such
    board" and "wrong token" must be indistinguishable.

    `ws` is the workspace the page's URL names (`/w/<ws>/storyboard/<slug>`): a
    board in another workspace is not at that address, so it 404s — and two
    workspaces may each have a board with this slug, so `ws` also picks which.
    """
    boards = Storyboard.objects.filter(slug=slug)
    if ws:
        boards = boards.filter(workspace_id=ws)
    board = boards.first()
    if board is None:
        raise HttpError(404, "storyboard not found")

    if request.user.is_authenticated:
        if board.workspace_id in wsvc.user_workspace_slugs(request.user):
            return board

    if board.token_matches(request.GET.get("t")):
        return board

    raise HttpError(404, "storyboard not found")


def _owned_or_404(request: HttpRequest, slug: str) -> Storyboard:
    board = _member_boards(request).filter(slug=slug).first()
    if board is None:
        raise HttpError(404, "storyboard not found")
    return board


def _editable_or_404(request: HttpRequest, slug: str) -> Storyboard:
    """A board the caller may CHANGE: editing its arc, minting or re-minting its
    share link. Membership first (404 to a non-member, so the role check never
    confirms a hidden board exists), then the author tier — a viewer reads the
    board and its notes but does not reshape what was sent, or kill the link
    everyone else was sent."""
    board = _owned_or_404(request, slug)
    if not perms.can(request.user, board.workspace_id, perms.CONTENT_WRITE):
        raise HttpError(403, "changing a storyboard requires the editor role in its workspace")
    return board


def _share_url(request: HttpRequest, board: Storyboard) -> str | None:
    """The absolute, token-bearing link — the thing you actually send someone.

    Built on CANOPY_PUBLIC_BASE_URL (which carries any deployment prefix), not
    `request.build_absolute_uri`: an MCP tool call reaches this route in-process
    with no real Host, and that minted `https://localhost/…` (canopy-web#1289).

    The page lives under the board's workspace like every other artifact
    (canopy-web#1337); the flat `/storyboard/<slug>` is a 404.
    """
    if not board.share_token:
        return None
    return wsvc.scoped_url(board.workspace_id, f"/storyboard/{board.slug}?t={board.share_token}")


# ------------------------------------------------------------------- write ops


def _replace_acts(board: Storyboard, acts) -> None:
    """Wholesale replace — reordering is a rewrite, not a diff.

    Same call the import command makes, so authoring from a repo and editing in
    the UI cannot drift apart.
    """
    board.acts.all().delete()
    taken: set[str] = set()
    for a_pos, act_in in enumerate(acts):
        act = Act.objects.create(
            storyboard=board,
            key=act_key(act_in.key, act_in.title, a_pos, taken),
            title=act_in.title,
            prose=act_in.prose,
            position=a_pos,
        )
        for e_pos, entry_in in enumerate(act_in.entries):
            Entry.objects.create(
                act=act,
                narrative_slug=entry_in.narrative_slug,
                pinned_run_id=entry_in.pinned_run_id,
                title=entry_in.title,
                blurb=entry_in.blurb,
                position=e_pos,
            )


def _write_decisions(board: Storyboard, payload) -> None:
    """The brief, minus its legacy lists, plus the decision set.

    The three #1501 lists are still accepted for one release and converted to
    rows, so a publisher that has not moved yet keeps working (spec B)."""
    rows = [d.model_dump() for d in payload.decisions] if payload.decisions is not None else None
    if payload.brief is not None:
        brief_in = payload.brief.model_dump(exclude_defaults=True)
        legacy = decision_rows.from_legacy_brief(brief_in)
        if legacy and rows is None:
            # An old publisher sends its whole list each time, so the lists ARE
            # its decision set: an unruled row it no longer sends is dropped
            # (else a reworded item piles up as a new row). A row a person ruled
            # on is kept as it stands, winning over its re-sent text — the old
            # publisher cannot supersede, and must never be 409'd for re-sending.
            by_id = {r["id"]: r for r in legacy}
            for d in board.decisions.all():
                if decision_rows.is_ruled(d):
                    by_id[d.key] = decision_rows.publishable(d)
            rows = list(by_id.values())
        elif legacy:
            # Both shapes in one write: the converted row wins (publish still
            # 409s on a ruled row whose value it would change).
            rows = list(({r["id"]: r for r in rows} | {r["id"]: r for r in legacy}).values())
        board.brief = {k: v for k, v in brief_in.items() if k not in decision_rows.LEGACY_BRIEF_KEYS}
        board.save(update_fields=["brief", "updated_at"])
    if rows is not None:
        decision_rows.publish(board, rows)


# --------------------------------------------------------------------- routes


def _signals(boards) -> dict[int, dict]:
    """Origin + reaction fields for a batch of boards (board task hal/T76).

    Comments match what the board's notes panel shows: feedback on the board
    and on the narratives it carries. Views are recorded per board id (a slug is
    only unique per workspace). A board has no owner column, so its creator is
    the agent whose turn made it, when one did."""
    targets = []
    for b in boards:
        slugs = {e.narrative_slug for a in b.acts.all() for e in a.entries.all()}
        turn = b.source_turn
        creator = turn.agent.user_id if turn is not None and turn.agent_id else None
        targets.append(reactions.Target(
            key=str(b.pk),
            feedback=[(reactions.STORYBOARD, b.slug, None)]
            + [(reactions.NARRATIVE, s, None) for s in sorted(slugs)],
            views=[(reactions.STORYBOARD, str(b.pk))],
            creator_id=creator,
        ))
    summary = reactions.summarize(targets)
    return {
        b.pk: {
            "session_id": b.source_session_id,
            "turn_id": b.source_turn_id,
            "agent_project": artifact_origin.project_out(b.agent_project),
            **summary[str(b.pk)],
        }
        for b in boards
    }


@router.get("/", response=StoryboardListOut, auth=session_auth, summary="List storyboards")
def list_storyboards(
    request: HttpRequest, session: str = "", turn: str = "", agent_project: str = ""
) -> dict:
    """`session` / `turn` = the canopy session / turn a board was made from;
    `agent_project` = its board project (`hal/P5`, `hal:P5` or an id). T76."""
    boards = _listed_boards(request).select_related(
        "agent_project__agent", "source_turn__agent"
    ).prefetch_related("acts__entries")
    for field, value in (("source_session_id", session), ("source_turn_id", turn)):
        if value:
            try:
                boards = boards.filter(**{field: uuid.UUID(value.strip())})
            except ValueError:
                boards = boards.none()
    project_filter = artifact_origin.parse_project_filter(agent_project)
    if project_filter:
        boards = boards.filter(**project_filter)
    boards = list(boards)
    signals = _signals(boards)
    return {
        "items": [
            {
                **signals[b.pk],
                "slug": b.slug,
                "title": b.title,
                "lede": b.lede,
                "capability": b.capability,
                "layout": b.layout,
                "act_count": len(b.acts.all()),
                "share_url": _share_url(request, b),
            }
            for b in boards
        ]
    }


@router.post("/", response=StoryboardOut, auth=session_auth, summary="Create a storyboard")
def create_storyboard(request: HttpRequest, payload: StoryboardIn) -> dict:
    ws = wsvc.creation_workspace(request)
    if ws is None:
        raise HttpError(422, f"no workspace to create this storyboard in: {wsvc.creation_refusal(request)}")
    workspace_slug = ws.slug
    if not perms.can(request.user, workspace_slug, perms.CONTENT_WRITE):
        raise HttpError(403, "creating a storyboard requires the editor role in this workspace")
    origin = artifact_origin.resolve(
        request, session=payload.session_id or "", turn=payload.turn_id or "",
        agent_project=payload.agent_project or "",
    )
    try:
        with transaction.atomic():
            board = Storyboard.objects.create(
                **origin,
                slug=payload.slug,
                title=payload.title,
                lede=payload.lede,
                brief={},
                capability=payload.capability,
                layout=payload.layout,
                workspace_id=workspace_slug,
            )
            _replace_acts(board, payload.acts)
            _write_decisions(board, payload)
    except decision_rows.DecisionInvalid as e:
        raise HttpError(422, str(e))
    except decision_rows.DecisionConflict as e:
        raise HttpError(409, str(e))
    return services.resolve_board(board, is_member=True, viewer=request.user)


@router.get(
    "/{slug}",
    response=StoryboardOut,
    auth=None,
    summary="Read a storyboard (public via ?t=<share_token>)",
)
def get_storyboard(request: HttpRequest, slug: str, ws: str = "") -> dict:
    """Anonymous-capable; the handler self-enforces. See the module docstring.
    `ws` is the workspace the page's URL names; a board elsewhere 404s."""
    board = _readable_or_404(request, slug, ws)
    reactions.record_view(request, reactions.STORYBOARD, board.pk)
    return services.resolve_board(
        board, is_member=_is_member(request, board), viewer=request.user
    )


@router.patch("/{slug}", response=StoryboardOut, auth=session_auth, summary="Edit a storyboard")
def patch_storyboard(request: HttpRequest, slug: str, payload: StoryboardPatchIn) -> dict:
    board = _editable_or_404(request, slug)
    try:
        with transaction.atomic():
            fields = []
            for name in ("title", "lede", "capability", "layout"):
                value = getattr(payload, name)
                if value is not None:
                    setattr(board, name, value)
                    fields.append(name)
            if fields:
                board.save(update_fields=[*fields, "updated_at"])
            if payload.acts is not None:
                _replace_acts(board, payload.acts)
            _write_decisions(board, payload)
    except decision_rows.DecisionInvalid as e:
        raise HttpError(422, str(e))
    except decision_rows.DecisionConflict as e:
        raise HttpError(409, str(e))
    return services.resolve_board(board, is_member=True, viewer=request.user)


@router.post(
    "/{slug}/rotate-token",
    response=ShareTokenOut,
    auth=session_auth,
    summary="Re-mint the share link, killing every link already sent",
)
def rotate_token(request: HttpRequest, slug: str) -> dict:
    board = _editable_or_404(request, slug)
    board.rotate_share_token()
    return {"share_url": _share_url(request, board)}


@router.post(
    "/{slug}/share",
    response=ShareTokenOut,
    auth=session_auth,
    summary="Mint the share link if it does not exist yet",
)
def ensure_token(request: HttpRequest, slug: str) -> dict:
    board = _editable_or_404(request, slug)
    board.ensure_share_token()
    return {"share_url": _share_url(request, board)}


@router.post(
    "/{slug}/feedback",
    response={200: dict},
    auth=None,
    summary="Leave feedback on a storyboard (public via ?t=<share_token>)",
)
def leave_feedback(request: HttpRequest, slug: str, payload: AnonFeedbackIn, ws: str = "") -> dict:
    """The anonymous write L2 deferred, gated by the board's capability.

    A ``suggestion`` needs the ``suggest`` grant; a ``comment`` needs
    ``comment``. A read-only link can do neither. The caller cannot choose its
    own channel or target kind — the server fills those in, so an outsider
    cannot file feedback against something this board does not contain.
    """
    board = _readable_or_404(request, slug, ws)
    token = request.GET.get("t")

    needed = Storyboard.CAP_SUGGEST if payload.kind == "suggestion" else Storyboard.CAP_COMMENT
    is_member = request.user.is_authenticated and board.workspace_id in wsvc.user_workspace_slugs(
        request.user
    )
    if not is_member and not board.grants(needed, token):
        raise HttpError(403, f"this link does not grant {needed}")

    if payload.narrative_slug:
        known = Entry.objects.filter(
            act__storyboard=board, narrative_slug=payload.narrative_slug
        ).exists()
        if not known:
            raise HttpError(404, "no such narrative on this storyboard")

    if not payload.body.strip() and not payload.suggested_text.strip():
        raise HttpError(422, "a note needs some words")

    rnd = board_rounds.current(board)
    try:
        board_rounds.check_anchor(board, payload.anchor_id)
        author = board_rounds.author_for(
            request.user, payload.author_name, member=is_member, in_round=rnd is not None
        )
    except board_rounds.RoundError as e:
        raise HttpError(e.status, e.detail)

    item = {
        "target_kind": "narrative" if payload.narrative_slug else "storyboard",
        "target_ref": payload.narrative_slug or board.slug,
        "target_version": payload.target_version,
        "anchor_id": payload.anchor_id,
        "kind": payload.kind,
        "body": payload.body,
        "suggested_text": payload.suggested_text,
        "author_name": author,
        "author_email": payload.author_email,
        "channel": "web",
        "source_ref": "",
    }
    if is_member:
        item["author_email"] = request.user.email or ""
    result = feedback_services.ingest(
        [item],
        workspace=board.workspace,
        submitted_by=request.user if request.user.is_authenticated else None,
        round=rnd,
    )
    if is_member and rnd is not None:
        board_rounds.link_participant(board, request.user)
    return result


@router.post(
    "/{slug}/rounds",
    response=RoundOut,
    auth=session_auth,
    summary="Open a feedback round on a storyboard",
)
def open_storyboard_round(request: HttpRequest, slug: str) -> dict:
    """Editors only. Pins every narrative's current version and adopts the notes
    left since the last round. Needs the board linked to a project."""
    board = _editable_or_404(request, slug)
    try:
        rnd = board_rounds.open_round(board, by_user=request.user)
    except board_rounds.RoundError as e:
        raise HttpError(e.status, e.detail)
    return board_rounds.summary(rnd)


@router.post(
    "/{slug}/rounds/{number}/close",
    response=RoundOut,
    auth=session_auth,
    summary="Close a feedback round and hand it to the project's agent",
)
def close_storyboard_round(request: HttpRequest, slug: str, number: int, payload: RoundCloseIn) -> dict:
    """Editors only. Applies staged decision edits, then creates one task on the
    board's project and approves it — exactly one agent turn for the whole round.
    A second close (another tab) gets 409; disagreements nobody picked get 409."""
    board = _editable_or_404(request, slug)
    rnd = board_rounds.by_number(board, number)
    if rnd is None:
        raise HttpError(404, "no such round")
    try:
        rnd = board_rounds.close_round(board, rnd, by_user=request.user,
                                       note=payload.close_note, picks=payload.picks)
    except board_rounds.RoundError as e:
        raise HttpError(e.status, e.detail)
    return board_rounds.summary(rnd)


@router.get(
    "/{slug}/rounds/{number}",
    response=RoundDetailOut,
    auth=session_auth,
    summary="Read a feedback round in full (members)",
)
def get_storyboard_round(request: HttpRequest, slug: str, number: int) -> dict:
    """The agent's read of a round. Members only; a token holder 404s."""
    board = _owned_or_404(request, slug)
    rnd = board_rounds.by_number(board, number)
    if rnd is None:
        raise HttpError(404, "no such round")
    return board_rounds.detail(board, rnd, viewer=request.user)


@router.post(
    "/{slug}/rounds/{number}/consume",
    response=RoundOut,
    auth=session_auth,
    summary="Mark a closed round consumed and open the next one",
)
def consume_storyboard_round(request: HttpRequest, slug: str, number: int, payload: RoundConsumeIn) -> dict:
    """Editors only (the agent's PAT). Refuses while any note is unanswered;
    opens round N+1 on each narrative's current version."""
    board = _editable_or_404(request, slug)
    rnd = board_rounds.by_number(board, number)
    if rnd is None:
        raise HttpError(404, "no such round")
    try:
        nxt = board_rounds.consume_round(board, rnd, run_id=payload.run_id, by_user=request.user)
    except board_rounds.RoundError as e:
        raise HttpError(e.status, e.detail)
    return board_rounds.summary(nxt)


@router.post(
    "/{slug}/decisions/{key}/edit",
    response=DecisionOut,
    auth=None,
    summary="Confirm, change, write in or undo one decision (public via ?t=<share_token>)",
)
def edit_storyboard_decision(
    request: HttpRequest, slug: str, key: str, payload: DecisionEditIn, ws: str = ""
) -> dict:
    # Self-enforcing like the read: a wrong token 404s. Inside an open feedback
    # round every edit is staged (members too); outside one a member's edit
    # applies immediately and a link holder's is refused.
    board = _readable_or_404(request, slug, ws)
    row = board.decisions.filter(key=key).first()
    if row is None:
        raise HttpError(404, "no such decision on this storyboard")
    member = _is_member(request, board)
    token = request.GET.get("t")
    # auth=None skips Ninja's cookie-auth CSRF check, so a session caller re-runs it.
    if request.user.is_authenticated and csrf_rejected(request):
        raise HttpError(403, "CSRF verification failed")
    rnd = board_rounds.current(board)
    try:
        if rnd is not None:
            if not member and not board.grants(Storyboard.CAP_SUGGEST, token):
                raise HttpError(403, "this link does not grant changing decisions")
            name = board_rounds.author_for(request.user, payload.author_name,
                                           member=member, in_round=True)
            decision_rows.check_edit(row, action=payload.action, value=payload.value)
            board_rounds.stage(rnd, row, action=payload.action, value=payload.value,
                               reasoning=payload.reasoning, by_name=name,
                               by_user=request.user if member else None)
            if member:
                board_rounds.link_participant(board, request.user)
            return decision_rows.serialize(row, pending=board_rounds.pending_for(board, row.key))
        if not member:
            raise HttpError(409, "No feedback round is open, so only a member can change a "
                                 "decision right now.")
        row = decision_rows.apply_edit(
            row, action=payload.action, value=payload.value,
            reasoning=payload.reasoning, by_user=request.user,
        )
    except board_rounds.RoundError as e:
        raise HttpError(e.status, e.detail)
    except decision_rows.DecisionInvalid as e:
        raise HttpError(422, str(e))
    except decision_rows.DecisionConflict as e:
        raise HttpError(409, str(e))
    return decision_rows.serialize(row)


@router.get(
    "/{slug}/notes",
    response=NotesOut,
    auth=session_auth,
    summary="Read the notes left on this board (members only)",
)
def list_notes(request: HttpRequest, slug: str) -> dict:
    """What came back. Members only, and 404 — never 403 — for everyone else.

    Without this the loop had no closing half: a reviewer could leave notes and
    the people who sent them the link had no way to read them short of curl.

    Outside a round, a token holder must not reach it. (Inside one,
    ``GET /{slug}`` shows the round's notes to everyone by design — spec C.)
    Seeing the other reviewers' notes would
    bias exactly the independent read you asked for, and the rows carry names
    that were given to us rather than to each other — so this uses
    ``_owned_or_404`` (membership), not ``_readable_or_404`` (membership OR
    token).
    """
    board = _owned_or_404(request, slug)
    return {
        "items": [
            {
                "id": fb.pk,
                "kind": fb.kind,
                "body": fb.body,
                "suggested_text": fb.suggested_text,
                "author_name": fb.author_name,
                "channel": fb.channel,
                "state": fb.state,
                "target_kind": fb.target_kind,
                "target_ref": fb.target_ref,
                "target_version": fb.target_version,
                "anchor_id": fb.anchor_id,
                "created_at": fb.created_at.isoformat(),
            }
            for fb in services.board_feedback(board)
        ]
    }


@router.get(
    "/{slug}/narratives/{narrative_slug}",
    response=NarrativeReadOut,
    auth=None,
    summary="Read one narrative on this storyboard (public via ?t=<share_token>)",
)
def get_board_narrative(request: HttpRequest, slug: str, narrative_slug: str, ws: str = "") -> dict:
    """The reviewer surface's read. Gated by the SAME token as the board.

    404s when the narrative is not on this board — a link to one arc must not be
    a read handle for every narrative in the workspace.
    """
    board = _readable_or_404(request, slug, ws)
    data = services.resolve_narrative(board, narrative_slug)
    if data is None:
        raise HttpError(404, "no such narrative on this storyboard")
    reactions.record_view(request, reactions.NARRATIVE, narrative_slug)
    return data
