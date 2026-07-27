"""The laptop runner's transcript writer, end to end against the real server code.

The retained raw transcript is what cost and structure are re-derived from
(spec 2026-07-26-run-execution-convergence), so the only property that matters is
that what the runner ships comes back byte-identical. That spans two packages —
the runner's byte-chunker and the server's gzip-member append — and neither side's
unit tests can see the seam, which is why this test imports both.
"""
import sys

import pytest

sys.path.insert(0, "packages/canopy_runner")
from django.contrib.auth.models import User
from apps.harness import services as hs
from apps.harness.models import Runner, Turn
from apps.agents.models import Agent
from apps.workspaces.models import Workspace
from canopy_runner import chat_bridge as cb

pytestmark = pytest.mark.django_db


def test_round_trip_is_byte_identical():
    """The laptop runner's chunker + the server's append must reproduce the
    original JSONL exactly — that is the whole premise of retaining raw bytes."""
    u = User.objects.create_user("a", "a@d.com", "p")
    ws = Workspace.objects.create(slug="w", display_name="W", created_by=u)
    ag = Agent.objects.create(slug="ace", name="Ace", workspace=ws)
    t = Turn.objects.create(agent=ag, prompt="x", idempotency_key="k1")

    lines = [
        '{"type":"assistant","message":{"usage":{"input_tokens":5,"output_tokens":7},"model":"claude-opus-5"}}',
        '{"type":"user","message":{"content":[{"type":"tool_result","content":"' + "z" * 3000 + '"}]}}',
        '{"type":"result","total_cost_usd":0.0123}',
    ]
    n = 0
    # Cap above the largest single line but below the total, so this splits
    # into real batches instead of tripping the oversized-line path.
    for batch in cb.chunk_raw_lines(lines, max_bytes=3100):
        hs.append_transcript(t, batch, batch_id=f"{t.id}:{n}")
        n += 1
    assert n > 1, "the fixture must actually exercise multi-batch"
    assert hs.read_transcript(t).decode() == "\n".join(lines)


def test_a_lost_ack_retry_does_not_double_append():
    u = User.objects.create_user("b", "b@d.com", "p")
    ws = Workspace.objects.create(slug="w2", display_name="W2", created_by=u)
    ag = Agent.objects.create(slug="ada", name="Ada", workspace=ws)
    t = Turn.objects.create(agent=ag, prompt="x", idempotency_key="k2")
    hs.append_transcript(t, ['{"a":1}'], batch_id=f"{t.id}:0")
    hs.append_transcript(t, ['{"a":1}'], batch_id=f"{t.id}:0")   # the retry
    assert hs.read_transcript(t).decode() == '{"a":1}'
