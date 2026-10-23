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
    return Runner.objects.create(name="cloud-ec2-1", kind=Runner.CLOUD, owner=owner)


ZK = "zk1:" + "A" * 60


@pytest.fixture()
def vaulted(owner):
    """A cloud runner with its own vault, keyed — where its tokens' values live."""
    from apps.agents.credentials import workspace_vaults
    from apps.agents.models import Vault
    from apps.workspaces.models import Workspace, WorkspaceMembership

    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    r = Runner.objects.create(name="cloud-ec2-9", kind=Runner.CLOUD, owner=owner, workspace=ws)
    Vault.objects.filter(pk=workspace_vaults.create_for_runner(r, by=owner).pk).update(kv=1)
    return r


def _held(runner, kind, name=""):
    from apps.agents.models import VaultSecret
    from apps.harness import runners

    entry = runners.add_runner_credential(runner, kind=kind, name=name)
    VaultSecret.objects.create(vault=runners.open_runner_vault(runner), name=entry.vault_secret_name,
                               value_zk=ZK, kv=1)
    return entry


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


def _legacy_mint(runner, slot=RunnerMint.PRIMARY):
    """A setup-token sign-in already in flight when token sign-ins were retired.

    New ones cannot be started (see test_token_sign_ins_are_refused), but a row
    started before the deploy must still finish rather than strand its human —
    so the finish path keeps its tests, on rows made directly."""
    return RunnerMint.objects.create(runner=runner, slot=slot)


def test_the_code_reaches_the_runner_and_a_token_sent_back_is_refused(client, runner):
    _legacy_mint(runner)
    _post(client, f"{_base(runner)}/url", {"url": "https://claude.com/cai/oauth/authorize?x=1"})
    _post(client, f"{_base(runner)}/code", {"code": "  the-code  "})

    claim = client.get(f"{_base(runner)}/claim").json()
    assert claim["mint"]["status"] == RunnerMint.COMPLETING
    assert claim["code"] == "the-code", "whitespace from a paste must be trimmed"

    # A token is sealed into the runner's vault on the box, never sent here: one that
    # arrives anyway (a box from before the vault) fails the sign-in, unstored.
    r = _post(client, f"{_base(runner)}/result", {"token": "sk-ant-oat01-real"})
    assert r.json()["status"] == RunnerMint.FAILED
    assert "holds no copy of its vault" in r.json()["detail"]
    status = client.get(f"/api/harness/runners/{runner.id}/credential/status").json()
    assert status["credentials"] == []


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
    """Same trust boundary as claim/heartbeat: owner == caller."""
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

    from apps.harness import runners
    from apps.harness.models import RunnerMint

    _post(client, _base(runner))
    _post(client, f"{_base(runner)}/url", {"url": "https://claude.com/cai/oauth/authorize?x=1"})

    stale = timezone.now() + dt.timedelta(seconds=runners.MINT_TTL_SECONDS + 60)
    monkeypatch.setattr(timezone, "now", lambda: stale)

    body = client.get(_base(runner)).json()
    assert body["status"] == RunnerMint.FAILED
    assert "expired" in body["detail"].lower()


def test_an_expired_mint_is_not_handed_to_the_runner(client, runner, monkeypatch):
    """Belt and braces: the runner must not pick up a code for a flow whose
    verifier is already dead — it can only fail, slowly."""
    from django.utils import timezone

    from apps.harness import runners

    _post(client, _base(runner))
    _post(client, f"{_base(runner)}/url", {"url": "https://claude.com/cai/oauth/authorize?x=1"})
    _post(client, f"{_base(runner)}/code", {"code": "the-code"})

    stale = timezone.now() + dt.timedelta(seconds=runners.MINT_TTL_SECONDS + 60)
    monkeypatch.setattr(timezone, "now", lambda: stale)
    assert client.get(f"{_base(runner)}/claim").json()["mint"] is None


def test_a_fresh_sign_in_is_not_expired(client, runner):
    """The guard must not fire on the ordinary case."""
    _post(client, _base(runner))
    _post(client, f"{_base(runner)}/url", {"url": "https://claude.com/cai/oauth/authorize?x=1"})
    assert client.get(_base(runner)).json()["status"] == "awaiting_code"


# ── which login a sign-in fills, and whose it is ───────────────────────────

def _sign_in(client, runner, *, slot=None, token):
    _legacy_mint(runner, slot or RunnerMint.PRIMARY)
    _post(client, f"{_base(runner)}/url", {"url": "https://claude.com/cai/oauth/authorize?x=1"})
    _post(client, f"{_base(runner)}/code", {"code": "c"})
    client.get(f"{_base(runner)}/claim")
    return _post(client, f"{_base(runner)}/result", {"token": token})


def test_swapping_moves_tokens_and_names_together(client, vaulted):
    a, b = _held(vaulted, "token", "a@dimagi.com"), _held(vaulted, "token", "b@dimagi.com")
    key = _held(vaulted, "api_key")
    r = _post(client, f"/api/harness/runners/{vaulted.id}/credential/swap")
    assert r.status_code == 200, r.content
    assert r.json()["claude_token_label"] == "b@dimagi.com"
    cascade = client.get(f"/api/harness/runners/{vaulted.id}/credential").json()["cascade"]
    assert [c["key"] for c in cascade] == [b.key, a.key, key.key], "the API key is not part of the swap"


