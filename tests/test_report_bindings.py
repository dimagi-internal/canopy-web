"""The report path upserts a durable Session(origin=runner) + RunnerBinding
per reported emdash session, keyed by (runner, session_key) — plus (host,
session_key) recovery for a binding this runner previously released — replacing
the deleted EmdashSession model."""
from __future__ import annotations

import pytest
from types import SimpleNamespace

from django.contrib.auth import get_user_model

from apps.harness.services import replace_reported_sessions
from apps.harness.models import Runner
from apps.canopy_sessions.models import Session, RunnerBinding
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def _reported(task, msgs, project="canopy-web"):
    return SimpleNamespace(
        emdash_task=task, project=project, status="running",
        last_interacted_at=None, recent_messages=msgs,
    )


def _user():
    return get_user_model().objects.create(username="jj", email="jj@dimagi.com")


def test_report_creates_session_and_binding():
    jj = _user()
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=jj)
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL)
    n = replace_reported_sessions(runner, ws, [_reported("feat-x", [{"role": "assistant", "text": "hi"}])])
    assert n == 1
    b = RunnerBinding.objects.get(runner=runner, session_key="feat-x")
    assert b.session.origin == Session.ORIGIN_RUNNER
    assert b.tail == [{"role": "assistant", "text": "hi"}]


def test_report_is_idempotent_and_updates_tail():
    jj = _user()
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=jj)
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL)
    replace_reported_sessions(runner, ws, [_reported("feat-x", [{"role": "user", "text": "a"}])])
    replace_reported_sessions(runner, ws, [_reported("feat-x", [{"role": "assistant", "text": "b"}])])
    assert Session.objects.filter(runner_binding__session_key="feat-x").count() == 1
    b = RunnerBinding.objects.get(runner=runner, session_key="feat-x")
    assert b.tail == [{"role": "assistant", "text": "b"}]


def test_dropped_session_keeps_both_its_session_and_its_runner():
    """Falling off a report is the NORMAL end of life (emdash deletes a closed task),
    not a reason to forget which box the session lived on. Liveness is live_seen_at
    going stale, not the FK going null."""
    jj = _user()
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=jj)
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL)
    replace_reported_sessions(runner, ws, [_reported("feat-x", [])])
    seen_when_live = RunnerBinding.objects.get(session_key="feat-x").live_seen_at

    replace_reported_sessions(runner, ws, [])  # feat-x no longer open

    b = RunnerBinding.objects.get(session_key="feat-x")
    assert b.runner_id == runner.id  # identity survives
    assert b.live_seen_at == seen_when_live, "liveness clock stops; it is not bumped"
    assert Session.objects.filter(runner_binding=b).exists()  # session kept


def test_a_dropped_session_reappearing_revives_the_same_row():
    """The clear nulls the live FK, so the upsert lookup must not key on it: a task
    that drops off a report and comes back is the SAME session, not a fork. Recovery
    is scoped by host — emdash task names collide across machines."""
    jj = _user()
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=jj)
    runner = Runner.objects.create(
        name="laptop", workspace=ws, location=Runner.LOCAL, host="jj@air"
    )
    replace_reported_sessions(runner, ws, [_reported("feat-x", [])])
    original = RunnerBinding.objects.get(session_key="feat-x")

    replace_reported_sessions(runner, ws, [])          # dropped: live pointer cleared
    replace_reported_sessions(runner, ws, [_reported("feat-x", [])])  # and back

    assert Session.objects.count() == 1                # no fork
    revived = RunnerBinding.objects.get(session_key="feat-x")
    assert revived.pk == original.pk
    assert revived.session_id == original.session_id
    assert revived.runner_id == runner.id


def test_recovery_of_a_released_binding_is_host_scoped():
    """A DIFFERENT machine reporting the same task name must not claim the released
    row — it gets its own session."""
    jj = _user()
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=jj)
    air = Runner.objects.create(name="air", workspace=ws, location=Runner.LOCAL, host="jj@air")
    mini = Runner.objects.create(name="mini", workspace=ws, location=Runner.LOCAL, host="jj@mini")

    replace_reported_sessions(air, ws, [_reported("feat-x", [])])
    replace_reported_sessions(air, ws, [])  # air releases it
    replace_reported_sessions(mini, ws, [_reported("feat-x", [])])

    assert Session.objects.count() == 2
    assert RunnerBinding.objects.get(runner=mini, session_key="feat-x").host == "jj@mini"
    # air's row keeps ITS identity — the point is that mini did not take it over.
    assert RunnerBinding.objects.get(host="jj@air", session_key="feat-x").runner_id == air.id


