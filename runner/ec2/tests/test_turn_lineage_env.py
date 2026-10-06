"""A cloud turn's Claude session knows which turn/session it runs for
(`_lineage_env`), so what it asks canopy for in turn names it as the parent; and
the claim log line says who and what the turn came from (`_provenance_brief`)."""
from __future__ import annotations


def test_lineage_env_exports_turn_session_and_task(load_cloud_runner):
    cr = load_cloud_runner()
    env = cr._lineage_env({
        "id": "6f1c0000-0000-0000-0000-000000000001",
        "origin_ref": {"chat_session_id": "7a2d0000-0000-0000-0000-000000000002",
                       "thread_key": "emdash:c-scratch-1"},
    })
    assert env["CANOPY_TURN_ID"] == "6f1c0000-0000-0000-0000-000000000001"
    assert env["CANOPY_SESSION_ID"] == "7a2d0000-0000-0000-0000-000000000002"
    assert env["CANOPY_EMDASH_TASK"] == "c-scratch-1"
    assert env["CANOPY_HOST"]


def test_lineage_env_drops_malformed_values(load_cloud_runner):
    cr = load_cloud_runner()
    env = cr._lineage_env({"id": "x; rm -rf /", "origin_ref": {"thread_key": "plain"}})
    assert "CANOPY_TURN_ID" not in env and "CANOPY_EMDASH_TASK" not in env


def test_provenance_brief(load_cloud_runner):
    cr = load_cloud_runner()
    brief = cr._provenance_brief({
        "origin": "api", "prompt": "hi",
        "initiator": {"kind": "user", "user": {"email": "jj@dimagi.com"},
                      "credential": {"type": "pat", "id": 3, "label": "cli"},
                      "client": "canopy-cli"},
        "parent_session_id": "abc",
    })
    assert brief == ('origin=api who=user:jj@dimagi.com credential=pat:3:cli '
                     'client=canopy-cli parent=session:abc prompt_head="hi"')
    assert cr._provenance_brief({}).startswith("origin=- who=unknown credential=-")
