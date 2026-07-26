"""Feedback is generic over its target and idempotent per (channel, source_ref)."""
from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from apps.feedback.models import Feedback

pytestmark = pytest.mark.django_db


def _mk(**over):
    kwargs = dict(
        target_kind="narrative",
        target_ref="verified-monitoring",
        target_version=17,
        anchor_id="the-goal",
        kind=Feedback.KIND_COMMENT,
        body="'Back-check' is the term of art here; 'audit' means something else.",
        author_name="Sophie",
        author_email="sophie@example.org",
        channel=Feedback.CHANNEL_EMAIL,
        source_ref="<msg-1@mail>",
    )
    kwargs.update(over)
    return Feedback.objects.create(**kwargs)


def test_defaults_to_state_new():
    assert _mk().state == Feedback.STATE_NEW


def test_same_channel_and_source_ref_cannot_be_ingested_twice():
    _mk()
    with pytest.raises(IntegrityError), transaction.atomic():
        _mk()


def test_the_same_source_ref_on_a_different_channel_is_a_different_row():
    _mk()
    assert _mk(channel=Feedback.CHANNEL_GDOC).pk


def test_a_blank_source_ref_does_not_collide():
    """Web submits have no natural id — two of them are two pieces of feedback."""
    _mk(channel=Feedback.CHANNEL_WEB, source_ref="")
    assert _mk(channel=Feedback.CHANNEL_WEB, source_ref="").pk


def test_anchor_is_optional_for_whole_narrative_feedback():
    assert _mk(anchor_id="").pk


def test_a_suggestion_carries_proposed_text():
    fb = _mk(
        kind=Feedback.KIND_SUGGESTION,
        suggested_text="…a re-visit by a QC enumerator from the survey firm.",
    )
    assert fb.suggested_text


def test_the_target_is_a_string_pair_not_a_foreign_key():
    """Generic on purpose — an FK to a product model would break the tier, and a
    narrative is not even a table."""
    field_types = {f.name: f.get_internal_type() for f in Feedback._meta.get_fields()}
    assert field_types["target_kind"] == "CharField"
    assert field_types["target_ref"] == "CharField"


def test_submitted_by_survives_deleting_the_caller():
    """SET_NULL, not CASCADE: deleting a departing teammate's account must not
    delete the domain expert's feedback they happened to ingest."""
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user("agent", "a@dimagi.com", "pw")
    fb = _mk(submitted_by=user)
    user.delete()
    fb.refresh_from_db()
    assert fb.pk and fb.submitted_by is None
    assert fb.author_name == "Sophie"
