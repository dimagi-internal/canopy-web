"""Browser-driven re-authentication of a runner's Claude credential.

The relay exists because canopy-web cannot be the OAuth client: `claude
setup-token` runs PKCE with a verifier generated on the box, so the exchange has
to happen there. canopy-web ferries a URL out to a human and a code back, and the
minted token comes home out-of-band — never through the browser.

These tests pin the parts that are easy to get subtly wrong: who owes work at
each step, that a single-use code is delivered exactly once, and that no
operator-facing shape ever carries a secret.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.harness.models import Runner, RunnerMint

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("owner", "owner@dimagi.com", "pw")


@pytest.fixture()
def client(owner):
    c = Client()
    c.force_login(owner)
    return c


@pytest.fixture()
def runner(owner):
    return Runner.objects.create(name="cloud-ec2-1", kind=Runner.CLOUD, paired_by=owner)


def _post(client, url, body=None):
    return client.post(url, data=json.dumps(body or {}), content_type="application/json")


def _base(runner) -> str:
    return f"/api/harness/runners/{runner.id}/mint"


# ── the happy path, one step at a time ─────────────────────────────────────

def test_a_human_starts_a_sign_in_and_the_runner_is_asked_to_act(client, runner):
    r = _post(client, _base(runner))
    assert r.status_code == 200, r.content
    assert r.json()["status"] == RunnerMint.REQUESTED

    claim = client.get(f"{_base(runner)}/claim").json()
    assert claim["mint"] is not None, "the runner should be asked to start"
    assert claim["code"] == ""


def test_the_runner_posts_a_url_and_the_ball_moves_to_the_human(client, runner):
    _post(client, _base(runner))
    url = "https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=y"
    r = _post(client, f"{_base(runner)}/url", {"url": url})
    assert r.status_code == 200, r.content
    assert r.json()["status"] == RunnerMint.AWAITING_CODE
    assert r.json()["authorize_url"] == url

    # The human's screen reads the same row.
    assert client.get(_base(runner)).json()["authorize_url"] == url


def test_the_runner_is_given_nothing_while_the_human_is_signing_in(client, runner):
    """The step that would otherwise loop: if `claim` kept returning the mint in
    `awaiting_code`, the runner would restart the CLI — under a fresh PKCE
    verifier — while somebody is part-way through the URL it already sent."""
    _post(client, _base(runner))
    _post(client, f"{_base(runner)}/url", {"url": "https://claude.com/cai/oauth/authorize?x=1"})
    assert client.get(f"{_base(runner)}/claim").json()["mint"] is None


def test_the_code_reaches_the_runner_and_the_token_lands_encrypted(client, runner):
    _post(client, _base(runner))
    _post(client, f"{_base(runner)}/url", {"url": "https://claude.com/cai/oauth/authorize?x=1"})
    _post(client, f"{_base(runner)}/code", {"code": "  the-code  "})

    claim = client.get(f"{_base(runner)}/claim").json()
    assert claim["mint"]["status"] == RunnerMint.COMPLETING
    assert claim["code"] == "the-code", "whitespace from a paste must be trimmed"

    r = _post(client, f"{_base(runner)}/result", {"token": "sk-ant-oat01-real"})
    assert r.json()["status"] == RunnerMint.DONE

    status = client.get(f"/api/harness/runners/{runner.id}/credential/status").json()
    assert status["has_claude_token"] is True
    cred = client.get(f"/api/harness/runners/{runner.id}/credential").json()
    assert cred["claude_token"] == "sk-ant-oat01-real"


# ── the properties that keep a secret a secret ─────────────────────────────

def test_the_code_is_delivered_exactly_once(client, runner):
    """An authorization code is spent on first use. A second delivery could only
    fail, and a used code left in the row is a credential nobody accounts for."""
    _post(client, _base(runner))
    _post(client, f"{_base(runner)}/url", {"url": "https://claude.com/cai/oauth/authorize?x=1"})
    _post(client, f"{_base(runner)}/code", {"code": "one-shot"})

    assert client.get(f"{_base(runner)}/claim").json()["code"] == "one-shot"
    assert client.get(f"{_base(runner)}/claim").json()["code"] == ""


def test_no_operator_facing_shape_ever_carries_a_secret(client, runner):
    _post(client, _base(runner))
    _post(client, f"{_base(runner)}/url", {"url": "https://claude.com/cai/oauth/authorize?x=1"})
    _post(client, f"{_base(runner)}/code", {"code": "the-code"})
    body = client.get(_base(runner)).content.decode()
    assert "the-code" not in body
    _post(client, f"{_base(runner)}/result", {"token": "sk-ant-oat01-real"})
    assert "sk-ant-oat01-real" not in client.get(_base(runner)).content.decode()


# ── the failure shapes ─────────────────────────────────────────────────────

def test_a_failed_exchange_says_why_and_stores_nothing(client, runner):
    _post(client, _base(runner))
    _post(client, f"{_base(runner)}/url", {"url": "https://claude.com/cai/oauth/authorize?x=1"})
    _post(client, f"{_base(runner)}/code", {"code": "stale"})
    r = _post(client, f"{_base(runner)}/result", {"detail": "the code had expired"})
    assert r.json()["status"] == RunnerMint.FAILED
    assert r.json()["detail"] == "the code had expired"
    status = client.get(f"/api/harness/runners/{runner.id}/credential/status").json()
    assert status["has_claude_token"] is False


def test_a_stalled_sign_in_never_blocks_the_next_attempt(client, runner):
    """A closed tab or a runner restart mid-flow must not wedge the box forever."""
    first = _post(client, _base(runner)).json()["id"]
    _post(client, f"{_base(runner)}/url", {"url": "https://claude.com/cai/oauth/authorize?x=1"})

    second = _post(client, _base(runner)).json()["id"]
    assert second != first
    assert client.get(_base(runner)).json()["status"] == RunnerMint.REQUESTED
    assert RunnerMint.objects.get(id=first).status == RunnerMint.FAILED


def test_a_code_is_refused_when_nothing_is_waiting_for_one(client, runner):
    _post(client, _base(runner))
    r = _post(client, f"{_base(runner)}/code", {"code": "premature"})
    assert r.status_code == 409


def test_an_empty_code_is_refused(client, runner):
    _post(client, _base(runner))
    _post(client, f"{_base(runner)}/url", {"url": "https://claude.com/cai/oauth/authorize?x=1"})
    assert _post(client, f"{_base(runner)}/code", {"code": "   "}).status_code == 422


def test_no_sign_in_started_reads_as_null_not_an_error(client, runner):
    """The ordinary state of every runner, and the one the UI renders most."""
    r = client.get(_base(runner))
    assert r.status_code == 200
    assert r.json() is None


def test_a_stranger_cannot_drive_someone_elses_runner(client, runner):
    """Same trust boundary as claim/heartbeat: paired_by == caller."""
    other = User.objects.create_user("other", "other@dimagi.com", "pw")
    stranger = Client()
    stranger.force_login(other)
    assert _post(stranger, _base(runner)).status_code == 404


# ── expiry: a dead link must stop being offered as live ────────────────────

def test_a_sign_in_left_open_too_long_expires(client, runner, monkeypatch):
    """The failure this prevents, measured 2026-09-08: a mint started at 17:56
    was completed at 19:04 and failed with "setup-token never printed a token".
    The authorize URL's PKCE state and the CLI process waiting on the box had
    both expired an hour earlier; nothing said so, and the only clue was the
    clock. An aged-out mint now says what happened."""
    from django.utils import timezone

    from apps.harness import services
    from apps.harness.models import RunnerMint

    _post(client, _base(runner))
    _post(client, f"{_base(runner)}/url", {"url": "https://claude.com/cai/oauth/authorize?x=1"})

    stale = timezone.now() + dt.timedelta(seconds=services.MINT_TTL_SECONDS + 60)
    monkeypatch.setattr(timezone, "now", lambda: stale)

    body = client.get(_base(runner)).json()
    assert body["status"] == RunnerMint.FAILED
    assert "expired" in body["detail"].lower()


def test_an_expired_mint_is_not_handed_to_the_runner(client, runner, monkeypatch):
    """Belt and braces: the runner must not pick up a code for a flow whose
    verifier is already dead — it can only fail, slowly."""
    from django.utils import timezone

    from apps.harness import services

    _post(client, _base(runner))
    _post(client, f"{_base(runner)}/url", {"url": "https://claude.com/cai/oauth/authorize?x=1"})
    _post(client, f"{_base(runner)}/code", {"code": "the-code"})

    stale = timezone.now() + dt.timedelta(seconds=services.MINT_TTL_SECONDS + 60)
    monkeypatch.setattr(timezone, "now", lambda: stale)
    assert client.get(f"{_base(runner)}/claim").json()["mint"] is None


def test_a_fresh_sign_in_is_not_expired(client, runner):
    """The guard must not fire on the ordinary case."""
    _post(client, _base(runner))
    _post(client, f"{_base(runner)}/url", {"url": "https://claude.com/cai/oauth/authorize?x=1"})
    assert client.get(_base(runner)).json()["status"] == "awaiting_code"
