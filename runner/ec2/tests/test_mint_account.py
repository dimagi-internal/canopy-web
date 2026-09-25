"""Naming a freshly minted login, so the settings page can tell primary from
fallback. Best-effort: a sign-in must never fail because the name could not be read."""
from __future__ import annotations

import io
import json


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_reads_the_email_from_the_profile(cloud_runner, monkeypatch):
    seen = {}

    def fake(req, timeout):
        seen["auth"] = req.get_header("Authorization")
        return _Resp(json.dumps({"account": {"email": "a@dimagi.com"}}).encode())

    monkeypatch.setattr(cloud_runner.urllib.request, "urlopen", fake)
    assert cloud_runner._account_for_token("tok") == "a@dimagi.com"
    assert seen["auth"] == "Bearer tok"


def test_a_token_that_cannot_read_its_profile_yields_no_name(cloud_runner, monkeypatch):
    def fake(req, timeout):
        raise cloud_runner.urllib.error.HTTPError(req.full_url, 403, "scope", {}, None)

    monkeypatch.setattr(cloud_runner.urllib.request, "urlopen", fake)
    assert cloud_runner._account_for_token("tok") == ""


def test_an_unexpected_shape_yields_no_name(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner.urllib.request, "urlopen",
                        lambda req, timeout: _Resp(b"[]"))
    assert cloud_runner._account_for_token("tok") == ""
