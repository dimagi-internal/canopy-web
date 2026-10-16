"""Decision rows on a storyboard: the ace-web pattern, in Postgres."""

from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import Client

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


def test_edits_and_undo_on_a_superseded_row_are_409(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    dsvc.apply_edit(row, action="confirm", by_user=owner)
    dsvc.publish(board, [_row(), _row(id="banner-v2", supersedes="banner", recommended="Keep as is")])
    old = board.decisions.get(key="banner")
    with pytest.raises(dsvc.DecisionConflict, match="banner-v2"):
        dsvc.apply_edit(old, action="undo", by_user=owner)
    with pytest.raises(dsvc.DecisionConflict, match="banner-v2"):
        dsvc.apply_edit(old, action="pick", value="Keep as is", by_user=owner)


LEGACY = {
    "audience": "Edesia and OES",
    "assumptions": ["SQ-LNS is not in the demo data."],
    "divergences": [
        {
            "script": "Four suppliers",
            "data": "Three in tender 356",
            "resolution": "Re-record cut 2",
            "narrative_slug": "supply-rutf",
        }
    ],
    "open_questions": ["Does OES want the forecast cut?"],
}


def test_from_legacy_brief_maps_each_list():
    rows = dsvc.from_legacy_brief(LEGACY)
    by_kind = {r["id"].split("-")[0]: r for r in rows}
    a, d, q = by_kind["assumption"], by_kind["divergence"], by_kind["question"]
    assert a["status"] == "ai-default" and a["evidence_basis"] == "inferred" and a["review_ask"] == ""
    assert a["options"] == ["SQ-LNS is not in the demo data."]
    assert a["recommended"] == "SQ-LNS is not in the demo data."
    assert d["evidence_basis"] == "conflicting"
    assert d["conflict_signals"] == ["Script: Four suppliers", "Data: Three in tender 356"]
    assert d["recommended"] == "Re-record cut 2" and d["review_ask"] == "recommended-confirmation"
    assert d["scope"] == "supply-rutf"
    assert q["review_ask"] == "recommended-confirmation" and q["options"] == []
    for r in rows:
        dsvc.validate(r)


def test_from_legacy_brief_long_text_stays_valid():
    long = "The demo data has no SQ-LNS product line at all so every cut avoids it entirely."
    rows = dsvc.from_legacy_brief(
        {"assumptions": [long], "divergences": [{"script": "S", "data": "D", "resolution": long}]}
    )
    assert rows[0]["options"] == [] and rows[0]["question"] == long
    assert rows[1]["options"] == [] and rows[1]["reasoning"] == long
    for r in rows:
        dsvc.validate(r)


def test_from_legacy_brief_ids_are_stable_and_unique():
    twice = {"assumptions": ["Same words.", "Same words."]}
    ids = [r["id"] for r in dsvc.from_legacy_brief(twice)]
    assert len(set(ids)) == 2
    assert ids == [r["id"] for r in dsvc.from_legacy_brief(twice)]


def test_create_converts_brief_lists_to_rows_and_strips_them(ws, owner):
    from django.test import Client

    c = Client()
    c.force_login(owner)
    resp = c.post(
        "/api/w/connect/storyboards/", {"slug": "s1", "title": "S1", "brief": LEGACY}, content_type="application/json"
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    for k in dsvc.LEGACY_BRIEF_KEYS:
        assert not body["brief"].get(k)
    assert len(body["decisions"]) == 3
    assert Storyboard.objects.get(slug="s1").brief == {"audience": "Edesia and OES"}


def test_legacy_brief_migrates_once_and_patch_title_keeps_rows(ws, owner):
    import importlib

    from django.apps import apps as django_apps
    from django.test import Client

    b = Storyboard.objects.create(slug="old", title="Old", workspace=ws, brief=dict(LEGACY))
    mig = importlib.import_module("apps.storyboards.migrations.0008_brief_lists_to_decisions")
    mig.forwards(django_apps, None)
    mig.forwards(django_apps, None)  # idempotent
    b.refresh_from_db()
    assert b.brief == {"audience": "Edesia and OES"}
    assert b.decisions.count() == 3
    c = Client()
    c.force_login(owner)
    resp = c.patch("/api/w/connect/storyboards/old", {"title": "New"}, content_type="application/json")
    assert resp.status_code == 200
    assert b.decisions.count() == 3


def test_narrative_caveats_become_read_only_scoped_rows():
    rows = dsvc.from_narrative_caveats(
        {"assumptions": ["Programme 10112 stands in."], "divergences": [], "open_questions": []}, "supply-rutf"
    )
    assert rows[0]["scope"] == "supply-rutf"
    assert rows[0]["id"].startswith("supply-rutf--assumption-")


def test_from_legacy_brief_survives_malformed_values():
    rows = dsvc.from_legacy_brief(
        {"divergences": [{"script": ["x"]}, {"resolution": 5}, 7, None], "assumptions": [3, None]}
    )
    assert rows == []


def test_from_legacy_brief_keeps_divergence_without_script_and_bare_strings():
    rows = dsvc.from_legacy_brief(
        {
            "divergences": [{"data": "Three in tender"}, {"resolution": "Cut it"}, "Script and data differ"],
            "assumptions": "free text",
            "open_questions": "Is this fine?",
        }
    )
    kinds = [r["id"].split("-")[0] for r in rows]
    assert kinds.count("divergence") == 3 and kinds.count("assumption") == 1 and kinds.count("question") == 1
    assert next(r for r in rows if r["id"].startswith("assumption"))["question"] == "free text"
    for r in rows:
        dsvc.validate(r)


def test_long_resolution_is_still_the_recommendation_and_columns_hold():
    long = "word " * 200
    rows = dsvc.from_legacy_brief(
        {
            "assumptions": ["a" * 400],
            "divergences": [{"script": "S" * 500, "resolution": long, "narrative_slug": "n" * 300}],
        },
        prefix="p" * 90,
    )
    a, d = rows
    assert a["options"] == [] and a["recommended"] == ""
    assert d["options"] == [] and d["recommended"] == long.strip()[:300] and d["reasoning"] == long.strip()
    assert len(d["scope"]) <= 200
    assert all(len(r["id"]) <= 120 for r in rows)
    for r in rows:
        dsvc.validate(r)



@pytest.fixture()
def member(owner):
    c = Client()
    c.force_login(owner)
    return c


def test_create_with_decisions_round_trips(ws, member):
    resp = member.post("/api/w/connect/storyboards/",
                       {"slug": "s2", "title": "S2", "decisions": [_row()]},
                       content_type="application/json")
    assert resp.status_code == 200, resp.content
    (d,) = resp.json()["decisions"]
    assert d["id"] == "banner" and d["value"] == "Rename and re-record" and d["editable"] is True


def test_invalid_decision_is_422(ws, member):
    resp = member.post("/api/w/connect/storyboards/",
                       {"slug": "s3", "title": "S3", "decisions": [_row(recommended="nope")]},
                       content_type="application/json")
    assert resp.status_code == 422
    assert not Storyboard.objects.filter(slug="s3").exists()


def test_patch_that_resets_a_ruling_is_409(board, member, owner):
    (row,) = dsvc.publish(board, [_row()])
    dsvc.apply_edit(row, action="pick", value="Keep as is", by_user=owner)
    resp = member.patch(f"/api/w/connect/storyboards/{board.slug}", {"decisions": [_row()]},
                        content_type="application/json")
    assert resp.status_code == 409
    assert "banner" in resp.json()["detail"]


def test_member_edit_applies_immediately(board, member):
    dsvc.publish(board, [_row()])
    resp = member.post(f"/api/storyboards/{board.slug}/decisions/banner/edit",
                       {"action": "pick", "value": "Keep as is", "reasoning": "fine"},
                       content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["value"] == "Keep as is" and resp.json()["decided_by"] == "Jonathan"


def test_non_member_edit_is_refused(board):
    dsvc.publish(board, [_row()])
    token = board.ensure_share_token()
    resp = Client().post(f"/api/storyboards/{board.slug}/decisions/banner/edit?t={token}",
                         {"action": "confirm", "author_name": "Lily"},
                         content_type="application/json")
    assert resp.status_code == 403


def test_edit_unknown_row_is_404(board, member):
    resp = member.post(f"/api/storyboards/{board.slug}/decisions/nope/edit",
                       {"action": "confirm"}, content_type="application/json")
    assert resp.status_code == 404


def test_member_edit_without_csrf_token_is_403(board, owner):
    dsvc.publish(board, [_row()])
    c = Client(enforce_csrf_checks=True)
    c.force_login(owner)
    resp = c.post(f"/api/storyboards/{board.slug}/decisions/banner/edit",
                  {"action": "confirm"}, content_type="application/json")
    assert resp.status_code == 403


def test_brief_only_patch_keeps_ruled_rows_and_drops_unruled_ones(board, member, owner):
    # The legacy lists are the old publisher's whole decision set: a ruled row
    # stays, an unruled row it did not send goes (the same as publish()).
    ruled, _ = dsvc.publish(board, [_row(), _row(id="other")])
    dsvc.apply_edit(ruled, action="confirm", by_user=owner)
    resp = member.patch(f"/api/w/connect/storyboards/{board.slug}",
                        {"brief": {"audience": "X", "assumptions": ["Y holds."]}},
                        content_type="application/json")
    assert resp.status_code == 200, resp.content
    ids = [d["id"] for d in resp.json()["decisions"]]
    assert "banner" in ids and "other" not in ids and len(ids) == 2


def test_legacy_resend_after_a_ruling_keeps_the_ruling_and_applies_the_patch(ws, member, owner):
    url = "/api/w/connect/storyboards/"
    member.post(url, {"slug": "s11", "title": "S11"}, content_type="application/json")
    member.patch(f"{url}s11", {"brief": LEGACY}, content_type="application/json")
    board = Storyboard.objects.get(slug="s11")
    div = board.decisions.get(key__startswith="divergence-")
    dsvc.apply_edit(div, action="write_in", value="Keep cut 2", by_user=owner)
    r = member.patch(f"{url}s11", {"title": "Renamed", "brief": LEGACY}, content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["title"] == "Renamed"
    (d,) = [d for d in r.json()["decisions"] if d["id"] == div.key]
    assert d["value"] == "Keep cut 2" and d["status"] == "overridden" and d["decided_by"] == "Jonathan"
    assert len(r.json()["decisions"]) == 3


def test_reworded_legacy_assumption_replaces_the_unruled_row(ws, member):
    url = "/api/w/connect/storyboards/"
    member.post(url, {"slug": "s12", "title": "S12"}, content_type="application/json")
    first = member.patch(f"{url}s12", {"brief": LEGACY}, content_type="application/json").json()
    (old_id,) = [d["id"] for d in first["decisions"] if d["id"].startswith("assumption-")]
    reworded = {**LEGACY, "assumptions": ["SQ-LNS is absent from the demo data."]}
    r = member.patch(f"{url}s12", {"brief": reworded}, content_type="application/json")
    assert r.status_code == 200, r.content
    ids = [d["id"] for d in r.json()["decisions"]]
    assert old_id not in ids
    assert any(d["question"] == "SQ-LNS is absent from the demo data." for d in r.json()["decisions"])
    assert len(ids) == 3


def test_overlong_id_is_422(ws, member):
    resp = member.post("/api/w/connect/storyboards/",
                       {"slug": "s13", "title": "S13", "decisions": [_row(id="a" * 130)]},
                       content_type="application/json")
    assert resp.status_code == 422
    assert not Storyboard.objects.filter(slug="s13").exists()


@pytest.mark.parametrize(
    "over",
    [
        {"id": "Not_A_Slug"},
        {"supersedes": "has spaces"},
        {"supersedes": "banner"},
        {"scope": "s" * 201},
        {"options": [], "recommended": "r" * 301},
        {"options": ["x" * 301], "recommended": "x" * 301},
    ],
)
def test_validate_enforces_column_bounds_and_slugs(over):
    with pytest.raises(dsvc.DecisionInvalid):
        dsvc.validate(_row(**over))


def test_overlong_write_in_is_422(board, member):
    dsvc.publish(board, [_row()])
    resp = member.post(f"/api/storyboards/{board.slug}/decisions/banner/edit",
                       {"action": "write_in", "value": "w" * 301}, content_type="application/json")
    assert resp.status_code == 422


def test_legacy_ids_are_valid_slugs_after_truncation():
    # slugify keeps "_"; with these prefixes the 120-char cut lands on a hyphen
    # of "ab-ab-…", and the cut before a "-2" suffix on one of "abc-abc-…".
    cases = [
        ({"assumptions": ["Uses stock_on_hand weekly"]}, ""),
        ({"assumptions": ["ab " * 40]}, "p" * 58),
        ({"assumptions": ["abc " * 30, "abc " * 30]}, "q" * 50),
    ]
    for brief, prefix in cases:
        rows = dsvc.from_legacy_brief(brief, prefix=prefix)
        assert len({r["id"] for r in rows}) == len(rows)
        for r in rows:
            dsvc.validate(r)


def test_a_narrative_in_two_acts_yields_its_caveat_rows_once(board, member, monkeypatch):
    from apps.storyboards import services

    row = dsvc.from_narrative_caveats({"assumptions": ["X holds."]}, "n1")[0]
    monkeypatch.setattr(services, "_narrative_caveat_rows", lambda b: [row, dict(row)])
    body = member.get(f"/api/w/connect/storyboards/{board.slug}").json()
    assert [d["id"] for d in body["decisions"]] == [row["id"]]


def test_board_read_includes_narrative_caveats_read_only(board, member, monkeypatch):
    from apps.storyboards import services

    monkeypatch.setattr(services, "_narrative_caveat_rows",
                        lambda b: [dsvc.from_narrative_caveats({"assumptions": ["X holds."]}, "n1")[0]])
    body = member.get(f"/api/w/connect/storyboards/{board.slug}").json()
    (d,) = body["decisions"]
    assert d["scope"] == "n1" and d["editable"] is False


def test_member_with_bogus_token_still_needs_csrf(board, owner):
    dsvc.publish(board, [_row()])
    c = Client(enforce_csrf_checks=True)
    c.force_login(owner)
    resp = c.post(f"/api/storyboards/{board.slug}/decisions/banner/edit?t=bogus",
                  {"action": "confirm"}, content_type="application/json")
    assert resp.status_code == 403


def test_resending_same_legacy_lists_is_idempotent(ws, member):
    url = "/api/w/connect/storyboards/"
    member.post(url, {"slug": "s9", "title": "S9"}, content_type="application/json")
    for _ in range(2):
        r = member.patch(f"{url}s9", {"brief": LEGACY}, content_type="application/json")
        assert r.status_code == 200, r.content
    assert len(r.json()["decisions"]) == 3


def test_changed_legacy_divergence_updates_unruled_row(ws, member):
    url = "/api/w/connect/storyboards/"
    member.post(url, {"slug": "s10", "title": "S10"}, content_type="application/json")
    member.patch(f"{url}s10", {"brief": LEGACY}, content_type="application/json")
    changed = {**LEGACY, "divergences": [{**LEGACY["divergences"][0], "resolution": "Something new"}]}
    r = member.patch(f"{url}s10", {"brief": changed}, content_type="application/json")
    assert r.status_code == 200, r.content
    assert any("Something new" in (d["recommended"] + " ".join(d["options"])) for d in r.json()["decisions"])
    assert len(r.json()["decisions"]) == 3


def test_invalid_read_time_caveat_is_skipped_not_500(board, member, monkeypatch):
    from apps.storyboards import services

    monkeypatch.setattr(services, "_narrative_caveat_rows", lambda b: [{"id": "x", "question": ""}])
    resp = member.get(f"/api/w/connect/storyboards/{board.slug}")
    assert resp.status_code == 200 and resp.json()["decisions"] == []


def test_patch_409_leaves_title_unchanged(board, member, owner):
    (row,) = dsvc.publish(board, [_row()])
    dsvc.apply_edit(row, action="pick", value="Keep as is", by_user=owner)
    resp = member.patch(f"/api/w/connect/storyboards/{board.slug}",
                        {"title": "New title", "decisions": [_row()]}, content_type="application/json")
    assert resp.status_code == 409
    board.refresh_from_db()
    assert board.title == "Connect Supply"
