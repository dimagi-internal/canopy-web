"""`/api/agents/{slug}/state-document`: one JSON document an agent backs up here.

Who may do what is `agent_state.py`. Every outsider gets the same 404 as a missing
agent. The MCP call log records these tools' argument names, never the document."""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.agents.gates import agent_for_read
from apps.api.auth import session_auth
from apps.api.errors import TYPE_CONFLICT, TYPE_PAYLOAD_TOO_LARGE, ProblemError
from apps.api.route_gates import gate

from . import agent_state
from .schemas import AgentStateDocumentIn, AgentStateDocumentOut, AgentStateWriteOut

router = Router(auth=session_auth, tags=["agents"])

_NOT_IN_A_TURN = ("only this agent's own login, inside a running turn of this agent "
                  "that is not confined to a caller, may do this")


@router.get("/{slug}/state-document", response=AgentStateDocumentOut,
            summary="An agent's state document")
@gate("agent-login", "agent-admin")  # the login only inside a turn that answers to nobody, or to an admin
def get_agent_state_document(request: HttpRequest, slug: str) -> dict:
    """The document an agent backs up here, with its version (0 when it has none). For
    the agent's owner and admins, and the agent's own login inside a running turn nobody
    in particular asked for or an admin of it asked for."""
    agent = agent_for_read(request, slug)
    if not agent_state.may_read(request.user, agent):
        if agent.is_own_login(request.user):
            raise HttpError(403, f"{_NOT_IN_A_TURN}, and whose asker may read it")
        raise HttpError(404, f"agent '{slug}' not found")
    row = agent_state.read(agent)
    if row is None:
        return {"version": 0, "document": None, "updated_at": None}
    return {"version": row.version, "document": row.document, "updated_at": row.updated_at}


@router.put("/{slug}/state-document", response=AgentStateWriteOut,
            summary="Replace an agent's state document")
@gate("agent-login", kind="content")
def put_agent_state_document(request: HttpRequest, slug: str, payload: AgentStateDocumentIn) -> dict:
    """Replace the document, for the agent's own login inside a running turn of that
    agent. `expected_version` is the version the write is based on: 409 with
    `current_version` if another write came first, so nothing is overwritten unseen; 413
    above 1 MiB."""
    agent = agent_for_read(request, slug)
    turn = agent_state.writing_turn(request.user, agent)
    if turn is None:
        if agent.is_own_login(request.user) or agent.is_admin(request.user):
            raise HttpError(403, _NOT_IN_A_TURN)
        raise HttpError(404, f"agent '{slug}' not found")
    try:
        row = agent_state.write(agent, expected_version=payload.expected_version,
                                document=payload.document, turn=turn)
    except agent_state.VersionConflict as exc:
        raise ProblemError(409, "State document has moved on", type_=TYPE_CONFLICT,
                           detail=f"it is at version {exc.current}, not {payload.expected_version}",
                           members={"current_version": exc.current}) from None
    except agent_state.TooLarge as exc:
        raise ProblemError(413, "Payload too large", type_=TYPE_PAYLOAD_TOO_LARGE,
                           detail=f"the document is {exc.size} bytes; the limit is "
                                  f"{agent_state.MAX_BYTES}") from None
    return {"version": row.version, "updated_at": row.updated_at}
