"""Creating an agent from the browser: check the repo, push the factory's scaffold, register.

GitHub is faked at the `requests` boundary, answering the way GitHub does for an EMPTY
repository — which is the part that is easy to get wrong: the Git Data API refuses an
empty repo (409), so the first commit has to come from the Contents API.

What is pinned:
- every file is the factory's, with its executable bit — nothing is a second copy;
- a repo canopy cannot see, or one with someone else's commits, is refused with a link
  to fix it, never pushed into;
- running it again adopts the agent's own scaffold instead of failing (retry-safe);
- the same gate as registering: a viewer cannot, a non-member gets a 404, a taken slug
  is refused;
- registering through the ordinary upsert makes the creator the owner — on create only;
- the setup checklist reads the system's own checks.
"""
from __future__ import annotations

import base64
import json
from unittest import mock

import pytest
from canopy_agent_factory import templates
from django.test import Client
from django.utils import timezone

from apps.agents import creation
from apps.agents.models import Agent, AgentSkill
from apps.agents.setup import scaffold_skill_names, setup_steps
from apps.harness.models import Runner, RunnerAssignment
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_member, a_workspace, enrolled

pytestmark = pytest.mark.django_db

REPO = "olive/scout"


class FakeGitHub:
    """One GitHub account's repos. `repos[name] = None` means canopy cannot see it."""

    def __init__(self, **repos):
        self.repos = {name: (state if state is None else {"files": {}, "modes": {}, **state})
                      for name, state in repos.items()}
        self.writes: list[str] = []

    def _resp(self, status, body=None):
        r = mock.Mock(status_code=status, ok=200 <= status < 300, text=json.dumps(body or {}))
        r.json.return_value = body if body is not None else {}
        r.raise_for_status.return_value = None
        return r

    def _repo(self, url):
        tail = url.split("/repos/", 1)[1]
        name = "/".join(tail.split("/")[:2])
        return name, self.repos.get(name), "/".join(tail.split("/")[2:])

    def get(self, url, headers=None, **_):
        if headers and headers.get("Authorization") != "Bearer github_pat_olive":
            return self._resp(401)
        if url.endswith("/user"):
            r = self._resp(200, {"login": "olive", "id": 7, "name": "Olive Owner"})
            r.headers = {}
            return r
        name, repo, rest = self._repo(url)
        if repo is None:
            return self._resp(404)
        if rest == "":
            return self._resp(200, {"default_branch": "main"})
        if rest.startswith("commits"):
            return self._resp(200, [{}]) if repo["files"] else self._resp(409, {"message": "Git Repository is empty."})
        path = rest.removeprefix("contents/")
        if path in repo["files"]:
            return self._resp(200, {"content": base64.b64encode(repo["files"][path].encode()).decode()})
        return self._resp(404)

    def request(self, method, url, json=None, **_):
        name, repo, rest = self._repo(url)
        self.writes.append(f"{method} {rest}")
        if rest.startswith("contents/"):
            assert not repo["files"], "the Contents API first commit is only for an empty repo"
            repo["files"][rest.removeprefix("contents/")] = base64.b64decode(json["content"]).decode()
            return self._resp(201, {"commit": {"sha": "c1", "tree": {"sha": "t1"}}})
        if rest == "git/trees":
            assert repo["files"], "the Git Data API refuses an empty repository"
            self._tree = json["tree"]
            return self._resp(201, {"sha": "t2"})
        if rest == "git/commits":
            assert json["parents"] == ["c1"]
            return self._resp(201, {"sha": "c2"})
        if rest == "git/refs/heads/main":
            for entry in self._tree:
                repo["files"][entry["path"]] = entry["content"]
                repo["modes"][entry["path"]] = entry["mode"]
            return self._resp(200, {})
        raise AssertionError(f"unexpected {method} {url}")


@pytest.fixture
def ws():
    return a_workspace("scouts")


@pytest.fixture
def editor(ws):
    user = a_member(ws, email="olive@dimagi.com", role=WorkspaceMembership.EDITOR)
    user.first_name, user.last_name = "Olive", "Owner"
    user.save()
    return user


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _github(fake):
    return mock.patch.multiple(creation.requests, get=fake.get, request=fake.request)


