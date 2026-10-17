"""Tests for BearerTokenAuthMiddleware."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from apps.tokens.middleware import BearerTokenAuthMiddleware
from apps.tokens.models import PersonalToken

User = get_user_model()


def _noop_response(request):
    from django.http import HttpResponse
    return HttpResponse(b"ok")


@pytest.mark.django_db
def test_valid_bearer_attaches_user():
    user = User.objects.create_user(username="alice", email="alice@dimagi.com")
    raw, _ = PersonalToken.create_for_user(user=user, label="ci")

    rf = RequestFactory()
    request = rf.get("/anything", HTTP_AUTHORIZATION=f"Bearer {raw}")
    request.user = AnonymousUser()

    middleware = BearerTokenAuthMiddleware(_noop_response)
    middleware(request)

    assert request.user.is_authenticated
    assert request.user.pk == user.pk
    assert getattr(request, "_dont_enforce_csrf_checks", False) is True


@pytest.mark.django_db
def test_unknown_token_leaves_user_anonymous():
    rf = RequestFactory()
    request = rf.get("/anything", HTTP_AUTHORIZATION="Bearer not-a-real-token")
    request.user = AnonymousUser()

    BearerTokenAuthMiddleware(_noop_response)(request)
    assert not request.user.is_authenticated


@pytest.mark.django_db
def test_revoked_token_leaves_user_anonymous():
    from django.utils import timezone

    user = User.objects.create_user(username="alice", email="alice@dimagi.com")
    raw, token = PersonalToken.create_for_user(user=user, label="ci")
    PersonalToken.objects.filter(pk=token.pk).update(revoked_at=timezone.now())

    rf = RequestFactory()
    request = rf.get("/anything", HTTP_AUTHORIZATION=f"Bearer {raw}")
    request.user = AnonymousUser()

    BearerTokenAuthMiddleware(_noop_response)(request)
    assert not request.user.is_authenticated


@pytest.mark.django_db
def test_no_auth_header_is_a_noop():
    rf = RequestFactory()
    request = rf.get("/anything")
    request.user = AnonymousUser()

    BearerTokenAuthMiddleware(_noop_response)(request)
    assert not request.user.is_authenticated


@pytest.mark.django_db
def test_already_authenticated_user_is_left_alone():
    """A session-authed user should never be replaced by a Bearer lookup."""
    user_session = User.objects.create_user(username="session", email="session@dimagi.com")
    user_bearer = User.objects.create_user(username="bearer", email="bearer@dimagi.com")
    raw, _ = PersonalToken.create_for_user(user=user_bearer, label="other")

    rf = RequestFactory()
    request = rf.get("/anything", HTTP_AUTHORIZATION=f"Bearer {raw}")
    request.user = user_session  # already authenticated

    BearerTokenAuthMiddleware(_noop_response)(request)
    assert request.user.pk == user_session.pk


@pytest.mark.django_db
def test_updates_last_used_at_on_success():
    from django.utils import timezone

    user = User.objects.create_user(username="alice", email="alice@dimagi.com")
    raw, token = PersonalToken.create_for_user(user=user, label="ci")
    assert token.last_used_at is None
    before = timezone.now()

    rf = RequestFactory()
    request = rf.get("/anything", HTTP_AUTHORIZATION=f"Bearer {raw}")
    request.user = AnonymousUser()
    BearerTokenAuthMiddleware(_noop_response)(request)

    token.refresh_from_db()
    assert token.last_used_at is not None
    assert token.last_used_at >= before


@pytest.mark.django_db
def test_records_what_used_the_token():
    """Last used alone read as "nothing uses this" for a cloud runner's token,
    which was then revoked (2026-10-17). The client is shown beside it."""
    user = User.objects.create_user(username="alice", email="alice@dimagi.com")
    raw, token = PersonalToken.create_for_user(user=user, label="box")

    rf = RequestFactory()
    request = rf.get("/anything", HTTP_AUTHORIZATION=f"Bearer {raw}",
                     HTTP_USER_AGENT="canopy-cloud-runner host=ip-172-31-41-219")
    request.user = AnonymousUser()
    BearerTokenAuthMiddleware(_noop_response)(request)

    token.refresh_from_db()
    assert token.last_used_client == "canopy-cloud-runner host=ip-172-31-41-219"


@pytest.mark.django_db
def test_non_bearer_header_is_ignored():
    rf = RequestFactory()
    request = rf.get("/anything", HTTP_AUTHORIZATION="Basic dXNlcjpwYXNz")
    request.user = AnonymousUser()

    BearerTokenAuthMiddleware(_noop_response)(request)
    assert not request.user.is_authenticated
