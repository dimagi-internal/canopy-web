"""The real `git` binary against canopy-web's live server: clone, fetch, push refused.

Nothing is stubbed between git and the view: the request goes through the whole
middleware stack with sign-in required (`REQUIRE_AUTH`), so this is what proves the
endpoint is admitted by `LoginRequiredMiddleware`, needs no CSRF token, answers git's
Basic challenge, streams a pack git accepts, negotiates an incremental fetch, and turns
a push away with words git prints. The token travels in a header (`http.extraHeader`),
the way the canopy CLI sends it, and never lands in `.git/config` or the logs.
"""
from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import uuid

import pytest
from canopy_sdk import agent_definition as sdk
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agent_definitions import services
from apps.agents import definition_store as store
from apps.agents.models import Agent, AgentDefinitionRevision
from apps.harness.models import Runner, RunnerAccessToken, RunnerAssignment, RunnerDeviceKey
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.token_helpers import pat_token

pytestmark = [pytest.mark.django_db(transaction=True),
              pytest.mark.skipif(shutil.which("git") is None, reason="needs the git binary")]

User = get_user_model()
SLUG = "trailhead"


@pytest.fixture
def world(live_server, settings, tmp_path):
    settings.REQUIRE_AUTH = True
    settings.CANOPY_PUBLIC_BASE_URL = live_server.url
    owner = User.objects.create_user("olive", "olive@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="trails", display_name="Trails", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    agent = services.create_stored_agent(slug=SLUG, name="Trailhead", mandate="Keeps the trails.",
                                         stakeholders="", workspace=ws, owner=owner, by=owner)
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C"}
    return {"url": store.git_url(agent), "agent": agent, "owner": owner, "ws": ws, "env": env, "tmp": tmp_path}


def _git(world, *args, token: str = "", basic: bool = False, check: bool = True, cwd=None):
    auth = []
    if token:
        value = ("Basic " + base64.b64encode(f"x:{token}".encode()).decode()) if basic else f"Bearer {token}"
        auth = ["-c", f"http.extraHeader=Authorization: {value}"]
    r = subprocess.run(["git", *auth, *args], cwd=cwd, env=world["env"], capture_output=True, text=True,
                       timeout=120)
    if check:
        assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r


def _publish_a_change(world, text: str) -> AgentDefinitionRevision:
    agent = Agent.objects.get(slug=SLUG)
    rev = services.create_draft(agent, base=agent.live_revision.number,
                                changes=[{"path": "skills/trail/SKILL.md",
                                          "content": f"---\nname: trail\n---\n{text}\n".encode()}],
                                message=f"trail: {text}", by=world["owner"], by_agent=False)
    return services.publish(agent, rev.number, by=world["owner"])


def test_a_person_clones_fetches_and_cannot_push(world, caplog):
    token = pat_token(world["owner"])
    dest = world["tmp"] / "clone"
    with caplog.at_level(logging.DEBUG):
        _git(world, "clone", "-q", world["url"], str(dest), token=token)
    agent = world["agent"]
    live = agent.live_revision
    # The working tree is the live revision, file for file, and main is its commit.
    files = [{"path": str(p.relative_to(dest)), "content": p.read_bytes(), "executable": os.access(p, os.X_OK)}
             for p in sorted(dest.rglob("*")) if p.is_file() and ".git" not in p.relative_to(dest).parts]
    assert sdk.manifest_root_of_files(files) == live.root_sha256
    assert _git(world, "rev-parse", "HEAD", cwd=dest).stdout.strip() == live.commit
    assert _git(world, "tag", cwd=dest).stdout.split() == ["r1"]
    # The token is nowhere git keeps anything.
    assert token not in (dest / ".git" / "config").read_text()

    # An incremental fetch after a publish: haves negotiated, only the new commit sent.
    two = _publish_a_change(world, "walk it")
    with caplog.at_level(logging.DEBUG):
        _git(world, "fetch", "-q", "--tags", "origin", token=token, basic=True, cwd=dest)
    assert _git(world, "rev-parse", "origin/main", cwd=dest).stdout.strip() == two.commit
    assert _git(world, "log", "--format=%s", "origin/main", cwd=dest).stdout.split("\n")[0] == "trail: walk it"
    assert set(_git(world, "tag", cwd=dest).stdout.split()) == {"r1", "r2"}

    # A push is refused, with words git prints.
    (dest / "CLAUDE.md").write_text("changed\n")
    _git(world, "-c", "user.email=o@o", "-c", "user.name=o", "commit", "-qam", "mine", cwd=dest)
    with caplog.at_level(logging.DEBUG):
        push = _git(world, "push", "origin", "HEAD:main", token=token, check=False, cwd=dest)
    assert push.returncode != 0
    assert "pushes are refused" in push.stderr and "canopy agent definition edit" in push.stderr
    assert Agent.objects.get(slug=SLUG).live_revision.commit == two.commit

    assert token not in caplog.text


def test_without_a_token_git_is_asked_for_one(world):
    r = _git(world, "clone", "-q", world["url"], str(world["tmp"] / "nope"), check=False)
    assert r.returncode != 0
    # GIT_TERMINAL_PROMPT=0: git wanted credentials (a 401 with a Basic challenge), and
    # was not redirected to a sign-in page.
    assert "could not read Username" in r.stderr or "Authentication failed" in r.stderr, r.stderr


def test_a_runner_clones_with_its_own_token_and_sees_no_draft(world):
    agent = world["agent"]
    runner = Runner.objects.create(name=f"box-{uuid.uuid4().hex[:6]}", kind=Runner.CLOUD, owner=world["owner"],
                                   workspace=world["ws"], last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
    key = RunnerDeviceKey.objects.create(runner=runner, kind=RunnerDeviceKey.SOFTWARE, jkt=uuid.uuid4().hex,
                                         status=RunnerDeviceKey.ACTIVE)
    raw, _ = RunnerAccessToken.mint(key)
    services.create_draft(agent, base=1, changes=[{"path": "README.md", "content": b"draft\n"}],
                          message="a draft", by=world["owner"], by_agent=False)
    dest = world["tmp"] / "box"
    _git(world, "clone", "-q", world["url"], str(dest), token=raw)
    heads = _git(world, "ls-remote", world["url"], token=raw).stdout
    assert "refs/heads/main" in heads and "refs/heads/draft" not in heads
    assert _git(world, "rev-parse", "HEAD", cwd=dest).stdout.strip() == agent.live_revision.commit
