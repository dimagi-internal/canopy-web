"""The runner's log says WHO and WHAT a claimed turn came from (canopy_runner.whois),
and every request it makes names itself (client.py's User-Agent / X-Canopy-Client),
so a scratch script on a person's PAT is no longer indistinguishable from them."""
from __future__ import annotations

import logging
import urllib.request

from canopy_runner import client as client_mod
from canopy_runner import whois

TURN = {
    "id": "6f1c0000-0000-0000-0000-000000000001",
    "origin": "canopy_web_chat",
    "prompt": 'say "hi" please',
    "initiator": {"kind": "user", "via": "chat", "assurance": "pat",
                  "user": {"id": 1, "email": "jj@dimagi.com", "name": "JJ"},
                  "credential": {"type": "pat", "id": 42, "label": "scratch script"},
                  "client": "e2e_session_chat.py"},
    "parent_turn_id": "6f1c0000-0000-0000-0000-0000000000aa",
}


def test_claim_line_names_origin_who_credential_client_parent_and_prompt():
    line = whois.claim_line(TURN)
    assert line.startswith(f"CLAIM turn={TURN['id']} origin=canopy_web_chat")
    assert "who=user:jj@dimagi.com" in line
    assert 'credential="pat:42:scratch script"' in line
    assert "client=e2e_session_chat.py" in line
    assert "parent=turn:6f1c0000-0000-0000-0000-0000000000aa" in line
    assert line.endswith('prompt_head="say \\"hi\\" please"')


def test_a_turn_from_an_older_server_still_logs():
    assert whois.brief({"id": "x"}) == "origin=- who=unknown credential=- client=- parent=-"


def test_the_claim_path_logs_the_claim_line(caplog):
    from canopy_runner import main

    class _C:
        def claim(self, runner_id, paused_agents):
            return dict(TURN)

    class _Cfg:
        runner_id = "r1"

    seen = {}

    def fake_execute(cfg, client, runner_id, turn, cancel_check):
        seen["turn"] = turn
        return "created:x"

    import canopy_runner.execute as execute

    orig, orig_mark = execute.execute_turn, main._mark_in_flight
    execute.execute_turn, main._mark_in_flight = fake_execute, lambda cfg, extra=0: None
    try:
        with caplog.at_level(logging.INFO):
            main._claim_and_execute(_Cfg(), _C(), set())
    finally:
        execute.execute_turn, main._mark_in_flight = orig, orig_mark
    assert any(r.getMessage().startswith("CLAIM turn=") for r in caplog.records)


def test_every_request_names_the_runner():
    req = urllib.request.Request("http://x/api/y")
    client_mod._identify(req)
    assert req.get_header("X-canopy-client") == "canopy-runner"
    assert req.get_header("User-agent").startswith("canopy-runner/")
    assert " host=" in req.get_header("User-agent")
