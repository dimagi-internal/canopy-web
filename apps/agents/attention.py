"""Tasks waiting on a person — the board's own "Waiting on you" filter
(`services.filter_tasks(waiting="me")`), so the agent's board, the supervisor
and Home name the same tasks. Actions go to the existing task route,
`act_on_task`."""
from __future__ import annotations

from apps.attention.types import ASKING, YOUR_MOVE, AttentionRow
from apps.workspaces.links import scoped_path

from . import services
from .models import AgentTask

_REASON = {AgentTask.ASK_REVIEW: "Approve or decline", AgentTask.ASK_QUESTION: "Question for you"}
_ACTIONS = {AgentTask.ASK_REVIEW: ("approve", "decline", "reply"), AgentTask.ASK_QUESTION: ("reply",)}


def items(request, *, slugs: set[str]) -> list[AttentionRow]:
    qs = services.filter_tasks(
        AgentTask.objects.filter(workspace_id__in=slugs).select_related("agent", "project", "created_by_agent"),
        user=request.user, waiting="me",
    )
    out = []
    for t in qs[:500]:
        ws = t.workspace_id
        open_ask = t.ask_is_open
        out.append(AttentionRow(
            # Hides are stored by this key: a task with an agent alias keeps the
            # name it had before tasks were the workspace's, so no hide is lost.
            key=(f"task:{t.created_by_agent.slug}:{t.ext_id}" if t.ext_id and t.created_by_agent_id
                 else f"task:{t.key}"),
            kind="task",
            state=ASKING if open_ask else YOUR_MOVE,
            reason=_REASON.get(t.ask_kind, "Your move") if open_ask else "Your move",
            title=t.title,
            body=(t.ask_body if open_ask else t.next_action or "")[:400],
            workspace=ws,
            agent_slug=t.agent_slug or "",
            project=t.project.name if t.project_id else "",
            url=scoped_path(ws, f"/tasks/{t.key}"),
            last_activity_at=t.updated_at,
            actions=_ACTIONS.get(t.ask_kind, ("reply",)) if open_ask else ("reply",),
            target={"agent": t.agent_slug or "", "task": t.ref, "key": t.key},
        ))
    return out
