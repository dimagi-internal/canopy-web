"""An agent's GitHub identity is a token a PERSON delegated to it — its owner or an admin —
held in the agent's vault as a delegated credential (spec 2026-10-26-delegated-credentials-
in-the-vault-design.md).

What these pin, in the order a turn meets them:

- only the agent's owner or an admin can delegate their identity to it, and a token that
  cannot push to and open a pull request on the agent's repo is refused, never stored;
- a delegated token is sealed to the machines that run the agent and to nothing else:
  not the vault's key, not its members, not canopy-web — and refused when no machine can
  open one yet;
- the delegation in force is its person's while they are an admin: transfer the agent,
  or take their admin away, and their identity stops travelling with it;
- a runner gets a turn's identity only for a turn it has claimed and is executing; the
  token itself only from its own resolve, proved by one of those machines;
- no vault write but its person's replaces it, and a machine that holds it re-seals it to
  a newly approved machine without anyone being asked;
- the box's readiness check reads what the machines found when they probed it.

GitHub is never called: `requests` is replaced by a fake that answers like
GitHub does for a fine-grained token.
"""
from __future__ import annotations

import datetime as dt
import uuid
from unittest import mock

import pytest
from canopy_sdk.jose import make_dpop_proof
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from apps.agents.credentials import accounts, delegations, vault_engine
from apps.agents.models import Agent, AgentAdmin, AgentCredential, AgentDelegation
from apps.common.encryption import encrypt_secret
from apps.harness import drills, services
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.passkeys.testing import grant_step_up
from apps.tokens.models import PersonalToken
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_workspace
from tests.fixtures.device_proof import accept_device_proof
from tests.fixtures.machines import a_machine
from tests.fixtures.vault_zk import ref

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
    grant_step_up(c)   # lending a token is a secret write: a browser steps up
    return c


def _lend(agent, owner, github, token="github_pat_olive"):
    return delegations.set_github(agent, owner, token)




def opened(entry, ecdh, key) -> str:
    """What a machine reads, opening a delegated value with its own key."""
    return ref.open_sealed_value(entry.value_zk, str(entry.agent_id), entry.name, "device", key.ecdh_jkt, ecdh)


@pytest.fixture
def operator(agent):
    # Pairs the box; a member of the agent's workspace, and NOT its owner — so a
    # token that came back as the operator's would be caught.
    user = User.objects.create_user("op", "op@dimagi.com", "pw")
    wsvc.ensure_member(agent.workspace, user, WorkspaceMembership.EDITOR)
    # Trusted with the agent: a box receives the agent's GitHub identity only
    # when its owner is one of the agent's admins (`runner_may_hold_agent`).
    AgentAdmin.objects.create(agent=agent, user=user)
    return user


