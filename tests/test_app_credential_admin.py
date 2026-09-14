"""The admin can no longer manage embedding apps — on purpose.

This file used to assert the opposite: that creating an `AppCredential` through
the Django admin minted a working credential. That was the only route there
was, and it was the wrong one twice over. Operationally the admin is staff-only,
so the person who wants to embed an agent cannot do it, and there is nowhere to
run the equivalent management command either (`EnableExecuteCommand` is off on
the service, the database is VPC-internal). Conceptually, connecting a site is
something a person does, not a row an administrator edits — and every field
here fails closed and silently, so a bare form produces a widget that never
appears with no way to find out why.

`/w/{workspace}/connected-apps` is the surface now (`tests/test_connected_apps.py`).
The admin stays registered because inspecting a row is genuinely useful when
something is broken, and stays read-only so it cannot drift back into being the
management path — which is exactly how it became one.
"""

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from apps.tokens.models import AppCredential

pytestmark = pytest.mark.django_db

LABS = "https://labs.connect.dimagi.com"


def _staff():
    user = User.objects.create_user("root", "root@dimagi.com", "pw", is_staff=True, is_superuser=True)
    c = Client()
    c.force_login(user)
    return user, c


def _existing():
    _raw, cred = AppCredential.create_credential(
        name="connect-labs", domains=[], created_by=None
    )
    cred.allowed_frame_origins = [LABS]
    cred.save(update_fields=["allowed_frame_origins"])
    return cred


def test_the_add_page_is_gone():
    """Not merely discouraged — unreachable, so it cannot quietly come back
    into use the next time someone needs a credential in a hurry."""
    _user, c = _staff()
    assert c.get(reverse("admin:tokens_appcredential_add")).status_code == 403


def test_posting_to_the_add_url_creates_nothing():
    """A 403 on the GET is not enough if the POST still writes."""
    _user, c = _staff()

    c.post(reverse("admin:tokens_appcredential_add"), {"name": "sneaky"}, follow=True)

    assert not AppCredential.objects.filter(name="sneaky").exists()


def test_a_row_can_still_be_INSPECTED():
    """Looking at one is the reason this is still registered at all."""
    cred = _existing()
    _user, c = _staff()

    body = c.get(reverse("admin:tokens_appcredential_change", args=[cred.pk])).content.decode()

    assert "connect-labs" in body
    assert LABS in body


def test_but_not_edited():
    _user, c = _staff()
    cred = _existing()

    c.post(
        reverse("admin:tokens_appcredential_change", args=[cred.pk]),
        {"name": "renamed", "allowed_frame_origins": '["https://evil.example"]'},
        follow=True,
    )

    cred.refresh_from_db()
    assert cred.name == "connect-labs"
    assert cred.frame_origins() == [LABS]


def test_and_not_deleted():
    """Disconnecting is a revoke on the product surface, which keeps the row as
    the record of what was once allowed to embed an agent."""
    _user, c = _staff()
    cred = _existing()
    assert c.get(reverse("admin:tokens_appcredential_delete", args=[cred.pk])).status_code == 403
    assert AppCredential.objects.filter(pk=cred.pk).exists()


def test_the_token_hash_is_never_a_form_field():
    """The raw value is unrecoverable, so a hash in an editable form is an
    invitation to paste something into it."""
    _user, c = _staff()
    cred = _existing()
    body = c.get(reverse("admin:tokens_appcredential_change", args=[cred.pk])).content.decode()
    assert 'name="token_hash"' not in body


def test_a_non_staff_user_cannot_reach_it_at_all():
    User.objects.create_user("plain", "plain@dimagi.com", "pw")
    c = Client()
    c.login(username="plain", password="pw")
    assert c.get(reverse("admin:tokens_appcredential_changelist")).status_code in (302, 403)
