"""apps/common/sessions.py: an unchanged session writes only its expiry."""
from __future__ import annotations

import pytest
from django.contrib.sessions.backends.base import UpdateError
from django.contrib.sessions.models import Session

from apps.common.sessions import SessionStore

pytestmark = pytest.mark.django_db


def _fresh() -> SessionStore:
    s = SessionStore()
    s["a"] = 1
    s.save()
    return s


def test_an_unchanged_session_keeps_what_another_request_wrote_and_rolls_its_expiry():
    key = _fresh().session_key
    stale = SessionStore(key)
    list(stale.items())
    other = SessionStore(key)
    other["b"] = 2
    other.save()
    Session.objects.filter(session_key=key).update(expire_date=stale.get_expiry_date().replace(year=2000))

    stale.save()

    assert SessionStore(key).load() == {"a": 1, "b": 2}
    assert Session.objects.get(session_key=key).expire_date.year > 2000


def test_a_changed_session_still_saves():
    key = _fresh().session_key
    s = SessionStore(key)
    s["a"] = 9
    s.save()
    assert SessionStore(key).load() == {"a": 9}


def test_an_unchanged_session_that_was_deleted_is_not_recreated():
    s = SessionStore(_fresh().session_key)
    list(s.items())
    Session.objects.filter(session_key=s.session_key).delete()
    with pytest.raises(UpdateError):
        s.save()
    assert not Session.objects.exists()