def _scaffold(client, **over):
    body = {"workspace": "scouts", "slug": "scout", "name": "Scout", "repo": REPO,
            "mandate": "Find good first issues for new contributors.", "mailbox": "scout@example.org",
            "token": "github_pat_olive"}
    body.update(over)
    return client.post("/api/agents/scaffold", body, content_type="application/json")


# ---- the push ------------------------------------------------------------------

def test_an_empty_repo_gets_exactly_the_factorys_scaffold(editor):
    fake = FakeGitHub(**{REPO: {}})
    with _github(fake):
        resp = _scaffold(_client(editor))
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body == {"repo": REPO, "repo_url": f"https://github.com/{REPO}", "repo_ref": "main",
                    "adopted": False, "files": len(templates())}
    files = fake.repos[REPO]["files"]
    assert set(files) == {p.replace("{{AGENT_SLUG}}", "scout") for p in templates()}
    assert json.loads(files[".claude-plugin/plugin.json"])["name"] == "scout"
    # The creator, not the factory's default author, is on the scaffold.
    assert "Olive Owner" in files[".claude-plugin/plugin.json"]
    modes = fake.repos[REPO]["modes"]
    assert modes["hooks/gating_guard.py"] == "100755"
    assert modes["bin/scout-email"] == "100755"
    assert modes["persona.md"] == "100644"


def test_running_it_again_adopts_the_scaffold_it_already_pushed(editor):
    fake = FakeGitHub(**{REPO: {}})
    with _github(fake):
        _scaffold(_client(editor))
        fake.writes.clear()
        again = _scaffold(_client(editor))
    assert again.status_code == 201
    assert again.json()["adopted"] is True
    assert fake.writes == []


def test_a_repo_the_token_cannot_see_is_refused_and_says_why(editor):
    with _github(FakeGitHub(**{REPO: None})):
        resp = _scaffold(_client(editor))
    assert resp.status_code == 409
    assert "token cannot see olive/scout" in resp.json()["title"]


def test_a_rejected_token_is_a_422_not_a_push(editor):
    fake = FakeGitHub(**{REPO: {}})
    with _github(fake):
        resp = _scaffold(_client(editor), token="github_pat_wrong")
    assert resp.status_code == 422
    assert fake.writes == []


def test_a_repo_with_someone_elses_commits_is_never_pushed_into(editor):
    fake = FakeGitHub(**{REPO: {"files": {"README.md": "# mine"}}})
    with _github(fake):
        resp = _scaffold(_client(editor))
    assert resp.status_code == 409
    assert fake.writes == []


def test_the_check_reports_each_state_without_writing(editor):
    fake = FakeGitHub(**{REPO: {}, "olive/other": None})
    c = _client(editor)

    def check(repo, token="github_pat_olive"):
        return c.post("/api/agents/scaffold/check", {"workspace": "scouts", "slug": "scout",
                                                     "repo": repo, "token": token},
                      content_type="application/json").json()

    with _github(fake):
        ready = check(REPO)
        missing = check("https://github.com/olive/other.git")
        no_token = check(REPO, token="")
    assert (ready["state"], ready["login"]) == ("ready", "olive")
    assert missing["state"] == "missing"
    assert missing["new_repo_url"].startswith("https://github.com/new?owner=olive&name=other")
    # The links come back before there is a token, so the page can offer them first.
    assert no_token["state"] == "no_token"
    assert no_token["token_url"].startswith("https://github.com/settings/personal-access-tokens/new?")
    assert "target_name=olive" in no_token["token_url"]
    assert fake.writes == []


# ---- who may ---------------------------------------------------------------------

def test_a_viewer_cannot_and_a_non_member_cannot_tell_the_workspace_exists(ws):
    viewer = a_member(ws, email="v@dimagi.com", role=WorkspaceMembership.VIEWER)
    outsider = a_member(a_workspace("elsewhere"), email="o@dimagi.com")
    with _github(FakeGitHub(**{REPO: {}})):
        assert _scaffold(_client(viewer)).status_code == 403
        assert _scaffold(_client(outsider)).status_code == 404


