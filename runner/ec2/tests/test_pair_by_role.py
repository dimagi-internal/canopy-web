"""A new cloud box pairs by invitation: it proves its instance role and holds no PAT.

The box finds no state file and no CANOPY_TOKEN, so it asks canopy-web to pair it
by its role (`POST /api/harness/runners/pair-by-role`) until the operator's
invitation is approved — then it is a runner, and signs in with its device key.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

EC2 = pathlib.Path(__file__).resolve().parent.parent
CREDS = {"access_key": "AKIAFAKE", "secret_key": "s", "token": "t", "expires_at": 0.0}


@pytest.fixture
def box(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(cloud_runner, "TOKEN", "")
    monkeypatch.setattr(cloud_runner, "BASE_URL", "https://canopy.example")
    monkeypatch.setattr(cloud_runner, "RUNNER_NAME", "cloud-ec2-fork")
    monkeypatch.setattr(cloud_runner, "_aws_instance_credentials", lambda: CREDS)
    monkeypatch.setattr(cloud_runner, "_ecdh_public_key", lambda: {})
    sleeps: list[float] = []
    monkeypatch.setattr(cloud_runner.time, "sleep", lambda s: sleeps.append(s))
    logs: list[str] = []
    monkeypatch.setattr(cloud_runner, "_log", lambda m: logs.append(m))
    cloud_runner._test = {"sleeps": sleeps, "logs": logs}
    return cloud_runner


def _answers(box, monkeypatch, *replies):
    sent: list[tuple[str, dict]] = []
    replies = list(replies)

    def post(url, body):
        sent.append((url, body))
        return replies.pop(0)

    monkeypatch.setattr(box, "_post_unauthenticated", post)
    return sent


def test_waits_for_approval_then_pairs_and_writes_its_state(box, monkeypatch):
    sent = _answers(box, monkeypatch, (202, {"status": "waiting"}),
                    (201, {"status": "paired", "runner_id": "rid-1"}))
    assert box.pair_by_role() == "rid-1"
    assert json.loads(box.STATE_FILE.read_text()) == {"runner_id": "rid-1"}
    url, body = sent[0]
    assert url == "https://canopy.example/api/harness/runners/pair-by-role"
    assert body["name"] == "cloud-ec2-fork" and body["kind"] == "cloud"
    assert body["proof"], "the instance role's proof rides the body"
    assert box._test["sleeps"] == [box.POLL_SECONDS]
    assert any("waiting for the pairing invitation to be approved" in m for m in box._test["logs"])


def test_the_proof_is_bound_to_the_pair_by_role_url(box, monkeypatch):
    import base64

    sent = _answers(box, monkeypatch, (201, {"status": "paired", "runner_id": "rid-1"}))
    box.pair_by_role()
    proof = json.loads(base64.b64decode(sent[0][1]["proof"]))
    assert proof["headers"]["X-Canopy-Proof-Htu"] == "https://canopy.example/api/harness/runners/pair-by-role"


def test_a_replaced_instance_reattaches(box, monkeypatch):
    _answers(box, monkeypatch, (200, {"status": "reattached", "runner_id": "rid-old"}))
    assert box.pair_by_role() == "rid-old"
    assert json.loads(box.STATE_FILE.read_text()) == {"runner_id": "rid-old"}


def test_a_refusal_says_run_up_sh_and_keeps_asking(box, monkeypatch):
    _answers(box, monkeypatch, (401, None), (201, {"status": "paired", "runner_id": "rid-1"}))
    assert box.pair_by_role() == "rid-1"
    assert any("no approved pairing invitation" in m and "cloud-ec2-fork" in m and "up.sh" in m
               for m in box._test["logs"])


def test_the_ecdh_key_rides_along(box, monkeypatch):
    jwk = {"kty": "EC", "crv": "P-256", "x": "x", "y": "y"}
    monkeypatch.setattr(box, "_ecdh_public_key", lambda: {"jwk": jwk, "jkt": "j", "kind": "aws-kms"})
    sent = _answers(box, monkeypatch, (201, {"status": "paired", "runner_id": "rid-1"}))
    box.pair_by_role()
    assert sent[0][1]["ecdh_public_jwk"] == jwk and sent[0][1]["ecdh_store"] == "aws-kms"


def test_no_instance_role_gives_up(box, monkeypatch):
    monkeypatch.setattr(box, "_aws_instance_credentials", lambda: None)
    _answers(box, monkeypatch)
    assert box.pair_by_role() is None


def test_pair_or_load_pairs_by_role_without_a_token(box, monkeypatch):
    monkeypatch.setattr(box, "pair_by_role", lambda: "rid-1")
    monkeypatch.setattr(box, "_api", lambda *a, **k: pytest.fail("no PAT pairing without a token"))
    assert box.pair_or_load() == "rid-1"


def test_a_state_file_means_pair_by_role_is_never_called(box, monkeypatch):
    box.STATE_FILE.write_text('{"runner_id": "existing"}')
    monkeypatch.setattr(box, "pair_by_role", lambda: pytest.fail("already paired"))
    monkeypatch.setattr(box, "_api", lambda method, path, body=None: (200, {}))
    assert box.pair_or_load() == "existing"


def test_with_a_token_an_old_stack_still_pairs_the_old_way(box, monkeypatch):
    monkeypatch.setattr(box, "TOKEN", "a-pat")
    monkeypatch.setattr(box, "pair_by_role", lambda: pytest.fail("an old stack pairs with its PAT"))
    calls = []

    def api(method, path, body=None):
        calls.append((method, path))
        return 201, {"id": "rid-pat"}

    monkeypatch.setattr(box, "_api", api)
    assert box.pair_or_load() == "rid-pat"
    assert calls == [("POST", "/runners/")]


def test_main_starts_without_a_token(box, monkeypatch):
    """A new box has neither a state file nor CANOPY_TOKEN — that is the normal
    case now, not a fatal one."""
    monkeypatch.setattr(box, "pair_or_load", lambda: (_ for _ in ()).throw(SystemExit("paired-check")))
    with pytest.raises(SystemExit, match="paired-check"):
        box.main()


def test_the_stack_writes_canopy_token_only_when_it_names_a_secret():
    cfn = (EC2 / "runner.cfn.yaml").read_text()
    line = next(ln for ln in cfn.splitlines() if ln.strip().startswith("CANOPY_TOKEN="))
    # Guarded by a non-empty check, and tolerant of a deleted secret.
    assert re.search(r'if \[ -n "\$\{CanopyTokenSecret\}" \]', line), line
    assert "|| true" in line, line
    # A new stack names no PAT secret; an existing one keeps its value on update.
    assert re.search(r"\n  CanopyTokenSecret:\n    Type: String\n    Default: ''\n", cfn)
