"""An agent's GitHub identity is its OWNER's, lent to that one agent (#747).

What these pin, in the order a turn meets them:

- only the agent's owner can lend it their identity, and a token that cannot
  open a pull request on the agent's repo is refused, never stored;
- the delegation in force is the CURRENT owner's — transfer the agent and the
  old owner's identity stops travelling with it;
- a runner gets a token only for a turn it has claimed and is executing, and
  canopy — not the runner — decides whose;
- there is no fallback: no delegation, an expired one, or a turn with no agent
  is a refusal that says why;
- the box's readiness check and the bootstrap resolve route read the same rule.

GitHub is never called: `requests` is replaced by a fake that answers like
GitHub does for a fine-grained token.
"""
from __future__ import annotations

import datetime as dt
import uuid
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents import delegations
from apps.agents.models import Agent, AgentDelegation
from apps.common.encryption import decrypt_secret
from apps.harness import services
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db

EXPIRY = "2026-12-01 10:00:00 UTC"


class FakeGitHub:
    """Answers `/user` and the two probes like GitHub does. `pr` / `push` map a
    repo to a probe's status code: 422 allowed, 403 no permission, 404 unseen.
    Push defaults to whatever the PR probe says, as it does for a token that
    has both permissions or neither."""

    def __init__(self, *, login="olive", pr=None, push=None, user_status=200, expiry=EXPIRY):
        self.login, self.pr, self.user_status, self.expiry = login, pr or {}, user_status, expiry
        self.push = push or {}
        self.probed: list[str] = []

    def _resp(self, status, body=None, headers=None):
        r = mock.Mock(status_code=status, headers=headers or {})
        r.json.return_value = body or {}
        return r

    def get(self, url, **_):
        assert url.endswith("/user")
        return self._resp(self.user_status, {"login": self.login, "id": 4242, "name": "Olive Owner"},
                          {"github-authentication-token-expiration": self.expiry} if self.expiry else {})

    def post(self, url, json=None, **_):
        path = url.split("/repos/", 1)[1]
        if path.endswith("/git/refs"):
            repo = path.rsplit("/git/refs", 1)[0]
            # A ref at a commit that cannot exist: nothing is ever created.
            assert json["ref"].startswith("refs/heads/canopy-permission-probe/")
            assert json["sha"] == "0" * 39 + "1"
            return self._resp(self.push.get(repo, self.pr.get(repo, 404)))
        repo = path.rsplit("/pulls", 1)[0]
        self.probed.append(repo)
        assert json["head"].startswith("canopy-permission-probe/")  # never a real branch
        return self._resp(self.pr.get(repo, 404))


@pytest.fixture
def github():
    fake = FakeGitHub(pr={"dimagi-internal/echo": 422})
    with mock.patch.object(delegations.requests, "get", fake.get), \
            mock.patch.object(delegations.requests, "post", fake.post):
        yield fake


@pytest.fixture
def owner():
    return User.objects.create_user("olive", "olive@dimagi.com", "pw", first_name="Olive")


@pytest.fixture
def agent(owner):
    ws = a_workspace()
    wsvc.ensure_member(ws, owner, WorkspaceMembership.OWNER)
    return Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=owner,
                                repo_url="git@github.com:dimagi-internal/echo.git")


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _lend(agent, owner, github, token="github_pat_olive"):
    return delegations.set_github(agent, owner, token)


# ---- lending ------------------------------------------------------------------

def test_owner_lends_a_working_token_and_it_is_stored_encrypted(agent, owner, github):
    row = _lend(agent, owner, github)
    assert row.secret_enc != "github_pat_olive"
    assert decrypt_secret(row.secret_enc) == "github_pat_olive"
    assert row.meta["login"] == "olive"
    assert row.meta["checks"] == [{"repo": "dimagi-internal/echo", "ok": True,
                                   "detail": "can push and open pull requests"}]
    assert row.expires_at == dt.datetime(2026, 12, 1, 10, 0, tzinfo=dt.UTC)


@pytest.mark.parametrize("code,says", [
    (403, "Pull requests"),       # reaches the repo, cannot open a PR (the #747 PAT)
    (404, "Resource owner"),      # the form's resource owner fell back to a person
])
def test_a_token_that_cannot_open_a_pull_request_is_refused_not_stored(agent, owner, github, code, says):
    github.pr["dimagi-internal/echo"] = code
    with pytest.raises(delegations.DelegationError, match=says):
        _lend(agent, owner, github)
    assert not AgentDelegation.objects.exists()


