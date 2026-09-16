"""Dismissing insights BY ID, from the server.

Why this exists at all: `clear_insights` takes filters, and a filter is only an
approximation of what somebody can see. On a paginated feed it also matches rows
below the fold they never looked at, and with no filters it deletes everything
(its own docstring says so). "Close the ones I am looking at" is a set of ids.

That gap is the only reason `dismissInsights` was ever a page action — a data
mutation routed through a browser tab, unaudited, unavailable once the tab
closed, and capped by a 20-second wait. With this tool and page invalidation, it
has no reason to exist and has been deleted.
"""

import pytest
from django.contrib.auth.models import User

from apps.projects import services
from apps.projects.models import Project, ProjectContext
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _tenant(slug, email):
    user = User.objects.create_user(email.split("@")[0], email, "pw")
    ws = Workspace.objects.create(slug=slug, display_name=slug, created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    project = Project.objects.create(slug=f"{slug}-p", name=slug, workspace=ws)
    return user, ws, project


def _insight(project, content="[stale] x"):
    return ProjectContext.objects.create(
        project=project, context_type="insight", content=content, source="test"
    )


def test_it_dismisses_exactly_the_ids_given():
    _u, ws, project = _tenant("w1", "jj@dimagi.com")
    keep, go_a, go_b = _insight(project), _insight(project), _insight(project)

    dismissed = services.dismiss_insights(workspace_slugs={ws.slug}, ids=[go_a.pk, go_b.pk])

    assert sorted(dismissed) == sorted([go_a.pk, go_b.pk])
    assert list(ProjectContext.objects.values_list("pk", flat=True)) == [keep.pk]


def test_it_is_more_precise_than_a_filter_which_is_the_whole_point():
    """A filter matching the same category would also take the third row — the
    one below the fold that the user never saw."""
    _u, ws, project = _tenant("w1", "jj@dimagi.com")
    visible_a = _insight(project, "[stale] one")
    visible_b = _insight(project, "[stale] two")
    below_the_fold = _insight(project, "[stale] three")

    services.dismiss_insights(workspace_slugs={ws.slug}, ids=[visible_a.pk, visible_b.pk])

    assert ProjectContext.objects.filter(pk=below_the_fold.pk).exists()


def test_an_id_in_another_workspace_is_not_deleted():
    """Same tenant predicate as the REST dismiss endpoint: a member of one
    workspace must not delete another's insight by enumerating pks."""
    _u1, ws1, p1 = _tenant("w1", "jj@dimagi.com")
    _u2, _ws2, p2 = _tenant("w2", "other@dimagi.com")
    mine, theirs = _insight(p1), _insight(p2)

    dismissed = services.dismiss_insights(workspace_slugs={ws1.slug}, ids=[mine.pk, theirs.pk])

    assert dismissed == [mine.pk]
    assert ProjectContext.objects.filter(pk=theirs.pk).exists()


def test_an_out_of_scope_id_is_absent_rather_than_an_error():
    """A partial result the caller can compare against what it asked for beats
    all-or-nothing, and leaks nothing about whether the row existed."""
    _u1, ws1, p1 = _tenant("w1", "jj@dimagi.com")
    mine = _insight(p1)

    assert services.dismiss_insights(workspace_slugs={ws1.slug}, ids=[mine.pk, 999999]) == [mine.pk]


def test_an_empty_list_is_a_no_op():
    _u, ws, project = _tenant("w1", "jj@dimagi.com")
    _insight(project)

    assert services.dismiss_insights(workspace_slugs={ws.slug}, ids=[]) == []
    assert ProjectContext.objects.count() == 1


def test_a_non_insight_row_is_not_reachable_through_it():
    """Scoped through `insights_queryset`, so a `summary` row cannot be deleted
    by passing its pk to an insights tool."""
    _u, ws, project = _tenant("w1", "jj@dimagi.com")
    summary = ProjectContext.objects.create(
        project=project, context_type="summary", content="not an insight", source="test"
    )

    assert services.dismiss_insights(workspace_slugs={ws.slug}, ids=[summary.pk]) == []
    assert ProjectContext.objects.filter(pk=summary.pk).exists()


def test_the_tool_is_served_by_the_mounted_server():
    """Not "the module has tests" — `page_tools.py` had ten of those and no
    import."""
    import asyncio

    from apps.mcp.server import mcp

    assert "dismiss_insights" in {t.name for t in asyncio.run(mcp._list_tools())}