def test_a_stranger_cannot_swap_someone_elses_logins(runner):
    stranger = User.objects.create_user("stranger", "s@dimagi.com", "pw")
    c = Client()
    c.force_login(stranger)
    assert _post(c, f"/api/harness/runners/{runner.id}/credential/swap").status_code == 404


# ── a full login kept on the box (the `box` slot) ──────────────────────────
# The one login that carries claude.ai connectors. Its refresh token rotates, so
# it never leaves the runner: success is a flag, and nothing lands in a slot.

def test_a_box_sign_in_finishes_on_logged_in_and_stores_nothing(client, runner):
    r = _post(client, _base(runner), {"slot": "box"})
    assert r.json()["slot"] == "box"
    claim = client.get(f"{_base(runner)}/claim").json()
    assert claim["mint"]["slot"] == "box", "the runner must know which CLI to run"
    _post(client, f"{_base(runner)}/url", {"url": "https://claude.com/cai/oauth/authorize?x=1"})
    _post(client, f"{_base(runner)}/code", {"code": "c"})
    client.get(f"{_base(runner)}/claim")
    r = _post(client, f"{_base(runner)}/result", {"logged_in": True})
    assert r.json()["status"] == RunnerMint.DONE
    status = client.get(f"/api/harness/runners/{runner.id}/credential/status").json()
    assert status["has_claude_token"] is False


def test_a_box_sign_in_ignores_a_stray_token(client, runner):
    _post(client, _base(runner), {"slot": "box"})
    r = _post(client, f"{_base(runner)}/result", {"token": "sk-ant-oat01-x"})
    assert r.json()["status"] == RunnerMint.FAILED
    cred = client.get(f"/api/harness/runners/{runner.id}/credential").json()
    assert not cred.get("claude_token")


def test_a_box_sign_in_that_did_not_log_in_fails_with_the_reason(client, runner):
    _post(client, _base(runner), {"slot": "box"})
    r = _post(client, f"{_base(runner)}/result", {"detail": "OAuth error: invalid code"})
    assert r.json()["status"] == RunnerMint.FAILED
    assert "invalid code" in r.json()["detail"]


def test_a_sign_in_with_no_slot_is_the_full_login(client, runner):
    r = _post(client, _base(runner))
    assert r.json()["slot"] == "box"


# ── a long-lived token for the credential list ───────────────────────────────

def _token_sign_in(client, runner, *, login_key="", vault_key, seal=True):
    """The box's side of a token sign-in: name the entry, seal the token into the
    runner's vault (here: the vault row a seal writes), then report it."""
    r = _post(client, _base(runner), {"slot": "token", "login_key": login_key})
    assert r.status_code == 200, r.content
    _post(client, f"{_base(runner)}/url", {"url": "https://claude.com/cai/oauth/authorize?x=1"})
    _post(client, f"{_base(runner)}/code", {"code": "c"})
    client.get(f"{_base(runner)}/claim")
    named = _post(client, f"{_base(runner)}/entry", {"vault_key": vault_key})
    assert named.status_code == 200, named.content
    if seal:
        from apps.agents.models import VaultSecret
        from apps.harness import runners
        VaultSecret.objects.update_or_create(vault=runners.open_runner_vault(runner),
                                             name=f"claude-token:{vault_key}",
                                             defaults={"value_zk": ZK, "kv": 1})
    return _post(client, f"{_base(runner)}/result", {"vault_key": vault_key})


def test_a_token_sign_in_adds_a_token_at_the_end_once_the_vault_holds_it(client, vaulted):
    key = _held(vaulted, "api_key")
    r = _token_sign_in(client, vaulted, vault_key="0a1b2c3d")
    assert r.json()["status"] == "done"
    cascade = client.get(f"/api/harness/runners/{vaulted.id}/credential").json()["cascade"]
    assert [(c["kind"], c["vault_secret"]) for c in cascade] == [
        ("api_key", f"anthropic-api-key:{key.key}"), ("token", "claude-token:0a1b2c3d")]


def test_a_sign_in_whose_token_never_reached_the_vault_is_not_a_success(client, vaulted):
    """The bug this flow exists to end: "signed in" for a token nobody can use."""
    r = _token_sign_in(client, vaulted, vault_key="0a1b2c3d", seal=False)
    assert r.json()["status"] == RunnerMint.FAILED
    assert "did not reach the runner's vault" in r.json()["detail"]
    status = client.get(f"/api/harness/runners/{vaulted.id}/credential/status").json()
    assert status["credentials"] == [], "the entry made for it goes again"


def test_a_token_sign_in_for_a_token_keeps_its_entry_and_name(client, vaulted):
    old = _held(vaulted, "token", "ace")
    _held(vaulted, "api_key")
    r = _token_sign_in(client, vaulted, login_key=old.key, vault_key=old.key)
    assert r.json()["status"] == "done"
    status = client.get(f"/api/harness/runners/{vaulted.id}/credential/status").json()
    assert [(c["key"], c["name"]) for c in status["credentials"]][0] == (old.key, "ace")
    assert len(status["credentials"]) == 2


def test_a_token_sign_in_names_only_a_token(client, vaulted):
    key = _held(vaulted, "api_key").key
    r = _post(client, _base(vaulted), {"slot": "token", "login_key": key})
    assert r.status_code == 404
