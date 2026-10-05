"""The runner unit must survive an OOM kill of one turn's process.

2026-10-05, cloud-ec2-1 (t3.medium, no swap): the kernel OOM-killed a turn's
`node` ACP adapter at 04:02 and a `node` + `claude` at 12:02. systemd's default
OOMPolicy=stop then stopped the WHOLE unit each time, so one turn's memory spike
took every in-flight turn with it. These pin the template's two defences; the
claim-side memory gate is covered in test_cloud_runner.py.
"""
from __future__ import annotations

import pathlib
import re

TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "runner.cfn.yaml"


def _unit() -> str:
    text = TEMPLATE.read_text()
    m = re.search(r"ExecStart=/usr/bin/python3 /opt/canopy-runner/cloud_runner\.py(.*?)\[Install\]",
                  text, re.S)
    assert m, "canopy-runner unit not found in the template"
    return m.group(1)


def test_an_oom_killed_child_does_not_stop_the_runner():
    assert re.search(r"^\s*OOMPolicy=continue\s*$", _unit(), re.M), (
        "without OOMPolicy=continue systemd stops the whole runner when the kernel "
        "kills any one turn's process, losing every in-flight turn"
    )


def test_the_box_gets_swap():
    text = TEMPLATE.read_text()
    assert "mkswap /swapfile" in text and "swapon /swapfile" in text
    assert "/swapfile none swap sw 0 0" in text, "swap must survive a reboot"
