"""`create_app_credential` — registering a connected site from the shell.

The provisioning tests that used to live here went with the grant itself
(2026-09-22): `--workspace`/`--role` let an app add users to a tenant as a side
effect of token-exchange, and both the flags and the endpoint are gone. What is
left is the one thing the command still does, and the one way it refuses.
"""
from __future__ import annotations

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.tokens.models import AppCredential

pytestmark = pytest.mark.django_db


def test_it_registers_a_site_and_prints_the_secret_once(capsys):
    call_command("create_app_credential", "--name", "connect-labs")
    out = capsys.readouterr().out
    cred = AppCredential.objects.get(name="connect-labs")
    assert cred.revoked_at is None
    # The secret is shown, and the row keeps only its hash.
    printed = out.strip().splitlines()[-1]
    assert printed and printed not in (cred.token_hash, "")
    assert AppCredential.lookup(printed) == cred


def test_a_duplicate_name_is_refused_rather_than_silently_rotating():
    call_command("create_app_credential", "--name", "connect-labs")
    with pytest.raises(CommandError) as exc:
        call_command("create_app_credential", "--name", "connect-labs")
    assert "already exists" in str(exc.value)


def test_registering_grants_nothing_on_its_own():
    """The credential identifies the site. Framing, vouching and which agents it
    may offer are each granted separately — so a fresh row can do nothing."""
    call_command("create_app_credential", "--name", "connect-labs")
    cred = AppCredential.objects.get(name="connect-labs")
    assert cred.frame_origins() == []
    assert list(cred.public_keys or []) == []
    assert cred.tenant_grants.count() == 0, "and no tenant has granted it yet"
    assert cred.allowed_agents.count() == 0
    assert cred.workspace_id is None
