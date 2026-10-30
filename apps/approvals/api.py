"""Django Ninja router for /api/approvals — the cards waiting on a person, and deciding them.

A card IS a board task (`agents.AgentTask`); this is the decision-shaped view of it: everything
a person needs to say yes or no, and the one door for saying it. Deciding calls the board's own
`act`, so the web page, a Slack button and an MCP client all do exactly what Approve does on the
board — start the turns `on_approve` names, once.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.agents.gates import task_or_404, visible_agent_workspaces
from apps.agents.models import AgentTask
from apps.api.auth import session_auth
from apps.api.route_gates import gate

from . import cards, deciders
from .decide import ApprovalError, decide
from .models import ApprovalDelivery
from .schemas import ApprovalCardOut, ApprovalDecideIn

router = Router(auth=session_auth, tags=["approvals"])

LIST_MAX = 100


def _card(task: AgentTask, viewer) -> dict:
    sent = list(ApprovalDelivery.objects.filter(task=task).select_related("user")
                .values_list("user__first_name", "user__last_name", "user__email").distinct())
    names = sorted({(f"{f} {l}".strip() or e) for f, l, e in sent})
    return cards.build(task, viewer=viewer, sent_to=names)


@router.get("/", response=list[ApprovalCardOut], summary="The cards waiting on you")
# lists only tasks in the caller's own workspaces, and only those they are a decider of
@gate("member")
def my_cards(request: HttpRequest) -> list[dict]:
    """Proposals waiting on a yes or no from YOU: the people a card is sent to
    (`deciders.for_task`) see it here. Anyone else in the workspace can still open a card by
    its key and decide it, as on the board."""
    slugs = visible_agent_workspaces(request)
    rows = (AgentTask.objects.filter(workspace_id__in=slugs)
            .filter(status=AgentTask.SUGGESTED)
            .select_related("agent", "created_by_agent", "created_by_user", "project", "workspace")
            .order_by("-updated_at")[:500])
    out = []
    for task in rows:
        if cards.needs_decision(task) and request.user in deciders.for_task(task):
            out.append(_card(task, request.user))
        if len(out) >= LIST_MAX:
            break
    return out


@router.get("/{key}", response=ApprovalCardOut, summary="One card, with everything needed to decide it")
@gate("member")
def get_card(request: HttpRequest, key: str) -> dict:
    """The card for task `key`: the ask, the person's words verbatim, the agent's reason, the exact
    plan, what approving starts and what declining does, who it was sent to and — once decided —
    who decided it, how, and what they said. 404 for a task in a workspace you are not in."""
    return _card(task_or_404(request, key.strip().upper()), request.user)


@router.post("/{key}/decide", response=ApprovalCardOut, summary="Approve or decline a card")
# deciding what is already on the board is the interaction tier: a viewer may, as on act_on_task
@gate("member", viewer_write="Approving or declining a proposal already on the board is the interaction "
      "tier — the same decision act_on_task lets a viewer make", kind="content")
def decide_card(request: HttpRequest, key: str, payload: ApprovalDecideIn) -> dict:
    """Approve (starts the turns the card names, once) or decline (nothing runs). 409 with who got
    there first when it is already decided — never a second run. 403 when the board itself
    would refuse you (a project decision card is the project owner's)."""
    task = task_or_404(request, key.strip().upper())
    try:
        task = decide(task, request.user, payload.decision, comment=payload.comment, via="web")
    except ApprovalError as exc:
        raise HttpError(exc.status, str(exc)) from exc
    return _card(AgentTask.objects.get(pk=task.pk), request.user)
