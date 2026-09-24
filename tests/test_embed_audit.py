"""The audit trail: who acted as whom, through which app.

The credential surfaces wrote `logger.info`/`logger.warning` and nothing else.
Application logs cannot answer the question an audit trail exists for — *did
anything act as me, and what let it* — because they are not queryable per user
or per app, they age out on a retention policy chosen for debugging, and on a
container they are the first thing lost.

The refusals matter as much as the successes: a run of `invalid_credential` is
somebody trying secrets against the endpoint, and it is invisible in a table
that only records what worked.
"""

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.tokens.models import AppCredential, EmbedAuditLog
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.site_tenant import host_workspace

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean_rate_limit_counters():
    """The limiter counts in the Django cache, which is process-wide and does
    NOT roll back with the test transaction. Without this, one test's mints
    spend the next test's budget — and since a rolled-back user can be handed
    the same pk again, the counter is keyed the same too. It failed exactly
    that way: passing alone, failing in file order.
    """
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()

LABS = "https://labs.connect.dimagi.com"


def _owner():
    user = User.objects.create_user("boss", "boss@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    c = Client()
    c.force_login(user)
    return user, ws, c


def _shown_app(user):
    """The connected site canopy shows its own panel for.

    A column an owner ticks, not a name matched against a setting — see
    `AppCredential.show_on_canopy_pages`.
    """
    app = AppCredential.create_credential(name="canopy-web", created_by=user,
                                                workspace=host_workspace())
    app.show_on_canopy_pages = True
    app.save(update_fields=["show_on_canopy_pages"])
    return app


def _rows(event=None):
    qs = EmbedAuditLog.objects.all()
    return list(qs.filter(event=event) if event else qs)


# --- identity handed over -----------------------------------------------------
#
# The three token-exchange tests that stood here went with that endpoint
# (2026-09-22). Its replacement writes the SAME `EXCHANGE` rows from
# `/api/auth/contact-token`, and they are asserted where that endpoint is
# tested — `tests/test_contact_assertions.py`, which covers the accepted row,
# each refusal and a throttled attempt. Duplicating them here would be two
# copies of one contract.


def test_a_session_mint_is_recorded():
    user, _ws, c = _owner()
    _shown_app(user)

    assert c.post("/api/embed/token").status_code == 200

    row = _rows(EmbedAuditLog.MINT)[0]
    assert row.ok and row.subject_email == "boss@dimagi.com"


# --- registration changes -----------------------------------------------------


def test_connecting_a_site_records_the_actor_and_what_was_granted():
    _user, ws, c = _owner()
    Agent.objects.create(slug="echo", name="Echo", workspace=ws)

    c.post("/api/workspaces/w1/connected-apps",
           data={"name": "connect-labs", "origins": [LABS], "agents": ["echo"]},
           content_type="application/json")

    row = _rows(EmbedAuditLog.CONNECT)[0]
    assert row.actor.email == "boss@dimagi.com"
    assert LABS in row.detail and "echo" in row.detail


def test_disconnecting_is_recorded():
    """The action someone takes when they think a site's key has leaked."""
    _user, _ws, c = _owner()
    app_id = c.post("/api/workspaces/w1/connected-apps",
                    data={"name": "x", "origins": [LABS]},
                    content_type="application/json").json()["id"]

    c.delete(f"/api/workspaces/w1/connected-apps/{app_id}")

    assert len(_rows(EmbedAuditLog.DISCONNECT)) == 1


def test_an_update_records_what_the_app_can_do_now():
    """Not what was asked for: a partial payload leaves the rest untouched, so
    the request body is not what the app ended up allowed to do."""
    _user, _ws, c = _owner()
    app_id = c.post("/api/workspaces/w1/connected-apps",
                    data={"name": "x", "origins": [LABS]},
                    content_type="application/json").json()["id"]

    c.patch(f"/api/workspaces/w1/connected-apps/{app_id}",
            data={"origins": [LABS, "http://localhost:8000"]},
            content_type="application/json")

    assert "localhost:8000" in _rows(EmbedAuditLog.UPDATE)[0].detail


# --- the trail has to outlive what it describes -------------------------------


def test_deleting_the_app_does_not_erase_its_history():
    """`SET_NULL` plus a denormalised name. A trail that vanishes with its
    subject is not a trail."""
    _user, _ws, c = _owner()
    app_id = c.post("/api/workspaces/w1/connected-apps",
                    data={"name": "connect-labs", "origins": [LABS]},
                    content_type="application/json").json()["id"]

    AppCredential.objects.filter(pk=app_id).delete()

    row = _rows(EmbedAuditLog.CONNECT)[0]
    assert row.app is None
    assert row.app_name == "connect-labs"


def test_an_audit_failure_never_breaks_the_operation(monkeypatch):
    """Deliberate: a logging outage must not become an authentication outage.
    The cost is a silently lost row, recoverable from the application log."""
    from apps.tokens import audit as audit_mod

    def boom(*_a, **_k):
        raise RuntimeError("table is on fire")

    monkeypatch.setattr(audit_mod.EmbedAuditLog.objects, "create", boom)

    user, _ws, c = _owner()
    _shown_app(user)

    assert c.post("/api/embed/token").status_code == 200


# --- rate limits --------------------------------------------------------------


def test_the_session_mint_is_rate_limited(settings):
    """It needs only a stolen cookie, and every call writes a token row."""
    settings.EMBED_MINT_LIMIT = 3
    user, _ws, c = _owner()
    _shown_app(user)

    codes = [c.post("/api/embed/token").status_code for _ in range(5)]

    assert codes[:3] == [200, 200, 200]
    assert 429 in codes[3:]
    assert any(r.reason == "rate_limited" for r in _rows(EmbedAuditLog.MINT))


def test_a_short_lived_token_is_what_gets_issued(settings):
    """Fifteen minutes, not an hour — the client refetches near expiry anyway,
    so the only thing a long life buys is a longer window for a leaked one."""
    user, _ws, c = _owner()
    _shown_app(user)

    from django.utils import timezone
    body = c.post("/api/embed/token").json()
    from datetime import datetime
    expires = datetime.fromisoformat(body["expires_at"])
    assert (expires - timezone.now()).total_seconds() < 16 * 60
