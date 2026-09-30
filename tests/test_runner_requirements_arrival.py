"""A host's runner requirements (ZDR) ride its visitor's token onto every session.

The host signs `canopy_runner_requirements` into the visitor assertion; canopy
verifies it at arrival, carries it on the token it mints (contact or member),
and stamps it onto every session that token starts or sends into. Routing then
enforces whatever the session says (`apps/harness/runner_requirements.py`).
The stamp only grows: a later token without the claim never removes it.
"""
from __future__ import annotations

import dataclasses

import pytest
from allauth.account.models import EmailAddress
from canopy_sdk import contract
from canopy_sdk.host import HostConfig, sign_visitor_assertion
from canopy_sdk.keys import generate_private_key, public_pem
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client

from apps.agents.interface import parse
from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.tokens import assertions
from apps.tokens.models import AppCredential, AppCredentialAgent, ContactToken, DelegatedToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

IFACE = {"capabilities": {"connect": {"callers": ["contact"],
                                      "tools": ["mcp__*canopy-web__who_is_asking"]}}}


@pytest.fixture(autouse=True)
def _clean():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture()
def key():
    return generate_private_key("EdDSA")


@pytest.fixture()
def site(key):
    owner = User.objects.create_user("op", "op@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner,
                                 interface=parse(IFACE))
    app = AppCredential.create_credential(name="connect-labs", created_by=owner, workspace=ws)
    app.public_keys = [public_pem(key)]
    app.save()
    AppCredentialAgent.objects.create(app=app, agent=agent)
    return {"owner": owner, "ws": ws, "agent": agent, "app": app}


def _config(key, requirements=()):
    cfg = HostConfig(signing_key=key, canopy_base_url=assertions.audience(),
                     app_name="connect-labs")
    # Bypass the SDK's own validation so canopy's check is the one exercised.
    object.__setattr__(cfg, "runner_requirements", tuple(requirements))
    return cfg


def _arrive(key, *, requirements=(), subject="u-42", email="", verified=False):
    assertion = sign_visitor_assertion(_config(key, requirements), subject, name="Gillian",
                                       email=email, email_verified=verified)
    return Client().post(contract.ARRIVAL_PATH,
                         data={"assertion": assertion, "agent_slug": "ace"},
                         content_type="application/json")


def _bearer(raw):
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


def _member(site):
    u = User.objects.create_user("mem", "mem@dimagi.com", "pw")
    EmailAddress.objects.create(user=u, email=u.email, verified=True, primary=True)
    WorkspaceMembership.objects.create(user=u, workspace=site["ws"],
                                       role=WorkspaceMembership.EDITOR)
    return u


def _contact_session(raw):
    r = _bearer(raw).post("/api/contact/sessions", {"agent_slug": "ace"},
                          content_type="application/json")
    assert r.status_code == 200, r.content
    return Session.objects.get(pk=r.json()["id"])


def test_an_arrival_requiring_zdr_mints_a_token_that_carries_it(site, key):
    r = _arrive(key, requirements=["zdr"])
    assert r.status_code == 200, r.content
    assert r.json()["kind"] == "contact"
    assert ContactToken.objects.get().runner_requirements == ["zdr"]


def test_an_arrival_without_the_claim_carries_none(site, key):
    assert _arrive(key).status_code == 200
    assert ContactToken.objects.get().runner_requirements == []


def test_a_member_arrival_carries_it_too(site, key):
    _member(site)
    r = _arrive(key, requirements=["zdr"], email="mem@dimagi.com", verified=True)
    assert r.status_code == 200, r.content
    assert r.json()["kind"] == "user"
    assert DelegatedToken.objects.get().runner_requirements == ["zdr"]


def test_an_unknown_flag_refuses_the_arrival(site, key):
    r = _arrive(key, requirements=["nope"])
    assert r.status_code == 400
    assert "runner" in r.json()["detail"]
    assert not ContactToken.objects.exists() and not DelegatedToken.objects.exists()


def test_a_session_started_under_that_token_is_stamped(site, key):
    session = _contact_session(_arrive(key, requirements=["zdr"]).json()["token"])
    assert session.metadata["runner_requirements"] == ["zdr"]


