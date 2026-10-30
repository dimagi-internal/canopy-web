"""Small vault helpers that decide who can open what, pinned on their own.

`device_keys.htu_matches` (the host and path a DPoP proof must name) and the delegated-session
clock (`accounts.session_until` / `drop_ended_sessions`).
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory
from django.utils import timezone

from apps.agents.credentials import accounts
from apps.agents.models import Agent, AgentCredential, VaultEntryAccount
from apps.harness import device_keys
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db


# ---- the host and path a device proof must name ---------------------------------------

def _request(path="/api/agents/eva/credentials/resolve", host="testserver"):
    return RequestFactory().get(path, HTTP_HOST=host)


@pytest.mark.parametrize("htu", [
    "http://testserver/api/agents/eva/credentials/resolve",           # the host the request came to
    "https://testserver/api/agents/eva/credentials/resolve",
    "https://testserver/canopy/api/agents/eva/credentials/resolve",   # the old address's prefix
    "https://testserver/api/w/dimagi/agents/eva/credentials/resolve",  # a tenant URL rewritten to the flat route
])
def test_a_proof_naming_this_endpoint_on_this_host_matches(htu):
    assert device_keys.htu_matches(htu, _request())


@pytest.mark.parametrize("htu", [
    "",
    "ftp://testserver/api/agents/eva/credentials/resolve",
    "https://evil.example/api/agents/eva/credentials/resolve",          # a host this deployment does not answer to
    "https://testserver/api/agents/ada/credentials/resolve",           # another agent's endpoint
    "https://testserver/api/agents/eva/credentials/resolve?x=1",       # a query is never part of what is signed
    "https://testserver/api/agents/eva/credentials/resolve#frag",
])
def test_a_proof_naming_anything_else_does_not(htu):
    assert not device_keys.htu_matches(htu, _request())


# ---- how long a delegated session lasts -----------------------------------------------

def _entry(agent, until, name="github-token"):
    return AgentCredential.objects.create(agent=agent, name=name, account=VaultEntryAccount.DELEGATED,
                                          delegated_by=agent.owner, provider="github",
                                          identity={"session_until": until} if until is not None else {})


@pytest.fixture
def agent():
    owner = get_user_model().objects.create_user(username="o", email="o@dimagi.com")
    return Agent.objects.create(slug="eva", name="Eva", workspace=a_workspace(), owner=owner)


def test_a_naive_session_end_is_read_as_utc_so_the_comparison_cannot_raise(agent):
    soon = (timezone.now() + dt.timedelta(hours=1)).replace(tzinfo=None).isoformat()
    entry = _entry(agent, soon)
    assert accounts.session_until(entry).tzinfo is not None
    assert not accounts.session_ended(entry)


def test_an_unreadable_session_end_counts_as_ended(agent):
    assert accounts.session_ended(_entry(agent, "not a date"))


def test_an_entry_with_no_session_never_ends(agent):
    entry = _entry(agent, None)
    assert accounts.session_until(entry) is None
    assert not accounts.session_ended(entry)


def test_listing_drops_the_sessions_that_ended_and_deletes_them(agent):
    past = (timezone.now() - dt.timedelta(hours=1)).isoformat()
    future = (timezone.now() + dt.timedelta(hours=1)).isoformat()
    over, live = _entry(agent, past, "a"), _entry(agent, future, "b")
    kept = accounts.drop_ended_sessions([over, live])
    assert [e.pk for e in kept] == [live.pk]
    assert not AgentCredential.objects.filter(pk=over.pk).exists()
    assert AgentCredential.objects.filter(pk=live.pk).exists()
