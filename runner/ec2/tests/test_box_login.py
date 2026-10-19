"""The box's own full Claude login — the one credential that carries claude.ai
connectors (Claude Docs and the rest).

A setup-token is inference-only by design, so a turn on a cloud runner could not
read a Claude Doc however it was shared (2026-10-14: ACE could read one on a
laptop and not on the box). The fix signs the box in with `claude auth login`,
keeps that login ON the box (its refresh token rotates, so a copy anywhere else
breaks both), and puts it first in the cascade.

The fixture is a REAL capture of `claude auth login --claudeai` under a pty
(`claude` 2.1.300, isolated config dir, killed at the code prompt — no login was
ever completed by it).
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

_HERE = pathlib.Path(__file__).parent
_FIXTURE = _HERE / "fixtures" / "auth_login_pty.bin"


@pytest.fixture()
def cr(cloud_runner, monkeypatch, tmp_path, tmp_path_factory):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    cloud_runner._BASE_CONFIG_DIR = str(tmp_path)
    cloud_runner._LOGINS_ROOT = tmp_path_factory.mktemp("logins")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cloud_runner._CLAUDE_CREDS.clear()
    return cloud_runner


def _store_login(tmp_path, *, scopes=("user:inference", "user:mcp_servers"),
                 refresh="rt-1"):
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "at", "refreshToken": refresh, "scopes": list(scopes)}}))


# ── recognising a usable box login ────────────────────────────────────────

def test_no_stored_login_is_not_a_box_login(cr):
    assert cr.box_login_ready() is False


def test_a_full_login_with_the_connector_scope_is_a_box_login(cr, tmp_path):
    _store_login(tmp_path)
    assert cr.box_login_ready() is True


def test_a_login_without_the_connector_scope_is_not_put_first(cr, tmp_path):
    """Putting an inference-only login first would cost every turn its
    connectors while looking like the fix had shipped."""
    _store_login(tmp_path, scopes=("user:inference",))
    assert cr.box_login_ready() is False


def test_a_login_with_no_refresh_token_is_not_put_first(cr, tmp_path):
    _store_login(tmp_path, refresh="")
    assert cr.box_login_ready() is False


def test_a_corrupt_credentials_file_is_not_a_box_login(cr, tmp_path):
    (tmp_path / ".credentials.json").write_text("{not json")
    assert cr.box_login_ready() is False


# ── its place in the cascade ──────────────────────────────────────────────

def test_the_box_login_goes_first_and_the_bundle_follows(cr, tmp_path):
    _store_login(tmp_path)
    cascade = cr._cascade_from({"claude_token": "tok-1", "claude_api_key": "sk-ant-x"})
    assert [c[0] for c in cascade] == ["box-login", "subscription-1", "api-key"]


def test_without_a_box_login_the_cascade_is_unchanged(cr):
    cascade = cr._cascade_from({"claude_token": "tok-1", "claude_token_secondary": "tok-2"})
    assert [c[0] for c in cascade] == ["subscription-1", "subscription-2"]


def test_selecting_the_box_login_clears_both_auth_vars(cr, tmp_path, monkeypatch):
    """The CLI prefers an env credential over its stored login, so the box login
    only takes effect if BOTH vars are gone."""
    _store_login(tmp_path)
    cr._CLAUDE_CREDS.extend(cr._cascade_from({"claude_token": "tok-1"}))
    cr._apply_claude_credential(1)
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-1"
    cr._apply_claude_credential(0)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_a_capped_box_login_falls_through_to_the_subscriptions(cr, tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "_notify_api_key_fallback", lambda *a, **k: None)
    _store_login(tmp_path)
    cr._CLAUDE_CREDS.extend(cr._cascade_from({"claude_token": "tok-1"}))
    cr._apply_claude_credential(0)
    assert cr._advance_claude_credential() is True
    assert cr._claude_cred_label() == "subscription-1"


def test_a_box_alone_with_no_bundle_still_stages(cr, tmp_path, monkeypatch):
    """A box signed in only with its own login must not sit waiting forever
    for a bundle token it does not need."""
    _store_login(tmp_path)
    monkeypatch.setattr(cr, "_api", lambda *a, **k: (200, {}))
    assert cr.fetch_and_stage_credential("rid") is True
    assert cr._claude_cred_label() == "box-login"


def test_adopting_a_fresh_login_puts_it_first_once(cr, tmp_path):
    cr._CLAUDE_CREDS.extend([("subscription-1", "CLAUDE_CODE_OAUTH_TOKEN", "tok-1")])
    cr._apply_claude_credential(0)
    _store_login(tmp_path)
    assert cr._adopt_box_login() is True
    assert cr._adopt_box_login() is True
    assert [c[0] for c in cr._CLAUDE_CREDS] == ["box-login", "subscription-1"]
    assert cr._claude_cred_label() == "box-login"


def test_promotion_still_swaps_the_subscriptions_behind_a_box_login(cr, tmp_path, monkeypatch):
    _store_login(tmp_path)
    cr._CLAUDE_CRED_RUNNER_ID = "rid"
    monkeypatch.setattr(cr, "_api", lambda *a, **k: (200, {}))
    cr._CLAUDE_CREDS.extend(cr._cascade_from(
        {"claude_token": "tok-1", "claude_token_secondary": "tok-2"}))
    cr._apply_claude_credential(2)          # subscription-2 just worked
    cr._promote_working_subscription()
    assert [c[2] for c in cr._CLAUDE_CREDS] == [str(tmp_path), "tok-2", "tok-1"]


# ── the sign-in itself ────────────────────────────────────────────────────

def test_the_authorize_url_is_recovered_from_a_real_auth_login_capture(cr):
    url = cr.extract_authorize_url(_FIXTURE.read_bytes())
    assert url and url.startswith("https://claude.com/cai/oauth/authorize?")
    assert "user%3Amcp_servers" in url, "the full login must ask for the connector scope"


def test_the_login_runs_with_no_inherited_claude_credential(cr, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    env = cr._login_env()
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env


def _fake_login(cr, monkeypatch, *, said: bytes):
    monkeypatch.setattr(cr.os, "write", lambda fd, data: len(data))
    monkeypatch.setattr(cr.time, "sleep", lambda s: None)
    s = cr.LoginSession()
    s._fd = 99
    s._buf = said
    monkeypatch.setattr(s, "_pump", lambda timeout, extract: extract(s._buf))
    monkeypatch.setattr(s, "close", lambda: None)
    return s


def test_a_completed_login_returns_no_token_to_carry(cr, tmp_path, monkeypatch):
    _store_login(tmp_path)
    s = _fake_login(cr, monkeypatch, said=b"Paste code here if prompted > \r\nLogin successful.\r\n")
    assert s.submit_code("CODE#STATE") == ""


def test_a_login_that_never_says_success_reports_the_clis_words(cr, monkeypatch):
    s = _fake_login(cr, monkeypatch, said=b"OAuth error: invalid code\r\n")
    with pytest.raises(cr.MintTimeout, match="invalid code"):
        s.submit_code("bad")


def test_success_without_the_connector_scope_is_a_failure(cr, tmp_path, monkeypatch):
    """The CLI can sign in a narrower login; reporting that as success would put
    a login first that still cannot see Claude Docs."""
    _store_login(tmp_path, scopes=("user:inference",))
    s = _fake_login(cr, monkeypatch, said=b"Login successful.\r\n")
    with pytest.raises(cr.MintTimeout, match="user:mcp_servers"):
        s.submit_code("CODE")


def test_the_relay_reports_a_box_login_without_a_token(cr, tmp_path, monkeypatch):
    posts = []
    claims = iter([
        (200, {"mint": {"id": "m1", "status": "requested", "slot": "box"}, "code": ""}),
        (200, {"mint": {"id": "m1", "status": "completing", "slot": "box"}, "code": "CODE"}),
    ])

    def fake_api(method, path, body=None, **kw):
        if method == "GET":
            return next(claims)
        posts.append((path, body))
        return 200, {}

    class FakeLogin(cr.LoginSession):
        def start(self, timeout=60.0):
            return "https://claude.com/cai/oauth/authorize?x=1"

        def submit_code(self, code, timeout=90.0):
            _store_login(tmp_path)
            return ""

    monkeypatch.setattr(cr, "_api", fake_api)
    monkeypatch.setattr(cr, "LoginSession", FakeLogin)
    reprobed = []
    monkeypatch.setattr(cr, "_reprobe", lambda labels: reprobed.append(list(labels)))
    cr._drain_mint("rid")
    cr._drain_mint("rid")
    result = [b for p, b in posts if p.endswith("/mint/result")]
    assert result == [result[0]] and result[0]["logged_in"] is True
    assert "token" not in result[0], "a box login is never sent off the box"
    assert cr._claude_cred_label() == "box-login"
    # A login signed in anew is asked again: its value (a config dir) is unchanged,
    # so nothing else would clear what it proved before (a "dead").
    assert ["box-login"] in reprobed


# ── what the credentials page reads ───────────────────────────────────────

def test_the_health_check_says_signed_in_when_the_box_login_is_first(cr, tmp_path):
    _store_login(tmp_path)
    cr._CLAUDE_CREDS.extend(cr._cascade_from({"claude_token": "tok-1"}))
    cr._credential_check()
    assert cr._HEALTH["claude.login"]["status"] == "ok"


def test_the_health_check_warns_on_backup_tokens_alone(cr):
    """Plenty of credentials and none that sees a connector is the exact state
    an operator mistook for signed in — so it is a warning, not silence."""
    cr._CLAUDE_CREDS.extend(cr._cascade_from(
        {"claude_token": "tok-1", "claude_token_secondary": "tok-2"}))
    cr._credential_check()
    assert cr._HEALTH["claude.credentials"]["status"] == "ok"
    assert cr._HEALTH["claude.login"]["status"] == "warn"
    assert "Sign in with Claude" in cr._HEALTH["claude.login"]["detail"]


# ── which account the box signed in as ─────────────────────────────────────

def test_the_box_reports_the_account_it_signed_in_as(cr, tmp_path):
    (tmp_path / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"emailAddress": "ace@dimagi-ai.com"}}))
    assert cr.box_login_account() == "ace@dimagi-ai.com"


def test_no_recorded_account_is_empty_not_an_error(cr, tmp_path):
    assert cr.box_login_account() == ""
    (tmp_path / ".claude.json").write_text("not json")
    assert cr.box_login_account() == ""


def test_the_health_check_names_the_account(cr, tmp_path):
    _store_login(tmp_path)
    (tmp_path / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"emailAddress": "ace@dimagi-ai.com"}}))
    cr._adopt_box_login()
    cr._credential_check()
    assert "as ace@dimagi-ai.com" in cr._HEALTH["claude.login"]["detail"]


# ── several logins, in the order a person set ─────────────────────────────

def _store_login_in(cr, key, *, account=""):
    d = cr._prepare_login_dir(key)
    _store_login(d)
    if account:
        state = cr._claude_state_file(key)
        data = json.loads(state.read_text()) if state.exists() else {}
        data["oauthAccount"] = {"emailAddress": account}
        state.write_text(json.dumps(data))
    return d


def test_each_login_gets_its_own_directory_sharing_the_rest(cr, tmp_path):
    (tmp_path / "settings.json").write_text("{}")
    (tmp_path / ".claude.json").write_text(json.dumps(
        {"hasCompletedOnboarding": True, "oauthAccount": {"emailAddress": "a@x.com"}}))
    d = cr._prepare_login_dir("ab12")
    assert d.parent == cr._LOGINS_ROOT
    assert (d / "projects").resolve() == (tmp_path / "projects").resolve(), \
        "transcripts must land where the runner reads them, whichever login ran"
    assert (d / "settings.json").is_symlink()
    state = json.loads((d / ".claude.json").read_text())
    assert state["hasCompletedOnboarding"] is True
    assert "oauthAccount" not in state, "a new login must not claim the default's account"


def test_the_cascade_follows_the_lists_order(cr, tmp_path):
    _store_login(tmp_path)
    _store_login_in(cr, "bb")
    _store_login_in(cr, "cc")
    (cr._LOGINS_ROOT / ".default-adopted").touch()
    cascade = cr._cascade_from({"logins": [{"key": "cc"}, {"key": "default"}, {"key": "bb"}],
                                "claude_token": "tok-1"})
    assert [c[0] for c in cascade] == [
        "box-login:cc", "box-login", "box-login:bb", "subscription-1"]


def test_a_listed_login_not_yet_signed_in_is_skipped(cr):
    cascade = cr._cascade_from({"logins": [{"key": "bb"}], "claude_token": "tok-1"})
    assert [c[0] for c in cascade] == ["subscription-1"]


def test_selecting_a_login_points_the_cli_at_its_directory(cr, tmp_path):
    d = _store_login_in(cr, "bb")
    cr._CLAUDE_CREDS.extend(cr._cascade_from(
        {"logins": [{"key": "bb"}], "claude_token": "tok-1"}))
    cr._apply_claude_credential(0)
    assert os.environ["CLAUDE_CONFIG_DIR"] == str(d)
    cr._apply_claude_credential(1)
    assert os.environ["CLAUDE_CONFIG_DIR"] == str(tmp_path), "a token runs in the CLI's own dir"
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-1"


def test_a_capped_login_falls_through_to_the_next_login(cr):
    _store_login_in(cr, "bb")
    _store_login_in(cr, "cc")
    cr._CLAUDE_CREDS.extend(cr._cascade_from({"logins": [{"key": "bb"}, {"key": "cc"}]}))
    cr._apply_claude_credential(0)
    assert cr._advance_claude_credential() is True
    assert cr._claude_cred_label() == "box-login:cc"


def test_the_login_env_signs_in_to_that_logins_directory(cr):
    d = cr._prepare_login_dir("bb")
    assert cr._login_env("bb")["CLAUDE_CONFIG_DIR"] == str(d)


def _reconcile(cr, monkeypatch, cred):
    posts = []
    monkeypatch.setattr(cr, "_api", lambda m, path, body=None, **k: (posts.append((path, body)), (200, {}))[1])
    cr._reconcile_logins("rid", cred)
    return posts


def test_a_removed_login_is_signed_out_of_the_box(cr, monkeypatch):
    gone = _store_login_in(cr, "bb")
    kept = _store_login_in(cr, "cc")
    _reconcile(cr, monkeypatch, {"logins": [{"key": "cc"}]})
    assert not gone.exists() and kept.exists()


def test_removing_the_default_login_signs_it_out_but_keeps_its_directory(cr, tmp_path, monkeypatch):
    _store_login(tmp_path)
    (cr._LOGINS_ROOT / ".default-adopted").touch()
    _reconcile(cr, monkeypatch, {"logins": []})
    assert not (tmp_path / ".credentials.json").exists()
    assert tmp_path.exists()


def test_a_login_from_before_the_list_is_reported_for_adoption_not_removed(cr, tmp_path, monkeypatch):
    _store_login(tmp_path)
    (tmp_path / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "a@x.com"}}))
    posts = _reconcile(cr, monkeypatch, {"logins": []})
    assert (tmp_path / ".credentials.json").exists()
    (path, body), = posts
    assert path == "/runners/rid/logins/report" and body["adopt_default"] is True
    assert body["logins"] == [{"key": "default", "signed_in": True, "account": "a@x.com"}]
    assert (cr._LOGINS_ROOT / ".default-adopted").exists()
    # Until the list holds it, it stays first in the cascade.
    assert [c[0] for c in cr._cascade_from({"logins": []})] == []
    assert [c[0] for c in cr._cascade_from({"logins": [{"key": "default"}]})] == ["box-login"]


def test_an_unchanged_state_is_reported_once(cr, monkeypatch):
    _store_login_in(cr, "bb", account="b@x.com")
    cred = {"logins": [{"key": "bb"}]}
    assert len(_reconcile(cr, monkeypatch, cred)) == 1
    assert _reconcile(cr, monkeypatch, cred) == []


def test_an_older_canopy_web_with_no_list_changes_nothing(cr, tmp_path, monkeypatch):
    _store_login(tmp_path)
    (cr._LOGINS_ROOT / ".default-adopted").touch()
    assert _reconcile(cr, monkeypatch, {"claude_token": "tok-1"}) == []
    assert (tmp_path / ".credentials.json").exists()
    assert [c[0] for c in cr._cascade_from({"claude_token": "tok-1"})][0] == "box-login"


def test_a_box_waiting_for_a_credential_still_serves_the_browser_sign_in(cr, tmp_path, monkeypatch):
    """cloud-ec2-2, 2026-10-15: its only credential was its own login, the login
    expired, and an auto-update restarted it into the credential wait. Sign-ins
    were drained only by the main loop, so the browser sign-in — the one way to
    give it a credential — never reached it. The wait must serve it, and a
    finished sign-in must end the wait."""
    drained = []

    def drain(runner_id):
        drained.append(runner_id)
        _store_login(tmp_path)           # the person finished signing in

    monkeypatch.setattr(cr, "_api", lambda *a, **k: (200, {}))
    monkeypatch.setattr(cr, "_drain_mint", drain)
    monkeypatch.setattr(cr, "POLL_SECONDS", 0)
    assert cr.fetch_and_stage_credential("rid") is True
    assert drained == ["rid"]
    assert cr._claude_cred_label() == "box-login"


def test_a_box_waiting_for_a_credential_heartbeats_as_not_ready(cr, tmp_path, monkeypatch):
    """The same box read "stale" for hours, which says it is down. It was up and
    needed a sign-in: the wait heartbeats, not ready (so nothing routes to it),
    with a note that says what to do."""
    beats = []

    def api(method, path, body=None):
        if path.endswith("/heartbeat"):
            beats.append(body)
        return 200, {}

    monkeypatch.setattr(cr, "_api", api)
    monkeypatch.setattr(cr, "_drain_mint", lambda rid: _store_login(tmp_path))
    monkeypatch.setattr(cr, "POLL_SECONDS", 0)
    assert cr.fetch_and_stage_credential("rid") is True
    # The wait's beat, then the one that reports the staged credentials' probes.
    assert len(beats) == 2
    assert beats[0]["ready"] is False
    assert "Sign in with Claude" in beats[0]["ready_note"]
    assert len(beats[0]["ready_note"]) <= 200