def test_a_session_started_without_the_claim_is_not_stamped(site, key):
    session = _contact_session(_arrive(key).json()["token"])
    assert "runner_requirements" not in session.metadata


def test_a_member_session_created_through_the_site_is_stamped(site, key):
    _member(site)
    raw = _arrive(key, requirements=["zdr"], email="mem@dimagi.com", verified=True).json()["token"]
    r = _bearer(raw).post("/api/canopy-sessions/", {"agent_slug": "ace"},
                          content_type="application/json")
    assert r.status_code == 200, r.content
    assert Session.objects.get(pk=r.json()["id"]).metadata["runner_requirements"] == ["zdr"]


def test_a_body_cannot_set_or_clear_it(site, key):
    # Under a zdr token, a body naming an empty list is dropped, not obeyed.
    raw = _arrive(key, requirements=["zdr"]).json()["token"]
    r = _bearer(raw).post("/api/contact/sessions",
                          {"agent_slug": "ace", "metadata": {"runner_requirements": []}},
                          content_type="application/json")
    assert r.status_code == 200, r.content
    assert Session.objects.get(pk=r.json()["id"]).metadata["runner_requirements"] == ["zdr"]

    # With no token at all, a body cannot set one either: it is server-owned.
    c = Client()
    c.force_login(site["owner"])
    r = c.post("/api/canopy-sessions/",
               {"agent_slug": "ace", "metadata": {"runner_requirements": ["zdr"]}},
               content_type="application/json")
    assert r.status_code == 200, r.content
    assert "runner_requirements" not in Session.objects.get(pk=r.json()["id"]).metadata


def _send(raw, session):
    r = _bearer(raw).post(f"/api/contact/sessions/{session.pk}/send", {"text": "hello"},
                          content_type="application/json")
    assert r.status_code == 200, r.content


def test_a_later_token_without_the_claim_does_not_unstamp(site, key):
    session = _contact_session(_arrive(key, requirements=["zdr"]).json()["token"])
    later = _arrive(key).json()["token"]
    _send(later, session)
    session.refresh_from_db()
    assert session.metadata["runner_requirements"] == ["zdr"]


def test_a_later_token_with_the_claim_stamps_an_older_session(site, key):
    session = _contact_session(_arrive(key).json()["token"])
    assert "runner_requirements" not in session.metadata
    _send(_arrive(key, requirements=["zdr"]).json()["token"], session)
    session.refresh_from_db()
    assert session.metadata["runner_requirements"] == ["zdr"]


def test_a_member_send_through_the_site_stamps_an_older_session(site, key):
    _member(site)
    plain = _arrive(key, email="mem@dimagi.com", verified=True).json()["token"]
    sid = _bearer(plain).post("/api/canopy-sessions/", {"agent_slug": "ace"},
                              content_type="application/json").json()["id"]
    zdr = _arrive(key, requirements=["zdr"], email="mem@dimagi.com", verified=True).json()["token"]
    r = _bearer(zdr).post(f"/api/canopy-sessions/{sid}/send", {"text": "hi"},
                          content_type="application/json")
    assert r.status_code == 200, r.content
    assert Session.objects.get(pk=sid).metadata["runner_requirements"] == ["zdr"]


def test_the_site_row_shows_what_the_host_last_sent(site, key):
    _arrive(key, requirements=["zdr"])
    assert AppCredential.objects.get(pk=site["app"].pk).last_runner_requirements == ["zdr"]
    _arrive(key)
    assert AppCredential.objects.get(pk=site["app"].pk).last_runner_requirements == []


def test_add_runner_requirements_is_a_union_that_never_removes(site):
    from apps.canopy_sessions import services

    s = services.create_session(workspace=site["ws"], created_by=site["owner"],
                                agent=site["agent"])
    services.add_runner_requirements(s, ())
    assert "runner_requirements" not in Session.objects.get(pk=s.pk).metadata
    services.add_runner_requirements(s, ("zdr",))
    services.add_runner_requirements(s, ())
    assert Session.objects.get(pk=s.pk).metadata["runner_requirements"] == ["zdr"]
    assert s.metadata["runner_requirements"] == ["zdr"]


