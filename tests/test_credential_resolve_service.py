"""`apps.agents.credentials.credential_resolve.resolve` at the service level: each refusal
is a typed `Refusal` (never an exception), and the read classification and
license withholding that the route used to hold inline.

The HTTP contract (status codes, problem+json members, real DPoP / AWS proofs)
stays pinned by tests/test_canopy_vault_*.py and tests/test_agent_credentials.py;
here the proof check is stubbed so each branch is reached directly.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import RequestFactory
from django.utils import timezone

from apps.agents.credentials import credential_resolve as cr
from apps.agents.models import Agent
from apps.harness.models import Runner, RunnerAssignment
from apps.tokens import kinds
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def owner():
    return get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")


@pytest.fixture
def ws(owner):
    w = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(workspace=w, user=owner, role=WorkspaceMembership.OWNER)
    return w


@pytest.fixture
def agent(ws, owner):
    return Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=owner,
                                runtime_secrets=["canopy-pat", "api-key"],
                                credential_source=Agent.CANOPY_WEB)


@pytest.fixture
def alerts(monkeypatch):
    """Record the owner alerts a refusal sends, instead of emailing."""
    sent = []
    monkeypatch.setattr(cr.vault_alerts, "unrouted_box_refused",
                        lambda agent, request, runner: sent.append(("unrouted", runner)))
    monkeypatch.setattr(cr.vault_alerts, "device_proof_refused",
                        lambda agent, request, reason, runner=None: sent.append(("refused", reason)))
    return sent


def _request(user, *, bearer=True, **meta):
    """`bearer`: a token authenticated the request, as the bearer door records it."""
    req = RequestFactory().get("/api/agents/eva/credentials/resolve",
                               **({"HTTP_AUTHORIZATION": "Bearer t"} if bearer else {}), **meta)
    req.user = user
    if bearer:
        req.auth_method = kinds.PAT
    return req


def _proof(monkeypatch, key=None, refused=None, unrouted=None):
    monkeypatch.setattr(cr, "device_key_from_proof", lambda request, agent: (key, refused, unrouted))


def _box(owner, ws, name="box", kind=Runner.CLOUD):
    return Runner.objects.create(name=name, kind=kind, owner=owner, status=Runner.ONLINE,
                                 workspace=ws, last_heartbeat_at=timezone.now(), capabilities={})


def test_a_browser_session_is_refused_before_anything_else(owner, agent):
    out = cr.resolve(_request(owner, bearer=False), agent, None)
    assert out == cr.Refusal(cr.BEARER_REQUIRED,
                             "resolve requires a bearer token; a browser session is never given values")


def test_a_bearer_header_that_authenticated_nothing_is_still_a_browser(owner, agent):
    """A cookie plus `Authorization: Bearer junk`: the header is there, but no
    token authenticated the request (`auth_method` unset), so it is a browser."""
    req = RequestFactory().get("/api/agents/eva/credentials/resolve", HTTP_AUTHORIZATION="Bearer junk")
    req.user = owner
    assert cr.resolve(req, agent, None).code == cr.BEARER_REQUIRED


def test_cookie_plus_junk_bearer_is_refused_over_http(owner, agent):
    from django.test import Client

    c = Client()
    c.force_login(owner)
    resp = c.get("/api/agents/eva/credentials/resolve", HTTP_AUTHORIZATION="Bearer junk")
    assert resp.status_code == 403
    assert "never given values" in resp.content.decode()


def test_a_member_who_is_no_principal_is_refused_and_told_what_to_ask(ws, agent):
    viewer = get_user_model().objects.create_user(username="vee", email="vee@dimagi.com")
    WorkspaceMembership.objects.create(workspace=ws, user=viewer, role=WorkspaceMembership.EDITOR)
    out = cr.resolve(_request(viewer), agent, None)
    assert isinstance(out, cr.Refusal) and out.code == cr.NO_ACCESS
    assert "ask its owner (jj@dimagi.com)" in out.detail


def test_a_runner_token_with_another_boxs_proof_is_refused(monkeypatch, owner, ws, agent):
    mine, theirs = _box(owner, ws, "mine"), _box(owner, ws, "theirs")
    monkeypatch.setattr(cr, "credential_access", lambda request, agent: ("runner", ""))
    key = type("Key", (), {"runner": theirs, "runner_id": theirs.pk})()
    _proof(monkeypatch, key=key)
    req = _request(owner)
    req.runner_token = type("Tok", (), {"runner": mine, "runner_id": mine.pk})()
    out = cr.resolve(req, agent, None)
    assert out.code == cr.FOREIGN_DEVICE_PROOF
    assert out.detail == "the device proof is theirs's, not this runner token's (mine)"


def test_an_approved_box_that_does_not_run_the_agent_is_not_routed(monkeypatch, owner, ws, agent, alerts):
    agent.require_device_binding = True
    agent.save()
    other = _box(owner, ws, "other")
    _proof(monkeypatch, refused="other does not run eva", unrouted=other)
    out = cr.resolve(_request(owner), agent, None)
    assert out == cr.Refusal(cr.DEVICE_PROOF_REQUIRED, "other does not run eva", reason="not_routed")
    assert alerts == [("unrouted", other)]


def test_a_missing_proof_is_refused_and_the_owner_alerted(monkeypatch, owner, agent, alerts):
    agent.require_device_binding = True
    agent.save()
    _proof(monkeypatch, refused="no device proof")
    out = cr.resolve(_request(owner), agent, None)
    assert out == cr.Refusal(cr.DEVICE_PROOF_REQUIRED, "no device proof", reason="no_valid_proof")
    assert alerts == [("refused", "no device proof")]


def test_no_proof_is_needed_while_the_secrets_live_in_1password(monkeypatch, owner, agent, alerts):
    agent.require_device_binding = True
    agent.credential_source = Agent.ONEPASSWORD
    agent.save()
    _proof(monkeypatch, refused="no device proof")
    monkeypatch.setattr(cr.services, "resolve_agent_vault", lambda a: ("Agent-Eva", "ops-key"))
    out = cr.resolve(_request(owner), agent, None)
    assert isinstance(out, cr.Resolved)
    assert out.op_sa_token == "ops-key" and alerts == []


def test_a_zk_agent_without_this_devices_wrap_is_refused(monkeypatch, owner, ws, agent):
    agent.zk_enabled, agent.zk_kv = True, 3
    agent.save()
    box = _box(owner, ws)
    key = type("Key", (), {"runner": box, "runner_id": box.pk, "pk": "k-1"})()
    _proof(monkeypatch, key=key)
    missing = []
    monkeypatch.setattr(cr.zk, "resolve_payload", lambda agent, key, wanted: ({"api-key": "zk1:x"}, None))
    monkeypatch.setattr(cr.zk, "needs_wrap", lambda values: True)
    monkeypatch.setattr(cr.zk, "device_wrap_missing", lambda agent, request, key: missing.append(key))
    out = cr.resolve(_request(owner), agent, None)
    assert out == cr.Refusal(cr.ZK_WRAP_MISSING,
                             "this device holds no vault-key wrap at kv 3; an admin must grant it",
                             kv=3, device_key="k-1")
    assert missing == [key]


def _values(monkeypatch, agent, values):
    """The agent's (zero-knowledge) values, as `zk.resolve_payload` hands them out. On
    1Password, so an unproven read is not refused first: what is under test is how a
    read is classified and what it withholds, not the device proof."""
    agent.zk_enabled, agent.zk_kv, agent.credential_source = True, 0, Agent.ONEPASSWORD
    agent.save()
    monkeypatch.setattr(cr.zk, "resolve_payload", lambda agent, key, wanted: (dict(values), None))
    monkeypatch.setattr(cr.zk, "needs_wrap", lambda values: False)


def test_an_owner_without_a_named_box_reads_as_admin(monkeypatch, owner, ws, agent):
    """`caller_runs_agent` makes the owner a `runner` caller, but a request that
    names no box is the owner's own read (canopy-web#1563)."""
    RunnerAssignment.objects.create(agent=agent, runner=_box(owner, ws), rank=0)
    _values(monkeypatch, agent, {"api-key": "v"})
    assert cr.credential_access(_request(owner), agent) == ("runner", "")
    out = cr.resolve(_request(owner), agent, None)
    assert isinstance(out, cr.Resolved)
    assert out.via == "admin" and out.values == {"api-key": "v"}


