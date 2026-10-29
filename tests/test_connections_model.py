"""The Connection and ConnectionApp models and the provider registry."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError

from apps.agents.models import Agent
from apps.connections import providers
from apps.connections.models import Connection, ConnectionApp, ConnectionGrant
from apps.workspaces.models import Workspace
from tests.fixtures.connections import sf_app

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")


@pytest.fixture
def ws(user):
    return Workspace.objects.create(slug="w", display_name="W", created_by=user)


@pytest.fixture
def agent(ws):
    return Agent.objects.create(slug="eva", name="Eva", workspace=ws)


@pytest.fixture(autouse=True)
def app(ws):
    return sf_app(ws)


def _conn(**kw):
    kw.setdefault("provider", "salesforce")
    kw.setdefault("account_id", "00D1/005A")
    kw.setdefault("app", ConnectionApp.objects.get(workspace_id="w", provider="salesforce"))
    return Connection(**kw)


def test_exactly_one_owner_enforced(user, agent):
    with pytest.raises(IntegrityError), transaction.atomic():
        _conn(user=user, agent=agent, connected_by=user).save()
    with pytest.raises(IntegrityError), transaction.atomic():
        _conn(connected_by=user).save()
    _conn(user=user, connected_by=user).save()
    _conn(agent=agent, connected_by=user).save()


def test_unique_per_owner_provider_account(user, agent):
    _conn(user=user, connected_by=user).save()
    with pytest.raises(IntegrityError), transaction.atomic():
        _conn(user=user, connected_by=user).save()
    # a different account for the same owner is fine
    _conn(user=user, connected_by=user, account_id="00D1/005B").save()
    _conn(agent=agent, connected_by=user).save()
    with pytest.raises(IntegrityError), transaction.atomic():
        _conn(agent=agent, connected_by=user).save()


def test_needs_reconnect_states(user):
    c = _conn(user=user, connected_by=user)
    assert c.needs_reconnect is True  # no refresh token
    c.refresh_token_enc = "x"
    assert c.needs_reconnect is False
    from django.utils import timezone

    c.refresh_failed_at = timezone.now()
    assert c.needs_reconnect is True


def test_owner_label(user, agent):
    assert _conn(user=user, connected_by=user).owner_label
    assert "eva" in _conn(agent=agent, connected_by=user).owner_label


def test_grant_only_for_agent_owned_connections(user, agent):
    ws = agent.workspace
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    personal = _conn(user=user, connected_by=user)
    personal.save()
    with pytest.raises(ValidationError):
        ConnectionGrant(connection=personal, agent=echo, granted_by=user).save()
    owned = _conn(agent=agent, connected_by=user, account_id="00D1/005Z")
    owned.save()
    ConnectionGrant(connection=owned, agent=echo, granted_by=user).save()
    with pytest.raises(IntegrityError), transaction.atomic():
        ConnectionGrant(connection=owned, agent=echo, granted_by=user).save()


def test_registry_returns_salesforce_and_rejects_unknown(app):
    cls = providers.get_class("salesforce")
    assert cls.name == "salesforce" and cls.title == "Salesforce"
    assert providers.CLASSES["salesforce"] is cls
    assert cls.rotates_refresh_token is True
    bound = providers.for_app(app)
    assert isinstance(bound, cls) and bound.app is app
    with pytest.raises(providers.UnknownProvider):
        providers.get_class("nope")


def test_salesforce_not_configured_without_an_app_or_its_secret(app):
    cls = providers.get_class("salesforce")
    assert cls().is_configured() is False                       # no app at all
    assert providers.for_app(app).is_configured() is True
    app.client_secret = ""
    assert providers.for_app(app).is_configured() is False
    app.client_secret, app.client_id = "shh", ""
    assert providers.for_app(app).is_configured() is False


def test_app_secret_is_stored_encrypted(app):
    app.refresh_from_db()
    assert app.client_secret == "shh" and "shh" not in app.client_secret_enc
    app.client_secret_enc = "garbage"
    assert app.client_secret == ""                              # unreadable reads as missing


def test_one_app_per_workspace_and_provider(ws, app):
    with pytest.raises(IntegrityError), transaction.atomic():
        ConnectionApp.objects.create(workspace=ws, provider="salesforce", client_id="other")


def test_an_app_with_connections_cannot_be_deleted_directly(user, app):
    _conn(user=user, connected_by=user).save()
    with pytest.raises(ProtectedError):
        app.delete()


def test_the_same_account_through_two_workspaces_apps_is_two_rows(user):
    other = Workspace.objects.create(slug="w2", display_name="W2", created_by=user)
    _conn(user=user, connected_by=user).save()
    _conn(user=user, connected_by=user, app=sf_app(other)).save()
    assert Connection.objects.filter(user=user, account_id="00D1/005A").count() == 2


def test_salesforce_authorize_url_shape(app):
    app.options = {**app.options, "login_url": "https://test.salesforce.com"}
    sf = providers.for_app(app)
    url = sf.authorize_url("st8", "https://canopy.test/cb", "chal")
    p = urlparse(url)
    assert f"{p.scheme}://{p.netloc}{p.path}" == "https://test.salesforce.com/services/oauth2/authorize"
    q = {k: v[0] for k, v in parse_qs(p.query).items()}
    assert q["response_type"] == "code"
    assert q["client_id"] == "3MVG-client"
    assert q["redirect_uri"] == "https://canopy.test/cb"
    assert q["state"] == "st8"
    assert q["code_challenge"] == "chal"
    assert q["code_challenge_method"] == "S256"
    assert q["scope"] == "api refresh_token"
