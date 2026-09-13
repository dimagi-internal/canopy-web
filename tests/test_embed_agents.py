"""Which agents may an embedding app offer, to this user.

The question the widget's agent picker asks is a THREE-way one — agent x host x
user — and nothing modelled it. `AppCredential` covered app x tenant and had no
agent relation at all; `Agent.workspace` covers agent x tenant. So a host picked
its agent in its own settings (ace-web: `CANOPY_AGENT_SLUG`, default "ace") and
canopy had no record of, or say in, the choice.

`AppCredentialAgent` adds the missing edge as explicit server-side rows, and
`GET /api/embed/agents` returns the intersection: agents this app is allowed to
target, that this user can reach through their own memberships. Both conditions,
neither sufficient alone.

The security property under test is that the APP comes from the bearer token,
never from anything the caller sends — otherwise one host could enumerate (or
borrow) another host's allowlist.
"""

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.tokens.models import AppCredential, AppCredentialAgent, DelegatedToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ws(slug, user, role=WorkspaceMembership.EDITOR):
    ws = Workspace.objects.create(slug=slug, display_name=slug.upper(), created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=role)
    return ws


def _agent(slug, ws):
    return Agent.objects.create(slug=slug, name=slug.title(), workspace=ws)


def _app(name, *, domains=("dimagi.com",)):
    admin = User.objects.create_user(f"admin-{name}", f"admin-{name}@dimagi.com", "pw")
    return AppCredential.create_credential(name=name, domains=list(domains), created_by=admin)


def _bearer(app, user):
    raw, _tok = DelegatedToken.issue(app=app, user=user, ttl_seconds=3600)
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


def test_returns_only_agents_the_app_is_allowed_to_target():
    user = User.objects.create_user("u", "u@dimagi.com", "pw")
    ws = _ws("w1", user)
    allowed, not_allowed = _agent("labs-helper", ws), _agent("secret-agent", ws)
    _raw, app = _app("connect-labs")
    AppCredentialAgent.objects.create(app=app, agent=allowed)

    body = Client().get("/api/embed/agents", **_bearer(app, user)).json()
    assert [a["slug"] for a in body] == ["labs-helper"]
    assert not_allowed.slug not in {a["slug"] for a in body}


def test_excludes_an_allowlisted_agent_the_user_cannot_reach():
    """Allowlisted is not sufficient — the user must also be a member of the
    agent's tenant. This is the half a host-side settings constant could never
    express."""
    user = User.objects.create_user("u", "u@dimagi.com", "pw")
    _ws("mine", user)
    theirs = Workspace.objects.create(slug="theirs", display_name="Theirs", created_by=user)
    stranger = _agent("stranger", theirs)  # user has NO membership in `theirs`
    _raw, app = _app("connect-labs")
    AppCredentialAgent.objects.create(app=app, agent=stranger)

    body = Client().get("/api/embed/agents", **_bearer(app, user)).json()
    assert body == []


def test_one_apps_allowlist_is_invisible_to_another_app():
    """The app is taken from the bearer token, so holding app B's token cannot
    surface app A's agents."""
    user = User.objects.create_user("u", "u@dimagi.com", "pw")
    ws = _ws("w1", user)
    a_agent, b_agent = _agent("a-only", ws), _agent("b-only", ws)
    _r1, app_a = _app("app-a")
    _r2, app_b = _app("app-b")
    AppCredentialAgent.objects.create(app=app_a, agent=a_agent)
    AppCredentialAgent.objects.create(app=app_b, agent=b_agent)

    body = Client().get("/api/embed/agents", **_bearer(app_b, user)).json()
    assert [a["slug"] for a in body] == ["b-only"]


def test_a_session_cookie_cannot_ask_this_question():
    """There is no app behind a browser session, so there is no allowlist to
    apply. Answering with the user's whole agent list would quietly turn the
    endpoint into an unscoped agent index."""
    user = User.objects.create_user("u", "u@dimagi.com", "pw")
    ws = _ws("w1", user)
    _agent("labs-helper", ws)
    c = Client()
    c.force_login(user)
    assert c.get("/api/embed/agents").status_code == 403


def test_an_app_with_no_allowlist_offers_nothing():
    """Fail closed. An unconfigured credential grants no agents, the same way an
    empty `allowed_delegation_domains` grants no domains."""
    user = User.objects.create_user("u", "u@dimagi.com", "pw")
    ws = _ws("w1", user)
    _agent("labs-helper", ws)
    _raw, app = _app("connect-labs")

    body = Client().get("/api/embed/agents", **_bearer(app, user)).json()
    assert body == []