@pytest.fixture
def runner(operator, agent):
    # Enrolled where the agent lives, as pairing does: a box serves only the
    # workspaces it is enrolled in, and readiness lists the agents it runs.
    r = Runner.objects.create(name="cloud-ec2-1", kind=Runner.CLOUD, owner=operator,
                              workspace=agent.workspace,
                              last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    RunnerAssignment.objects.create(agent=agent, runner=r, rank=0)
    return r


@pytest.fixture
def machine(runner, operator):
    """The box's approved device key — the one machine that runs the agent."""
    return a_machine(runner, operator)


# ---- delegating ---------------------------------------------------------------

def test_the_owner_delegates_a_working_token_sealed_to_the_machines_that_run_the_agent(
        agent, owner, github, machine):
    key, _, ecdh = machine
    entry = _lend(agent, owner, github)
    entry.refresh_from_db()
    assert (entry.name, entry.account, entry.provider) == ("github-token", "delegated", "github")
    assert entry.delegated_by == owner
    assert entry.value_zk.startswith("zks1:") and "github_pat_olive" not in entry.value_zk
    # Sealed to the machine, and to no person — not even the one it came from (no vault key).
    assert accounts.zk_seal.sealed_targets(entry.value_zk) == {("device", key.ecdh_jkt)}
    assert opened(entry, ecdh, key) == "github_pat_olive"
    assert entry.identity == {"login": "olive", "user_id": 4242, "name": "Olive Owner"}
    assert entry.checks == [{"repo": "dimagi-internal/echo", "ok": True,
                             "detail": "can push and open pull requests"}]
    assert entry.provider_expires_at == dt.datetime(2026, 12, 1, 10, 0, tzinfo=dt.UTC)
    assert accounts.label(entry, agent) == (
        "GitHub · delegated by Olive · GitHub says it expires 1 Dec 2026")
    assert not AgentDelegation.objects.exists()


def test_a_token_is_refused_when_no_machine_that_runs_the_agent_can_open_it(agent, owner, github):
    r = _client(owner).put("/api/agents/echo/github", {"token": "github_pat_olive"},
                           content_type="application/json")
    assert r.status_code == 409 and r.json()["error"] == "no_machine_can_open"
    assert not AgentCredential.objects.exists()


def test_a_license_holders_or_a_strangers_box_is_never_sealed_to(agent, owner, github, machine):
    stranger = User.objects.create_user("sam", "sam@dimagi.com", "pw")
    wsvc.ensure_member(agent.workspace, stranger, WorkspaceMembership.EDITOR)
    theirs = Runner.objects.create(name="sam-laptop", kind=Runner.EMDASH, owner=stranger,
                                   workspace=agent.workspace, status=Runner.ONLINE)
    RunnerAssignment.objects.create(agent=agent, runner=theirs, rank=1)
    other, _, _ = a_machine(theirs, stranger)
    entry = _lend(agent, owner, github)
    assert ("device", other.ecdh_jkt) not in accounts.zk_seal.sealed_targets(entry.value_zk)
    assert accounts.delegated_devices(agent, owner) == [machine[0]]


def _another_admins_laptop(agent):
    ada = User.objects.create_user("ada", "ada@dimagi.com", "pw")
    wsvc.ensure_member(agent.workspace, ada, WorkspaceMembership.EDITOR)
    AgentAdmin.objects.create(agent=agent, user=ada)
    laptop = Runner.objects.create(name="ada-laptop", kind=Runner.EMDASH, owner=ada,
                                   workspace=agent.workspace, status=Runner.ONLINE)
    RunnerAssignment.objects.create(agent=agent, runner=laptop, rank=2)
    return laptop, a_machine(laptop, ada)


def test_another_admins_laptop_is_never_sealed_to_but_the_persons_own_is(agent, owner, github, machine):
    # A laptop's device key is in its owner's home directory: sealing the owner's token
    # to another admin's laptop would let that admin open it. Their cloud box may hold it.
    _, (theirs, _, _) = _another_admins_laptop(agent)
    mine_runner = Runner.objects.create(name="olive-laptop", kind=Runner.EMDASH, owner=owner,
                                        workspace=agent.workspace, status=Runner.ONLINE)
    RunnerAssignment.objects.create(agent=agent, runner=mine_runner, rank=3)
    mine, _, _ = a_machine(mine_runner, owner)
    entry = _lend(agent, owner, github)
    sealed = accounts.zk_seal.sealed_targets(entry.value_zk)
    assert ("device", theirs.ecdh_jkt) not in sealed
    assert {("device", machine[0].ecdh_jkt), ("device", mine.ecdh_jkt)} <= sealed
    assert {k.runner.name for k in accounts.delegated_devices(agent, owner)} == {"cloud-ec2-1", "olive-laptop"}
    # The laptop's own admin delegating: their laptop qualifies for THEIR token, not the owner's.
    assert "ada-laptop" in {k.runner.name for k in accounts.delegated_devices(agent, theirs.runner.owner)}


def test_a_copy_sealed_to_another_admins_laptop_earlier_is_not_handed_out_and_is_pruned(
        agent, owner, github, machine):
    _, (theirs, _, _) = _another_admins_laptop(agent)
    entry = _lend(agent, owner, github)
    # As the first version sealed it: to every admin's runner, laptops included.
    entry.value_zk = accounts.zk_seal.seal_to(
        "github_pat_olive", agent_id=str(agent.pk), name=entry.name, kv=agent.zk_kv,
        recipients=[("device", k.ecdh_jkt, k.ecdh_public_jwk) for k in (machine[0], theirs)])
    entry.save(update_fields=["value_zk"])
    assert accounts.resolve_payload(agent, theirs) == {}
    assert "github-token" in accounts.resolve_payload(agent, machine[0])
    assert accounts.machines_that_can_open(entry) == ["ada-laptop", "cloud-ec2-1"]

    assert accounts.prune(entry, agent) == ["ada-laptop"]
    entry.refresh_from_db()
    assert accounts.zk_seal.sealed_targets(entry.value_zk) == {("device", machine[0].ecdh_jkt)}
    assert opened(entry, machine[2], machine[0]) == "github_pat_olive"   # nothing re-encrypted
    assert accounts.prune(entry, agent) == []


@pytest.mark.parametrize("code,says", [
    (403, "Pull requests"),       # reaches the repo, cannot open a PR (the #747 PAT)
    (404, "Resource owner"),      # the form's resource owner fell back to a person
])
def test_a_token_that_cannot_open_a_pull_request_is_refused_not_stored(agent, owner, github, machine,
                                                                       code, says):
    github.pr["dimagi-internal/echo"] = code
    with pytest.raises(delegations.DelegationError, match=says):
        _lend(agent, owner, github)
    assert not AgentCredential.objects.exists()


def test_a_token_that_can_open_prs_but_not_push_is_refused(agent, owner, github, machine):
    # Pull requests: write without Contents: write — it could never push the
    # branch the pull request would come from.
    github.push["dimagi-internal/echo"] = 403
    with pytest.raises(delegations.DelegationError, match="Contents"):
        _lend(agent, owner, github)
    assert not AgentCredential.objects.exists()


def test_a_token_github_rejects_is_refused(agent, owner, github, machine):
    github.user_status = 401
    with pytest.raises(delegations.DelegationError, match="rejected"):
        _lend(agent, owner, github)


def test_a_paste_with_more_than_the_token_is_refused_with_a_sentence(agent, owner, github, machine):
    # A curly quote in the paste used to crash the GitHub request (latin-1 header) as a 500.
    with pytest.raises(delegations.DelegationError, match="not just a GitHub token"):
        delegations.set_github(agent, owner, "github_pat_olive\nGitHub’s token page text")
    r = _client(owner).put("/api/agents/echo/github", {"token": "github_pat_x and some ’ text"},
                           content_type="application/json")
    assert r.status_code == 422 and "starts with github_pat_" in r.json()["detail"]
    assert not AgentCredential.objects.exists()


def test_only_the_owner_or_an_admin_can_delegate(agent, github, machine):
    editor = User.objects.create_user("adam", "adam@dimagi.com", "pw")
    wsvc.ensure_member(agent.workspace, editor, WorkspaceMembership.EDITOR)
    with pytest.raises(delegations.DelegationError, match="owner or an admin"):
        delegations.set_github(agent, editor, "github_pat_adam")
    assert not AgentCredential.objects.exists()


def _admin(agent, username="ada"):
    user = User.objects.create_user(username, f"{username}@dimagi-ai.com", "pw")
    wsvc.ensure_member(agent.workspace, user, WorkspaceMembership.EDITOR)
    AgentAdmin.objects.create(agent=agent, user=user)
    return user


def test_an_admin_delegates_and_a_later_delegation_replaces_it(agent, owner, github, machine):
    ada = _admin(agent)
    delegations.set_github(agent, ada, "github_pat_ada")
    st = delegations.status(agent, machines=True)
    assert st["set"] and st["lent_by_email"] == "ada@dimagi-ai.com" and st["in_vault"]
    assert st["machines"] == ["cloud-ec2-1"]
    _lend(agent, owner, github)
    assert delegations.github_entry(agent).delegated_by == owner
    assert AgentCredential.objects.filter(agent=agent).count() == 1


def test_an_admin_who_loses_the_grant_stops_delegating(agent, github, machine, runner):
    ada = _admin(agent)
    delegations.set_github(agent, ada, "github_pat_ada")
    AgentAdmin.objects.filter(agent=agent, user=ada).delete()
    assert delegations.status(agent)["set"] is False
    assert "no longer an admin" in delegations.readiness(agent)[1]
    with pytest.raises(delegations.DelegationError, match="no longer an admin"):
        delegations.github_token_for_turn(_turn(agent, runner=runner))


def test_the_route_says_whether_the_caller_may_delegate(agent, owner, github, machine):
    ada = _admin(agent)
    editor = User.objects.create_user("eve", "eve@dimagi.com", "pw")
    wsvc.ensure_member(agent.workspace, editor, WorkspaceMembership.EDITOR)
    assert _client(ada).get("/api/agents/echo/github").json()["can_lend"] is True
    assert _client(editor).get("/api/agents/echo/github").json()["can_lend"] is False
    r = _client(ada).put("/api/agents/echo/github", {"token": "github_pat_ada"},
                         content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["lent_by_email"] == "ada@dimagi-ai.com"
    assert r.json()["account_label"].startswith("GitHub · delegated by ")


def test_a_browser_delegates_a_token_only_after_a_passkey_step_up(agent, owner, github, machine):
    """Delegating replaces the identity every turn of the agent pushes as: a browser
    steps up first, after the check that it may delegate at all. A token may delegate."""
    c = Client()
    c.force_login(owner)
    r = c.put("/api/agents/echo/github", {"token": "github_pat_olive"}, content_type="application/json")
    assert r.status_code == 403 and r.json()["error"] == "step_up_required"
    assert not AgentCredential.objects.exists()

    stranger = User.objects.create_user("eve", "eve@dimagi.com", "pw")
    wsvc.ensure_member(agent.workspace, stranger, WorkspaceMembership.EDITOR)
    c = Client()
    c.force_login(stranger)
    r = c.put("/api/agents/echo/github", {"token": "github_pat_eve"}, content_type="application/json")
    assert r.status_code == 422 and "owner or an admin" in r.content.decode()   # not a passkey prompt

    raw, _ = PersonalToken.create_for_user(user=owner, label="cli")
    r = Client().put("/api/agents/echo/github", {"token": "github_pat_olive"},
                     content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert r.status_code == 200, r.content


def test_the_route_refuses_with_githubs_reason_and_never_echoes_the_token(agent, owner, github, machine):
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


def test_the_person_withdraws_their_own_and_nobody_elses(agent, owner, github, machine):
    _lend(agent, owner, github)
    ada = _admin(agent)
    assert _client(ada).delete("/api/agents/echo/github").json()["set"] is True
    assert _client(owner).delete("/api/agents/echo/github").json()["set"] is False
    assert not AgentCredential.objects.exists()


def test_create_url_prefills_githubs_form_for_this_agent(agent):
    url = delegations.create_url(agent)
    assert url.startswith("https://github.com/settings/personal-access-tokens/new?")
    for part in ("target_name=dimagi-internal", "pull_requests=write", "contents=write", "name=canopy+echo"):
        assert part in url


# ---- nobody but its machines can read it ------------------------------------------

def test_an_admin_cannot_delete_what_a_person_delegated(agent, owner, github, machine):
    """The owner administers the agent, but the GitHub token is Olive's: only she withdraws it."""
    _lend(agent, owner, github)
    r = _client(owner).delete("/api/agents/echo/credentials/github-token")
    assert r.status_code == 409 and r.json()["error"] == "delegated_entry"
    assert AgentCredential.objects.filter(agent=agent, name="github-token").exists()


def test_no_vault_write_but_its_persons_replaces_it(agent, owner, github, machine):
    _lend(agent, owner, github)
    c = _client(owner)
    r = c.put("/api/agents/echo/credentials", {"values": {"github-token": "mine-now"}},
              content_type="application/json")
    assert r.status_code == 409 and r.json()["error"] == "delegated_entry"
    r = c.put("/api/agents/echo/vault/zk/values",
              {"kv": agent.zk_kv, "values": [{"name": "github-token", "value_zk": "zk1:" + "A" * 40}]},
              content_type="application/json")
    assert r.status_code == 409, r.content
    assert opened(delegations.github_entry(agent), machine[2], machine[0]) == "github_pat_olive"


def test_a_members_read_of_the_vault_names_it_and_never_carries_it(agent, owner, github, machine):
    _lend(agent, owner, github)
    body = _client(owner).get("/api/agents/echo/vault/zk?values=true").json()
    assert body["delegated"] == ["github-token"]
    assert "github-token" not in (body["values"] or {}) and "github-token" not in body["sealed"]
    [row] = [r for r in _client(owner).get("/api/agents/echo/credentials/status").json()
             if r["name"] == "github-token"]
    assert row["account"] == "delegated" and row["account_label"].startswith("GitHub · delegated by Olive")


def _resolve(agent, user, sign=None, path=""):
    raw, _ = PersonalToken.create_for_user(user=user, label="t")
    url = f"/api/agents/{agent.slug}/credentials/resolve"
    headers = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
    if sign is not None:
        headers["HTTP_DPOP"] = make_dpop_proof(sign, "GET", f"http://testserver{url}")
    return Client().get(url + path, **headers)


def test_only_a_machine_that_runs_the_agent_gets_it_from_resolve(agent, owner, github, machine, runner, monkeypatch):
    key, sign, ecdh = machine
    _lend(agent, owner, github)
    r = _resolve(agent, runner.owner, sign)
    assert r.status_code == 200, r.content
    got = r.json()["delegated"]["github-token"]
    assert got["provider"] == "github" and got["identity"]["login"] == "olive"
    assert got["delegated_by_email"] == "olive@dimagi.com" and got["waiting"] == []
    assert ref.open_sealed_value(got["value_zk"], str(agent.pk), "github-token", "device",
                                 key.ecdh_jkt, ecdh) == "github_pat_olive"
    assert "github-token" not in r.json()["values"]
    assert r.json()["delegated_withheld"] == []
    # An admin's read — the owner by hand, a box's owner without its proof — never; they
    # are told it is someone's own, so they use their own account on their machine.
    accept_device_proof(monkeypatch)
    for who, proof in ((owner, None), (runner.owner, None)):
        r = _resolve(agent, who, proof)
        assert r.status_code == 200, r.content
        assert r.json()["delegated"] == {} and "github_pat" not in r.content.decode()
        [w] = r.json()["delegated_withheld"]
        assert (w["name"], w["provider"], w["delegated_by_email"]) == ("github-token", "github",
                                                                       "olive@dimagi.com")
        assert w["label"].startswith("GitHub · delegated by Olive")


def test_resolve_never_hands_out_a_github_token_in_the_clear(agent, owner, github, machine, runner, monkeypatch):
    accept_device_proof(monkeypatch)
    _lend(agent, owner, github)
    for who in (runner.owner, owner):
        for path in ("", "?refs=github_token"):
            r = _resolve(agent, who, path=path)
            assert r.status_code == 200, r.content
            assert "github_token" not in r.json()
            assert "github_pat_olive" not in r.content.decode()


# ---- a new machine gets it from one that holds it -----------------------------------

def _reseal(entry, agent, to, ecdh, key) -> str:
    plain = opened(entry, ecdh, key)
    pubs = [("device", k.ecdh_jkt, k.ecdh_public_jwk) for k in to]
    return accounts.zk_seal.seal_to(plain, agent_id=str(agent.pk), name=entry.name, kv=agent.zk_kv,
                                    recipients=pubs)


def test_a_machine_that_holds_it_reseals_it_for_a_new_one(agent, owner, github, machine, runner):
    key, _, ecdh = machine
    entry = _lend(agent, owner, github)
    new, _, new_ecdh = a_machine(runner, runner.owner)
    c = _client(runner.owner)
    [work] = c.get(f"/api/harness/runners/{runner.pk}/vault-keeper").json()["delegated"]
    assert work["waiting"] == [new.ecdh_jkt] and work["device_jkt"] == key.ecdh_jkt
    assert {r["target"] for r in work["recipients"]} == {key.ecdh_jkt, new.ecdh_jkt}

    value = _reseal(entry, agent, [key, new], ecdh, key)
    r = c.put(work["reseal_path"], {"value_zk": value, "expect": work["expect"]},
              content_type="application/json")
    assert r.status_code == 200, r.content
    entry.refresh_from_db()
    assert opened(entry, new_ecdh, new) == "github_pat_olive"
    assert c.get(f"/api/harness/runners/{runner.pk}/vault-keeper").json()["delegated"] == []

    # The same write again is stale.
    r = c.put(work["reseal_path"], {"value_zk": value, "expect": work["expect"]},
              content_type="application/json")
    assert r.status_code == 409 and r.json()["error"] == "zk_value_changed"


def test_a_reseal_to_anyone_else_or_dropping_a_holder_is_refused(agent, owner, github, machine, runner):
    key, _, ecdh = machine
    entry = _lend(agent, owner, github)
    expect = vault_engine.value_digest(entry.value_zk)
    stranger = User.objects.create_user("sam", "sam@dimagi.com", "pw")
    theirs = Runner.objects.create(name="sam-box", kind=Runner.CLOUD, owner=stranger, status=Runner.ONLINE)
    other, _, _ = a_machine(theirs, stranger)
    c = _client(runner.owner)
    url = f"/api/harness/runners/{runner.pk}/vault-keeper/echo/delegated/github-token"
    r = c.put(url, {"value_zk": _reseal(entry, agent, [key, other], ecdh, key), "expect": expect},
              content_type="application/json")
    assert r.status_code == 422 and r.json()["error"] == "zk_invalid_target"
    new, _, _ = a_machine(runner, runner.owner)
    r = c.put(url, {"value_zk": _reseal(entry, agent, [new], ecdh, key), "expect": expect},
              content_type="application/json")
    assert r.status_code == 422 and r.json()["error"] == "zk_rewrap_required"  # drops the machine holding it


# ---- what the machines found ----------------------------------------------------------

def test_a_machine_reports_its_probe_and_a_failed_one_says_replace_it(agent, owner, github, machine, runner):
    _lend(agent, owner, github)
    c = _client(runner.owner)
    url = f"/api/harness/runners/{runner.pk}/delegated-checks"
    r = c.post(url, {"agent": "echo", "name": "github-token", "error": "GitHub rejected this token (401)"},
               content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["label"].endswith("no longer works — replace it")
    verdict, detail = delegations.readiness(agent)
    assert verdict == "fail" and "replace it" in detail
    st = delegations.status(agent, machines=True)
    assert st["checked_by"] == "cloud-ec2-1" and st["no_longer_works"]

    r = c.post(url, {"agent": "echo", "name": "github-token",
                     "checks": [{"repo": "dimagi-internal/echo", "ok": True, "detail": "can push"}]},
               content_type="application/json")
    assert r.status_code == 200 and delegations.readiness(agent)[0] == "ok"


def test_a_box_that_does_not_run_the_agent_cannot_report(agent, owner, github, machine):
    _lend(agent, owner, github)
    stranger = User.objects.create_user("sam", "sam@dimagi.com", "pw")
    theirs = Runner.objects.create(name="sam-box", kind=Runner.CLOUD, owner=stranger, status=Runner.ONLINE)
    r = _client(stranger).post(f"/api/harness/runners/{theirs.pk}/delegated-checks",
                               {"agent": "echo", "name": "github-token", "error": "x"},
                               content_type="application/json")
    assert r.status_code == 404


# ---- whose delegation is in force ---------------------------------------------

def test_transferring_the_agent_stops_the_old_owners_identity(agent, owner, github, machine):
    _lend(agent, owner, github)
    # Still a member, but no longer an admin: neither the owner nor a workspace owner.
    WorkspaceMembership.objects.filter(workspace=agent.workspace, user=owner).update(
        role=WorkspaceMembership.EDITOR)
    agent.owner = User.objects.create_user("nina", "nina@dimagi.com", "pw")
    agent.save()
    assert delegations.status(agent)["set"] is False


# ---- handing it to one turn ---------------------------------------------------

def _turn(agent, *, runner=None, status=Turn.CLAIMED, **kw):
    return Turn.objects.create(agent=agent, origin=Turn.ORIGIN_EMAIL, prompt="x",
                               idempotency_key=uuid.uuid4().hex, status=status,
                               claimed_by=runner, **kw)


def _token_url(runner, turn):
    return f"/api/harness/runners/{runner.pk}/turns/{turn.pk}/github-token"


def _as_runner(runner):
    # The runner speaks as its owner, exactly like claim/heartbeat.
    return _client(runner.owner)


def test_a_claimed_turn_gets_its_identity_and_where_the_token_is(agent, owner, github, runner, machine):
    _lend(agent, owner, github)
    caller = User.objects.create_user("andrea", "andrea@partner.org", "pw",
                                      first_name="Andrea", last_name="King")
    turn = _turn(agent, runner=runner, initiator_user=caller)
    r = _as_runner(runner).post(_token_url(runner, turn))
    assert r.status_code == 200, r.content
    body = r.json()
    # No token: the box opens it from its own resolve of the agent.
    assert (body["token"], body["delegated"], body["agent"]) == ("", "github-token", "echo")
    # The OWNER's identity, not the operator who pairs the box, not the caller.
    assert body["github_login"] == "olive"
    assert body["git_name"] == "Olive Owner"
    assert body["git_email"] == "4242+olive@users.noreply.github.com"
    # The caller is recorded, for a Requested-by: trailer.
    assert body["requested_by"] == "Andrea King <andrea@partner.org>"


@pytest.mark.parametrize("case", ["queued", "other_runner", "finished"])
def test_a_runner_cannot_ask_for_a_turn_it_is_not_executing(agent, owner, github, runner, machine, operator,
                                                            case):
    _lend(agent, owner, github)
    other = Runner.objects.create(name="laptop", kind=Runner.EMDASH, owner=operator)
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
    assert "olive@dimagi.com nor an admin has delegated it" in r.content.decode()


def test_canopy_sets_no_expiry_of_its_own(agent, owner, github, runner, machine):
    """GitHub's own expiry is the only one: canopy shows it, and the provider refuses it."""
    entry = _lend(agent, owner, github)
    AgentCredential.objects.filter(pk=entry.pk).update(provider_expires_at=timezone.now() - dt.timedelta(days=1))
    turn = _turn(agent, runner=runner)
    r = _as_runner(runner).post(_token_url(runner, turn))
    assert r.status_code == 200 and r.json()["delegated"] == "github-token"
    assert "GitHub says the token expired" in delegations.readiness(agent)[1]


def test_a_token_still_held_the_old_way_serves_until_it_is_moved(agent, owner, runner, machine):
    AgentDelegation.objects.create(user=owner, agent=agent, service=AgentDelegation.GITHUB,
                                   secret_enc=encrypt_secret("github_pat_old"),
                                   meta={"login": "olive", "user_id": 4242, "name": "Olive Owner"})
    r = _as_runner(runner).post(_token_url(runner, _turn(agent, runner=runner)))
    assert r.status_code == 200 and r.json()["token"] == "github_pat_old" and r.json()["delegated"] == ""


def test_moving_old_tokens_into_the_vault(agent, owner, runner, machine, capsys):
    key, _, ecdh = machine
    old = AgentDelegation.objects.create(user=owner, agent=agent, service=AgentDelegation.GITHUB,
                                         secret_enc=encrypt_secret("github_pat_old"),
                                         meta={"login": "olive", "user_id": 4242, "name": "Olive Owner",
                                               "checks": [{"repo": "dimagi-internal/echo", "ok": True,
                                                           "detail": "can push"}]})
    call_command("move_delegations_to_vault")
    assert "would move, sealed to cloud-ec2-1" in capsys.readouterr().out
    assert AgentDelegation.objects.filter(pk=old.pk).exists()
    call_command("move_delegations_to_vault", "--apply")
    assert "moved, sealed to cloud-ec2-1" in capsys.readouterr().out
    assert not AgentDelegation.objects.exists()
    entry = delegations.github_entry(agent)
    assert entry.delegated_by == owner and entry.identity["login"] == "olive"
    assert opened(entry, ecdh, key) == "github_pat_old"
    r = _as_runner(runner).post(_token_url(runner, _turn(agent, runner=runner)))
    assert r.json()["token"] == "" and r.json()["delegated"] == "github-token"


def test_an_agent_no_machine_can_open_one_for_keeps_its_old_token(agent, owner, capsys):
    AgentDelegation.objects.create(user=owner, agent=agent, service=AgentDelegation.GITHUB,
                                   secret_enc=encrypt_secret("github_pat_old"), meta={"login": "olive"})
    call_command("move_delegations_to_vault", "--apply")
    assert "KEPT" in capsys.readouterr().out
    assert AgentDelegation.objects.exists() and delegations.github_entry(agent) is None


def test_a_callers_confined_turn_is_never_given_the_owners_identity(agent, owner, github, runner, machine):
    # Lent, live, and the runner holds the turn — the only thing that differs from
    # the success case above is that a caller is steering it.
    _lend(agent, owner, github)
    turn = _turn(agent, runner=runner, capability="ask")
    r = _as_runner(runner).post(_token_url(runner, turn))
    assert r.status_code == 409, r.content
    assert "confined to the caller capability 'ask'" in r.content.decode()
    # The same turn, run in the agent's full profile, still gets it.
    Turn.objects.filter(pk=turn.pk).update(capability="")
    assert _as_runner(runner).post(_token_url(runner, turn)).status_code == 200


def test_a_confined_chat_turn_is_refused_too(agent, owner, github, machine):
    from apps.canopy_sessions.models import Session

    _lend(agent, owner, github)
    session = Session.objects.create(agent=agent, workspace=agent.workspace, created_by=owner)
    turn = Turn(chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, capability="connect")
    with pytest.raises(delegations.DelegationError, match="confined"):
        delegations.github_token_for_turn(turn)


def test_a_turn_with_no_agent_has_no_github_identity(agent, owner, github, machine):
    _lend(agent, owner, github)
    turn = Turn(project="canopy-web", origin=Turn.ORIGIN_API)
    with pytest.raises(delegations.DelegationError, match="no agent"):
        delegations.github_token_for_turn(turn)


def test_a_chat_with_the_agent_uses_its_owners_identity(agent, owner, github, machine):
    from apps.canopy_sessions.models import Session

    _lend(agent, owner, github)
    session = Session.objects.create(agent=agent, workspace=agent.workspace, created_by=owner)
    turn = Turn(chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT)
    assert delegations.github_token_for_turn(turn)["github_login"] == "olive"


# ---- the box's view -----------------------------------------------------------

def test_readiness_reports_each_agent_this_runner_serves(agent, owner, github, runner, machine):
    r = _as_runner(runner).get(f"/api/harness/runners/{runner.pk}/github-readiness")
    assert r.status_code == 200
    [row] = r.json()
    assert row["agent_slug"] == "echo" and row["status"] == "fail"
    assert "nor an admin has delegated" in row["detail"]

    _lend(agent, owner, github)
    [row] = _as_runner(runner).get(f"/api/harness/runners/{runner.pk}/github-readiness").json()
    assert row["status"] == "ok" and "@olive" in row["detail"]

    # canopy-web cannot open it to re-probe: what the machines report is the answer.
    github.pr["dimagi-internal/echo"] = 403
    [row] = _as_runner(runner).get(f"/api/harness/runners/{runner.pk}/github-readiness").json()
    assert row["status"] == "ok"
    delegations.record_checks(agent, "github-token", by="cloud-ec2-1", checks=[
        {"repo": "dimagi-internal/echo", "ok": False, "detail": "lacks Pull requests"}])
    [row] = _as_runner(runner).get(f"/api/harness/runners/{runner.pk}/github-readiness").json()
    assert row["status"] == "fail" and "Pull requests" in row["detail"]


def test_the_fleet_view_answers_github_from_the_rows_not_the_boot_report(agent, owner, github, runner,
                                                                        machine):
    """The box asks about GitHub once, at boot, then repeats that answer on every
    heartbeat. A token lent after the boot must show as lent — eva, 2026-10-03:
    `github.eva: fail` sat beside a fresh `health_received_at` while canopy was
    issuing eva's token to that very box."""
    boot = _as_runner(runner).get(f"/api/harness/runners/{runner.pk}/github-readiness").json()
    services.heartbeat(runner, active_turn_ids=[], health={
        "checks": [{"name": "claude.version", "status": "ok", "detail": "2.1"},
                   *({"name": f"github.{r['agent_slug']}", "status": r["status"],
                      "detail": r["detail"]} for r in boot)],
        "checked_at": 0, "bootstrapped_at": 0,
    })

    def checks():
        [row] = [r for r in _client(runner.owner).get("/api/harness/runners/").json()
                 if r["id"] == str(runner.pk)]
        return row["health_checks"]

    assert checks()["github.echo"]["status"] == "fail"
    _lend(agent, owner, github)
    now = checks()
    assert now["github.echo"]["status"] == "ok" and "@olive" in now["github.echo"]["detail"]
    assert now["claude.version"]["status"] == "ok"  # the box's own checks are untouched


# ---- the drill proves shipping, not login ---------------------------------------

def test_the_drill_probes_pull_request_permission_on_the_agents_repo(agent, runner):
    [drill] = drills.start_drill(runner, [agent])
    prompt = drill.turn.prompt
    assert "https://api.github.com/repos/dimagi-internal/echo/pulls" in prompt
    assert "422 = PASS" in prompt and "403 = FAIL" in prompt


def test_bootstrap_resolve_carries_this_instances_mailbox(agent, runner, monkeypatch):
    """canopy-web#984: the box sets Gmail up for the INSTANCE's mailbox, never an
    address derived from the slug or read from the shared repo."""
    from apps.tokens.models import PersonalToken

    accept_device_proof(monkeypatch, runner)
    agent.email = "echo@dimagi-ai.com"
    agent.save(update_fields=["email"])
    raw, _ = PersonalToken.create_for_user(user=runner.owner, label="runner")
    r = Client().get("/api/agents/echo/credentials/resolve", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert r.json()["mailbox"] == "echo@dimagi-ai.com"


# ---- the person's own list -------------------------------------------------------------

def test_the_person_sees_what_they_delegated_and_withdraws_it_after_leaving(agent, owner, github, machine):
    _lend(agent, owner, github)
    c = _client(owner)
    [row] = c.get("/api/me/delegated-credentials/").json()
    assert (row["agent"], row["provider"], row["identity"]["login"]) == ("echo", "github", "olive")
    assert row["machines"] == ["cloud-ec2-1"] and row["in_force"] is True
    assert "github_pat" not in c.get("/api/me/delegated-credentials/").content.decode()
    # Nobody else sees it in their list, nor withdraws it.
    ada = _admin(agent)
    assert _client(ada).get("/api/me/delegated-credentials/").json() == []
    assert _client(ada).delete(f"/api/me/delegated-credentials/{row['id']}").status_code == 404
    # No longer an admin (the agent changed hands): still theirs to withdraw.
    WorkspaceMembership.objects.filter(workspace=agent.workspace, user=owner).update(
        role=WorkspaceMembership.EDITOR)
    agent.owner = ada
    agent.save()
    [row] = c.get("/api/me/delegated-credentials/").json()
    assert row["in_force"] is False
    assert c.delete(f"/api/me/delegated-credentials/{row['id']}").status_code == 204
    assert not AgentCredential.objects.exists()


def test_the_access_check_names_what_a_local_user_must_bring_themselves(agent, owner, github, machine):
    """`canopy agent use` reads this before any resolve: on your own machine, each
    credential someone delegated to the agent is theirs, so you use your own."""
    _lend(agent, owner, github)
    editor = User.objects.create_user("eve", "eve@dimagi.com", "pw")
    wsvc.ensure_member(agent.workspace, editor, WorkspaceMembership.EDITOR)
    body = _client(editor).get("/api/agents/echo/credentials/access").json()
    [w] = body["delegated_withheld"]
    assert (w["provider"], w["delegated_by_email"]) == ("github", "olive@dimagi.com")
    assert "github_pat" not in str(body)
