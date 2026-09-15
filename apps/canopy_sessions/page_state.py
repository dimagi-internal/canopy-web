"""What the user is looking at, as state the agent can re-read.

The sibling of `page_actions`: that module is what the page can DO, this is
what the page currently SHOWS. Together they are the two halves AG-UI splits a
frontend into — frontend tools and shared state — and the second half is the
one canopy did not have.

**Why this is not "context appended to the prompt".** Before this, the page
snapshot was fetched once when the frame initialised, rendered to prose, and
pasted onto the first message. Three things were wrong with that and only the
third is obvious:

  * it was read at the wrong MOMENT — the user filters the page *after* opening
    the widget far more often than before, so the one snapshot we took was
    routinely of a view that no longer existed;
  * it reached the agent as PROSE, which is measurably worse than structure at
    equal size (the WebMCP origin-trial ablation recovered the right next action
    19/20 from a bounded structured packet versus 14/20 from a byte-matched
    free-text summary);
  * it was attached to the FIRST message only, so by turn three the agent was
    reasoning about a page the user had left.

State fixes all three by being a thing the agent can read *whenever it asks*,
rather than a thing the page says once.

**It is a cache, never the authority.** Exactly as with contacts. The state says
*which* rows are on screen; the agent re-reads the rows themselves through the
ordinary MCP tools, where the caller's own ACL applies. That is why the size cap
below is deliberately too small to hold the rows: a page that tries to send its
data instead of its selection is refused, out loud, at the point the mistake is
made rather than in a code review nobody runs.
"""

from __future__ import annotations

import json

from .models import Session

#: The whole point is a BOUNDED packet. 8 KiB holds a filter set and several
#: hundred ids comfortably and cannot hold the rows behind them, which is the
#: constraint we actually want to enforce: send the selection, not the data.
#: Chosen to be generous for the intended shape and hostile to the wrong one.
MAX_STATE_BYTES = 8192

#: Reserved: the tool a page names as backing its visible rows. Not validated
#: against the MCP registry here (this app must not import `apps.mcp`), but
#: carried through so the agent is told where to go and read properly.
BACKING_TOOL_KEY = "backing_tool"


class PageStateError(Exception):
    """A declaration the page must fix, with a `code` the caller can branch on.

    `too_large` is the only one that fires in practice, and it is a design
    guard rather than a resource limit — see MAX_STATE_BYTES.
    """

    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def current_page_state(session: Session) -> dict:
    """What the attached page currently shows.

    An empty dict means no page is attached or the page declares nothing — NOT
    that the page is empty. A caller that cannot tell those apart will describe
    a blank screen to the user.
    """
    return dict(session.page_state or {})


def set_page_state(session: Session, state: dict) -> dict:
    """Replace the declaration wholesale, and bump the version.

    Never merged, for the same reason actions are not: a page has ONE current
    view, and a key left over from the page the user navigated away from is a
    key the agent would reason about as if it were still on screen. Merging
    makes stale state indistinguishable from fresh state, which is the failure
    this whole module exists to remove.

    The version is monotonic per session so a consumer can tell one snapshot
    from the next, and so a later delta-based transport has an ordering to apply
    against. It is assigned here rather than accepted from the page: a client
    that picks its own version numbers can silently go backwards.
    """
    if not isinstance(state, dict):
        raise PageStateError("bad_state", "page state must be a JSON object")

    encoded = json.dumps(state, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_STATE_BYTES:
        raise PageStateError(
            "too_large",
            f"page state is larger than {MAX_STATE_BYTES} bytes. Send the "
            f"selection (ids, filters) and the tool that resolves it, not the "
            f"rows themselves — the agent reads those itself, with the user's "
            f"own permissions applied.",
        )

    previous = dict(session.page_state or {})
    version = int(previous.get("version") or 0) + 1
    # `version` lives inside the blob rather than in its own column: it has no
    # meaning apart from the state it stamps, and a separate column invites the
    # two to be written apart and disagree.
    session.page_state = {**state, "version": version}
    session.save(update_fields=["page_state", "updated_at"])
    return dict(session.page_state)


def clear_page_state(session: Session) -> None:
    """Forget the view. Called when a page detaches.

    Clearing is not the same as declaring `{}`: both read as "nothing on
    screen", which is correct, and neither is the same as never having spoken.
    """
    if session.page_state:
        session.page_state = {}
        session.save(update_fields=["page_state", "updated_at"])
