"""The page a session is attached to: what it shows, what it can do."""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja.errors import HttpError

from apps.api.route_gates import gate
from apps.harness import sensitivity

from . import (
    page_actions,
    page_state,
    services,
)
from .schemas import (
    PageActionInvokeIn,
    PageActionOut,
    PageActionResultIn,
    PageActionsDeclareIn,
    PageActionSpec,
    PageStateIn,
    PageStateOut,
    RunAgentInputIn,
    RunAgentInputOut,
)
# The one session router and the helpers its routes share; api.py imports this
# module after defining them.
from .api import (
    _session_or_404,
    router,
)

#: Published operationIds keep this app's label (apps/api/api.py `_app_label`): an
#: operationId is an MCP tool name and a `generated.ts` key.
OPERATION_LABEL = "canopy_sessions"


def _page_in_it_or_403(request: HttpRequest, session) -> None:
    # A sensitive conversation's page is its people's screen: what it shows, what it
    # can do and what an action on it answers are read and driven behind the same
    # gate as its words (`sensitivity.reads_content`) — its own people, on its chat
    # surfaces. The agent running in it reaches the page through its chat key
    # (`apps/mcp/page_tools.py`, `apps/mcp/tools/page.py`), never through these routes.
    if not sensitivity.reads_content(session, user=request.user,
                                     chat_surface=sensitivity.request_is_chat_surface(request)):
        raise HttpError(403, sensitivity.NOT_IN_IT_PAGE)


# --- page actions -----------------------------------------------------------
#
# An embedded host declares what its page can do; the agent driving the session
# calls one. Deliberately NOT MCP: canopy's MCP tools are static Python
# functions scoped to a connection, while these are declared by a host at
# runtime, scoped to one session, and valid only while a tab is open. ACP
# already models this shape — a client advertises capabilities and the agent
# invokes them over the session — so canopy takes the shape rather than the
# wire format. See apps/canopy_sessions/page_actions.py.


@router.put("/{session_id}/page-actions", response=list[PageActionSpec],
            summary="Declare what the attached page can do")
@gate("session-acl", kind="conversation")  # write
def declare_page_actions(request: HttpRequest, session_id: uuid.UUID,
                         payload: PageActionsDeclareIn) -> list[PageActionSpec]:
    """Called by the page itself as it mounts, and whenever its actions change.

    Replaces the declaration wholesale — see `set_declared_actions` for why
    merging would leave the agent able to call into a page the user has left.
    """
    session = _session_or_404(request, session_id, write=True)
    _page_in_it_or_403(request, session)
    services.note_request_grant(session, request)
    page_actions.set_declared_actions(
        session, [a.dict() for a in payload.actions]
    )
    return [PageActionSpec(**a) for a in page_actions.declared_actions(session)]


@router.get("/{session_id}/page-actions", response=list[PageActionSpec],
            summary="What the attached page can do")
@gate("session-acl")
def list_page_actions(request: HttpRequest, session_id: uuid.UUID) -> list[PageActionSpec]:
    """How the agent discovers its options. An empty list means no page is
    attached — not that the page can do nothing."""
    session = _session_or_404(request, session_id)
    _page_in_it_or_403(request, session)
    return [PageActionSpec(**a) for a in page_actions.declared_actions(session)]


@router.put("/{session_id}/page-state", response=PageStateOut,
            summary="Declare what the attached page is showing")
@gate("session-acl", kind="conversation")  # write
def declare_page_state(request: HttpRequest, session_id: uuid.UUID,
                       payload: PageStateIn) -> PageStateOut:
    """Called by the page as it mounts and whenever its view changes.

    Replaces the declaration wholesale. A state larger than the server's cap is
    rejected with `too_large`: send the selection (ids, filters) and the tool
    that resolves it, not the rows themselves.
    """
    session = _session_or_404(request, session_id, write=True)
    _page_in_it_or_403(request, session)
    services.note_request_grant(session, request)
    try:
        stored = page_state.set_page_state(session, payload.state)
    except page_state.PageStateError as exc:
        # 422: well-formed request, unacceptable CONTENT — the page must change
        # what it sends, which is a different fix from retrying.
        raise HttpError(422, f"{exc.code}: {exc.message}")
    return PageStateOut(state=stored, version=int(stored.get("version") or 0))