def test_a_license_read_never_gets_the_agents_login_or_the_extras(monkeypatch, owner, agent):
    monkeypatch.setattr(cr, "credential_access", lambda request, agent: ("license", ""))
    monkeypatch.setattr(cr, "license_for_read", lambda request, agent: None)
    _values(monkeypatch, agent, {"api-key": "v", "canopy-pat": "pat"})
    monkeypatch.setattr(cr.services, "resolve_agent_vault", lambda a: ("Agent-Eva", "ops-key"))
    out = cr.resolve(_request(owner), agent, None)
    assert out.via == "license"
    assert out.values == {"api-key": "v"}
    assert out.op_sa_token == ""
    assert not hasattr(out, "github_token")


def test_parse_refs():
    assert cr.parse_refs(None) is None
    assert cr.parse_refs("") == set()
    assert cr.parse_refs(" a, b ,,") == {"a", "b"}


def test_an_owner_in_their_own_local_turn_reads_like_anyone_else(monkeypatch, owner, ws, agent):
    """Being an admin, or pairing a runner the agent routes to, changes nothing in a direct
    session: a request naming the person's own open local turn is a `license` read, so the
    agent's login stays withheld and they step up like anyone (`authority`)."""
    RunnerAssignment.objects.create(agent=agent, runner=_box(owner, ws), rank=0)
    assert cr.credential_access(_request(owner), agent) == ("runner", "")  # no turn named
    from apps.agents.models import AgentLicense

    lic = AgentLicense.objects.create(agent=agent, user=owner, status=AgentLicense.ACTIVE)
    monkeypatch.setattr(cr, "license_for_read", lambda request, agent: lic)
    assert cr.credential_access(_request(owner), agent) == ("license", "")
    _values(monkeypatch, agent, {"api-key": "v", "canopy-pat": "pat"})
    out = cr.resolve(_request(owner), agent, None)
    assert out.via == "license" and out.values == {"api-key": "v"}


def test_a_laptop_runner_is_a_runner_not_a_direct_session(monkeypatch, owner, ws, agent):
    """Placement is who drives, not which machine: a laptop running the runner app claims
    turns as a runner, even with the owner's own direct session open on the same machine."""
    from types import SimpleNamespace

    from apps.agents.models import AgentLicense
    from apps.harness import device_keys

    laptop = _box(owner, ws, name="jj-laptop", kind=Runner.EMDASH)
    lic = AgentLicense.objects.create(agent=agent, user=owner, status=AgentLicense.ACTIVE)
    monkeypatch.setattr(cr, "license_for_read", lambda request, agent: lic)
    monkeypatch.setattr(device_keys, "runner_serves_agent", lambda runner, agent, facts=None: True)
    req = _request(owner)
    req.runner_token = SimpleNamespace(runner=laptop, runner_id=laptop.pk)
    assert cr.credential_access(req, agent) == ("runner", "")
    assert cr.authority.placement_of("runner") == cr.authority.RUNNER
