"""A personal collection: each record its writer's alone, a json field for settings and caches, and
one record per (person, key) — the per-person store an app keeps what a browser extension kept in its
own storage."""
from __future__ import annotations

import copy

import pytest

from apps.workspace_apps import manifest as mf
from apps.workspace_apps import services, template_diff
from apps.workspace_apps.models import AppInstallation, AppRecord, AppRecordEvent
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.workspace_apps import BASE, J, _act, _client, _user

pytestmark = pytest.mark.django_db

APP = {
    "key": "sales", "name": "Sales", "version": "1",
    "collections": {
        "person_store": {
            "personal": True, "key": "key", "read": "member",
            "fields": {"key": {"type": "text", "required": True, "max": 200},
                       "value": {"type": "json", "max_kb": 256}},
        },
        "note": {"read": "member", "fields": {"body": {"type": "text"}}},
    },
    "actions": {
        "store_put": {"role": "member", "op": "create", "collection": "person_store", "input": ["key", "value"]},
        "store_delete": {"role": "member", "op": "delete", "collection": "person_store"},
        "add_note": {"role": "member", "op": "create", "collection": "note", "input": ["body"]},
    },
    "pages": [{"slug": "home", "title": "Home", "audience": "member",
               "code": "function View() { return <div/>; }"}],
}
S = f"{BASE}/sales"


def _install(world, m=None):
    r = _client(world["admin"]).post("/api/workspace-apps/dimagi/", {"manifest": m or APP}, content_type=J)
    assert r.status_code == 201, r.content
    return AppInstallation.objects.get(workspace_id="dimagi", key="sales")


def _put(client, key, value):
    return _act(client, "store_put", {"key": key, "value": value}, base=S)


def _mine(client):
    r = client.get(f"{S}/query?collection=person_store&limit=2000")
    assert r.status_code == 200, r.content
    return {row["key"]: row for row in r.json()}


def _with(**collection_over):
    m = copy.deepcopy(APP)
    m["collections"]["person_store"].update(collection_over)
    return m


# ---- validation ---------------------------------------------------------------------------

@pytest.mark.parametrize("change, why", [
    (lambda m: m["collections"]["person_store"].update(public={"fields": ["key"]}), "no public projection"),
    (lambda m: m["collections"]["person_store"].update(scope={"team": ["key"]}), "no scope"),
    (lambda m: m["collections"]["note"].update(key="body"), "only a personal collection has a key"),
    (lambda m: m["collections"]["person_store"].update(key="value"), "required text field"),
    (lambda m: m["collections"]["note"]["fields"].update(
        about={"type": "ref", "collection": "person_store"}), "nothing may point at"),
    (lambda m: m["actions"].update(sign={"role": "public", "op": "create", "collection": "person_store",
                                         "input": ["key"]}), "written only by its members"),
    (lambda m: m["actions"]["store_put"].update(then=[{"op": "create", "collection": "note",
                                                        "set": {"body": "x"}}]), "no then steps"),
    (lambda m: m["actions"]["add_note"].update(then=[{"op": "create", "collection": "person_store",
                                                       "set": {"key": "x"}}]), "never writes a personal"),
    (lambda m: m["collections"]["person_store"]["fields"]["value"].update(max_kb=257), "max_kb"),
    (lambda m: m["actions"]["store_put"].update(input=["key"], set={"value": "x"}), "only ever the caller's"),
    (lambda m: m["collections"]["note"]["fields"].update(meta={"type": "json"}) or m["collections"]["note"].update(
        public={"fields": ["body", "meta"]}), "never public"),
    (lambda m: m.update(retention=[{"name": "old", "collection": "person_store", "after_days": 30, "delete": True,
                                    "where": {"value": "x"}}]), "never matched on"),
])
def test_what_a_personal_collection_and_a_json_field_refuse(change, why):
    m = copy.deepcopy(APP)
    change(m)
    with pytest.raises(ValueError, match=why):
        mf.validate(m)


def test_the_normalised_shape():
    m = mf.validate(APP)
    c = m["collections"]["person_store"]
    assert c["personal"] is True and c["key"] == "key"
    assert c["fields"]["value"]["max_kb"] == 256
    assert mf.validate({**APP, "collections": {**APP["collections"], "person_store": {
        **APP["collections"]["person_store"], "fields": {
            "key": {"type": "text", "required": True}, "value": {"type": "json"}}}}})[
        "collections"]["person_store"]["fields"]["value"]["max_kb"] == 64
    assert "personal" not in m["collections"]["note"], "an ordinary collection keeps its exact shape"


# ---- owner-only reads and writes ----------------------------------------------------------

def test_a_record_is_its_writers_alone_whatever_the_reader_holds(world):
    _install(world)
    editor, admin, viewer = (_client(world[k]) for k in ("editor", "admin", "viewer"))
    r = _put(editor, "kanbanBoards", {"boards": [1, 2]})
    assert r.status_code == 200, r.content
    rid = r.json()["id"]
    assert _put(viewer, "kanbanBoards", {"boards": []}).status_code == 200

    assert _mine(editor)["kanbanBoards"]["value"] == {"boards": [1, 2]}
    assert _mine(viewer)["kanbanBoards"]["value"] == {"boards": []}
    assert _mine(admin) == {}, "a workspace owner reads nobody's store"
    assert admin.get(f"{S}/query?collection=person_store&where=" + '{"id": %d}' % rid).json() == []
    assert admin.get(f"{S}/records/{rid}/history").status_code == 404
    assert viewer.get(f"{S}/records/{rid}/history").status_code == 404
    assert editor.get(f"{S}/records/{rid}/history").status_code == 200
    assert _act(admin, "store_delete", {}, record=rid, base=S).status_code == 404
    assert _act(editor, "store_delete", {}, record=rid, base=S).status_code == 200
    assert _mine(editor) == {}


