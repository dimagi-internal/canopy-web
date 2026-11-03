"""The names a provider shares with the canopy CLI (`orchestrator/connections/providers/<name>.py`):
the CLI pins this same dict, so a drift on either side fails a test on that side."""
from __future__ import annotations

from apps.connections import providers, services

SHARED = {
    "salesforce": {"required_scopes": ("api", "refresh_token"), "rotates_refresh_token": True},
    "google": {"required_scopes": ("openid", "https://www.googleapis.com/auth/userinfo.email"),
               "rotates_refresh_token": False},
}


def test_provider_shared_names_match_the_cli_contract():
    assert set(SHARED) == set(providers.CLASSES)
    for name, want in SHARED.items():
        cls = providers.get_class(name)
        assert (cls.required_scopes, cls.rotates_refresh_token) == (
            want["required_scopes"], want["rotates_refresh_token"])


def test_new_verbs_are_audited():
    assert {"shared", "refreshed", "resealed", "revoked", "revoke_unconfirmed",
            "lease_expired_unwritten"} <= services.AUDITED
