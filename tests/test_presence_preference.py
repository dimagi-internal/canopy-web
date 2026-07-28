import pytest
from django.contrib.auth import get_user_model

from apps.realtime.models import PresencePreference, show_presence_for

pytestmark = pytest.mark.django_db


def _user(email="a@x.com"):
    return get_user_model().objects.create_user(username=email, email=email)


def test_defaults_to_visible_when_no_row_exists():
    assert show_presence_for(_user()) is True


def test_honours_an_explicit_opt_out():
    user = _user()
    PresencePreference.objects.create(user=user, show_presence=False)
    assert show_presence_for(user) is False


def test_a_user_has_at_most_one_preference_row():
    user = _user()
    PresencePreference.objects.create(user=user, show_presence=False)
    with pytest.raises(Exception):
        PresencePreference.objects.create(user=user, show_presence=True)
