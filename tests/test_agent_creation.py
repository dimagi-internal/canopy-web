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
from apps.agents.models import Agent, AgentBootstrapReport, AgentSkill
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
        self.commits: list[dict] = []

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
            if not repo["files"]:
                return self._resp(409, {"message": "Git Repository is empty."})
            return self._resp(200, [{"sha": "h1", "commit": {"tree": {"sha": "t0"}}}])
        if rest.startswith("git/trees/t0"):
            return self._resp(200, {"tree": [{"path": p, "type": "blob"} for p in repo["files"]]})
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
            self.commits.append(json)
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


def test_a_repo_that_is_not_an_agent_is_never_written_into(editor):
    fake = FakeGitHub(**{REPO: {"files": {"README.md": "# mine", "src/app.py": "print(1)"}}})
    with _github(fake):
        resp = _scaffold(_client(editor))
    assert resp.status_code == 409
    assert "not an agent repository" in resp.json()["title"]
    assert fake.writes == []


def test_a_repo_made_with_a_readme_is_filled_in_and_keeps_its_readme(editor):
    """Ticking GitHub's "Add a README" is the commonest way a repo stops being empty."""
    fake = FakeGitHub(**{REPO: {"files": {"README.md": "# Scout, by Olive"}}})
    with _github(fake):
        resp = _scaffold(_client(editor))
    assert resp.status_code == 201, resp.content
    files = fake.repos[REPO]["files"]
    assert files["README.md"] == "# Scout, by Olive"
    assert set(files) == {p.replace("{{AGENT_SLUG}}", "scout") for p in templates()}
    assert resp.json()["files"] == len(templates()) - 1
    # One commit on top of what was there; the Contents API is only for empty repos.
    assert [c["parents"] for c in fake.commits] == [["h1"]]
    assert not any(w.startswith("PUT contents/") for w in fake.writes)


def test_an_earlier_attempt_is_completed_and_nothing_it_wrote_is_overwritten(editor):
    """Someone ran /canopy:create-agent (or this page) before and stopped partway, or
    already wrote a persona: only the missing template files are added."""
    earlier = {".claude-plugin/plugin.json": json.dumps({"name": "scout"}),
               "persona.md": "Scout is curious and terse.", "skills/find-issues/SKILL.md": "x"}
    fake = FakeGitHub(**{REPO: {"files": dict(earlier)}})
    c = _client(editor)
    with _github(fake):
        check = c.post("/api/agents/scaffold/check", {"workspace": "scouts", "slug": "scout",
                                                      "repo": REPO, "token": "github_pat_olive"},
                       content_type="application/json").json()
        resp = _scaffold(c)
    assert check["state"] == "update"
    assert check["files_to_add"] == len(templates()) - 2
    assert resp.status_code == 201, resp.content
    files = fake.repos[REPO]["files"]
    for path, text in earlier.items():
        assert files[path] == text  # kept exactly
    assert "hooks/gating_guard.py" in files and fake.repos[REPO]["modes"]["hooks/gating_guard.py"] == "100755"


def test_another_agents_repo_is_refused_and_names_its_slug(editor):
    fake = FakeGitHub(**{REPO: {"files": {".claude-plugin/plugin.json": json.dumps({"name": "ranger"})}}})
    c = _client(editor)
    with _github(fake):
        check = c.post("/api/agents/scaffold/check", {"workspace": "scouts", "slug": "scout",
                                                      "repo": REPO, "token": "github_pat_olive"},
                       content_type="application/json").json()
        resp = _scaffold(c)
    assert (check["state"], check["suggested_slug"]) == ("name_mismatch", "ranger")
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
    assert [s["key"] for s in steps] == ["repo", "github", "mailbox", "vault", "runner", "job",
                                         "first_turn"]
    assert {s["key"]: s["status"] for s in steps} == {
        "repo": "done", "github": "todo", "mailbox": "todo", "vault": "todo", "runner": "todo",
        "job": "todo", "first_turn": "todo"}
    job = next(s for s in steps if s["key"] == "job")
    assert job["where"] == "claude-code"
    assert next(s for s in steps if s["key"] == "github")["action"] == "Add a GitHub token"
    # Where the prompt box actually is.
    assert next(s for s in steps if s["key"] == "first_turn")["href"].endswith("/agents/scout/tasks")
    assert job["command"].startswith(
        f"git clone https://github.com/{REPO} ~/emdash-projects/scout && cd ~/emdash-projects/scout && claude ")