def test_the_sdk_config_really_puts_the_claim_on_the_wire(site, key):
    """The unvalidated `_config` above is only for the unknown-flag case; the
    real SDK path is what a host runs."""
    cfg = dataclasses.replace(_config(key), runner_requirements=("zdr",))
    assertion = sign_visitor_assertion(cfg, "u-42", name="Gillian")
    r = Client().post(contract.ARRIVAL_PATH, data={"assertion": assertion, "agent_slug": "ace"},
                      content_type="application/json")
    assert r.status_code == 200, r.content
    assert ContactToken.objects.get().runner_requirements == ["zdr"]


# --- nothing that rewrites metadata may drop the stamp --------------------------------


def _runners(site):
    from apps.harness.models import Runner, RunnerFlag

    caps = {"sessions": True, "projects": ["canopy-web"]}
    cloud = Runner.objects.create(name="cloud", workspace=site["ws"], kind=Runner.CLOUD,
                                  status=Runner.ONLINE, paired_by=site["owner"], host="cloud",
                                  capabilities=caps)
    zdr = Runner.objects.create(name="zdr-box", workspace=site["ws"], kind=Runner.CLOUD,
                                status=Runner.ONLINE, paired_by=site["owner"], host="zdr-box",
                                capabilities=caps)
    RunnerFlag.objects.create(runner=zdr, flag="zdr", declared_by=site["owner"])
    return cloud, zdr


def _stale_copy_then_stamp(session):
    """Hand back a copy loaded BEFORE a ZDR send stamped the row."""
    from apps.canopy_sessions import services

    stale = Session.objects.get(pk=session.pk)
    services.add_runner_requirements(Session.objects.get(pk=session.pk), ("zdr",))
    assert "runner_requirements" not in (stale.metadata or {})
    return stale


def test_a_transfer_from_a_stale_copy_keeps_the_stamp(site):
    from apps.canopy_sessions import services
    from apps.canopy_sessions.models import RunnerBinding

    cloud, zdr = _runners(site)
    s = Session.objects.create(workspace=site["ws"], project="canopy-web",
                               created_by=site["owner"])
    RunnerBinding.objects.create(session=s, runner=cloud, session_key="t",
                                 emdash_project="canopy-web", host="cloud", thread_key=str(s.id))
    stale = _stale_copy_then_stamp(s)
    services.transfer_session(session=stale, placement=str(zdr.id))
    meta = Session.objects.get(pk=s.pk).metadata
    assert meta["runner_requirements"] == ["zdr"]
    assert meta["requested_runner_id"] == str(zdr.id)


def test_moving_queued_turns_from_a_stale_copy_keeps_the_stamp(site):
    from apps.canopy_sessions import services

    _cloud, zdr = _runners(site)
    s = services.create_session(workspace=site["ws"], created_by=site["owner"],
                                agent=site["agent"])
    services.send_message(session=s, text="hello", user=site["owner"])
    stale = _stale_copy_then_stamp(s)
    moved = services.move_queued_turns(session=stale, placement=str(zdr.id))
    assert moved
    meta = Session.objects.get(pk=s.pk).metadata
    assert meta["runner_requirements"] == ["zdr"]
    assert meta["requested_runner_id"] == str(zdr.id)


def test_the_arrival_refused_as_not_granted_does_not_touch_the_site_row(site, key):
    AppCredentialAgent.objects.all().delete()
    r = _arrive(key, requirements=["zdr"])
    assert r.status_code == 404 or r.status_code == 403, r.content
    assert AppCredential.objects.get(pk=site["app"].pk).last_runner_requirements == []


# --- the member widget's socket ------------------------------------------------------


def test_the_socket_carries_the_tokens_requirements(site, key):
    from asgiref.sync import async_to_sync

    from apps.realtime import channels_auth

    _member(site)
    raw = _arrive(key, requirements=["zdr"], email="mem@dimagi.com", verified=True).json()["token"]
    assert async_to_sync(channels_auth._delegated_runner_requirements)(
        {"query_string": f"token={raw}".encode(), "headers": []}) == ("zdr",)
    assert async_to_sync(channels_auth._delegated_runner_requirements)(
        {"query_string": b"", "headers": [(b"authorization", f"Bearer {raw}".encode())]}) == ("zdr",)
    assert async_to_sync(channels_auth._delegated_runner_requirements)(
        {"query_string": b"", "headers": []}) == ()
