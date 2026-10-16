"""The cloud runner's IAM-role device proof (Canopy Vault Phase 1b, canopy-web#1549).

An EC2 box proves its instance role by SigV4-signing — not sending — an
sts:GetCallerIdentity and sending it base64'd in X-Canopy-Aws-Proof. The signer is the
stdlib's (cloud_runner.py must stay stdlib-only), so the first test pins it to botocore's
SigV4Auth, live, for the exact request. IMDS and STS are faked; nothing reaches AWS.
"""
from __future__ import annotations

import base64
import calendar
import json
import time

import pytest

CREDS = {"access_key": "AKIDEXAMPLE", "secret_key": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
         "token": "SESSIONTOKEN/abc==", "expires_at": 0.0}
HTU = "https://canopy.example/api/agents/eva/credentials/resolve"
ROLE = "arn:aws:iam::123456789012:role/canopy-runner-RunnerRole-ABC"


@pytest.fixture(autouse=True)
def _no_imds(monkeypatch, tmp_path):
    monkeypatch.delenv("CANOPY_DEVICE_KEY", raising=False)              # conftest turns it off
    monkeypatch.setenv("CANOPY_AWS_IMDS_URL", "http://127.0.0.1:9")   # closed: refuses at once
    monkeypatch.setenv("CANOPY_DEVICE_KEY_DIR", str(tmp_path / "dk"))


@pytest.fixture(autouse=True)
def _no_canopy_cli(cloud_runner, monkeypatch):
    """No test may run this machine's real canopy CLI to make an ECDH key; the default is
    an older canopy that cannot (the enrollment is then exactly the pre-#1550 one).
    Returns the real function for the test that drives it with a faked subprocess."""
    real = cloud_runner._ecdh_public_key
    monkeypatch.setattr(cloud_runner, "_ecdh_public_key", lambda: {})
    return real


def test_signature_matches_botocore(cloud_runner):
    pytest.importorskip("botocore")
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    ours = cloud_runner._signed_get_caller_identity(CREDS, HTU, iat=1700000000)
    h = ours["headers"]
    req = AWSRequest(method="POST", url=ours["url"], data=ours["body"],
                     headers={k: h[k] for k in ("Content-Type", "Host", "X-Canopy-Proof-Htu",
                                                "X-Canopy-Proof-Iat")})
    SigV4Auth(Credentials(CREDS["access_key"], CREDS["secret_key"], CREDS["token"]),
              "sts", "us-east-1").add_auth(req)
    # botocore signs at its own "now"; re-sign ours at that same second and compare.
    at = calendar.timegm(time.strptime(req.headers["X-Amz-Date"], "%Y%m%dT%H%M%SZ"))
    again = cloud_runner._signed_get_caller_identity(CREDS, HTU, iat=1700000000, now=at)
    assert again["headers"]["X-Amz-Date"] == req.headers["X-Amz-Date"]
    assert again["headers"]["X-Amz-Security-Token"] == req.headers["X-Amz-Security-Token"]
    assert again["headers"]["Authorization"] == req.headers["Authorization"]


def test_signature_matches_the_pinned_botocore_vector(cloud_runner):
    # The same vector canopy's tests/test_device_key_aws.py pins for its copy of the signer.
    req = cloud_runner._signed_get_caller_identity(CREDS, HTU, iat=1700000000, now=1792181563)
    assert req["headers"]["Authorization"].endswith(
        "Signature=391ecfc04bb5eccd8b4e5c5e1939896c237c7a794d23335de7722d77befafad9")


def test_proof_shape_and_signed_binding(cloud_runner):
    raw = cloud_runner.aws_proof(HTU + "?refs=A#x", creds=CREDS, now=1792181563.4)
    doc = json.loads(base64.b64decode(raw))
    assert set(doc) == {"method", "url", "headers", "body"}
    assert (doc["method"], doc["url"]) == ("POST", "https://sts.amazonaws.com/")
    assert doc["body"] == "Action=GetCallerIdentity&Version=2011-06-15"
    h = doc["headers"]
    assert h["X-Canopy-Proof-Htu"] == HTU and h["X-Canopy-Proof-Iat"] == "1792181563"
    signed = h["Authorization"].split("SignedHeaders=")[1].split(",")[0].split(";")
    assert {"x-canopy-proof-htu", "x-canopy-proof-iat"} <= set(signed)


def test_no_instance_role_means_no_header(cloud_runner):
    assert cloud_runner._aws_proof_headers(HTU) == {}


