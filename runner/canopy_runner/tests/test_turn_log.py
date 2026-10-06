"""The runner's CLAIM line says who asked, with what, from where, under which parent."""
from __future__ import annotations

from canopy_runner import client, turn_log

TURN = {
    "id": "t-1", "agent_slug": "hal", "origin": "canopy_web_chat",
    "prompt": 'say "hi"\nplease',
    "initiator": {
        "kind": "user", "via": "chat", "assurance": "pat",
        "user": {"id": 1, "email": "jj@dimagi.com", "name": "JJ"},
        "credential": {"type": "pat", "id": 7, "label": "scratch script"},
        "client": "canopy-cli/0.2.590",
        "parent": {"turn_id": "p-1", "session_id": "s-1"},
    },
    "parent_task": "c-scratch",
}


def test_claim_line_names_who_credential_client_and_parent():
    line = turn_log.claim_line(TURN)
    assert line.startswith("CLAIM turn=t-1 target=hal origin=canopy_web_chat")
    assert "who=user:jj@dimagi.com" in line
    assert 'credential="pat:7:scratch script"' in line
    assert 'client="canopy-cli/0.2.590"' in line
    assert "parent=turn:p-1/session:s-1/task:c-scratch" in line
    assert 'prompt_head="say \\"hi\\"\\nplease"' in line


def test_a_bare_turn_still_renders():
    line = turn_log.claim_line({"id": "t-2", "origin": "api"})
    assert "who=unknown" in line and "parent=-" in line and 'credential="-"' in line
    assert turn_log.summary({"origin": "email"}) == "origin=email who=unknown parent=-"


def test_requests_identify_the_runner():
    import urllib.request

    req = urllib.request.Request("https://example.invalid/api/x")
    client._identify(req)
    assert req.get_header("User-agent").startswith("canopy-runner/")
    assert " host=" in req.get_header("User-agent")
    assert req.get_header("X-canopy-client") == "canopy-runner"
