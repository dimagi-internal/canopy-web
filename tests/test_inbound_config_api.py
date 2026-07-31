"""/api/inbound config — the surface the UI drives, and its tenancy.

The point of this file: everything an operator needs is a record they can edit,
not an env var and a Django shell. That is also what makes the app multi-tenant —
a second workspace configures its own audience, signer and topic without touching
the deployment.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.inbound.models import InboundMailbox, InboundPushConfig
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("own", "own@dimagi.com", "pw")


@pytest.fixture()
def workspace(owner):
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role="owner")
    return ws


@pytest.fixture()
def editor(workspace):
    u = User.objects.create_user("ed", "ed@dimagi.com", "pw")
    WorkspaceMembership.objects.create(workspace=workspace, user=u, role="editor")
    return u


@pytest.fixture()
def outsider():
    u = User.objects.create_user("out", "out@dimagi.com", "pw")
    other = Workspace.objects.create(slug="otherco", display_name="E", created_by=u)
    WorkspaceMembership.objects.create(workspace=other, user=u, role="owner")
    return u


@pytest.fixture()
def agent(workspace):
    return Agent.objects.create(slug="eva", name="Eva", workspace=workspace)


def _c(user):
    c = Client()
    c.force_login(user)
    return c


# ── push config ──────────────────────────────────────────────────────────────


def test_reading_config_materialises_an_empty_one(owner, workspace):
    """Empty is a real, safe state — no audience means every push is refused —
    so the UI renders a form instead of a 'not configured' special case."""
    r = _c(owner).get("/api/inbound/config/dimagi")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["audience"] == ""
    assert body["verifies"] is False


def test_config_serves_the_push_url_so_nobody_hand_copies_it(owner, workspace):
    """Getting it wrong is silent — pushes just go nowhere."""
    body = _c(owner).get("/api/inbound/config/dimagi").json()
    assert body["push_url"].endswith("/api/inbound/gmail/dimagi/")


def test_push_url_carries_the_script_prefix():
    """The whole reason this value is server-computed.

    Labs runs behind ``FORCE_SCRIPT_NAME=/canopy``; a push URL missing that
    prefix resolves to a SIBLING tenant on the same host, so pushes are accepted
    by something that is not us and the mailbox silently keeps polling. Asserting
    only the tail (the test above) cannot see this — it passes either way, which
    is exactly how the prefix-less URL shipped.

    Driven through the service rather than the test client on purpose: the real
    WSGI/ASGI handlers call ``set_script_prefix``, ``django.test.Client`` does
    not, so a client-level test is structurally blind to this bug.
    """
    from django.test import RequestFactory
    from django.urls import set_script_prefix

    from apps.inbound import services

    try:
        set_script_prefix("/canopy/")
        url = services.push_url(RequestFactory().get("/api/inbound/config/dimagi"), "dimagi")
    finally:
        set_script_prefix("/")

    assert url.endswith("/canopy/api/inbound/gmail/dimagi/"), url


def test_push_url_reverses_rather_than_falling_back(monkeypatch):
    """The fallback must stay unreachable in practice.

    Naming a namespace that does not exist (Ninja's ``api-1.0.0`` default, where
    this API declares ``api_v2``) made ``reverse`` raise on every call, and the
    bare ``except`` turned that into a plausible-looking wrong URL instead of a
    loud failure — so the route was never actually consulted.

    Patching ``reverse`` to a sentinel proves the service reaches it: if the name
    is wrong again, the call raises, the except branch hands back a hand-built
    path, and the sentinel is absent. Fixing only the fallback's prefix would
    satisfy the test above while leaving the route unreversed; this catches that.
    """
    import django.urls
    from django.test import RequestFactory

    from apps.inbound import services

    # `push_url` imports from django.urls at call time, so patching there lands.
    monkeypatch.setattr(
        django.urls, "reverse",
        lambda name, kwargs=None: f"/sentinel/{name}/{kwargs['workspace']}/",
    )
    url = services.push_url(RequestFactory().get("/api/inbound/config/dimagi"), "dimagi")

    assert "/sentinel/api_v2:gmail_push/dimagi/" in url, url


def test_owner_can_set_config(owner, workspace):
    r = _c(owner).put(
        "/api/inbound/config/dimagi",
        {"audience": "https://x/api/inbound/gmail/dimagi/",
         "service_account": "push@p.iam.gserviceaccount.com",
         "watch_topic": "projects/p/topics/t"},
        content_type="application/json",
    )
    assert r.status_code == 200, r.content
    assert r.json()["verifies"] is True
    cfg = InboundPushConfig.objects.get()
    assert cfg.watch_topic == "projects/p/topics/t"


def test_a_non_owner_member_can_read_but_not_write(editor, workspace):
    assert _c(editor).get("/api/inbound/config/dimagi").status_code == 200
    r = _c(editor).put(
        "/api/inbound/config/dimagi", {"audience": "x"}, content_type="application/json"
    )
    assert r.status_code == 403


def test_a_non_member_gets_404_not_403(outsider, workspace):
    """Never a role hint — a stranger shouldn't learn the workspace exists."""
    assert _c(outsider).get("/api/inbound/config/dimagi").status_code == 404
    assert _c(outsider).put(
        "/api/inbound/config/dimagi", {"audience": "x"}, content_type="application/json"
    ).status_code == 404


