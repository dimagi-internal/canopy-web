"""Site sign-off requests waiting on the people who may approve them (`sign_offs.may_approve`)."""
from __future__ import annotations

from apps.attention.types import ASKING, AttentionRow

from . import sign_offs


def items(request, *, slugs: set[str]) -> list[AttentionRow]:
    return [
        AttentionRow(
            key=f"sign_off:{r.id}",
            kind="sign_off",
            state=ASKING,
            reason="Approve or decline",
            title=f"{r.app.name} asks: {r.action_title}",
            body=(f"For {r.agent.name}, an agent you administer" if r.agent else f"For {r.subject}"),
            workspace=r.app.workspace_id,
            url=sign_offs.page_path(r),
            last_activity_at=r.created_at,
            actions=("open",),
            target={"sign_off": r.id},
            agent_slug=r.agent.slug if r.agent else "",
        )
        for r in sign_offs.open_for(request.user, workspaces=slugs)
    ]
