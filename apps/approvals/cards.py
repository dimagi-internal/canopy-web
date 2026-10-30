"""The card: everything a person needs to decide, built from the task, in one place.

One renderer, so the page, the Slack message, the push, the email and the API all say the
same thing. Nothing here is invented: every field is the task's own — the person's words
verbatim (`human_why`), the agent's reason, the exact plan, and what approving starts.
"""
from __future__ import annotations

import re

from django.utils import timezone

from apps.agents.models import AgentTask, AgentTaskAction
from apps.workspaces import permissions as perms
from apps.workspaces.links import scoped_path

OPEN, APPROVED, DECLINED, CLOSED = "open", "approved", "declined", "closed"
_BULLET = re.compile(r"^\s*(?:[-*•]+|\d+[.)])\s*")
IF_DECLINED = "Nothing runs. The proposal is closed, and the agent is told why if you say."


def needs_decision(task: AgentTask) -> bool:
    """A proposal whose approval starts work, still waiting on a person.

    Deliberately NOT every open ask: a plain question or review on the board does not push (owner
    decision, 2026-10-05 — those were noise, `push.services`). A card is the narrower thing an agent
    files when a person's yes is what lets it act: a suggested task carrying `on_approve`. A
    snoozed one is out of sight until its date."""
    if task.status != AgentTask.SUGGESTED or not task.on_approve:
        return False
    return task.snoozed_until is None or task.snoozed_until <= timezone.now()


def url(task: AgentTask) -> str:
    return scoped_path(task.workspace_id, f"/approvals/{task.key}")


def _lines(text: str, limit: int = 12) -> list[str]:
    return [_BULLET.sub("", ln).strip() for ln in (text or "").splitlines() if ln.strip()][:limit]


def _who(user) -> str:
    return (user.get_full_name() or "").strip() or user.email or user.get_username()


def _will_run(task: AgentTask) -> list[dict]:
    out = []
    for spec in task.on_approve or []:
        agent = spec.get("target_agent") or task.agent_slug or ""
        prompt = str(spec.get("prompt") or "").strip()
        if agent or prompt:
            out.append({"agent": agent, "prompt": prompt[:600]})
    if not out and task.agent_id is not None:
        out.append({"agent": task.agent_slug or "", "prompt": task.next_action or task.title})
    return out


def _decided(task: AgentTask) -> dict | None:
    last = task.approval_decisions.select_related("user").first()
    if last is not None:
        return {"decision": last.decision, "by": _who(last.user) if last.user_id else "someone",
                "via": last.via, "at": last.created_at, "comment": last.comment}
    row = (task.actions.filter(action__in=[AgentTaskAction.APPROVE, AgentTaskAction.DECLINE,
                                           AgentTaskAction.ACCEPT, AgentTaskAction.BACKLOG])
           .select_related("by_user").order_by("-created_at").first())
    if row is None:
        return None
    return {"decision": row.action if row.action in ("approve", "decline") else "other",
            "by": (_who(row.by_user) if row.by_user_id else row.by) or "someone", "via": "app",
            "at": row.created_at, "comment": row.comment}


def state_of(task: AgentTask, decided: dict | None) -> str:
    if needs_decision(task):
        return OPEN
    if decided and decided["decision"] == "approve":
        return APPROVED
    if decided and decided["decision"] == "decline":
        return DECLINED
    return CLOSED


def build(task: AgentTask, *, viewer=None, sent_to=None) -> dict:
    """The card as a dict matching `ApprovalCardOut`. `viewer` fills `can_decide`."""
    decided = _decided(task)
    state = state_of(task, decided)
    open_ask = task.ask_kind == AgentTask.ASK_REVIEW and task.ask_is_open
    summary = (task.ask_body if open_ask and task.ask_body else task.next_action) or ""
    filer = (task.created_by_agent.name or task.created_by_agent.slug if task.created_by_agent_id
             else _who(task.created_by_user) if task.created_by_user_id else "")
    links = [{"label": str(l.get("label") or l.get("url")), "url": str(l["url"])}
             for l in (task.links or []) if isinstance(l, dict) and l.get("url")]
    return {
        "key": task.key, "title": task.title, "state": state, "workspace": task.workspace_id,
        "url": url(task),
        "agent_slug": task.agent_slug or "", "agent_name": (task.agent.name if task.agent_id else "") or "",
        "filed_by": filer, "summary": summary,
        "evidence": [{"quote": w["quote"],
                      "who": (w["who"].get("name") or w["who"].get("email") or "someone"),
                      "source_url": w.get("source_url") or "", "said_at": w.get("said_at")}
                     for w in task.human_why],
        "rationale": task.rationale or "",
        "plan": _lines(task.plan),
        "will_run": _will_run(task),
        "if_declined": IF_DECLINED,
        "links": links, "source_url": task.source_url or "",
        "priority": task.priority, "confidence": task.confidence or "",
        "created_at": task.created_at,
        "sent_to": sent_to if sent_to is not None else [],
        "decided": decided if state != OPEN else None,
        "can_decide": bool(state == OPEN and viewer is not None
                           and perms.can(viewer, task.workspace_id, perms.READ)),
    }


def outcome_line(card: dict) -> str:
    """One plain sentence for a copy that has been decided: who, what, through which door."""
    d = card.get("decided")
    if not d:
        return "This card is closed."
    verb = {"approve": "Approved", "decline": "Declined"}.get(d["decision"], "Closed")
    via = {"slack": "Slack", "web": "the web", "app": "the board", "email": "email",
           "push": "a notification", "api": "the API"}.get(d["via"], d["via"])
    return f"{verb} by {d['by']} via {via}."
