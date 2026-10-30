"""The vault's diagnostic log lines: who, what client, what the keyset holds — never a secret."""
from __future__ import annotations

import logging

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.passkeys import vault_log
from apps.passkeys.models import Passkey, UserVaultKeyset

pytestmark = pytest.mark.django_db


@pytest.fixture
def lilianna():
    return get_user_model().objects.create_user(username="lb", email="lb@dimagi.com")


def _keyset_with_only_a_passphrase_seal(user):
    Passkey.objects.create(user=user, credential_id="AAAABBBBCCCC", public_key=b"x", label="1Password",
                           aaguid="bada5566-a7aa-401f-bd96-45619a55120d", prf_enabled=False, transports=["internal"])
    UserVaultKeyset.objects.create(user=user, public_jwk={"kty": "EC"},
                                   seals=[{"alg": "2skd", "secret": "never-logged"}])


def test_client_report_logs_client_keyset_and_no_secret(lilianna, caplog):
    _keyset_with_only_a_passphrase_seal(lilianna)
    c = Client(HTTP_USER_AGENT="Mozilla/5.0 Chrome/130", HTTP_SEC_CH_UA_PLATFORM='"macOS"')
    c.force_login(lilianna)
    with caplog.at_level(logging.INFO, logger="canopy.vault"):
        r = c.post("/api/vault/client-report", {
            "stage": "unlock", "method": "passphrase", "outcome": "failed",
            "error": "no passphrase seal opened (1 tried, Secret Key remembered)",
            "asked": ["passphrase"], "secret_key_remembered": True, "browser_prf": True,
        }, content_type="application/json")
    assert r.status_code == 204
    line = next(rec.getMessage() for rec in caplog.records if "vault.client_report" in rec.getMessage())
    assert "kind=browser-session" in line and "platform=macOS" in line and "Chrome/130" in line
    assert "seals=2skd" in line                      # only a passphrase seal: the root cause is visible
    assert "1Password" in line and "prf=False" in line and "sealed=False" in line
    assert "secret_key_remembered=True" in line and "stage=unlock" in line
    assert "never-logged" not in line


def test_authenticator_names():
    assert vault_log.authenticator_name("BADA5566-A7AA-401F-BD96-45619A55120D") == "1Password"
    assert vault_log.authenticator_name("") == "unknown"


def test_a_log_value_cannot_forge_a_second_line():
    assert "\n" not in vault_log._val("a\nvault.keyset_written user=1")