def test_every_resolve_carries_the_proof(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner, "_aws_instance_credentials", lambda: CREDS)
    seen = []

    def api(method, path, body=None, *, prefix="/api/harness", headers=None):
        seen.append(dict(headers or {}))
        return 200, {"op_sa_token": "ops_agent", "op_vault": "Agent-Eva"}

    monkeypatch.setattr(cloud_runner, "_api", api)
    monkeypatch.setattr(cloud_runner, "BASE_URL", "https://canopy.example")
    cloud_runner._AGENT_OP.clear()
    assert cloud_runner._agent_op_token("eva") == "ops_agent"
    doc = json.loads(base64.b64decode(seen[0]["X-Canopy-Aws-Proof"]))
    assert doc["headers"]["X-Canopy-Proof-Htu"] == HTU


def test_a_broken_signer_never_breaks_a_resolve(cloud_runner, monkeypatch):
    def boom():
        raise RuntimeError("imds exploded")

    monkeypatch.setattr(cloud_runner, "_aws_instance_credentials", boom)
    assert cloud_runner._aws_proof_headers(HTU) == {}


def test_opt_out(cloud_runner, monkeypatch):
    monkeypatch.setenv("CANOPY_DEVICE_KEY", "0")
    monkeypatch.setattr(cloud_runner, "_aws_instance_credentials", lambda: CREDS)
    assert cloud_runner._aws_proof_headers(HTU) == {}
    assert cloud_runner.enroll_aws_device_key("r-1") == {"skipped": "CANOPY_DEVICE_KEY=0"}


@pytest.mark.parametrize("arn,want", [
    ("arn:aws:sts::123456789012:assumed-role/canopy-runner-RunnerRole-ABC/i-0abc", ROLE),
    ("arn:aws:iam::1:role/R", "arn:aws:iam::1:role/R"),
])
def test_normalize_role_arn(cloud_runner, arn, want):
    assert cloud_runner.normalize_role_arn(arn) == want


# ── IMDSv2 ────────────────────────────────────────────────────────────────────

def test_instance_credentials_via_imdsv2(cloud_runner, monkeypatch):
    calls = []

    def imds(path, *, token="", put=False, headers=None):
        calls.append((path, token, put))
        if path == "/latest/api/token":
            assert put
            return "T"
        assert token == "T"
        if path.endswith("/security-credentials/"):
            return "RunnerRole\n"
        return json.dumps({"Code": "Success", "AccessKeyId": "ASIA1", "SecretAccessKey": "s",
                           "Token": "t", "Expiration": "2099-01-01T00:00:00Z"})

    monkeypatch.setattr(cloud_runner, "_imds", imds)
    c = cloud_runner._aws_instance_credentials()
    assert (c["access_key"], c["secret_key"], c["token"]) == ("ASIA1", "s", "t")
    assert c["expires_at"] > 4e9
    assert cloud_runner._aws_instance_credentials() is c and len(calls) == 3     # cached


# ── enrollment ────────────────────────────────────────────────────────────────

def test_enrollment_posts_the_role_and_a_proof(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner, "_aws_instance_credentials", lambda: CREDS)
    monkeypatch.setattr(cloud_runner, "_aws_role_arn", lambda creds: ROLE)
    monkeypatch.setattr(cloud_runner, "BASE_URL", "https://canopy.example")
    posts, logs = [], []
    monkeypatch.setattr(cloud_runner, "_log", logs.append)

    def api(method, path, body=None, *, prefix="/api/harness", headers=None):
        posts.append((method, prefix + path, body))
        return 201, {"id": "k1", "jkt": "f" * 64, "status": "pending",
                     "approve_url": "https://canopy.example/runners/x/device-keys/k1"}

    monkeypatch.setattr(cloud_runner, "_api", api)
    st = cloud_runner.enroll_aws_device_key("r-ec2")
    (method, path, body), = posts
    assert (method, path) == ("POST", "/api/runner-device-keys/")
    assert set(body) == {"runner", "kind", "role_arn", "label", "proof"}
    assert body["runner"] == "r-ec2" and body["kind"] == "aws-iam" and body["role_arn"] == ROLE
    proof = json.loads(base64.b64decode(body["proof"]))
    assert proof["headers"]["X-Canopy-Proof-Htu"] == "https://canopy.example/api/runner-device-keys/"
    assert st["status"] == "pending"
    assert any("PENDING" in line and "device-keys/k1" in line for line in logs)
    # canopy's own cache, so the canopy CLI's resolves on this box attach the proof too
    d = tmp_path / "dk"
    assert json.loads((d / "aws-iam.json").read_text()) == {"role_arn": ROLE}
    cached = json.loads((d / "status.json").read_text())
    assert cached["kind"] == "aws-iam" and cached["role_arn"] == ROLE and cached["jkt"] == "f" * 64
    assert oct((d / "status.json").stat().st_mode & 0o777) == "0o600"


