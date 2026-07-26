"""Ingest is idempotent per (channel, source_ref) and emits nothing."""
from __future__ import annotations

import pytest

from apps.feedback import services
from apps.feedback.models import Feedback

pytestmark = pytest.mark.django_db


def _item(**over):
    d = dict(
        target_kind="narrative",
        target_ref="verified-monitoring",
        target_version=17,
        anchor_id="the-goal",
        kind="comment",
        body="Say 'back-check', not 'audit'.",
        author_name="Sophie",
        channel="email",
        source_ref="<m1@mail>",
    )
    d.update(over)
    return d


def test_ingest_creates_rows():
    out = services.ingest([_item(), _item(source_ref="<m2@mail>")])
    assert (out["created"], out["duplicate"]) == (2, 0)


def test_re_ingesting_the_same_source_ref_is_a_no_op():
    services.ingest([_item()])
    out = services.ingest([_item(body="edited in the mail client")])
    assert (out["created"], out["duplicate"]) == (0, 1)
    assert Feedback.objects.count() == 1


def test_a_partial_duplicate_batch_still_creates_the_new_rows():
    services.ingest([_item()])
    out = services.ingest([_item(), _item(source_ref="<m3@mail>")])
    assert (out["created"], out["duplicate"]) == (1, 1)


def test_two_web_submits_without_a_source_ref_are_both_kept():
    out = services.ingest([
        _item(channel="web", source_ref=""),
        _item(channel="web", source_ref=""),
    ])
    assert out["created"] == 2


def test_the_same_id_on_two_channels_is_two_rows():
    out = services.ingest([_item(channel="email"), _item(channel="gdoc")])
    assert out["created"] == 2


def test_list_filters_by_target_and_state():
    services.ingest([_item(), _item(target_ref="other", source_ref="<m9@mail>")])
    assert services.list_feedback(target_ref="verified-monitoring").count() == 1
    assert services.list_feedback(state="new").count() == 2
    assert services.list_feedback(channel="email").count() == 2


def test_resolve_records_the_disposition():
    pk = services.ingest([_item()])["ids"][0]
    fb = services.resolve(pk, state="answered", note="folded into v18", resolved_in_version=18)
    assert (fb.state, fb.resolved_in_version) == ("answered", 18)
    assert "v18" in fb.disposition_note


def test_resolve_without_a_note_keeps_the_existing_one():
    pk = services.ingest([_item()])["ids"][0]
    services.resolve(pk, state="triaged", note="looked at it")
    fb = services.resolve(pk, state="answered")
    assert fb.disposition_note == "looked at it"


def test_ingest_creates_no_work():
    """The whole point: feedback is inert until a turn reads it.

    If this fails because someone wired feedback to raise an Item, remove the
    wiring — auto-promotion is the design decision this app exists to refuse.
    """
    from apps.harness.models import Item

    before = Item.objects.count()
    services.ingest([_item()])
    assert Item.objects.count() == before
