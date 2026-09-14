"""Registering an embedding app has to be possible on a real deployment.

Every fact the widget needs — the credential, the origins allowed to frame it,
the agents it may offer — had a management command and no other route. There is
nowhere to run one: the deployed service has `EnableExecuteCommand` off, so
there is no `aws ecs execute-command`, and the RDS instance is VPC-internal, so
there is no laptop shell either. Registering an app was therefore not actually
possible on a deployment, which makes the admin the load-bearing path rather
than a convenience.

The case that must not fail quietly is creation: `create_credential` is the only
writer that generates a token, so a bare `save()` through the admin would leave
a row whose `token_hash` is empty — a credential that authenticates nothing,
with nothing to show it went wrong.
"""

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from apps.agents.models import Agent
from apps.tokens.models import AppCredential, AppCredentialAgent
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

LABS = "https://labs.connect.dimagi.com"


def _staff():
    user = User.objects.create_user("root", "root@dimagi.com", "pw", is_staff=True, is_superuser=True)
    c = Client()
    c.force_login(user)
    return user, c


def _agent(slug="labs-helper"):
    owner = User.objects.create_user(f"o-{slug}", f"o-{slug}@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return Agent.objects.create(slug=slug, name=slug.title(), workspace=ws)


def _add_url():
    return reverse("admin:tokens_appcredential_add")


def _form(**overrides):
    body = {
        "name": "connect-labs",
        "allowed_delegation_domains": '["dimagi.com"]',
        "allowed_frame_origins": f'["{LABS}"]',
        "provision_role": "editor",
        # Management-form fields for the allowed-agents inline.
        "allowed_agents-TOTAL_FORMS": "0",
        "allowed_agents-INITIAL_FORMS": "0",
        "allowed_agents-MIN_NUM_FORMS": "0",
        "allowed_agents-MAX_NUM_FORMS": "1000",
    }
    body.update(overrides)
    return body


def test_creating_through_the_admin_mints_a_usable_credential():
    """The defect this guards: a bare save() leaves token_hash empty, so the
    credential exists and authenticates nothing."""
    _user, c = _staff()

    c.post(_add_url(), _form(), follow=True)

    cred = AppCredential.objects.get(name="connect-labs")
    assert cred.token_hash, "created with no token — it would authenticate nothing"
    assert len(cred.token_hash) == 64  # sha256 hex


def test_the_raw_credential_is_shown_once_and_is_the_real_one():
    _user, c = _staff()

    response = c.post(_add_url(), _form(), follow=True)

    messages = [str(m) for m in response.context["messages"]]
    shown = [m for m in messages if "Copy it now" in m]
    assert shown, f"the raw value was never surfaced: {messages}"

    raw = shown[0].rsplit(" ", 1)[-1]
    # It must actually authenticate — a message that prints something other
    # than the real token is worse than no message.
    assert AppCredential.lookup(raw) == AppCredential.objects.get(name="connect-labs")


def test_frame_origins_are_saved_so_the_widget_can_load():
    _user, c = _staff()
    c.post(_add_url(), _form(), follow=True)
    assert AppCredential.objects.get(name="connect-labs").frame_origins() == [LABS]


def test_the_self_app_registers_with_no_delegation_domains():
    """Empty is a real configuration, and the one canopy's own widget wants.

    The self-embed mints through `POST /api/embed/token`, which is
    session-authenticated and asks no domain question — so the credential
    needs to vouch for nobody. While the field was required, registering it
    forced an operator to grant a domain the app never uses, which turns a
    credential that can only frame a shell into one that can impersonate every
    user in that domain.
    """
    _user, c = _staff()

    c.post(_add_url(), _form(name="canopy-web", allowed_delegation_domains="[]"), follow=True)

    cred = AppCredential.objects.get(name="canopy-web")
    # A list, not None: `exchange_api` iterates this field directly.
    assert cred.allowed_delegation_domains == []
    assert cred.frame_origins() == [LABS]


def test_the_shell_then_serves_for_it():
    """The 404 an unregistered app returns is exactly this row missing."""
    _user, c = _staff()
    c.post(_add_url(), _form(name="canopy-web", allowed_delegation_domains="[]"), follow=True)

    response = Client().get("/embed/chat?app=canopy-web")

    assert response.status_code == 200
    assert LABS in response.headers["Content-Security-Policy"]


def test_an_address_is_not_a_domain():
    """`allowed_delegation_domains` is compared against the part after the @,
    so an address here matches nobody and reads as a working grant."""
    _user, c = _staff()

    response = c.post(_add_url(), _form(allowed_delegation_domains='["jj@dimagi.com"]'))

    assert not AppCredential.objects.filter(name="connect-labs").exists()
    assert b"Not email domains" in response.content


def test_a_wildcard_origin_is_refused_at_the_form():
    """It would be filtered on read anyway, but silently — an admin who typed
    it would believe the grant was in force."""
    _user, c = _staff()

    response = c.post(_add_url(), _form(allowed_frame_origins='["*"]'))

    assert response.status_code == 200  # re-rendered with errors, not saved
    assert not AppCredential.objects.filter(name="connect-labs").exists()
    assert b"no wildcard" in response.content


def test_a_path_origin_is_refused_at_the_form():
    _user, c = _staff()
    response = c.post(_add_url(), _form(allowed_frame_origins=f'["{LABS}/supply"]'))
    assert not AppCredential.objects.filter(name="connect-labs").exists()
    assert b"Not valid origins" in response.content


def test_agents_can_be_allowed_while_registering():
    """"Register the app" and "decide what it may offer" are one act."""
    agent = _agent()
    _user, c = _staff()

    c.post(
        _add_url(),
        _form(
            **{
                "allowed_agents-TOTAL_FORMS": "1",
                "allowed_agents-0-agent": agent.pk,
            }
        ),
        follow=True,
    )

    cred = AppCredential.objects.get(name="connect-labs")
    assert list(
        AppCredentialAgent.objects.filter(app=cred).values_list("agent__slug", flat=True)
    ) == [agent.slug]


def test_editing_does_not_re_mint():
    """A second token would silently invalidate whatever the host already has
    deployed."""
    _user, c = _staff()
    c.post(_add_url(), _form(), follow=True)
    cred = AppCredential.objects.get(name="connect-labs")
    before = cred.token_hash

    c.post(
        reverse("admin:tokens_appcredential_change", args=[cred.pk]),
        _form(allowed_frame_origins=f'["{LABS}", "http://localhost:8000"]'),
        follow=True,
    )

    cred.refresh_from_db()
    assert cred.token_hash == before
    assert cred.frame_origins() == [LABS, "http://localhost:8000"]


def test_the_token_hash_is_not_an_editable_field():
    """The raw value is unrecoverable after creation, so a hash in the form is
    an invitation to paste something into it."""
    _user, c = _staff()
    body = c.get(_add_url()).content.decode()
    assert 'name="token_hash"' not in body


def test_a_non_staff_user_cannot_reach_it():
    User.objects.create_user("plain", "plain@dimagi.com", "pw")
    c = Client()
    c.login(username="plain", password="pw")
    response = c.get(_add_url())
    assert response.status_code in (302, 403)


def test_revoking_is_available_as_an_action():
    _user, c = _staff()
    c.post(_add_url(), _form(), follow=True)
    cred = AppCredential.objects.get(name="connect-labs")

    c.post(
        reverse("admin:tokens_appcredential_changelist"),
        {"action": "revoke_selected", "_selected_action": [str(cred.pk)]},
        follow=True,
    )

    cred.refresh_from_db()
    assert cred.revoked_at is not None
    # And the widget stops serving for it, which is the point.
    assert Client().get("/embed/chat?app=connect-labs").status_code == 404