def test_recovery_does_not_fuse_two_runners_with_blank_host():
    """Two distinct runners that both have host="" (legacy/unheartbeated) each
    report the same task name and then release it. Runner B's fresh report must
    NOT recover runner A's released binding — the null-recovery branch requires a
    non-blank host, so a blank host never matches. Runner B gets its OWN session."""
    jj = _user()
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=jj)
    a = Runner.objects.create(name="a", workspace=ws, location=Runner.LOCAL, host="")
    b = Runner.objects.create(name="b", workspace=ws, location=Runner.LOCAL, host="")

    replace_reported_sessions(a, ws, [_reported("feat-x", [])])
    binding_a = RunnerBinding.objects.get(runner=a, session_key="feat-x")
    session_a = binding_a.session_id

    replace_reported_sessions(a, ws, [])  # a stops reporting it

    replace_reported_sessions(b, ws, [_reported("feat-x", [])])

    assert Session.objects.count() == 2  # no fusion: b got its own session

    binding_a.refresh_from_db()
    assert binding_a.runner_id == a.id
    assert binding_a.session_id == session_a  # a's binding/session untouched

    binding_b = RunnerBinding.objects.get(runner=b, session_key="feat-x")
    assert binding_b.pk != binding_a.pk
    assert binding_b.session_id != session_a


def test_list_visible_sessions_maps_to_wire_shape():
    from django.utils import timezone

    from apps.harness.services import list_visible_sessions
    from apps.workspaces.services import ensure_member
    from apps.workspaces.models import WorkspaceMembership

    jj = _user()
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=jj)
    ensure_member(ws, jj, WorkspaceMembership.OWNER)
    runner = Runner.objects.create(
        name="laptop", workspace=ws, location=Runner.LOCAL,
        status=Runner.ONLINE, paired_by=jj, last_heartbeat_at=timezone.now(),
    )
    replace_reported_sessions(runner, ws, [_reported("feat-x", [{"role": "assistant", "text": "hi"}])])
    rows = list_visible_sessions(jj)
    assert len(rows) == 1
    r = rows[0]
    assert r.emdash_task == "feat-x"
    assert r.recent_messages == [{"role": "assistant", "text": "hi"}]
    assert r.runner_name == "laptop"


# --- the project is half the key -------------------------------------------
#
# An emdash task name is scoped to a project, so `issues` under `ace` and `issues`
# under `connect-labs` are two conversations. Keying the binding on the bare name
# fused them; see RunnerBinding.emdash_project.


def _ws_runner():
    jj = _user()
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=jj)
    runner = Runner.objects.create(
        name="laptop", workspace=ws, location=Runner.LOCAL, host="jj@air"
    )
    return ws, runner


def test_same_name_in_two_projects_is_two_sessions():
    """The expensive half: the report DEDUPLICATES before it upserts, so a
    concurrently-open namesake in another repo was not merely mislabelled — it was
    dropped from the report entirely, with no row and no error."""
    ws, runner = _ws_runner()

    replace_reported_sessions(runner, ws, [
        _reported("issues", [{"role": "user", "text": "ace"}], project="ace"),
        _reported("issues", [{"role": "user", "text": "labs"}], project="connect-labs"),
    ])

    bindings = {b.emdash_project: b for b in RunnerBinding.objects.all()}
    assert set(bindings) == {"ace", "connect-labs"}
    assert bindings["ace"].session_id != bindings["connect-labs"].session_id
    assert bindings["ace"].session.project == "ace"
    assert bindings["connect-labs"].session.project == "connect-labs"
    assert bindings["connect-labs"].tail == [{"role": "user", "text": "labs"}]


def test_a_name_reused_in_another_project_does_not_inherit_the_first_session():
    """Labs 2026-08-27: an `ace` task called `issues` claimed the name on 08-14; a
    connect-labs task of the same name three weeks later was served under it, so the
    session reported `project: "ace"` while running `gh ... -R .../connect-labs`."""
    ws, runner = _ws_runner()

    replace_reported_sessions(runner, ws, [_reported("issues", [], project="ace")])
    ace_session = RunnerBinding.objects.get(emdash_project="ace").session_id

    replace_reported_sessions(runner, ws, [])  # the ace task is closed and deleted
    replace_reported_sessions(runner, ws, [_reported("issues", [], project="connect-labs")])

    labs = RunnerBinding.objects.get(emdash_project="connect-labs")
    assert labs.session_id != ace_session
    assert labs.session.project == "connect-labs"
    assert Session.objects.get(pk=ace_session).project == "ace", "the old row is not relabelled"