def test_a_turn_through_its_chat_counts_as_its_first_turn(editor, ws):
    """An email or Slack turn carries the agent on its session, not on itself — and
    it is still the agent's turn (`agent_turns_q`, what its Turns page lists)."""
    from apps.canopy_sessions.models import Session
    from apps.harness.models import Turn

    agent = _agent(ws, editor)
    assert _status(agent)["first_turn"] == "todo"
    session = Session.objects.create(workspace=ws, agent=agent, title="mail")
    Turn.objects.create(chat_session=session, agent=None, origin=Turn.ORIGIN_EMAIL, status=Turn.DONE,
                        prompt="hi", idempotency_key="mail-1")
    assert _status(agent)["first_turn"] == "done"


def test_no_mailbox_is_optional_not_missing(editor, ws):
    agent = _agent(ws, editor)
    agent.email = ""
    assert _status(agent)["mailbox"] == "optional"


def test_a_mailbox_a_box_has_proved_is_done_without_a_stored_token(editor, ws):
    """Tokens minted by a laptop `gog login` live in the vault, not here; the box that runs
    the turns is the one that knows the mailbox works (`turn_ready`)."""
    agent = _agent(ws, editor)
    AgentBootstrapReport.objects.create(agent=agent, runner_name="cloud-ec2-2", turn_ready=False)
    assert _status(agent)["mailbox"] == "todo"
    AgentBootstrapReport.objects.filter(agent=agent).update(turn_ready=True)
    mailbox = next(s for s in setup_steps(agent) if s["key"] == "mailbox")
    assert mailbox["status"] == "done" and "cloud-ec2-2 reads it for turns" in mailbox["detail"]


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
    # Routed, but a cloud box has not cloned it until its bootstrap reports so.
    assert _status(agent)["runner"] == "waiting"
    AgentBootstrapReport.objects.create(agent=agent, runner_name="olive-cloud")
    assert _status(agent)["runner"] == "done"

    # A box whose owner is not one of the agent's admins cannot hold it, routed or not.
    agent.owner = a_member(ws, email="z@dimagi.com", role=WorkspaceMembership.EDITOR)
    agent.save()
    assert _status(agent)["runner"] == "todo"


def test_the_vault_step_is_one_terminal_command_and_reads_what_the_box_reported(editor, ws):
    """Creating a vault needs a person's 1Password rights, which nothing here holds — so
    the step is the command, not a link. Done means the box could READ it (`env_ok`), not
    merely that a key is stored: a stored key that cannot read its vault is the failure
    that sat in journald for two weeks (2026-09-22)."""
    from apps.agents import services

    agent = _agent(ws, editor)
    vault = next(s for s in setup_steps(agent) if s["key"] == "vault")
    assert (vault["status"], vault["where"]) == ("todo", "terminal")
    assert vault["command"] == "canopy agent vault --slug scout --workspace scouts --vault <vault name>"
    assert "no 1Password vault" in vault["detail"]

    services.set_agent_vault(agent, vault="Agent-Scout")
    vault = next(s for s in setup_steps(agent) if s["key"] == "vault")
    assert "has no key stored here" in vault["detail"]
    assert vault["command"] == "canopy agent vault --slug scout --workspace scouts"

    AgentBootstrapReport.objects.create(agent=agent, runner_name="olive-cloud", env_ok=False,
                                        detail="op inject: vault not found")
    services.set_agent_vault(agent, service_key="ops_key")
    # Storing a key wipes what a box said about the old one — not yet reported is not failed.
    assert AgentBootstrapReport.objects.get(agent=agent).env_ok is None
    assert _status(agent)["vault"] == "done"

    AgentBootstrapReport.objects.filter(agent=agent).update(env_ok=False, detail="403 Forbidden")
    vault = next(s for s in setup_steps(agent) if s["key"] == "vault")
    assert vault["status"] == "todo"
    assert "olive-cloud could not read its secrets from Agent-Scout: 403 Forbidden" in vault["detail"]


def test_the_setup_route_is_readable_by_any_member(editor, ws):
    _agent(ws, editor)
    viewer = a_member(ws, email="v@dimagi.com", role=WorkspaceMembership.VIEWER)
    resp = _client(viewer).get("/api/agents/scout/setup")
    assert resp.status_code == 200
    assert resp.json()[0]["key"] == "repo"


def test_registering_a_new_agent_asks_its_cloud_box_to_set_it_up(editor, ws):
    """A cloud box learns of an agent only by re-running its bootstrap; without
    this ask, a new agent's first turn ran on the box with none of its code."""
    box = enrolled(Runner.objects.create(name="olive-cloud", kind=Runner.CLOUD, owner=editor,
                                         status=Runner.ONLINE, last_heartbeat_at=timezone.now()), ws)
    from apps.harness.models import WorkspaceRunnerOrder

    WorkspaceRunnerOrder.objects.create(workspace=ws, runner=box, rank=0, enabled=True)
    assert box.refresh_requested_at is None
    resp = _client(editor).post("/api/agents/", {"slug": "scout", "name": "Scout", "workspace": "scouts",
                                                 "repo_url": f"https://github.com/{REPO}"},
                                content_type="application/json")
    assert resp.status_code == 201, resp.content
    box.refresh_from_db()
    assert box.refresh_requested_at is not None


