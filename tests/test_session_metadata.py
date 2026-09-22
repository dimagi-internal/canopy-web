"""Session metadata: one rule for every principal, and canopy's own keys are canopy's.

A caller could set keys canopy ACTS on — the Slack relay posts replies to
`slack_team/slack_channel/slack_thread_ts`, inbound mail is bound to the session
holding `email_thread_key` — so a member could aim an agent's replies at any
Slack channel, or capture a partner's email thread. And a contact's session could
carry no host link at all. Now both go through `host_metadata`.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, override_settings

from apps.agents.models import Agent
from apps.canopy_sessions import services
from apps.canopy_sessions.models import Session
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.test_contact_websocket import _contact_token, _world

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


def test_the_list_names_every_key_its_owner_writes():
    """Spelled as strings to avoid framework import cycles — so pinned here."""
    from apps.canopy_sessions.api import EMBED_APP_KEY
    from apps.harness.services import EMAIL_THREAD_KEY
    from apps.slack.services import SLACK_THREAD_KEY

    owned = services.SERVER_OWNED_METADATA
    assert {EMBED_APP_KEY, EMAIL_THREAD_KEY, SLACK_THREAD_KEY, services.TRANSCRIPT_SOURCED} <= owned
    assert {"slack_team", "slack_channel", "slack_thread_ts", "requested_runner_id"} <= owned


@pytest.fixture()
def member():
    u = User.objects.create_user("m", "m@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=u)
    WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.EDITOR)
    Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    c = Client()
    c.force_login(u)
    return c


HIJACK = {"slack_thread": "T1:C1:1.0", "slack_team": "T1", "slack_channel": "C-anyone",
          "slack_thread_ts": "1.0", "email_thread_key": "ace:18c9abc", "transcript_sourced": False,
          "embed_app": "someone-else", "requested_runner_id": "x"}


def test_a_member_cannot_aim_the_slack_relay_or_capture_an_email_thread(member):
    r = member.post("/api/canopy-sessions/", {"agent_slug": "echo",
                                              "metadata": {**HIJACK, "origin_key": "ace-web:w"}},
                    content_type="application/json")
    assert r.status_code == 200, r.content
    meta = Session.objects.get(pk=r.json()["id"]).metadata
    assert not (set(HIJACK) - {"transcript_sourced"}) & set(meta)
    assert meta.get("transcript_sourced") is not False
    assert meta["origin_key"] == "ace-web:w"                       # the host's own key survives


def test_host_metadata_is_bounded():
    with pytest.raises(ValueError):
        services.host_metadata({f"k{i}": i for i in range(services.MAX_HOST_METADATA_KEYS + 1)})
    with pytest.raises(ValueError):
        services.host_metadata({"big": "x" * services.MAX_HOST_METADATA_BYTES})
    assert services.host_metadata("not a dict") == {}


# --- contacts: the same rule, and the same host link -----------------------------------

def _contact_client():
    owner, ws, app, priv = _world()
    return Client(HTTP_AUTHORIZATION=f"Bearer {_contact_token(priv)}")


def test_a_contacts_conversation_carries_the_hosts_link():
    c = _contact_client()
    r = c.post("/api/contact/sessions", {"agent_slug": "echo", "title": "Payments",
                                         "metadata": {"origin_key": "ace-web:w1", "opp_slug": "bednets",
                                                      **HIJACK}},
               content_type="application/json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["title"] == "Payments"
    assert body["metadata"] == {"origin_key": "ace-web:w1", "opp_slug": "bednets"}
    meta = Session.objects.get(pk=body["id"]).metadata
    assert meta["embed_app"] == "connect-labs"                     # from the token, not the body
    assert "email_thread_key" not in meta and "slack_channel" not in meta


def test_a_contacts_list_takes_the_same_host_filters():
    c = _contact_client()
    for opp in ("bednets", "vitamins"):
        c.post("/api/contact/sessions", {"agent_slug": "echo", "metadata": {"opp_slug": opp}},
               content_type="application/json")
    rows = c.get("/api/contact/sessions?opp_slug=bednets").json()
    assert [r["metadata"]["opp_slug"] for r in rows] == ["bednets"]


@override_settings(CHAT_STUB_EXECUTOR=False)
def test_a_contacts_conversation_is_recorded_like_anyone_elses():
    """Built by the same constructor a user's is, so under a real runner its
    transcript is its record — it was created ad hoc and never was."""
    c = _contact_client()
    sid = c.post("/api/contact/sessions", {"agent_slug": "echo"},
                 content_type="application/json").json()["id"]
    s = Session.objects.get(pk=sid)
    assert s.metadata.get(services.TRANSCRIPT_SOURCED) is True
    assert s.created_by_id is None and s.contact_id is not None
    assert not s.participants.exists()


def test_a_contacts_history_is_the_same_page_a_users_is():
    """It 500'd: the rows were built from `m.body`, which Message does not have."""
    from apps.canopy_sessions.models import Message

    c = _contact_client()
    sid = c.post("/api/contact/sessions", {"agent_slug": "echo"},
                 content_type="application/json").json()["id"]
    s = Session.objects.get(pk=sid)
    for i, (role, text) in enumerate([("user", "hi"), ("assistant", "hello")]):
        Message.objects.create(session=s, turn_index=i, role=role, plaintext=text,
                               content={"text": text})
    r = c.get(f"/api/contact/sessions/{sid}/messages?before=99")
    assert r.status_code == 200, r.content
    rows = r.json()["messages"]
    assert [(m["role"], m["plaintext"]) for m in rows] == [("user", "hi"), ("assistant", "hello")]
    assert set(rows[0]) == {"turn_index", "role", "plaintext", "content", "created_at"}


def test_a_contact_can_attach_to_their_own_conversation_only():
    from apps.canopy_sessions.models import Session as S

    c = _contact_client()
    sid = c.post("/api/contact/sessions", {"agent_slug": "echo"},
                 content_type="application/json").json()["id"]
    assert c.post(f"/api/contact/sessions/{sid}/attach").status_code == 200
    assert c.post(f"/api/contact/sessions/{sid}/detach").status_code == 200
    other = S.objects.create(workspace_id=S.objects.get(pk=sid).workspace_id, title="not theirs")
    assert c.post(f"/api/contact/sessions/{other.pk}/attach").status_code == 404
