"""End-to-end against the REAL `claude-agent-acp`.

Everything else in this package tests our side of the protocol against a fake
agent. This file tests the assumption underneath all of it — that the adapter
speaks what we think it speaks — and it is the only test here that spends
tokens, needs Node, and needs Claude auth.

Skipped unless the adapter is actually resolvable, so it is opt-in on a laptop
and on the cloud box and silent in CI:

    ACP_ADAPTER_PATH=…/node_modules/@agentclientprotocol/claude-agent-acp/dist/index.js \\
        pytest tests/test_live_adapter.py -v
"""
import os

import pytest

from canopy_acp import AcpAgent, UpdateReducer, find_adapter

pytestmark = pytest.mark.skipif(
    find_adapter(os.environ.get("ACP_ADAPTER_PATH")) is None,
    reason="claude-agent-acp not installed (set ACP_ADAPTER_PATH)",
)


@pytest.fixture
def agent(tmp_path):
    updates = []
    a = AcpAgent(cwd=tmp_path, on_update=lambda sid, u: updates.append(u))
    a.updates = updates
    init = a.start()
    a.init_result = init
    yield a
    a.close()


def test_a_turn_streams_the_lifecycle_and_writes_a_transcript(agent, tmp_path):
    """The load-bearing claim of the whole adoption: ACP gives us the live
    layer AND leaves the durable transcript exactly where it was."""
    reducer = UpdateReducer()
    agent.session_id = agent.new_session()
    assert agent.session_id

    for update in list(agent.updates):
        reducer.apply(update)

    result = agent.prompt(
        "Run `echo acp-live-test` using Bash, then reply with one short sentence."
    ).result(timeout=180)
    assert result["stopReason"] == "end_turn"

    for update in agent.updates:
        reducer.apply(update)

    # 1. A tool call arrived, complete, with the agent's own title.
    calls = [c for c in reducer.tool_calls if c.tool_name == "Bash"]
    assert calls, f"no Bash tool call in {[u.get('sessionUpdate') for u in agent.updates]}"
    call = calls[0]
    assert call.is_complete
    assert call.id.startswith("toolu_")      # the transcript's own tool_use id
    assert "acp-live-test" in call.result_text
    assert call.title and call.title != "Terminal"   # the placeholder was superseded

    # 2. Rows are the shape the client already renders.
    rows = reducer.rows_for_tool_call(call.id)
    assert [r["role"] for r in rows] == ["tool_use", "tool_result"]
    assert rows[0]["content"]["input"].get("command")

    # 3. Reply text streamed.
    assert reducer.assistant_text.strip()

    # 4. The durable path is untouched — a normal transcript, at the standard
    #    path, keyed by the ACP session id.
    from canopy_transcript import encode_project_dir
    import pathlib
    transcript = (pathlib.Path.home() / ".claude" / "projects"
                  / encode_project_dir(str(tmp_path)) / f"{agent.session_id}.jsonl")
    assert transcript.exists(), f"no transcript at {transcript}"
    assert transcript.stat().st_size > 0


def test_subscription_auth_needs_no_auth_step(agent):
    """`authMethods: []` is what says the adapter uses the ambient credentials
    rather than demanding an API key."""
    assert agent.init_result.get("authMethods") == []


def test_rate_limit_metadata_is_present(agent):
    """The predictive signal for the runner cascade — and the positive proof
    this ran on a subscription (`five_hour` is the subscription limiter)."""
    reducer = UpdateReducer()
    agent.new_session()
    agent.prompt("Reply with the single word: ok").result(timeout=120)
    for update in agent.updates:
        reducer.apply(update)
    assert reducer.rate_limit, "no _claude/rateLimit meta on any usage_update"
    assert reducer.rate_limit.get("rateLimitType")


def test_cancel_stops_a_running_turn(agent):
    """`session/cancel` is the Escape equivalent — the interaction the web view
    needs to reach parity with emdash."""
    import threading
    agent.new_session()
    pending = agent.prompt(
        "Run `sleep 3 && echo one`, then `sleep 3 && echo two`, then `sleep 3 && echo three`, "
        "each as a separate Bash call."
    )
    threading.Timer(5.0, agent.cancel).start()
    result = pending.result(timeout=120)
    assert result["stopReason"] == "cancelled"


def test_steering_lands_mid_turn(agent):
    """A second prompt while the first is running — typing into emdash while it
    works. Proves the protocol carries the interaction, so the web view can."""
    import time
    reducer = UpdateReducer()
    agent.new_session()
    first = agent.prompt(
        "Run `sleep 2 && echo one`, then `sleep 2 && echo two`, then `sleep 2 && echo three`, "
        "each as a separate Bash call, narrating briefly between each."
    )
    time.sleep(4)
    second = agent.prompt("Stop what you are doing. Reply with only the word BANANA.")
    first.result(timeout=180)
    second.result(timeout=180)
    for update in agent.updates:
        reducer.apply(update)
    assert "BANANA" in reducer.assistant_text


def test_a_loaded_session_keeps_its_context_and_its_transcript(tmp_path):
    """`session/load` is the ACP form of `--resume`: same conversation, same
    transcript file, so the durable record stays one file per session."""
    import pathlib
    from canopy_transcript import encode_project_dir

    first = AcpAgent(cwd=tmp_path)
    first.start()
    session_id = first.new_session()
    first.prompt("Remember this codeword: ZUCCHINI. Reply with just: noted.").result(timeout=120)
    first.close()

    transcript = (pathlib.Path.home() / ".claude" / "projects"
                  / encode_project_dir(str(tmp_path)) / f"{session_id}.jsonl")
    size_before = transcript.stat().st_size

    updates = []
    second = AcpAgent(cwd=tmp_path, on_update=lambda sid, u: updates.append(u))
    second.start()
    second.load_session(session_id)
    second.prompt("What was the codeword? Reply with just that word.").result(timeout=120)
    second.close()

    reducer = UpdateReducer()
    for update in updates:
        reducer.apply(update)
    assert "ZUCCHINI" in reducer.assistant_text.upper()
    assert transcript.stat().st_size > size_before   # appended, not forked