def test_revoked_app_credential_offers_nothing():
    user = User.objects.create_user("u", "u@dimagi.com", "pw")
    ws = _ws("w1", user)
    agent = _agent("labs-helper", ws)
    _raw, app = _app("connect-labs")
    AppCredentialAgent.objects.create(app=app, agent=agent)
    headers = _bearer(app, user)
    from django.utils import timezone
    AppCredential.objects.filter(pk=app.pk).update(revoked_at=timezone.now())

    assert Client().get("/api/embed/agents", **headers).status_code == 403


def test_the_same_agent_may_be_offered_by_two_apps():
    """Nothing about the allowlist is exclusive — an agent can be embedded in
    several products at once."""
    user = User.objects.create_user("u", "u@dimagi.com", "pw")
    ws = _ws("w1", user)
    shared = _agent("shared", ws)
    _r1, app_a = _app("app-a")
    _r2, app_b = _app("app-b")
    AppCredentialAgent.objects.create(app=app_a, agent=shared)
    AppCredentialAgent.objects.create(app=app_b, agent=shared)

    for app in (app_a, app_b):
        body = Client().get("/api/embed/agents", **_bearer(app, user)).json()
        assert [a["slug"] for a in body] == ["shared"]


def test_rows_are_unique_per_app_and_agent():
    user = User.objects.create_user("u", "u@dimagi.com", "pw")
    ws = _ws("w1", user)
    agent = _agent("labs-helper", ws)
    _raw, app = _app("connect-labs")
    AppCredentialAgent.objects.create(app=app, agent=agent)

    from django.db import IntegrityError, transaction
    with pytest.raises(IntegrityError), transaction.atomic():
        AppCredentialAgent.objects.create(app=app, agent=agent)


def test_payload_carries_what_a_picker_needs():
    user = User.objects.create_user("u", "u@dimagi.com", "pw")
    ws = _ws("w1", user)
    Agent.objects.create(slug="labs-helper", name="Labs Helper", workspace=ws,
                         description="Knows connect-labs", avatar_url="https://x/a.png")
    _raw, app = _app("connect-labs")
    AppCredentialAgent.objects.create(app=app, agent=Agent.objects.get(slug="labs-helper"))

    row = Client().get("/api/embed/agents", **_bearer(app, user)).json()[0]
    assert row["slug"] == "labs-helper"
    assert row["name"] == "Labs Helper"
    assert row["description"] == "Knows connect-labs"
    assert row["avatar_url"] == "https://x/a.png"
    assert row["workspace"] == "w1"          # which tenant the session will land in


# --- the grant command (the allowlist has to be operable without a prod shell) ---


def _run(*args):
    from io import StringIO
    from django.core.management import call_command
    out = StringIO()
    call_command("grant_app_agent", *args, stdout=out)
    return out.getvalue()


def test_grant_command_is_idempotent():
    """The operational shape is "make sure this is allowed", often from a script,
    so a re-run must not be an IntegrityError."""
    user = User.objects.create_user("u", "u@dimagi.com", "pw")
    ws = _ws("w1", user)
    _agent("labs-helper", ws)
    _raw, app = _app("connect-labs")

    assert "may now offer" in _run("--name", "connect-labs", "--agent", "labs-helper")
    assert "already offers" in _run("--name", "connect-labs", "--agent", "labs-helper")
    assert AppCredentialAgent.objects.filter(app=app).count() == 1


def test_grant_command_revokes_and_reports_when_there_was_nothing_to_revoke():
    user = User.objects.create_user("u", "u@dimagi.com", "pw")
    ws = _ws("w1", user)
    _agent("labs-helper", ws)
    _raw, app = _app("connect-labs")
    _run("--name", "connect-labs", "--agent", "labs-helper")

    assert "may no longer offer" in _run("--name", "connect-labs", "--agent", "labs-helper", "--revoke")
    assert not AppCredentialAgent.objects.filter(app=app).exists()
    assert "nothing to do" in _run("--name", "connect-labs", "--agent", "labs-helper", "--revoke")


def test_grant_command_refuses_unknown_names_rather_than_creating_them():
    """A mistyped slug must fail loudly — silently offering the wrong agent to a
    host's whole user base is the failure this command exists to prevent."""
    from django.core.management.base import CommandError
    user = User.objects.create_user("u", "u@dimagi.com", "pw")
    ws = _ws("w1", user)
    _agent("labs-helper", ws)
    _app("connect-labs")

    with pytest.raises(CommandError, match="does not exist"):
        _run("--name", "connect-labs", "--agent", "labs-helpr")
    with pytest.raises(CommandError, match="does not exist"):
        _run("--name", "connect-labz", "--agent", "labs-helper")


def test_grant_command_list_says_so_when_nothing_is_offered():
    """Fail-closed is easy to mistake for broken, so --list names it."""
    _app("connect-labs")
    assert "NO agents" in _run("--name", "connect-labs", "--list")
