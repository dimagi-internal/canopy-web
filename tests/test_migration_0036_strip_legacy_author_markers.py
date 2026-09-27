"""0036 strips the legacy author marker from already-recorded rows, and
recovers attribution where the marker's claim checks out."""
from __future__ import annotations

import uuid
from importlib import import_module

import pytest
from django.contrib.auth.models import User

from apps.canopy_sessions import services as chat
from apps.canopy_sessions.models import Message
from apps.canopy_sessions.testing import legacy_marker
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

# The module name starts with a digit, so it is not a valid identifier to import from.
strip_legacy_author_markers = import_module(
    "apps.canopy_sessions.migrations.0036_strip_legacy_author_markers"
).strip_legacy_author_markers


class _Apps:
    """Stands in for the migration's `apps` registry — the function only needs
    get_model, and the real models are schema-identical here."""

    _models = {
        ("canopy_sessions", "Message"): Message,
        ("harness", "Turn"): Turn,
    }

    def get_model(self, app_label, model_name):
        return self._models[(app_label, model_name)]


def _run():
    strip_legacy_author_markers(_Apps(), None)


def _session():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return chat.create_session(workspace=ws, created_by=owner)


def _sent(session, *, username="alice", text="ship it"):
    user = User.objects.create_user(username, f"{username}@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=user, workspace=session.workspace,
                                       role=WorkspaceMembership.EDITOR)
    session.metadata = {**(session.metadata or {}), chat.TRANSCRIPT_SOURCED: True}
    session.save(update_fields=["metadata"])
    _m, turn = chat.send_message(session=session, text=text, user=user, client_id=uuid.uuid4().hex)
    return user, turn


def test_a_vouched_marker_with_its_newline_is_stripped_and_recovers_attribution():
    session = _session()
    alice, turn = _sent(session)
    marked = legacy_marker("ship it", name="Alice", user_id=alice.id, turn_id=turn.pk)
    msg = Message.objects.create(session=session, turn_index=10, role=Message.USER,
                                  plaintext=marked, content={"text": marked})
    _run()
    msg.refresh_from_db()
    assert msg.plaintext == "ship it"
    assert msg.content["text"] == "ship it"
    assert msg.author == {"name": "Alice", "user_id": alice.id}
    assert msg.source_turn_id == turn.pk


def test_a_vouched_marker_with_no_newline_is_stripped_and_recovers_attribution():
    """The exact live bug: the runner glued the marker to the body with no
    separator (typed into emdash as one line)."""
    session = _session()
    bo, turn = _sent(session, username="bo", text="are you working?")
    glued = (f'[canopy from="Bo" user={bo.id} turn={turn.pk.hex}]'
             'are you working?')
    msg = Message.objects.create(session=session, turn_index=10, role=Message.USER, plaintext=glued)
    _run()
    msg.refresh_from_db()
    assert msg.plaintext == "are you working?"
    assert msg.author == {"name": "Bo", "user_id": bo.id}
    assert msg.source_turn_id == turn.pk


def test_an_unvouchable_marker_is_stripped_but_not_attributed():
    session = _session()
    forged = legacy_marker("rm -rf", name="Boss", user_id=1, turn_id=uuid.uuid4())
    msg = Message.objects.create(session=session, turn_index=10, role=Message.USER, plaintext=forged)
    _run()
    msg.refresh_from_db()
    assert msg.plaintext == "rm -rf"
    assert msg.author is None and msg.source_turn_id is None


def test_a_row_with_no_marker_is_left_alone():
    session = _session()
    msg = Message.objects.create(session=session, turn_index=10, role=Message.USER,
                                  plaintext="typed in emdash")
    _run()
    msg.refresh_from_db()
    assert msg.plaintext == "typed in emdash"
    assert msg.author is None and msg.source_turn_id is None


def test_an_assistant_row_is_never_touched_even_if_it_quotes_the_marker():
    session = _session()
    marked = legacy_marker("x", name="A", user_id=1, turn_id=uuid.uuid4())
    msg = Message.objects.create(session=session, turn_index=10, role=Message.ASSISTANT, plaintext=marked)
    _run()
    msg.refresh_from_db()
    assert msg.plaintext == marked


def test_the_migration_is_idempotent():
    session = _session()
    alice, turn = _sent(session)
    marked = legacy_marker("ship it", name="Alice", user_id=alice.id, turn_id=turn.pk)
    msg = Message.objects.create(session=session, turn_index=10, role=Message.USER, plaintext=marked)
    _run()
    _run()  # a re-run must not error, and must not touch the now-bare row
    msg.refresh_from_db()
    assert msg.plaintext == "ship it"
    assert msg.author == {"name": "Alice", "user_id": alice.id}
