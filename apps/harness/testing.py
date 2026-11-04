"""Test helpers that build harness rows (kept out of `apps.workspaces.testing`, which harness sits above)."""
from __future__ import annotations

from apps.workspaces import permissions as perms

from .models import RunnerEnrollment


def enrolled(runner, *workspaces):
    """`runner`, enrolled in `workspaces` (Workspaces or slugs) — by default in
    every workspace its owner may run work in right now, which is what migration
    harness/0071 did for every box that existed before enrollment.

    A runner serves only workspaces it is enrolled in (`RunnerEnrollment`), and
    pairing enrolls it only in `Runner.workspace`; a test that builds a runner
    with no `workspace`, or expects it to serve several, says so here."""
    slugs = ({getattr(w, "slug", w) for w in workspaces} if workspaces
             else perms.slugs_with(runner.owner, perms.AGENT_WORK) if runner.owner_id else set())
    for slug in sorted(slugs):
        RunnerEnrollment.objects.get_or_create(runner=runner, workspace_id=slug)
    return runner
