"""The Connection model and the provider registry."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import override_settings

from apps.agents.models import Agent
from apps.connections import providers
from apps.connections.models import Connection, ConnectionGrant
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

SF = {
    "SALESFORCE_CLIENT_ID": "3MVG-client",
    "SALESFORCE_CLIENT_SECRET": "shh",
    "SALESFORCE_LOGIN_URL": "https://login.salesforce.com",
}


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")


@pytest.fixture
def agent(user):
    ws = Workspace.objects.create(slug="w", display_name="W", created_by=user)
    return Agent.objects.create(slug="eva", name="Eva", workspace=ws)


def _conn(**kw):
    kw.setdefault("provider", "salesforce")
    kw.setdefault("account_id", "00D1/005A")
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


def test_registry_returns_salesforce_and_rejects_unknown():
    sf = providers.get("salesforce")
    assert sf.name == "salesforce"
    assert providers.ALL["salesforce"] is sf
    assert sf.rotates_refresh_token is True
    with pytest.raises(providers.UnknownProvider):
        providers.get("nope")


def test_salesforce_not_configured_without_settings():
    sf = providers.get("salesforce")
    with override_settings(SALESFORCE_CLIENT_ID="", SALESFORCE_CLIENT_SECRET=""):
        assert sf.is_configured() is False
    with override_settings(**SF):
        assert sf.is_configured() is True


def test_salesforce_authorize_url_shape():
    sf = providers.get("salesforce")
    with override_settings(**{**SF, "SALESFORCE_LOGIN_URL": "https://test.salesforce.com"}):
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