def test_config_is_per_workspace(owner, workspace, outsider):
    _c(owner).put(
        "/api/inbound/config/dimagi",
        {"audience": "aud-dimagi", "service_account": "a@b", "watch_topic": "t1"},
        content_type="application/json",
    )
    _c(outsider).put(
        "/api/inbound/config/otherco",
        {"audience": "aud-otherco", "service_account": "c@d", "watch_topic": "t2"},
        content_type="application/json",
    )
    assert InboundPushConfig.objects.get(workspace_id="dimagi").audience == "aud-dimagi"
    assert InboundPushConfig.objects.get(workspace_id="otherco").audience == "aud-otherco"


# ── mailboxes ────────────────────────────────────────────────────────────────


def test_owner_registers_a_mailbox(owner, workspace, agent):
    r = _c(owner).post(
        "/api/inbound/mailboxes/dimagi",
        {"address": "Eva@Dimagi-AI.com", "agent_slug": "eva"},
        content_type="application/json",
    )
    assert r.status_code == 200, r.content
    assert r.json()["address"] == "eva@dimagi-ai.com"  # normalised
    assert r.json()["watch_state"] == "none"


def test_registering_an_agent_from_another_workspace_422s(owner, workspace, outsider):
    Agent.objects.create(slug="zed", name="Z", workspace=Workspace.objects.get(slug="otherco"))
    r = _c(owner).post(
        "/api/inbound/mailboxes/dimagi",
        {"address": "zed@dimagi-ai.com", "agent_slug": "zed"},
        content_type="application/json",
    )
    assert r.status_code == 422


def test_a_duplicate_address_409s_without_naming_the_other_workspace(
    owner, workspace, agent, outsider
):
    """The address column is globally unique because a Gmail push carries nothing
    else to disambiguate on — but which tenant holds it is not the caller's business."""
    other = Workspace.objects.get(slug="otherco")
    other_agent = Agent.objects.create(slug="zed", name="Z", workspace=other)
    InboundMailbox.objects.create(address="shared@dimagi-ai.com", agent=other_agent)
    r = _c(owner).post(
        "/api/inbound/mailboxes/dimagi",
        {"address": "shared@dimagi-ai.com", "agent_slug": "eva"},
        content_type="application/json",
    )
    assert r.status_code == 409
    assert "otherco" not in r.content.decode()


