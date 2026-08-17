"""A mailbox nobody can arm has to be loud, and a parked runner has to be quiet.

``note_watch_state`` used to take only an expiry, and returned early on a null one
— so the single most useful thing a runner could say ("I am supposed to be watching
this and I can't") had nowhere to go. The failure then surfaced days later as
``gmail.watch.expired``, which blames the clock instead of the credential.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.events.models import Event
from apps.inbound import services
from apps.inbound.models import InboundMailbox, InboundPushConfig
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

ERROR = "https://oauth2.googleapis.com/token -> 401: unauthorized_client"


@pytest.fixture()
def user():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture()
def workspace(user):
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role="owner")
    return ws


@pytest.fixture()
def mailbox(workspace, user):
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=workspace)
    InboundPushConfig.objects.create(workspace=workspace, audience="a", service_account="s")
    return InboundMailbox.objects.create(address="echo@dimagi-ai.com", agent=agent)


def _events(kind):
    return Event.objects.filter(kind=kind)


# ── the runner cannot arm ────────────────────────────────────────────────────


def test_a_reported_failure_records_an_error_event(mailbox):
    services.note_watch_state(mailbox, None, error=ERROR)

    event = _events("gmail.watch.failed").get()
    assert event.level == "error"
    assert mailbox.address in event.summary
    assert event.payload["error"] == ERROR


def test_a_reported_failure_shows_on_the_mailbox(mailbox):
    services.note_watch_state(mailbox, None, error=ERROR)

    mailbox.refresh_from_db()
    assert mailbox.watch_error == ERROR
    # The badge must agree with the row — a mailbox with a live-looking expiry
    # that cannot be re-armed is exactly the green-badge-over-an-error-row case
    # `watch_state` exists to prevent.
    assert services.watch_state(mailbox) == "failed"


def test_a_failure_outranks_a_still_valid_expiry(mailbox):
    later = timezone.now() + dt.timedelta(days=6)
    services.note_watch_state(mailbox, later)
    assert services.watch_state(mailbox) == "armed"

    services.note_watch_state(mailbox, None, error=ERROR)
    mailbox.refresh_from_db()
    assert services.watch_state(mailbox) == "failed"


# ── recovery + retraction ────────────────────────────────────────────────────


def test_a_successful_arm_resolves_the_failure(mailbox):
    services.note_watch_state(mailbox, None, error=ERROR)
    services.note_watch_state(mailbox, timezone.now() + dt.timedelta(days=7))

    mailbox.refresh_from_db()
    assert mailbox.watch_error == ""
    assert services.watch_state(mailbox) == "armed"
    # Events coalesce on (workspace, source, key), so the recovery flips the SAME
    # row rather than leaving a stale error behind next to a new info row.
    assert not _events("gmail.watch.failed").exists()
    assert _events("gmail.watch.armed").get().level == "info"


def test_retracting_without_an_expiry_also_clears(mailbox):
    """What a paused runner sends: no watch, no complaint."""
    services.note_watch_state(mailbox, None, error=ERROR)
    services.note_watch_state(mailbox, None, error="")

    mailbox.refresh_from_db()
    assert mailbox.watch_error == ""
    assert not _events("gmail.watch.failed").exists()


def test_a_quiet_mailbox_stays_quiet(mailbox):
    """A healthy report must not manufacture rows nobody asked for."""
    services.note_watch_state(mailbox, timezone.now() + dt.timedelta(days=7))
    assert Event.objects.count() == 0


# ── the wire ─────────────────────────────────────────────────────────────────


def test_the_api_accepts_an_error_report(client_logged_in, mailbox):
    resp = client_logged_in.post(
        "/api/inbound/watch/",
        data={"address": mailbox.address, "expires_at": None, "error": ERROR},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    mailbox.refresh_from_db()
    assert mailbox.watch_error == ERROR
    assert _events("gmail.watch.failed").exists()


@pytest.fixture()
def client_logged_in(user):
    c = Client()
    c.force_login(user)
    return c
