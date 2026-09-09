"""The box's node must be new enough for the ACP adapter it is asked to run.

@agentclientprotocol/claude-agent-acp declares `engines: {node: >=22}` from
0.63.0. On an older node, `npm i -g` does not fail — it resolves DOWN to the
newest version whose engines still match. Measured 2026-09-09 on cloud-ec2-1:
node 20.20.2 got adapter 0.49.0 while the registry's latest was 0.75.1, and the
ACP executor then ran with no steering support, because steering landed after
0.49.0. Every layer reported success; only the missing capability in
`initialize` showed it.

That is the shape worth pinning: a silent downgrade, not an error.
"""
from __future__ import annotations

import pathlib
import re

TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "runner.cfn.yaml"

#: The floor the adapter's own `engines` field demands.
MIN_NODE_MAJOR = 22


def test_the_nodesource_channel_is_new_enough_for_the_adapter():
    setups = re.findall(r"deb\.nodesource\.com/setup_(\d+)\.x", TEMPLATE.read_text())
    assert setups, "no NodeSource setup line found — did node installation move?"
    for major in setups:
        assert int(major) >= MIN_NODE_MAJOR, (
            f"node {major} installs claude-agent-acp <0.63.0 by silent npm "
            f"downgrade, which has no steering support; the adapter needs "
            f"node >={MIN_NODE_MAJOR}"
        )


def test_the_reason_is_written_down_next_to_the_pin():
    """A bare version bump gets 'tidied' back to whatever looks current. The
    constraint is another package's engines field, so it has to be legible here."""
    text = TEMPLATE.read_text()
    idx = text.index("deb.nodesource.com/setup_")
    preamble = text[max(0, idx - 900):idx]
    assert "engines" in preamble and "claude-agent-acp" in preamble, (
        "the node pin must say WHY it is pinned — it is the ACP adapter's "
        "engines constraint, not a general preference"
    )