def test_a_token_that_can_open_prs_but_not_push_is_refused(agent, owner, github):
    # Pull requests: write without Contents: write — it could never push the
    # branch the pull request would come from.
    github.push["dimagi-internal/echo"] = 403
    with pytest.raises(delegations.DelegationError, match="Contents"):
        _lend(agent, owner, github)
    assert not AgentDelegation.objects.exists()


def test_a_token_github_rejects_is_refused(agent, owner, github):
    github.user_status = 401
    with pytest.raises(delegations.DelegationError, match="rejected"):
        _lend(agent, owner, github)


def test_only_the_owner_can_lend(agent, github):
    admin = User.objects.create_user("adam", "adam@dimagi.com", "pw")
    with pytest.raises(delegations.DelegationError, match="owner"):
        delegations.set_github(agent, admin, "github_pat_adam")
    assert not AgentDelegation.objects.exists()


def test_the_route_refuses_with_githubs_reason_and_never_echoes_the_token(agent, owner, github):
    github.pr["dimagi-internal/echo"] = 404
    c = _client(owner)
    r = c.put("/api/agents/echo/github", {"token": "github_pat_secret"}, content_type="application/json")
    assert r.status_code == 422
    assert "cannot see this repo" in r.content.decode()

    github.pr["dimagi-internal/echo"] = 422
    r = c.put("/api/agents/echo/github", {"token": "github_pat_secret"}, content_type="application/json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["set"] and body["login"] == "olive" and body["repo"] == "dimagi-internal/echo"
    assert "github_pat_secret" not in r.content.decode()
    assert "github_pat_secret" not in c.get("/api/agents/echo/github").content.decode()


def test_create_url_prefills_githubs_form_for_this_agent(agent):
    url = delegations.create_url(agent)
    assert url.startswith("https://github.com/settings/personal-access-tokens/new?")
    for part in ("target_name=dimagi-internal", "pull_requests=write", "contents=write", "name=canopy+echo"):
        assert part in url


# ---- whose delegation is in force ---------------------------------------------

def test_transferring_the_agent_stops_the_old_owners_identity(agent, owner, github):
    _lend(agent, owner, github)
    agent.owner = User.objects.create_user("nina", "nina@dimagi.com", "pw")
    agent.save()
    assert delegations.delegation_for(agent) is None
    assert delegations.status(agent)["set"] is False


# ---- handing it to one turn ---------------------------------------------------

@pytest.fixture
def operator(agent):
    # Pairs the box; a member of the agent's workspace, and NOT its owner — so a
    # token that came back as the operator's would be caught.
    user = User.objects.create_user("op", "op@dimagi.com", "pw")
    wsvc.ensure_member(agent.workspace, user, WorkspaceMembership.EDITOR)
    return user


@pytest.fixture
def runner(operator, agent):
    r = Runner.objects.create(name="cloud-ec2-1", kind=Runner.CLOUD, paired_by=operator,
                              last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    RunnerAssignment.objects.create(agent=agent, runner=r, rank=0)
    return r


def _turn(agent, *, runner=None, status=Turn.CLAIMED, **kw):
    return Turn.objects.create(agent=agent, origin=Turn.ORIGIN_EMAIL, prompt="x",
                               idempotency_key=uuid.uuid4().hex, status=status,
                               claimed_by=runner, **kw)


def _token_url(runner, turn):
    return f"/api/harness/runners/{runner.pk}/turns/{turn.pk}/github-token"


def _as_runner(runner):
    # The runner speaks as its pairer, exactly like claim/heartbeat.
    return _client(runner.paired_by)


def test_a_claimed_turn_gets_its_agent_owners_identity(agent, owner, github, runner):
    _lend(agent, owner, github)
    caller = User.objects.create_user("andrea", "andrea@partner.org", "pw",
                                      first_name="Andrea", last_name="King")
    turn = _turn(agent, runner=runner, initiator_user=caller)
    r = _as_runner(runner).post(_token_url(runner, turn))
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["token"] == "github_pat_olive"
    # The OWNER's identity, not the operator who pairs the box, not the caller.
    assert body["github_login"] == "olive"
    assert body["git_name"] == "Olive Owner"
    assert body["git_email"] == "4242+olive@users.noreply.github.com"
    # The caller is recorded, for a Requested-by: trailer.
    assert body["requested_by"] == "Andrea King <andrea@partner.org>"


@pytest.mark.parametrize("case", ["queued", "other_runner", "finished"])
def test_a_runner_cannot_ask_for_a_turn_it_is_not_executing(agent, owner, github, runner, operator, case):
    _lend(agent, owner, github)
    other = Runner.objects.create(name="laptop", kind=Runner.EMDASH, paired_by=operator)
    turn = {
        "queued": lambda: _turn(agent, runner=None, status=Turn.QUEUED),
        "other_runner": lambda: _turn(agent, runner=other),
        "finished": lambda: _turn(agent, runner=runner, status=Turn.DONE),
    }[case]()
    r = _as_runner(runner).post(_token_url(runner, turn))
    assert r.status_code == 404
    assert "github_pat" not in r.content.decode()


def test_no_delegation_is_a_refusal_that_says_what_to_do(agent, runner):
    turn = _turn(agent, runner=runner)
    r = _as_runner(runner).post(_token_url(runner, turn))
    assert r.status_code == 409
    assert "olive@dimagi.com has not lent it" in r.content.decode()


def test_an_expired_delegation_is_refused(agent, owner, github, runner):
    row = _lend(agent, owner, github)
    AgentDelegation.objects.filter(pk=row.pk).update(expires_at=timezone.now() - dt.timedelta(days=1))
    turn = _turn(agent, runner=runner)
    r = _as_runner(runner).post(_token_url(runner, turn))
    assert r.status_code == 409
    assert "expired" in r.content.decode()


def test_a_turn_with_no_agent_has_no_github_identity(agent, owner, github):
    _lend(agent, owner, github)
    turn = Turn(project="canopy-web", origin=Turn.ORIGIN_API)
    with pytest.raises(delegations.DelegationError, match="no agent"):
        delegations.github_token_for_turn(turn)


def test_a_chat_with_the_agent_uses_its_owners_identity(agent, owner, github):
    from apps.canopy_sessions.models import Session

    _lend(agent, owner, github)
    session = Session.objects.create(agent=agent, workspace=agent.workspace, created_by=owner)
    turn = Turn(chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT)
    assert delegations.github_token_for_turn(turn)["github_login"] == "olive"


# ---- the box's view -----------------------------------------------------------

def test_readiness_reports_each_agent_this_runner_serves(agent, owner, github, runner):
    r = _as_runner(runner).get(f"/api/harness/runners/{runner.pk}/github-readiness")
    assert r.status_code == 200
    [row] = r.json()
    assert row["agent_slug"] == "echo" and row["status"] == "fail"
    assert "has not lent" in row["detail"]

    _lend(agent, owner, github)
    [row] = _as_runner(runner).get(f"/api/harness/runners/{runner.pk}/github-readiness").json()
    assert row["status"] == "ok" and "@olive" in row["detail"]

    # Checked LIVE: a token that has since lost the repo turns it red at boot.
    github.pr["dimagi-internal/echo"] = 403
    [row] = _as_runner(runner).get(f"/api/harness/runners/{runner.pk}/github-readiness").json()
    assert row["status"] == "fail" and "Pull requests" in row["detail"]


def test_bootstrap_resolve_carries_the_owners_token_for_private_clones(agent, owner, github, runner):
    from apps.tokens.models import PersonalToken

    _lend(agent, owner, github)
    # Bearer only, like the box: a browser session is never given values.
    raw, _ = PersonalToken.create_for_user(user=runner.paired_by, label="runner")
    r = Client().get("/api/agents/echo/credentials/resolve", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert r.status_code == 200, r.content
    assert r.json()["github_token"] == "github_pat_olive"


# ---- the drill proves shipping, not login ---------------------------------------

def test_the_drill_probes_pull_request_permission_on_the_agents_repo(agent, runner):
    [drill] = services.start_drill(runner, [agent])
    prompt = drill.turn.prompt
    assert "https://api.github.com/repos/dimagi-internal/echo/pulls" in prompt
    assert "422 = PASS" in prompt and "403 = FAIL" in prompt
