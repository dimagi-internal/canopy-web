"""A page is told when the data it is showing changes.

The bug: "close everything" on /insights did one of two things depending on which
tool the agent happened to pick — rows vanishing in front of you
(`page_dismissInsights`), or the page going on displaying twenty rows that no
longer existed (`clear_insights`). And the same staleness arrives with no agent
involved: the fleet, a schedule, a second tab, a colleague.

Two of these tests exist because `apps/push` documented traps in its own
docstrings that the first draft of this module walked straight into, and only a
test keeps them from being re-introduced by someone who reads the code and not
the comment.
"""

import pytest
from django.contrib.auth.models import User
from django.db import transaction

from apps.canopy_sessions import invalidation, page_state
from apps.canopy_sessions.models import Session
from apps.projects.models import Project, ProjectContext
from apps.projects.signals import INSIGHT_CONTEXT_TYPE, INSIGHT_RESOURCE
from apps.workspaces.models import Workspace, WorkspaceMembership

#: `transaction=True` because `on_commit` is the SUBJECT here. pytest-django's
#: default wraps each test in a transaction it rolls back, so commit hooks never
#: fire and every assertion below would pass or fail for a reason that has
#: nothing to do with the code. Slower, and the only faithful option.
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def sent(monkeypatch):
    """Everything published to a session group."""
    out = []
    monkeypatch.setattr(invalidation, "publish", lambda group, msg: out.append((group, msg)))
    return out


def _world():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    session = Session.objects.create(workspace=ws, created_by=user, title="chat")
    project = Project.objects.create(slug="canopy", name="canopy", workspace=ws)
    return user, ws, session, project


def _showing(session, uri=INSIGHT_RESOURCE):
    page_state.set_page_state(session, {"path": "/insights", "resource": uri,
                                        "backing_tool": "list_insights"})


def _an_insight(project, content="[stale] something"):
    return ProjectContext.objects.create(
        project=project, context_type=INSIGHT_CONTEXT_TYPE, content=content, source="test"
    )


# --- the loop ----------------------------------------------------------------


def test_a_page_showing_insights_is_told_when_one_changes(sent):
    _user, _ws, session, project = _world()
    _showing(session)

    _an_insight(project)

    assert [m["type"] for _g, m in sent] == ["page.invalidate"]
    assert sent[0][1]["uri"] == INSIGHT_RESOURCE


def test_it_fires_on_deletion_too_which_is_the_case_that_actually_bit(sent):
    """"Close everything" is a delete, and a delete is what left the page
    showing rows that no longer existed."""
    _user, _ws, session, project = _world()
    insight = _an_insight(project)
    _showing(session)
    sent.clear()

    insight.delete()

    assert [m["uri"] for _g, m in sent] == [INSIGHT_RESOURCE]


def test_a_page_showing_something_else_is_not_disturbed(sent):
    _user, _ws, session, project = _world()
    _showing(session, uri="walkthrough://")

    _an_insight(project)

    assert sent == []


def test_a_page_declaring_no_resource_is_not_notified(sent):
    """Silence, not a default-to-everyone. A page that never said what it shows
    cannot be told its data moved."""
    _user, _ws, session, project = _world()
    page_state.set_page_state(session, {"path": "/insights"})

    _an_insight(project)

    assert sent == []


def test_a_non_insight_context_row_does_not_invalidate_the_feed(sent):
    """`ProjectContext` carries five kinds. A summary being written must not make
    the insights feed refetch."""
    _user, _ws, session, project = _world()
    _showing(session)

    ProjectContext.objects.create(
        project=project, context_type="summary", content="unrelated", source="test"
    )

    assert sent == []


# --- the traps apps/push paid for --------------------------------------------


def test_a_bulk_change_sends_one_notification_not_one_per_row(sent):
    """A fleet audit writes hundreds of rows in one transaction. Coalescing is
    the difference between one refetch and a page refetching itself to death."""
    _user, _ws, session, project = _world()
    _showing(session)
    sent.clear()

    with transaction.atomic():
        for n in range(25):
            _an_insight(project, content=f"[stale] {n}")

    assert len(sent) == 1, f"expected one coalesced notification, got {len(sent)}"