@router.put("/{session_id}/run-input", response=RunAgentInputOut,
            summary="Declare the page in AG-UI's own shape")
@gate("session-acl", kind="conversation")  # write
def declare_run_input(request: HttpRequest, session_id: uuid.UUID,
                      payload: RunAgentInputIn) -> RunAgentInputOut:
    """Accepts AG-UI's `RunAgentInput` and applies the parts canopy honours.

    `state` becomes the page's declared view and `tools` become its callable
    actions — one call where canopy otherwise needs two. Fields canopy has no
    use for are accepted and ignored, so a conforming client can send the whole
    object unchanged.

    A `state` larger than the server's cap is rejected with `too_large`: send
    the selection (ids, filters) and the tool that resolves it, not the rows.
    """
    session = _session_or_404(request, session_id, write=True)
    _page_in_it_or_403(request, session)
    services.note_request_grant(session, request)

    # Actions first, then state — the same order the widget uses, and for the
    # same reason: whichever lands last, the agent must never see a page that
    # declares rows it has no way to act on.
    page_actions.set_declared_actions(session, [t.dict() for t in payload.tools])
    stored = page_state.current_page_state(session)
    if payload.state:
        try:
            stored = page_state.set_page_state(session, payload.state)
        except page_state.PageStateError as exc:
            raise HttpError(422, f"{exc.code}: {exc.message}")
    return RunAgentInputOut(
        state=stored,
        version=int(stored.get("version") or 0),
        tools=[PageActionSpec(**a) for a in page_actions.declared_actions(session)],
    )


@router.get("/{session_id}/page-state", response=PageStateOut,
            summary="What the attached page is showing")
@gate("session-acl")
def read_page_state(request: HttpRequest, session_id: uuid.UUID) -> PageStateOut:
    """How a surface other than the agent's MCP tool reads the current view."""
    session = _session_or_404(request, session_id)
    _page_in_it_or_403(request, session)
    stored = page_state.current_page_state(session)
    return PageStateOut(state=stored, version=int(stored.get("version") or 0))


@router.post("/{session_id}/page-actions/invoke", response=PageActionOut,
             summary="Ask the attached page to run an action")
@gate("session-acl", kind="conversation")  # write
def invoke_page_action(request: HttpRequest, session_id: uuid.UUID,
                       payload: PageActionInvokeIn) -> PageActionOut:
    """Blocks until the page answers, or refuses with a reason.

    Every non-success is an error with a `code` the caller can branch on
    (`no_page`, `unknown_action`, `bad_arguments`, `timeout`, `refused`) — a
    caller must never be able to read "the tab was closed" as "done".
    """
    session = _session_or_404(request, session_id, write=True)
    _page_in_it_or_403(request, session)
    try:
        action = page_actions.request_action(
            session=session, name=payload.name, args=payload.args, user=request.user
        )
    except page_actions.PageActionError as exc:
        # 409: the request was well-formed, the PAGE could not satisfy it.
        raise HttpError(422 if exc.code == "bad_arguments" else 409,
                        f"{exc.code}: {exc.message}")
    return PageActionOut(id=str(action.id), name=action.name, status=action.status,
                         result=action.result, error=action.error)


@router.post("/{session_id}/page-actions/{action_id}/result", response=PageActionOut,
             summary="The page reporting an action's outcome")
@gate("session-acl", kind="conversation")  # write
def resolve_page_action(request: HttpRequest, session_id: uuid.UUID, action_id: uuid.UUID,
                        payload: PageActionResultIn) -> PageActionOut:
    """Posted by the page after it runs the callback.

    Membership-gated like every other by-id read, and scoped to the session, so
    one page cannot resolve another's action.
    """
    session = _session_or_404(request, session_id, write=True)
    _page_in_it_or_403(request, session)
    action = session.page_actions.filter(pk=action_id).first()
    if action is None:
        raise HttpError(404, "no such page action on this session")
    action = page_actions.resolve(action, result=payload.result, error=payload.error)
    return PageActionOut(id=str(action.id), name=action.name, status=action.status,
                         result=action.result, error=action.error)