def test_a_legacy_binding_with_no_project_is_adopted_and_filled():
    """Rows written before the column existed carry a blank. They must still be
    recognised on the first report after deploy — a miss would fork a duplicate
    session for every live conversation at once — and then filled, so the hole
    closes behind them."""
    ws, runner = _ws_runner()
    replace_reported_sessions(runner, ws, [_reported("feat-x", [], project="canopy-web")])
    RunnerBinding.objects.update(emdash_project="")
    Session.objects.update(project="")
    before = RunnerBinding.objects.get().pk

    replace_reported_sessions(runner, ws, [_reported("feat-x", [], project="canopy-web")])

    binding = RunnerBinding.objects.get()
    assert binding.pk == before, "adopted, not forked"
    assert binding.emdash_project == "canopy-web"
    assert binding.session.project == "canopy-web", "and the stale label is repaired"


def test_an_agent_chat_keeps_its_blank_project():
    """`Session.project` is blank for an agent chat by constraint (you chat WITH an
    agent or IN a project, never both). The label repair must not violate that even
    though emdash runs the task under the agent's own repo."""
    from apps.agents.models import Agent

    ws, runner = _ws_runner()
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws)
    session = Session.objects.create(
        workspace=ws, agent=agent, origin=Session.ORIGIN_RUNNER, title="chat"
    )
    RunnerBinding.objects.create(
        session=session, runner=runner, host=runner.host,
        session_key="chat", emdash_project="hal",
    )

    replace_reported_sessions(runner, ws, [_reported("chat", [], project="hal")])

    session.refresh_from_db()
    assert session.project == ""
    assert session.agent_id == agent.id
    assert RunnerBinding.objects.count() == 1


def test_reporting_one_project_does_not_revive_anothers_archived_namesake():
    """The un-archive step keyed on `session_key__in=now_keys`, which a namesake in
    a different project satisfies."""
    ws, runner = _ws_runner()
    replace_reported_sessions(runner, ws, [_reported("issues", [], project="ace")])
    ace = RunnerBinding.objects.get(emdash_project="ace").session
    Session.objects.filter(pk=ace.pk).update(status=Session.ARCHIVED)

    replace_reported_sessions(runner, ws, [_reported("issues", [], project="connect-labs")])

    ace.refresh_from_db()
    assert ace.status == Session.ARCHIVED



def test_the_0021_backfill_copies_the_project_off_the_session():
    """The backfill is what makes the deploy a no-op: without it every live binding
    carries a blank on the first report, the exact-match lookup misses, and every
    conversation on the box forks a duplicate session at once.

    Driven through the real models (the migration module is imported by path — its
    name starts with a digit, so it is not importable as an identifier). The pass
    reads only fields a historical model also has.
    """
    import importlib.util
    from pathlib import Path

    from apps.agents.models import Agent

    import apps.canopy_sessions as pkg

    path = Path(pkg.__file__).parent / "migrations" / "0021_runnerbinding_emdash_project.py"
    spec = importlib.util.spec_from_file_location("m0021", path)
    m0021 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m0021)

    ws, runner = _ws_runner()
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws)
    repo_session = Session.objects.create(
        workspace=ws, project="connect-labs", origin=Session.ORIGIN_RUNNER, title="issues"
    )
    agent_session = Session.objects.create(
        workspace=ws, agent=agent, origin=Session.ORIGIN_RUNNER, title="chat"
    )
    bare_session = Session.objects.create(workspace=ws, origin=Session.ORIGIN_WEB, title="web")
    for session, key in ((repo_session, "issues"), (agent_session, "chat"), (bare_session, "web")):
        RunnerBinding.objects.create(
            session=session, runner=runner, host=runner.host, session_key=key
        )

    class _Apps:
        def get_model(self, app_label, model_name):
            return {"RunnerBinding": RunnerBinding}[model_name]

    m0021.backfill_emdash_project(_Apps(), None)

    by_key = {b.session_key: b.emdash_project for b in RunnerBinding.objects.all()}
    assert by_key["issues"] == "connect-labs"
    assert by_key["chat"] == "hal", "an agent chat's worktree lives under the agent's own repo"
    assert by_key["web"] == "", "neither agent nor project — the legacy branch still finds it"