def test_enrollment_off_ec2_is_one_log_line(cloud_runner, monkeypatch):
    logs = []
    monkeypatch.setattr(cloud_runner, "_log", logs.append)
    assert cloud_runner.enroll_aws_device_key("r-1") == {"error": "no instance role"}
    assert len(logs) == 1


def test_enrollment_survives_an_older_canopy_web(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner, "_aws_instance_credentials", lambda: CREDS)
    monkeypatch.setattr(cloud_runner, "_aws_role_arn", lambda creds: ROLE)
    monkeypatch.setattr(cloud_runner, "_api", lambda *a, **k: (422, None))
    monkeypatch.setattr(cloud_runner, "_log", lambda m: None)
    assert cloud_runner.enroll_aws_device_key("r-1") == {"error": "HTTP 422"}


# ── the zero-knowledge vault's ECDH key (canopy-web#1550) ─────────────────────

ECDH_JWK = {"kty": "EC", "crv": "P-256", "x": "a_hdX-hFmLEMphmewJzNfTXc1xlf3R3Hc3oMZwBu8lE",
            "y": "uVLyXuwd7tH8oZGDiZBGY1fTFK37fpdguNmvyPOala0"}


def test_enrollment_sends_the_ecdh_key_canopy_made(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner, "_aws_instance_credentials", lambda: CREDS)
    monkeypatch.setattr(cloud_runner, "_aws_role_arn", lambda creds: ROLE)
    monkeypatch.setattr(cloud_runner, "_log", lambda m: None)
    monkeypatch.setattr(cloud_runner, "_ecdh_public_key",
                        lambda: {"jwk": ECDH_JWK, "jkt": "PypibSbF5m1Gccn93S70AQoOITKZeDXpPTyfRY-cAHc"})
    posts = []

    def api(method, path, body=None, *, prefix="/api/harness", headers=None):
        posts.append(body)
        return 201, {"id": "k1", "jkt": "f" * 64, "status": "pending", "approve_url": "u"}

    monkeypatch.setattr(cloud_runner, "_api", api)
    cloud_runner.enroll_aws_device_key("r-ec2")
    assert posts[0]["ecdh_public_jwk"] == ECDH_JWK
    cached = json.loads((tmp_path / "dk" / "status.json").read_text())
    assert cached["ecdh_jkt"].startswith("Pypib") and cached["ecdh_kind"] == "software"


@pytest.mark.parametrize("rc,out,want", [
    (0, json.dumps({"kind": "software", "jkt": "J", "jwk": ECDH_JWK, "created": True}),
     {"jwk": ECDH_JWK, "jkt": "J"}),
    (2, "", {}),                                                   # an older canopy: no flag
    (0, json.dumps({"jwk": {**ECDH_JWK, "d": "PRIVATE"}}), {}),    # never a private member
    (0, "not json", {}),
])
def test_ecdh_key_comes_from_the_canopy_cli(cloud_runner, monkeypatch, _no_canopy_cli,
                                            rc, out, want):
    import subprocess
    monkeypatch.setattr(cloud_runner, "_ecdh_public_key", _no_canopy_cli)   # the real one
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, rc, out, "Error: No such option" if rc else "")

    monkeypatch.setattr(cloud_runner.subprocess, "run", run)
    monkeypatch.setattr(cloud_runner, "_log", lambda m: None)
    assert cloud_runner._ecdh_public_key() == want
    assert seen[0][1:] == ["vault", "device", "--ecdh-jwk", "--kind", "aws-iam"]


def test_a_zk_agent_without_a_wrap_is_a_named_health_failure(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner, "_api",
                        lambda *a, **k: (409, {"error": "zk_wrap_missing"}))
    monkeypatch.setattr(cloud_runner, "_aws_proof_headers", lambda url: {})
    logs = []
    monkeypatch.setattr(cloud_runner, "_log", logs.append)
    cloud_runner._AGENT_OP.pop("eva", None)
    assert cloud_runner._agent_op_token("eva") == ""
    assert any("grant" in line for line in logs)
    assert cloud_runner._HEALTH["zk.eva"]["status"] == "fail"
