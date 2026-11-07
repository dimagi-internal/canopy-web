"""Runner attestations (``canopy_sdk.runner_attestations``): the shared wire contract."""
from __future__ import annotations

import json

import pytest
from canopy_sdk import runner_attestations as ra
from canopy_sdk.fetch import FetchError
from canopy_sdk.keys import generate_private_key

SITE_KEY = generate_private_key("EdDSA")


def test_the_bound_url_covers_the_body_and_ignores_query_and_slash():
    a = ra.bound_url("https://labs.example.org/mcp/admin/agent-requests/?x=1#f", b'{"k":1}')
    assert a.startswith("https://labs.example.org/mcp/admin/agent-requests/attest/")
    assert a == ra.bound_url("https://labs.example.org/mcp/admin/agent-requests", '{"k":1}')
    assert a != ra.bound_url("https://labs.example.org/mcp/admin/agent-requests", b'{"k":2}')


def test_the_header_round_trips_and_refuses_junk():
    assert ra.parse_header(ra.header_value("dpop", "a.b.c")) == ("dpop", "a.b.c")
    for bad in ("", "dpop", "rsa x", "aws-iam   "):
        with pytest.raises(ra.AttestationError):
            ra.parse_header(bad)
    with pytest.raises(ra.AttestationError):
        ra.header_value("rsa", "x")


def _ok(url, payload, headers=None):
    assert url == "https://canopy.example.org/api/sites/runner-attestations"
    assert payload == {"kind": "dpop", "proof": "p", "agent": "ace"}
    assert headers["Authorization"].startswith("Bearer ")
    return 200, json.dumps({"runner": "cloud-ec2-2", "kind": "aws-iam", "jkt": "j", "agent": "ace",
                            "serves_agent": True}).encode(), {}


def test_request_attestation_reads_canopys_answer():
    got = ra.request_attestation("https://canopy.example.org", "connect-labs", SITE_KEY, kind="dpop",
                                 proof="p", speaks_for="ace", post=_ok)
    assert got == ra.Attestation("cloud-ec2-2", "aws-iam", "j", "ace", True)


def test_a_refusal_carries_canopys_code_and_a_dead_canopy_is_unreachable():
    def refuse(url, payload, headers=None):
        return 401, json.dumps({"detail": "proof_refused: no approved runner key proved this request"}).encode(), {}

    with pytest.raises(ra.AttestationError) as err:
        ra.request_attestation("https://canopy.example.org", "connect-labs", SITE_KEY, kind="dpop", proof="p",
                               post=refuse)
    assert err.value.code == "proof_refused" and err.value.status == 401

    def down(url, payload, headers=None):
        raise FetchError("boom")

    with pytest.raises(ra.AttestationError) as err:
        ra.request_attestation("https://canopy.example.org", "connect-labs", SITE_KEY, kind="dpop", proof="p",
                               post=down)
    assert err.value.code == "unreachable"


def test_a_malformed_answer_is_not_believed():
    with pytest.raises(ra.AttestationError) as err:
        ra.request_attestation("https://canopy.example.org", "connect-labs", SITE_KEY, kind="dpop", proof="p",
                               post=lambda *a, **k: (200, b'{"runner": "x"}', {}))
    assert err.value.code == "malformed"