def test_a_create_with_a_key_already_held_updates_it_and_a_race_leaves_the_newest(world):
    inst = _install(world)
    editor = _client(world["editor"])
    first = _put(editor, "uiConfig", {"v": 1}).json()["id"]
    again = _put(editor, "uiConfig", {"v": 2}).json()
    assert again["id"] == first and again["value"] == {"v": 2}
    assert AppRecord.objects.filter(installation=inst, collection="person_store").count() == 1

    # Two tabs created it at once: the next write keeps the newest and drops the rest.
    AppRecord.objects.create(installation=inst, collection="person_store", created_by=world["editor"],
                             data={"key": "uiConfig", "value": {"v": 3}})
    out = _put(editor, "uiConfig", {"v": 4}).json()
    assert out["value"] == {"v": 4}
    assert AppRecord.objects.filter(installation=inst, collection="person_store",
                                    data__key="uiConfig").count() == 1


def test_a_json_value_is_capped_and_its_history_says_it_changed_never_what_to(world):
    inst = _install(world, _with(fields={"key": {"type": "text", "required": True},
                                         "value": {"type": "json", "max_kb": 1}}))
    editor = _client(world["editor"])
    assert _put(editor, "k", "x" * 2000).status_code == 422
    assert _put(editor, "k", {"secret": "s3"}).status_code == 200
    assert _put(editor, "k", {"secret": "s4"}).status_code == 200
    changes = list(AppRecordEvent.objects.filter(installation=inst).order_by("id").values_list("op", "changes"))
    assert changes == [("create", {"key": "k", "value": "set"}), ("update", {"value": "updated"})]
    assert "s3" not in str(changes)


def test_a_json_field_cannot_be_matched_or_ordered(world):
    _install(world)
    editor = _client(world["editor"])
    _put(editor, "k", {"a": 1})
    assert editor.get(f'{S}/query?collection=person_store&where={{"value": 1}}').status_code == 422
    assert editor.get(f"{S}/query?collection=person_store&order=value").status_code == 422


def test_a_100_kb_value_round_trips_and_one_query_returns_a_persons_keys(world):
    _install(world)
    editor = _client(world["editor"])
    big = {"objectFieldMeta": {f"Field{i}": {"label": "x" * 90, "type": "string"} for i in range(1000)}}
    assert 100_000 < len(str(big)) < 200_000
    assert _put(editor, "objectFieldMeta", big).status_code == 200
    for i in range(40):
        assert _put(editor, f"k{i}", {"i": i}).status_code == 200
    mine = _mine(editor)
    assert len(mine) == 41 and mine["objectFieldMeta"]["value"] == big


def test_an_app_save_needs_no_click(world):
    """`app.act` is never click-gated (unlike a connection write): a page saves its store as it goes."""
    _install(world)
    r = _client(world["editor"]).post(f"{S}/actions/store_put", {"input": {"key": "k", "value": [1]}},
                                      content_type=J)  # no operation, no gesture: just the session
    assert r.status_code == 200, r.content


def test_an_agent_acting_for_a_person_sees_that_persons_store_only(world):
    inst = _install(world)
    _put(_client(world["editor"]), "mine", 1)
    _put(_client(world["viewer"]), "theirs", 2)
    roles = {"member"}
    rows = services.query(inst, roles=roles, collection="person_store", user=world["viewer"])
    assert [r["key"] for r in rows] == ["theirs"]
    # The app itself (a scenario, retention) is not narrowed.
    assert len(services.query(inst, roles=roles, collection="person_store", user=None)) == 2


def test_a_subject_export_finds_a_persons_own_store(world):
    inst = _install(world)
    _put(_client(world["editor"]), "mine", {"a": 1})
    _put(_client(world["viewer"]), "theirs", 2)
    out = services.subject_export(inst, email=world["editor"].email)
    assert [r["key"] for r in out["records"]["person_store"]] == ["mine"]


def test_a_template_update_that_ends_personal_is_an_admins_to_publish():
    old = mf.validate(APP)
    m = copy.deepcopy(APP)
    m["collections"]["person_store"].pop("personal")
    m["collections"]["person_store"].pop("key")
    found = template_diff.changes(old, mf.validate(m))
    assert any("each person's records were their own" in f for f in found)
    assert template_diff.changes(old, mf.validate(APP)) == []


def test_a_member_outside_the_workspace_cannot_reach_it(world):
    _install(world)
    stranger = _user("stranger")
    assert _put(_client(stranger), "k", 1).status_code in (403, 404)
    WorkspaceMembership.objects.create(user=stranger, workspace=world["ws"], role=WorkspaceMembership.VIEWER)
    assert _put(_client(stranger), "k", 1).status_code == 200
