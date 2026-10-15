"""Decision rows on a storyboard: the ace-web pattern, in Postgres."""

from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError

from apps.storyboards import decisions as dsvc
from apps.storyboards.models import Storyboard, StoryboardDecision
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw", first_name="Jonathan")


@pytest.fixture()
def ws(owner):
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role="owner")
    return ws


@pytest.fixture()
def board(ws):
    return Storyboard.objects.create(
        slug="supply", title="Connect Supply", workspace=ws, capability=Storyboard.CAP_COMMENT
    )


def test_decision_key_is_unique_per_board(board):
    StoryboardDecision.objects.create(storyboard=board, key="banner", question="Rename?")
    with pytest.raises(IntegrityError):
        StoryboardDecision.objects.create(storyboard=board, key="banner", question="Again?")


def test_decision_defaults(board):
    d = StoryboardDecision.objects.create(storyboard=board, key="sqlns", question="Show SQ-LNS?")
    assert d.status == "ai-default"
    assert d.options == [] and d.history == [] and d.conflict_signals == []
    assert d.confirmed is False and d.override == "" and d.scope == ""


def _row(**over):
    base = {
        "id": "banner",
        "question": "Rename the demo programme banner before sharing?",
        "plain_question": "Should the banner read “RUTF programme (demo)” before Edesia sees it?",
        "options": ["Rename and re-record", "Keep as is"],
        "recommended": "Rename and re-record",
        "reasoning": "The internal name is in every cut.",
        "evidence_basis": "stated",
        "review_ask": "required-before",
        "needed_by": "external-share",
    }
    base.update(over)
    return base


def test_validate_rejects_recommended_outside_options():
    with pytest.raises(dsvc.DecisionInvalid, match="recommended"):
        dsvc.validate(_row(recommended="Something else"))


def test_validate_rejects_unknown_vocabulary():
    with pytest.raises(dsvc.DecisionInvalid, match="review_ask"):
        dsvc.validate(_row(review_ask="before-launch"))
    with pytest.raises(dsvc.DecisionInvalid, match="needed_by"):
        dsvc.validate(_row(needed_by="go-live"))


def test_validate_requires_needed_by_for_required_before():
    with pytest.raises(dsvc.DecisionInvalid, match="needed_by"):
        dsvc.validate(_row(needed_by=""))


def test_validate_rejects_long_option():
    with pytest.raises(dsvc.DecisionInvalid, match="8 words"):
        dsvc.validate(
            _row(
                options=["one two three four five six seven eight nine"],
                recommended="one two three four five six seven eight nine",
            )
        )


def test_validate_refuses_caller_supplied_decider():
    with pytest.raises(dsvc.DecisionInvalid, match="decided_by"):
        dsvc.validate(_row(decided_by="mallory"))


def test_publish_creates_rows_in_order(board):
    dsvc.publish(
        board,
        [
            _row(),
            _row(
                id="sqlns",
                question="Show SQ-LNS?",
                options=["RUTF only"],
                recommended="RUTF only",
                review_ask="",
                needed_by="",
            ),
        ],
    )
    keys = list(board.decisions.values_list("key", flat=True))
    assert keys == ["banner", "sqlns"]
    assert dsvc.value_of(board.decisions.get(key="banner")) == "Rename and re-record"


def test_member_pick_overrides_and_records_history(board, owner):
    (row, *_rest) = dsvc.publish(board, [_row()])
    row = dsvc.apply_edit(row, action="pick", value="Keep as is", reasoning="Edesia knows it is a demo", by_user=owner)
    assert row.status == "overridden"
    assert row.override == "Keep as is"
    assert row.decided_by == owner and row.decided_by_verified is True
    assert row.decided_by_name == "Jonathan"
    assert row.history[0]["status"] == "ai-default"
    assert dsvc.value_of(row) == "Keep as is"


