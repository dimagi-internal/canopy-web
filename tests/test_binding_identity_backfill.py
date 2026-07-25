"""The one-shot repair for bindings the old report path stripped of their runner.

`host` is the durable identity that survived the nulling, so it is what the backfill
matches on — conservatively: ambiguity is left NULL rather than guessed, because
claim stickiness reads this FK and pointing a session at the wrong box would send its
turns somewhere they can never run.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from importlib import import_module

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

# The module name starts with a digit, so it is not a valid identifier to import from.
restore_runner_identity = import_module(
    "apps.canopy_sessions.migrations.0011_restore_binding_runner_identity"
).restore_runner_identity


class _Apps:
    """Stands in for the migration's `apps` registry — the function only needs
    get_model, and the real models are schema-identical here."""

    _models = {
        ("canopy_sessions", "RunnerBinding"): RunnerBinding,
        ("harness", "Runner"): Runner,
    }

    def get_model(self, app_label, model_name):
        return self._models[(app_label, model_name)]


def _run():
    restore_runner_identity(_Apps(), None)


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    return user, ws


def _orphan(ws, host, key):
    session = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title=key)
    return RunnerBinding.objects.create(
        session=session, runner=None, session_key=key, host=host,
        live_seen_at=timezone.now(),
    )


def test_an_orphan_is_re_pointed_at_the_runner_on_its_host():
    user, ws = _ctx()
    runner = Runner.objects.create(name="mbp", workspace=ws, paired_by=user, host="jj@mbp")
    binding = _orphan(ws, "jj@mbp", "ddd")

    _run()

    binding.refresh_from_db()
    assert binding.runner_id == runner.id


def test_a_retired_runner_still_claims_its_orphans():
    """The case the identity matters MOST for: it cannot be re-derived from a live
    report, and it is now recoverable via /unretire."""
    user, ws = _ctx()
    runner = Runner.objects.create(
        name="gone", workspace=ws, paired_by=user, host="jj@old", status=Runner.RETIRED
    )
    binding = _orphan(ws, "jj@old", "ddd")

    _run()

    binding.refresh_from_db()
    assert binding.runner_id == runner.id


def test_an_ambiguous_host_is_left_alone():
    """Two runners re-paired on one host: a wrong runner is worse than none."""
    user, ws = _ctx()
    Runner.objects.create(name="old", workspace=ws, paired_by=user, host="jj@mbp")
    Runner.objects.create(name="new", workspace=ws, paired_by=user, host="jj@mbp")
    binding = _orphan(ws, "jj@mbp", "ddd")

    _run()

    binding.refresh_from_db()
    assert binding.runner_id is None


def test_a_blank_host_is_left_alone():
    """Legacy rows predating the host stamp carry no identity to recover."""
    user, ws = _ctx()
    Runner.objects.create(name="mbp", workspace=ws, paired_by=user, host="")
    binding = _orphan(ws, "", "ddd")

    _run()

    binding.refresh_from_db()
    assert binding.runner_id is None


def test_it_never_violates_the_one_binding_per_runner_key_index():
    """Orphans were exempt from the partial unique index over (runner, session_key)
    while NULL, so two of them on one host can share a key. Filling both would
    IntegrityError mid-migration on prod."""
    user, ws = _ctx()
    Runner.objects.create(name="mbp", workspace=ws, paired_by=user, host="jj@mbp")
    first = _orphan(ws, "jj@mbp", "ddd")
    second = _orphan(ws, "jj@mbp", "ddd")

    _run()  # must not raise

    filled = [b for b in (first, second) if RunnerBinding.objects.get(pk=b.pk).runner_id]
    assert len(filled) == 1, "exactly one may take the key; the other stays NULL"


def test_a_binding_that_already_has_a_runner_is_untouched():
    user, ws = _ctx()
    mine = Runner.objects.create(name="mbp", workspace=ws, paired_by=user, host="jj@mbp")
    other = Runner.objects.create(name="air", workspace=ws, paired_by=user, host="jj@air")
    session = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="ddd")
    binding = RunnerBinding.objects.create(
        session=session, runner=other, session_key="ddd", host="jj@mbp",
    )

    _run()

    binding.refresh_from_db()
    assert binding.runner_id == other.id, "only NULLs are filled"
    assert binding.runner_id != mine.id
