"""An agent's Google sign-in, finished on this box (`_drain_google_mint`).

The box makes the PKCE verifier and never sends it; canopy-web gets the challenge,
later relays the code, and receives only what `canopy vault google-mint` printed —
ciphertext, or why it failed."""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess

import pytest


@pytest.fixture()
def cr(cloud_runner):
    cloud_runner._GOOGLE_VERIFIERS.clear()
    return cloud_runner


def _wire(cr, monkeypatch, claims):
    posts = []
    claims = iter(claims)

    def fake_api(method, path, body=None, **kw):
        if method == "GET":
            return 200, next(claims)
        posts.append((path, body))
        return 204, None

    monkeypatch.setattr(cr, "_api", fake_api)
    return posts


def test_the_box_declares_it_can_finish_a_google_sign_in(cr):
    assert cr.RUNNER_CAPS["google_mint"] is True


def test_the_challenge_goes_out_and_the_verifier_stays(cr, monkeypatch):
    posts = _wire(cr, monkeypatch, [{"mint": None, "google": {"id": "m1", "agent": "ace", "status": "requested"}}])
    cr._drain_mint("rid")
    [(path, body)] = posts
    assert path == "/runners/rid/google-mint/m1/challenge"
    verifier = cr._GOOGLE_VERIFIERS["m1"]
    assert 43 <= len(verifier) <= 128 and verifier not in json.dumps(posts)
    want = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert body == {"code_challenge": want}


def test_the_code_is_exchanged_by_the_cli_and_only_its_answer_goes_back(cr, monkeypatch):
    cr._GOOGLE_VERIFIERS["m1"] = "v" * 64
    work = {"id": "m1", "agent": "ace", "status": "completing", "code": "4/code", "client_id": "cid"}
    posts = _wire(cr, monkeypatch, [{"mint": None, "google": work}])
    ran = {}

    def fake_run(argv, input=None, **kw):
        ran.update(argv=argv, stdin=json.loads(input))
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(
            {"name": "gog-token", "kv": 2, "value_zk": "zk1:abc", "account": "ace@dimagi-ai.com"}) + "\n", stderr="")

    monkeypatch.setattr(cr.subprocess, "run", fake_run)
    monkeypatch.setattr(cr, "_bearer", lambda **kw: "crt_x")
    cr._drain_mint("rid")
    assert ran["argv"][-6:] == ["vault", "google-mint", "--agent", "ace", "--runner", "rid"]
    assert ran["stdin"]["code"] == "4/code" and ran["stdin"]["code_verifier"] == "v" * 64
    assert posts == [("/runners/rid/google-mint/m1/result",
                      {"value_zk": "zk1:abc", "kv": 2, "account": "ace@dimagi-ai.com", "name": "gog-token"})]
    assert "m1" not in cr._GOOGLE_VERIFIERS, "a verifier is used once"


def test_a_refusal_from_the_cli_is_passed_on(cr, monkeypatch):
    cr._GOOGLE_VERIFIERS["m1"] = "v" * 64
    posts = _wire(cr, monkeypatch, [{"mint": None, "google": {"id": "m1", "agent": "ace", "status": "completing"}}])
    monkeypatch.setattr(cr.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 1, stdout=json.dumps({"error": "no-refresh-token", "detail": "Google returned no refresh token"}),
        stderr=""))
    monkeypatch.setattr(cr, "_bearer", lambda **kw: "crt_x")
    cr._drain_mint("rid")
    assert posts[0][1] == {"error": "no-refresh-token", "detail": "Google returned no refresh token"}


def test_an_old_cli_says_so_rather_than_failing_silently(cr, monkeypatch):
    cr._GOOGLE_VERIFIERS["m1"] = "v" * 64
    posts = _wire(cr, monkeypatch, [{"mint": None, "google": {"id": "m1", "agent": "ace", "status": "completing"}}])
    monkeypatch.setattr(cr.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 2, stdout="", stderr="Error: No such command 'google-mint'."))
    monkeypatch.setattr(cr, "_bearer", lambda **kw: "crt_x")
    cr._drain_mint("rid")
    assert posts[0][1]["error"] == "exchange-failed" and "updates itself" in posts[0][1]["detail"]


def test_a_code_without_its_verifier_is_reported_interrupted(cr, monkeypatch):
    """A restart loses the verifier; the code can never be exchanged, so say so."""
    posts = _wire(cr, monkeypatch, [{"mint": None, "google": {"id": "m9", "agent": "ace", "status": "completing"}}])
    cr._drain_mint("rid")
    assert posts == [("/runners/rid/google-mint/m9/result", {
        "error": "interrupted",
        "detail": "the sign-in was interrupted on the runner (it restarted) — please start again"})]


# --- a person's own Google account, delegated to the agent ----------------------------

def _staging(cr, monkeypatch, tmp_path, delegated):
    monkeypatch.setattr(cr.pathlib.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cr, "_bearer", lambda **kw: "crt_x")
    monkeypatch.setitem(cr._AGENT_DELEGATED, "bailey", delegated)
    ran = []

    def fake_run(argv, env=None, **kw):
        ran.append((argv, env))
        account = argv[argv.index("--delegated") + 1]
        f = tmp_path / ".canopy" / "delegated" / "bailey" / "google" / f"{account}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"access_token": "ya29.x", "expires_at": 9e12, "account": account}))
        return subprocess.CompletedProcess(argv, 0, stdout=str(f) + "\n", stderr="")

    monkeypatch.setattr(cr.subprocess, "run", fake_run)
    return ran


def test_a_delegated_google_account_is_staged_as_an_access_token_by_the_cli(cr, monkeypatch, tmp_path):
    ran = _staging(cr, monkeypatch, tmp_path, {
        "google-delegated-gina@gmail.com": '{"refresh_token": "1//r"}', "github-token": "ghp_x"})
    assert cr._stage_delegated_google("bailey") == ["gina@gmail.com"]
    [(argv, env)] = ran
    assert argv[1:] == ["cred", "google-token", "--runner", "--agent", "bailey", "--delegated", "gina@gmail.com"]
    assert env["CANOPY_TOKEN"] == "crt_x"
    assert "1//r" not in json.dumps(argv), "the refresh token never rides argv"
    # Fresh: not minted again.
    assert cr._stage_delegated_google("bailey") == ["gina@gmail.com"] and len(ran) == 1


def test_a_withdrawn_google_account_loses_its_staged_token(cr, monkeypatch, tmp_path):
    _staging(cr, monkeypatch, tmp_path, {"google-delegated-gina@gmail.com": "{}"})
    cr._stage_delegated_google("bailey")
    staged = tmp_path / ".canopy" / "delegated" / "bailey" / "google" / "gina@gmail.com.json"
    assert staged.exists()
    cr._AGENT_DELEGATED["bailey"] = {}
    assert cr._stage_delegated_google("bailey") == [] and not staged.exists()