def test_a_taken_or_reserved_slug_is_refused(editor, ws):
    Agent.objects.create(slug="scout", name="Other", workspace=a_workspace("elsewhere"))
    with _github(FakeGitHub(**{REPO: {}})):
        assert _scaffold(_client(editor)).status_code == 409
        assert _scaffold(_client(editor), slug="new").status_code == 422
        assert _scaffold(_client(editor), slug="Not A Slug!").status_code == 422


# ---- registering ------------------------------------------------------------------

def test_registering_makes_the_creator_the_owner_on_create_only(editor, ws):
    c = _client(editor)
    resp = c.post("/api/agents/", {"slug": "scout", "name": "Scout", "workspace": "scouts",
                                   "repo_url": f"https://github.com/{REPO}", "repo_ref": "main"},
                  content_type="application/json")
    assert resp.status_code == 201, resp.content
    assert Agent.objects.get(slug="scout").owner == editor

    other = a_member(ws, email="w@dimagi.com", role=WorkspaceMembership.OWNER)
    _client(other).post("/api/agents/", {"slug": "scout", "name": "Scout 2"},
                        content_type="application/json")
    assert Agent.objects.get(slug="scout").owner == editor


# ---- the checklist -----------------------------------------------------------------

def _agent(ws, owner, **over):
    return Agent.objects.create(slug="scout", name="Scout", workspace=ws, owner=owner,
                                repo_url=f"https://github.com/{REPO}", email="scout@example.org", **over)


def _status(agent):
    return {s["key"]: s["status"] for s in setup_steps(agent)}


def test_a_new_agent_has_everything_but_its_repo_left_to_do(editor, ws):
    steps = setup_steps(_agent(ws, editor))
    assert [s["key"] for s in steps] == ["repo", "github", "mailbox", "runner", "job", "first_turn"]
    assert {s["key"]: s["status"] for s in steps} == {
        "repo": "done", "github": "todo", "mailbox": "todo", "runner": "todo",
        "job": "todo", "first_turn": "todo"}
    job = next(s for s in steps if s["key"] == "job")
    assert job["where"] == "claude-code"
    assert next(s for s in steps if s["key"] == "github")["action"] == "Add a GitHub token"
    assert job["command"].startswith(
        f"git clone https://github.com/{REPO} ~/emdash-projects/scout && cd ~/emdash-projects/scout && claude ")


def test_no_mailbox_is_optional_not_missing(editor, ws):
    agent = _agent(ws, editor)
    agent.email = ""
    assert _status(agent)["mailbox"] == "optional"


def test_the_scaffolds_own_skills_are_not_a_job(editor, ws):
    agent = _agent(ws, editor)
    assert "turn" in scaffold_skill_names()
    AgentSkill.objects.create(agent=agent, name="turn")
    assert _status(agent)["job"] == "todo"
    AgentSkill.objects.create(agent=agent, name="find-issues")
    assert _status(agent)["job"] == "done"


def test_the_runner_step_follows_routing_and_who_may_hold_the_agent(editor, ws):
    agent = _agent(ws, editor)
    box = enrolled(Runner.objects.create(name="olive-cloud", kind=Runner.CLOUD, owner=editor,
                                         status=Runner.ONLINE, last_heartbeat_at=timezone.now()), ws)
    assert _status(agent)["runner"] == "todo"  # nothing routes to it yet
    RunnerAssignment.objects.create(agent=agent, runner=box, rank=0)
    assert _status(agent)["runner"] == "done"

    # A box whose owner is not one of the agent's admins cannot hold it, routed or not.
    agent.owner = a_member(ws, email="z@dimagi.com", role=WorkspaceMembership.EDITOR)
    agent.save()
    assert _status(agent)["runner"] == "todo"


def test_the_setup_route_is_readable_by_any_member(editor, ws):
    _agent(ws, editor)
    viewer = a_member(ws, email="v@dimagi.com", role=WorkspaceMembership.VIEWER)
    resp = _client(viewer).get("/api/agents/scout/setup")
    assert resp.status_code == 200
    assert resp.json()[0]["key"] == "repo"
