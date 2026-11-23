"""A turn that fails over to the next Claude credential never shows the refused
credential's own line to the people in the conversation.

The CLI writes a credential's refusal ("You've hit your weekly limit · resets 9pm (UTC)")
into the session's transcript as an assistant record. The runner ships that transcript as
the conversation's stored rows, which the panel, Slack and email all read, so the box
drops the refusal from every ship: the next credential's answer is the reply.
"""
from __future__ import annotations

import json

import pytest

CAP = "You've hit your weekly limit · resets 9pm (UTC)"


def _cap_record(uuid="r-cap"):
    # The exact shape Claude Code writes for a credential's refusal.
    return {"type": "assistant", "uuid": uuid, "isApiErrorMessage": True, "error": "rate_limit",
            "message": {"role": "assistant", "model": "<synthetic>",
                        "content": [{"type": "text", "text": CAP}]}}


def _user(text, uuid):
    return {"type": "user", "uuid": uuid, "message": {"role": "user", "content": text}}


def _reply(text, uuid):
    return {"type": "assistant", "uuid": uuid,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


@pytest.fixture()
def shipped(cloud_runner, monkeypatch):
    posts = []

    def fake_api(method, path, body=None, **kw):
        posts.append((path, body))
        return 200, {"count": len((body or {}).get("events") or (body or {}).get("messages") or [])}

    monkeypatch.setattr(cloud_runner, "_api", fake_api)
    return posts


def _rows(cloud_runner, records):
    return cloud_runner._transcript_core().conversational_messages(records, -1)


def test_the_cap_line_is_never_shipped_and_the_answer_is(cloud_runner, shipped):
    records = [_user("Which worker is furthest behind?", "u1"), _cap_record(),
               _user("Which worker is furthest behind?", "u2"),
               _reply("Worker 3 in opportunity 10131 is furthest behind.", "a1")]
    rows = _rows(cloud_runner, records)
    # The transcript reader does flag it as the CLI's error notice …
    assert any(r["role"] == "assistant" and r["content"].get("api_error") == "rate_limit"
               for r in rows)
    assert cloud_runner._post_stream_rows("rid", "sess", rows, "tid") is True
    sent = [e for _, body in shipped for e in body["events"]]
    texts = [e["payload"].get("text") or e["payload"].get("plaintext") or json.dumps(e["payload"])
             for e in sent if e["kind"] == "assistant"]
    # … and it never leaves the box; the next credential's answer does.
    assert not any(CAP in t for t in texts)
    assert any("Worker 3" in t for t in texts)
    assert sum(1 for e in sent if e["kind"] == "user") == 2


def test_a_batch_of_only_the_refusal_sends_nothing(cloud_runner, shipped):
    assert cloud_runner._post_stream_rows("rid", "sess", _rows(cloud_runner, [_cap_record()]),
                                          "tid") is True
    assert shipped == []


@pytest.mark.parametrize("error", ["rate_limit", "authentication_failed", "billing_error"])
def test_every_credential_refusal_is_held_back(cloud_runner, error):
    row = {"role": "assistant", "text": "anything", "content": {"api_error": error}}
    assert cloud_runner._is_credential_refusal(row)


def test_other_cli_errors_and_the_agents_own_words_still_ship(cloud_runner):
    server_error = {"role": "assistant", "text": "API Error: 500 Internal server error",
                    "content": {"api_error": "unknown"}}
    talk = {"role": "assistant", "text": CAP, "content": {}}  # the agent quoting it
    assert not cloud_runner._is_credential_refusal(server_error)
    assert not cloud_runner._is_credential_refusal(talk)


def test_a_full_history_backfill_leaves_it_out_too(cloud_runner, shipped, monkeypatch, tmp_path):
    path = tmp_path / "abc.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in [
        _user("q", "u1"), _cap_record(), _user("q", "u2"), _reply("the answer", "a1")]))
    monkeypatch.setattr(cloud_runner, "_session_transcript_path", lambda *a: path)
    monkeypatch.setattr(cloud_runner, "_api", lambda m, p, b=None, **k: (
        shipped.append((p, b)) or (200, {"backfills": [{"session_id": "s1"}]} if m == "GET" else {})))
    cloud_runner._drain_backfills("rid")
    sent = [m for p, b in shipped if p.endswith("session-backfill") for m in b["messages"]]
    assert sent and not any(CAP in json.dumps(m) for m in sent)


def test_the_failover_is_on_the_turns_ledger(cloud_runner, monkeypatch):
    cr = cloud_runner
    monkeypatch.setenv("RUNNER_NAME", "cloud-ec2-1")
    cr._CLAUDE_CREDS.clear()
    cr._CLAUDE_CREDS.extend([("subscription-1", "CLAUDE_CODE_OAUTH_TOKEN", "tok-1"),
                             ("subscription-2", "CLAUDE_CODE_OAUTH_TOKEN", "tok-2")])
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(cr, "box_login_ready", lambda *a, **k: False)
    monkeypatch.setattr(cr, "_notify_api_key_fallback", lambda *a, **k: None)
    cr._apply_claude_credential(0)
    seen = []
    monkeypatch.setattr(cr, "_lifecycle", lambda tid, ev, **kw: seen.append((ev, kw)))

    def once(prompt, turn_id, emit, **kw):
        if cr._claude_cred_label() == "subscription-1":
            return False, CAP, ""
        return True, "the answer", "sess-1"

    monkeypatch.setattr(cr, "_execute_once", once)
    ok, _, _ = cr.execute_prompt("q", "turn-abcdef12", lambda e: None)
    assert ok is True
    assert ("credential_failover", {"skipped": "subscription-1", "reason": "capped"}) in seen