def test_a_rolled_back_transaction_notifies_nothing(sent):
    """A notification for a write that never landed makes a correct page refetch
    into the same state — which looks like a bug and teaches people to distrust
    the signal."""
    _user, _ws, session, project = _world()
    _showing(session)
    sent.clear()

    class Boom(Exception):
        pass

    with pytest.raises(Boom), transaction.atomic():
        _an_insight(project)
        raise Boom()

    assert sent == []


def test_invalidation_still_works_after_a_rollback(sent):
    """THE trap, documented in `apps/push/services.mark_dirty` and walked into by
    the first draft of this module.

    Django discards on_commit callbacks when a transaction rolls back, but the
    dirty set is not transactional and keeps its entries. A
    `if not _dirty_set(): register()` guard therefore sees a non-empty set
    forever after the first rollback, never registers again, and silently kills
    invalidation process-wide until restart. Registering unconditionally is what
    makes this test pass.
    """
    _user, _ws, session, project = _world()
    _showing(session)

    class Boom(Exception):
        pass

    with pytest.raises(Boom), transaction.atomic():
        _an_insight(project)
        raise Boom()
    sent.clear()

    _an_insight(project, content="[stale] after the rollback")

    assert len(sent) == 1, "invalidation died after a rollback — the guard is back"


def test_a_publish_failure_does_not_take_down_the_write(monkeypatch):
    """Best-effort by design: a page that misses a notification shows stale data
    until its next read, while an exception inside on_commit would fail the
    request that did the real work."""
    _user, _ws, session, project = _world()
    _showing(session)

    def boom(*_a, **_k):
        raise RuntimeError("channel layer is down")

    monkeypatch.setattr(invalidation, "publish", boom)

    _an_insight(project)  # must not raise


# --- the receiver is actually connected --------------------------------------


def test_the_receiver_is_connected_through_the_app_registry():
    """Not "the module has tests" — "Django actually calls it".

    `apps/mcp/page_tools.py` shipped with ten passing tests and no import, so
    the feature did not exist. This asserts against the live signal registry.
    """
    from django.db.models.signals import post_save

    # Entries are (lookup_key, weakref, is_async) in this Django; read the
    # reference positionally rather than unpacking, so a shape change in a
    # future Django fails this test loudly instead of silently matching nothing.
    names = []
    for entry in post_save.receivers:
        ref = entry[1]
        fn = ref() if callable(ref) and hasattr(ref, "__callback__") else ref
        if fn is None:
            continue
        names.append(getattr(fn, "__name__", ""))
    assert "_insight_changed" in names


def test_the_receiver_filters_on_the_same_value_the_feed_queries():
    """A drift where rows are invalidated but never listed — or listed and never
    invalidated — is invisible at runtime."""
    import inspect

    from apps.projects import services

    assert INSIGHT_CONTEXT_TYPE == "insight"
    assert f'context_type="{INSIGHT_CONTEXT_TYPE}"' in inspect.getsource(
        services.insights_queryset
    )


def test_only_active_sessions_are_notified(sent):
    _user, _ws, session, project = _world()
    _showing(session)
    session.status = Session.ARCHIVED
    session.save(update_fields=["status"])
    sent.clear()

    _an_insight(project)

    assert sent == []


def test_the_server_side_dismiss_invalidates_the_page(sent):
    """The composition that replaced the page action.

    `dismiss_insights` deletes row by row precisely so `post_delete` fires for
    each — `QuerySet.delete()` does not reliably emit it per row, and post_delete
    is what raises invalidation. Without this the new server tool would delete
    the rows and leave the page displaying them, which is the exact bug the page
    action existed to dodge.
    """
    from apps.projects import services

    _user, ws, session, project = _world()
    a, b = _an_insight(project, "[stale] a"), _an_insight(project, "[stale] b")
    _showing(session)
    sent.clear()

    dismissed = services.dismiss_insights(workspace_slugs={ws.slug}, ids=[a.pk, b.pk])

    assert sorted(dismissed) == sorted([a.pk, b.pk])
    # Coalesced to ONE notification for the two rows, not silence and not two.
    assert [m["uri"] for _g, m in sent] == [INSIGHT_RESOURCE]
