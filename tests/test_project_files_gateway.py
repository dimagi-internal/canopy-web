"""A project's published files reach a session through the projects connection, and only
then: the card lists them, `project_skill` reads a skill kept as files, `project_file`
reads one. Driven the way Claude Code drives it (`tests/fixtures/project_gateway.py`)."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from apps.agents import project_files as pf
from apps.agents.models import AgentProject
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.project_gateway import _text, _token, run_sessions

# Committed: the gateway's calls run on a worker thread with their own connection.
pytestmark = pytest.mark.django_db(transaction=True)

SKILL = "---\nname: intacct-login\ndescription: Sign in to Intacct read-only\n---\nRun login.py.\n"
SCRIPT = "print('hello from intacct')\n"


@pytest.fixture
def world():
    users = get_user_model().objects
    lead = users.create_user("lead", "lead@dimagi.com", "pw")
    stranger = users.create_user("stranger", "s@else.org", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=lead)
    WorkspaceMembership.objects.create(workspace=ws, user=lead, role=WorkspaceMembership.OWNER)
    other = Workspace.objects.create(slug="else", display_name="Else", created_by=stranger)
    WorkspaceMembership.objects.create(workspace=other, user=stranger, role=WorkspaceMembership.OWNER)
    project = AgentProject.objects.create(name="Intacct", workspace=ws, handle="intacct", owner_user=lead)
    return {"lead": lead, "stranger": stranger, "project": project}


def _publish(world, files):
    rev = pf.create_draft(world["project"], base=0, message="tooling", by=world["lead"],
                          changes=[{"path": p, "content": c} for p, c in files.items()])
    return pf.publish(world["project"], rev.number, by=world["lead"])


def test_a_draft_reaches_nobody_until_it_is_published(world):
    pf.create_draft(world["project"], base=0, message="wip", by=world["lead"],
                    changes=[{"path": "skills/intacct-login/SKILL.md", "content": SKILL}])

    async def script(new_session):
        call = await new_session()
        return (await call("tools/call", {"name": "projects", "arguments": {}}))[0]

    assert "dimagi/intacct" not in _text(run_sessions(_token(world["lead"]), script))


def test_published_files_are_on_the_card_and_read_on_use(world):
    rev = _publish(world, {"skills/intacct-login/SKILL.md": SKILL, "skills/intacct-login/login.py": SCRIPT})

    async def script(new_session):
        call = await new_session()
        listed, _ = await call("tools/call", {"name": "projects", "arguments": {}})
        card, _ = await call("tools/call", {"name": "open_project", "arguments": {"project": "dimagi/intacct"}})
        skill, _ = await call("tools/call", {"name": "project_skill",
                                             "arguments": {"project": "dimagi/intacct", "skill": "intacct-login"}})
        file, _ = await call("tools/call", {"name": "project_file", "arguments": {
            "project": "dimagi/intacct", "path": "skills/intacct-login/login.py"}})
        missing, _ = await call("tools/call", {"name": "project_file", "arguments": {
            "project": "dimagi/intacct", "path": "skills/intacct-login/nope.py"}})
        return listed, card, skill, file, missing

    listed, card, skill, file, missing = run_sessions(_token(world["lead"]), script)
    assert "dimagi/intacct — Intacct — 1 skill, 2 files" in _text(listed)
    text = _text(card)
    assert "- intacct-login — Sign in to Intacct read-only" in text
    assert f"Files (revision {rev.number};" in text and "- skills/intacct-login/login.py" in text
    assert "Run login.py." in _text(skill) and "- skills/intacct-login/login.py" in _text(skill)
    assert _text(file) == SCRIPT
    assert missing.get("isError") and "has no file" in _text(missing)


def test_a_stranger_cannot_open_a_projects_files(world):
    _publish(world, {"skills/intacct-login/SKILL.md": SKILL})

    async def script(new_session):
        call = await new_session()
        return (await call("tools/call", {"name": "project_file", "arguments": {
            "project": "dimagi/intacct", "path": "skills/intacct-login/SKILL.md"}}))[0]

    refused = run_sessions(_token(world["stranger"]), script)
    assert refused.get("isError") and "no project" in _text(refused)
