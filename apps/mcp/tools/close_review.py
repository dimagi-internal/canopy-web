"""/ide close review — two tools and the View they share.

Spec: docs/superpowers/specs/2026-10-16-ide-close-review-design.md. Hand-written
rather than routes because a tool that carries an MCP Apps View has no REST
shape; the rule itself is `apps.canopy_sessions.close_review`.

The agent reads the candidates from /ide itself (`page_reviewCloseCandidates`,
a page action the person's browser answers as them), then:

* `propose_session_closes` (model + app) — the agent's proposal. Read-only: it
  returns canopy's own rows for the sessions named and shows them in the panel
  as the review View.
* `close_proposed_sessions` (app only) — the View's Close button, as the person
  who clicked. The model is never offered it (`site_tools` hides app-only tools,
  `site_call` refuses them). Visibility is not a security boundary (MCP Apps
  §2), and an agent's own login lists every canopy tool, so it also refuses any
  credential but a person's host grant — the only one a View call carries —
  and applies the same per-session write check as `POST /canopy-sessions/{id}/close`.
"""
from __future__ import annotations

from pathlib import Path

from asgiref.sync import sync_to_async
from fastmcp.exceptions import ToolError

from apps.canopy_sessions import close_review
from apps.mcp.audit import current_user_id, write_audit
from apps.mcp.rate_limit import RateLimitError, check_write_limit
from apps.mcp.server import mcp

VIEW_URI = "ui://canopy/ide/close-review"
VIEW_MIME = "text/html;profile=mcp-app"
_VIEW_HTML = Path(__file__).with_name("close_review_view.html")

#: Every `ui://` resource a delegated (self-host) token may read, keyed to the
#: tool that shows it: the token may read the View only while that tool is
#: within its grant (`apps.mcp.delegation`).
VIEW_TOOLS: dict[str, str] = {VIEW_URI: "propose_session_closes"}

#: Each tool's `_meta`, declared once: the decorators below use it, and
#: `self_host._index_views` records it in the canopy-web site's UI index.
VIEW_TOOL_META: dict[str, dict] = {
    "propose_session_closes": {"ui": {"resourceUri": VIEW_URI, "visibility": ["model", "app"]}},
    "close_proposed_sessions": {"ui": {"resourceUri": VIEW_URI, "visibility": ["app"]}},
}


async def _user():
    from django.contrib.auth import get_user_model

    user_id = current_user_id()
    if user_id is None:
        raise ToolError("sign in to canopy to review sessions")
    user = await sync_to_async(get_user_model().objects.filter(pk=user_id).first,
                               thread_sensitive=True)()
    if user is None:
        raise ToolError("sign in to canopy to review sessions")
    return user


@mcp.tool(annotations={"readOnlyHint": True}, meta=VIEW_TOOL_META["propose_session_closes"])
async def propose_session_closes(proposals: list[dict]) -> dict:
    """Propose sessions to close; the person reviews and confirms in the panel.

    `proposals` is `[{session_id, quote, why}]`: `quote` is the line from the
    session's last reply that says the work is done, `why` one sentence of your
    own. Propose only sessions whose last reply claims it is finished.

    This closes nothing. It shows the person a review of these sessions — the
    same facts /ide's rail shows, re-read as them when they look, beside your
    quote — with a Close button they press themselves. Sessions that cannot be
    closed now (running, waiting on someone, offline runner) are listed with the
    reason. `rows` holds only the sessions YOU can read; on your own login that
    is usually none, which is expected — the person's review reads them all.
    """
    user = await _user()
    out = await sync_to_async(close_review.proposal_rows, thread_sensitive=True)(user, proposals)
    await write_audit(user_id=user.pk, tool="propose_session_closes",
                      args_summary=f"proposed={len(proposals or [])} rows={len(out['rows'])}")
    return out


@mcp.tool(meta=VIEW_TOOL_META["close_proposed_sessions"])
async def close_proposed_sessions(session_ids: list[str]) -> dict:
    """Close the sessions a person ticked in the close review. Called by the
    review's Close button only; the agent proposes with `propose_session_closes`."""
    from apps.mcp import delegation

    if delegation._delegated_claims() is None:
        raise ToolError("only the close review's button closes sessions — "
                        "propose them with propose_session_closes")
    user = await _user()
    summary = f"sessions={len(session_ids or [])}"
    try:
        check_write_limit(user.pk)
    except RateLimitError as exc:
        await write_audit(user_id=user.pk, tool="close_proposed_sessions",
                          args_summary=summary, ok=False, error=str(exc))
        raise
    out = await sync_to_async(close_review.close, thread_sensitive=True)(user, session_ids)
    await write_audit(user_id=user.pk, tool="close_proposed_sessions",
                      args_summary=f"{summary} closed={out['closed']}")
    return out


@mcp.resource(VIEW_URI, mime_type=VIEW_MIME, meta={"ui": {"prefersBorder": False}})
def close_review_view() -> str:
    return _VIEW_HTML.read_text()