def test_confirm_marks_confirmed_and_human_decided(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    row = dsvc.apply_edit(row, action="confirm", by_user=owner)
    assert row.confirmed is True and row.status == "human-decided" and row.override == ""


def test_pick_of_recommended_is_a_confirm(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    row = dsvc.apply_edit(row, action="pick", value="Rename and re-record", by_user=owner)
    assert row.confirmed is True and row.status == "human-decided"


def test_write_in_appends_option(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    row = dsvc.apply_edit(row, action="write_in", value="Rename only, no re-record", by_user=owner)
    assert "Rename only, no re-record" in row.options
    assert row.override == "Rename only, no re-record" and row.status == "overridden"


def test_write_in_matching_existing_option_does_not_duplicate(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    row = dsvc.apply_edit(row, action="write_in", value="  keep as IS ", by_user=owner)
    assert row.options == ["Rename and re-record", "Keep as is"]
    assert row.override == "Keep as is"


def test_pick_of_unknown_option_is_invalid(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    with pytest.raises(dsvc.DecisionInvalid):
        dsvc.apply_edit(row, action="pick", value="Not an option", by_user=owner)


def test_undo_restores_previous_state(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    row = dsvc.apply_edit(row, action="pick", value="Keep as is", by_user=owner)
    row = dsvc.apply_edit(row, action="undo", by_user=owner)
    assert row.status == "ai-default" and row.override == "" and row.history == []


def test_undo_with_empty_history_is_409(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    with pytest.raises(dsvc.DecisionConflict, match="nothing to undo"):
        dsvc.apply_edit(row, action="undo", by_user=owner)


def test_concurrent_edits_both_recorded_in_history(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    stale = StoryboardDecision.objects.get(pk=row.pk)  # a second tab's copy
    dsvc.apply_edit(row, action="pick", value="Keep as is", by_user=owner)
    after = dsvc.apply_edit(stale, action="confirm", by_user=owner)  # re-read under lock
    assert after.history[0]["override"] == "Keep as is"
    assert len(after.history) == 2


def test_publish_cannot_reset_a_ruling_and_rolls_back(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    dsvc.apply_edit(row, action="pick", value="Keep as is", by_user=owner)
    with pytest.raises(dsvc.DecisionConflict, match="banner"):
        dsvc.publish(
            board,
            [_row(), _row(id="new-row", question="New?", options=[], recommended="", review_ask="", needed_by="")],
        )
    assert not board.decisions.filter(key="new-row").exists()
    assert dsvc.value_of(board.decisions.get(key="banner")) == "Keep as is"


def test_publish_keeps_ruling_when_value_matches(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    dsvc.apply_edit(row, action="pick", value="Keep as is", by_user=owner)
    dsvc.publish(board, [_row(recommended="Keep as is", plain_question="Reworded?")])
    row = board.decisions.get(key="banner")
    assert row.status == "overridden" and row.plain_question == "Reworded?"
    assert row.decided_by == owner and len(row.history) == 1


def test_publish_cannot_drop_a_ruled_row(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    dsvc.apply_edit(row, action="confirm", by_user=owner)
    with pytest.raises(dsvc.DecisionConflict, match="supersede"):
        dsvc.publish(board, [])


def test_publish_drops_untouched_ai_rows(board):
    dsvc.publish(board, [_row()])
    dsvc.publish(board, [])
    assert not board.decisions.exists()


def test_supersede_marks_old_row(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    dsvc.apply_edit(row, action="confirm", by_user=owner)
    dsvc.publish(board, [_row(), _row(id="banner-v2", supersedes="banner", recommended="Keep as is")])
    assert board.decisions.get(key="banner").superseded_by == "banner-v2"


def test_serialize_derives_value_and_hides_user_ids(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    row = dsvc.apply_edit(row, action="pick", value="Keep as is", by_user=owner)
    out = dsvc.serialize(row)
    assert out["id"] == "banner" and out["value"] == "Keep as is"
    assert out["decided_by"] == "Jonathan" and "decided_by_id" not in out
    assert out["editable"] is True