def test_the_runner_step_offers_pairing_to_whoever_could_hold_the_agent(editor, ws):
    """No runner can take it: an admin of the agent (whose own box could hold it) gets
    links to pair a laptop or stand up a cloud box, beside "Pick a runner". A viewer's
    box could never hold the agent (`runner_may_hold_agent`), so it gets none."""
    agent = _agent(ws, editor)
    runner = next(s for s in setup_steps(agent, viewer=editor) if s["key"] == "runner")
    assert runner["status"] == "todo" and runner["action"] == "Pick a runner"
    assert runner["links"] == [
        {"href": "/get-started#runner", "action": "Pair your laptop"},
        {"href": "https://github.com/dimagi-internal/canopy-web/blob/main/runner/ec2/README.md"
                 "#lifecycle--commands-only", "action": "Stand up a cloud box"},
    ]
    viewer = a_member(ws, email="v@dimagi.com", role=WorkspaceMembership.VIEWER)
    assert next(s for s in setup_steps(agent, viewer=viewer) if s["key"] == "runner")["links"] == []
    assert next(s for s in setup_steps(agent) if s["key"] == "runner")["links"] == []

    # Through the route, as the caller.
    steps = _client(editor).get("/api/agents/scout/setup").json()
    assert [link["action"] for link in next(s for s in steps if s["key"] == "runner")["links"]] == [
        "Pair your laptop", "Stand up a cloud box"]

    # Once a runner takes it, there is nothing to pair.
    box = enrolled(Runner.objects.create(name="olive-cloud", kind=Runner.CLOUD, owner=editor,
                                         status=Runner.ONLINE, last_heartbeat_at=timezone.now()), ws)
    RunnerAssignment.objects.create(agent=agent, runner=box, rank=0)
    AgentBootstrapReport.objects.create(agent=agent, runner_name="olive-cloud")
    assert next(s for s in setup_steps(agent, viewer=editor) if s["key"] == "runner")["links"] == []


def _zk_agent(ws, owner):
    """A new agent as creation makes it: zero-knowledge, on the canopy-web vault, at kv 0."""
    return _agent(ws, owner, zk_enabled=True, zk_kv=0, credential_source=Agent.CANOPY_WEB)


def test_a_zero_knowledge_agent_sets_up_its_vault_before_google(editor, ws):
    """Connecting Google seals the token to whoever can open the vault; with nobody yet
    the mint ends in `zk-no-recipients`. So the vault comes first."""
    steps = setup_steps(_zk_agent(ws, editor))
    assert [s["key"] for s in steps] == ["repo", "github", "vault", "canopy_login", "mailbox", "runner",
                                         "job", "first_turn"]


def test_a_vault_nobody_can_open_is_not_done(editor, ws):
    from apps.passkeys.models import UserVaultKeyset

    agent = _zk_agent(ws, editor)
    vault = next(s for s in setup_steps(agent) if s["key"] == "vault")
    assert vault["status"] == "todo"
    assert vault["href"] == "/settings#vault-key" and vault["action"] == "Set up your vault key"
    assert "passkey" in vault["detail"] and "Google" in vault["detail"]

    # An admin of the agent with a vault key can open it.
    UserVaultKeyset.objects.create(user=editor, public_jwk={"kty": "EC", "crv": "P-256", "x": "x", "y": "y"})
    assert _status(agent)["vault"] == "done"


def test_a_vault_open_only_to_a_non_admin_still_has_nobody(editor, ws):
    from apps.passkeys.models import UserVaultKeyset

    agent = _zk_agent(ws, editor)
    viewer = a_member(ws, email="v@dimagi.com", role=WorkspaceMembership.VIEWER)
    UserVaultKeyset.objects.create(user=viewer, public_jwk={"kty": "EC", "crv": "P-256", "x": "x", "y": "y"})
    assert _status(agent)["vault"] == "todo"


def test_an_agent_that_is_not_zero_knowledge_keeps_google_before_its_vault(editor, ws):
    agent = _agent(ws, editor, credential_source=Agent.CANOPY_WEB)
    keys = [s["key"] for s in setup_steps(agent)]
    assert keys.index("mailbox") < keys.index("vault")