def test_list_is_workspace_scoped(owner, workspace, agent, outsider):
    InboundMailbox.objects.create(address="eva@dimagi-ai.com", agent=agent)
    other = Workspace.objects.get(slug="otherco")
    InboundMailbox.objects.create(
        address="zed@dimagi-ai.com",
        agent=Agent.objects.create(slug="zed", name="Z", workspace=other),
    )
    items = _c(owner).get("/api/inbound/mailboxes/dimagi").json()["items"]
    assert [i["address"] for i in items] == ["eva@dimagi-ai.com"]


def test_toggle_and_delete(owner, workspace, agent):
    mb = InboundMailbox.objects.create(address="eva@dimagi-ai.com", agent=agent)
    r = _c(owner).patch(
        f"/api/inbound/mailboxes/dimagi/{mb.pk}",
        {"enabled": False},
        content_type="application/json",
    )
    assert r.json()["enabled"] is False
    assert _c(owner).delete(f"/api/inbound/mailboxes/dimagi/{mb.pk}").status_code == 204
    assert not InboundMailbox.objects.exists()


def test_a_non_owner_cannot_mutate_mailboxes(editor, workspace, agent):
    mb = InboundMailbox.objects.create(address="eva@dimagi-ai.com", agent=agent)
    assert _c(editor).post(
        "/api/inbound/mailboxes/dimagi",
        {"address": "x@dimagi-ai.com", "agent_slug": "eva"},
        content_type="application/json",
    ).status_code == 403
    assert _c(editor).delete(f"/api/inbound/mailboxes/dimagi/{mb.pk}").status_code == 403


def test_another_workspaces_mailbox_is_not_mutable(outsider, workspace, agent):
    mb = InboundMailbox.objects.create(address="eva@dimagi-ai.com", agent=agent)
    assert _c(outsider).delete(f"/api/inbound/mailboxes/otherco/{mb.pk}").status_code == 404


@pytest.mark.parametrize(
    "delta,expected",
    [
        (dt.timedelta(days=5), "armed"),
        (dt.timedelta(hours=3), "expiring"),
        (dt.timedelta(hours=-1), "expired"),
    ],
)
def test_watch_state_is_computed_server_side(owner, workspace, agent, delta, expected):
    """One definition, so a green badge can never disagree with a watch.expired row."""
    InboundMailbox.objects.create(
        address="eva@dimagi-ai.com", agent=agent, watch_expires_at=timezone.now() + delta
    )
    items = _c(owner).get("/api/inbound/mailboxes/dimagi").json()["items"]
    assert items[0]["watch_state"] == expected


# ── what the runner reads ────────────────────────────────────────────────────


def test_runner_mailboxes_serve_the_configured_topic(owner, workspace, agent):
    InboundPushConfig.objects.create(workspace=workspace, watch_topic="projects/p/topics/t")
    InboundMailbox.objects.create(address="eva@dimagi-ai.com", agent=agent)
    items = _c(owner).get("/api/inbound/runner-mailboxes").json()["items"]
    assert items == [{"address": "eva@dimagi-ai.com", "watch_topic": "projects/p/topics/t"}]


def test_a_workspace_with_no_topic_arms_nothing(owner, workspace, agent):
    InboundMailbox.objects.create(address="eva@dimagi-ai.com", agent=agent)
    assert _c(owner).get("/api/inbound/runner-mailboxes").json()["items"] == []


def test_a_disabled_mailbox_is_not_armed(owner, workspace, agent):
    InboundPushConfig.objects.create(workspace=workspace, watch_topic="projects/p/topics/t")
    InboundMailbox.objects.create(address="eva@dimagi-ai.com", agent=agent, enabled=False)
    assert _c(owner).get("/api/inbound/runner-mailboxes").json()["items"] == []


def test_runner_mailboxes_are_scoped_to_the_callers_workspaces(outsider, workspace, agent):
    InboundPushConfig.objects.create(workspace=workspace, watch_topic="projects/p/topics/t")
    InboundMailbox.objects.create(address="eva@dimagi-ai.com", agent=agent)
    assert _c(outsider).get("/api/inbound/runner-mailboxes").json()["items"] == []
