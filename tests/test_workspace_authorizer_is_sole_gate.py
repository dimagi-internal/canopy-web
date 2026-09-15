"""Fitness test: "is this user allowed in this workspace?" is answered in ONE place.

WHY THIS EXISTS, in this repo's own words. `CLAUDE.md` records that while
`Agent.workspace` was nullable, "**six separate tenancy predicates independently
grew a `workspace_id IS NULL` leg meaning *allow***", each written by someone
re-deriving the rule locally. Four were fixed one site at a time (PRs #378,
#421, #423) before it was clear that the recurrence — not any single site — was
the defect. `tests/test_claim_schedule_parity.py` exists for the same reason at
a different seam: two predicates that had to agree, and did not.

The fix for a rule that keeps being rewritten is not another careful rewrite. It
is making the rewrite fail the build.

Borrowed from Scout's `tests/test_authorizer_is_sole_gate.py` (dimagi-rad/scout,
read 2026-09-14), which guards the same seam with the same technique. Scout's
insight is the SIGNATURE: a `WorkspaceMembership` read filtered by BOTH a
workspace key and a user key is, definitionally, an access decision — nobody
writes that query for any other reason. So the test does not need to understand
authorization, only to recognise its shape.

DELIBERATELY NARROW, and the narrowness is what makes it usable:

* Filtering by USER alone is a listing ("which workspaces am I in?") — not a
  decision, not flagged. `apps/tokens/embed_apps.py::owned_workspace_slugs` is
  exactly this and is correct as written.
* Filtering by WORKSPACE alone is a listing ("who is in this workspace?") — not
  flagged either.
* WRITES are not decisions. `create` / `get_or_create` / `update` grant or change
  access; they are gated by the code that calls them, and `ensure_member` has its
  own guard in `tests/test_no_implicit_enrolment.py`.

The escape hatch is an inline `# authz-exempt` comment, for the genuine
non-authorization use of that shape — checking whether a TARGET is already a
member, say, which is a question about someone else rather than about the
caller. If you reach for it, say why in the same breath.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The one module allowed to decide workspace access. `member_role`,
#: `has_role_at_least`, `is_member` and `membership` all live here, so every
#: gate in the codebase reads the same rule from the same place.
AUTHORIZER = REPO_ROOT / "apps" / "workspaces" / "services.py"

SCAN_DIRS = ["apps"]

WORKSPACE_KEYS = {"workspace", "workspace_id"}
USER_KEYS = {"user", "user_id"}

#: Only READS resolve access. A write grants or changes it and is a different
#: question, guarded elsewhere.
READ_METHODS = {
    "get", "aget", "filter", "exclude", "exists", "aexists", "first", "afirst", "count",
}

EXEMPT_MARKER = "authz-exempt"


def _bypass_lines(source: str) -> list[int]:
    """1-based lines of `WorkspaceMembership` READS keyed by both user and workspace.

    Walks the AST rather than grepping, so a call split across lines, wrapped in
    `select_related`, or spelled `wsvc.WorkspaceMembership` is still caught —
    all three occur in this codebase.
    """
    tree = ast.parse(source)
    lines = source.splitlines()
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in READ_METHODS:
            continue
        # `WorkspaceMembership.objects…`, however it is imported or chained.
        if "WorkspaceMembership.objects" not in ast.unparse(node.func):
            continue
        kwargs = {kw.arg for kw in node.keywords if kw.arg}
        if not (kwargs & WORKSPACE_KEYS and kwargs & USER_KEYS):
            continue
        # The marker may sit on the call's line or in the few lines above it,
        # which is where a reader would naturally put the justification.
        window = "\n".join(lines[max(0, node.lineno - 4):node.lineno])
        if EXEMPT_MARKER in window:
            continue
        hits.append(node.lineno)
    return hits


def _production_files():
    for d in SCAN_DIRS:
        for path in sorted((REPO_ROOT / d).rglob("*.py")):
            if path == AUTHORIZER:
                continue
            parts = set(path.parts)
            if "migrations" in parts or "tests" in parts:
                continue
            if path.name.startswith("test_"):
                continue
            yield path


def test_only_the_authorizer_decides_workspace_access():
    """No production module outside `workspaces/services.py` may read a
    membership keyed by both a user and a workspace."""
    offenders: list[str] = []
    for path in _production_files():
        for line in _bypass_lines(path.read_text()):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{line}")

    assert not offenders, (
        "These read WorkspaceMembership by BOTH user and workspace — the signature of "
        "an access decision — outside apps/workspaces/services.py:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse wsvc.member_role / wsvc.has_role_at_least / wsvc.is_member / "
        "wsvc.membership instead, so the rule has one definition. If this genuinely "
        "is not an access decision (checking whether a TARGET is already a member, "
        "say), add an inline `# authz-exempt` comment saying why."
    )


def test_the_detector_actually_detects():
    """Guards the guard.

    A fitness test that silently matched nothing would pass forever and protect
    nothing — which is this file's own failure mode, and the one worth pinning.
    """
    caught = _bypass_lines(
        "WorkspaceMembership.objects.filter(user=u, workspace_id=s).exists()\n"
    )
    assert caught == [1]

    # Both spellings of the attribute chain this repo actually uses.
    assert _bypass_lines(
        "wsvc.WorkspaceMembership.objects.filter(user=u, workspace_id=s).first()\n"
    ) == [1]

    # And a call the authorizer's own helpers make — split across lines, which a
    # line-oriented grep would miss.
    assert _bypass_lines(
        "WorkspaceMembership.objects.select_related('workspace').get(\n"
        "    workspace_id=slug, user=user\n"
        ")\n"
    ) == [1]


@pytest.mark.parametrize("source", [
    # A listing of the caller's own workspaces — not a decision.
    "WorkspaceMembership.objects.filter(user=u).values_list('workspace_id', flat=True)\n",
    # A listing of a workspace's members — not a decision.
    "WorkspaceMembership.objects.filter(workspace_id=s)\n",
    # A write. Granting access is gated by its caller, not by this shape.
    "WorkspaceMembership.objects.get_or_create(workspace=ws, user=u)\n",
    # An explicitly justified non-auth use.
    "# authz-exempt: is the TARGET already a member?\n"
    "WorkspaceMembership.objects.filter(user=target, workspace_id=s).exists()\n",
])
def test_the_narrow_cases_are_not_flagged(source):
    """The test is only worth having if it does not cry wolf.

    Each of these is a real shape in this codebase, and flagging any of them
    would push people to blanket-exempt the file — which is how a fitness test
    stops being read.
    """
    assert _bypass_lines(source) == []
