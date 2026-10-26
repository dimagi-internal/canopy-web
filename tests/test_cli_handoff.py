"""A transfer may continue a conversation on another agent CLI (`placement.transfer_session`
`cli=`): the way off a box whose Claude ran out, onto Codex, or back. Nothing resumes
across CLIs — the target starts the new CLI fresh from the handoff's prompt, and the
conversation is held to the new CLI from then on."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import placement
from apps.canopy_sessions.models import Message, RunnerBinding, Session
from apps.harness import clis
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

CONFINING = {"signed_in": True, "profiles": 3, "envelope": 2}


@pytest.fixture
def ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=ws, owner=user,
                                 clis=["claude-code", "codex"])
    now = timezone.now()

    def box(name, report):
        return Runner.objects.create(name=name, workspace=ws, kind=Runner.CLOUD, owner=user,
                                     status=Runner.ONLINE, last_heartbeat_at=now, host=name,
                                     capabilities={"sessions": True, "clis": report})
    claude_box = box("claude-box", {"claude-code": CONFINING})
    codex_box = box("codex-box", {"codex": CONFINING})
    s = Session.objects.create(workspace=ws, agent=agent, title="t", created_by=user,
                               cli="claude-code")
    RunnerBinding.objects.create(session=s, runner=claude_box, session_key="k",
                                 transcript_id="claude-uuid", thread_key=str(s.id))
    Message.objects.create(session=s, turn_index=0, role=Message.ASSISTANT, plaintext="hi")
    # Nothing to pack: the move settles at once.
    claude_box.last_heartbeat_at = now - dt.timedelta(hours=2)
    claude_box.save(update_fields=["last_heartbeat_at"])
    c = Client()
    c.force_login(user)
    return SimpleNamespace(user=user, agent=agent, s=s, claude_box=claude_box,
                           codex_box=codex_box, c=c)


def test_moving_to_a_box_without_the_cli_names_the_way_through(ctx):
    with pytest.raises(ValueError) as err:
        placement.transfer_session(session=ctx.s, placement=str(ctx.codex_box.id), user=ctx.user)
    assert "runs on Claude Code, and codex-box does not have it" in str(err.value)
    assert "cli=codex" in str(err.value)


def test_a_move_to_codex_starts_codex_fresh_and_holds_the_conversation_there(ctx):
    _binding, turn = placement.transfer_session(session=ctx.s, placement=str(ctx.codex_box.id),
                                                user=ctx.user, cli="codex")
    h = ctx.s.handoffs.get()
    assert h.to_cli == "codex" and not h.want_transcript and not h.resume_planned
    ctx.s.refresh_from_db()
    assert (ctx.s.cli, ctx.s.cli_session_id) == ("codex", "")
    assert turn.requested_cli == "codex" and turn.pinned_runner_id == ctx.codex_box.id
    assert clis.allowed(turn) == ["codex"]
    assert "It also moved to Codex from Claude Code" in turn.prompt
    assert "FRESH session" in turn.prompt


def test_a_cli_the_agent_lacks_is_refused(ctx):
    Agent.objects.filter(pk=ctx.agent.pk).update(clis=["claude-code"])
    ctx.s.agent.refresh_from_db()
    with pytest.raises(ValueError, match="isn't set up to run on Codex"):
        placement.transfer_session(session=ctx.s, placement=str(ctx.codex_box.id),
                                   user=ctx.user, cli="codex")


def test_a_sensitive_conversation_never_moves_off_claude_code(ctx, monkeypatch):
    from apps.harness import sensitivity

    monkeypatch.setattr(sensitivity, "is_sensitive", lambda _s: True)
    assert placement.cli_switch_refusal(ctx.s, "codex").startswith("a conversation holding")


def test_the_transfer_route_takes_the_cli(ctx):
    r = ctx.c.post(f"/api/w/w1/canopy-sessions/{ctx.s.id}/transfer",
                   data={"runner": "codex-box", "cli": "codex"}, content_type="application/json")
    assert r.status_code == 200, r.content
    ctx.s.refresh_from_db()
    assert ctx.s.cli == "codex"


def test_transfer_all_dry_run_says_what_a_cli_switch_would_do(ctx):
    body = {"from_runner": "claude-box", "to_runner": "codex-box", "dry_run": True}
    r = ctx.c.post("/api/w/w1/canopy-sessions/transfer-all", data=body,
                   content_type="application/json")
    (row,) = r.json()["results"]
    assert row["status"] == "refused" and "cli=codex" in row["error"]
    r = ctx.c.post("/api/w/w1/canopy-sessions/transfer-all", data={**body, "cli": "codex"},
                   content_type="application/json")
    (row,) = r.json()["results"]
    assert row["status"] == "would_move", row
