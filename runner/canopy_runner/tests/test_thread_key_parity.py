"""Both runners key an agent turn's session the same way.

The cloud runner (`runner/ec2/cloud_runner.py`) is one stdlib-only file seeded
whole onto a box, so it cannot import `execute._thread_key`; it carries its own
`_agent_thread_key`. This pins the two together. When they disagreed (until
2026-10-29), one Hal alarm thread re-dispatched four times kept one session on
the laptop (keyed by its Gmail thread) and opened four cold ones on
cloud-ec2-live (keyed `hal:<turn id>`).
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

from canopy_runner import execute

_CLOUD = pathlib.Path(__file__).resolve().parents[2] / "ec2" / "cloud_runner.py"


@pytest.fixture(scope="module")
def cloud_runner():
    spec = importlib.util.spec_from_file_location("cloud_runner_parity", _CLOUD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cloud_runner_parity"] = module
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        yield module
    finally:
        sys.modules.pop("cloud_runner_parity", None)


def _turn(**kw):
    d = {"id": "a72a5d39-3ae9-41ec-8aa4-00776b868b28", "agent_slug": "hal",
         "prompt": "/hal:turn", "origin_ref": {}}
    d.update(kw)
    return d


CONFINED = {"profile": "confined", "capability": {"name": "ask"}}


@pytest.mark.parametrize("turn", [
    _turn(origin_ref={"thread_id": "1a184f500cd30f72", "subject": "ALARM"}),  # an email
    _turn(origin_ref={"thread_key": "phone:jj:hal"}),
    _turn(origin_ref={"thread_key": "dispatch-hal-0fccbf2661bb", "thread_id": "x"}),
    _turn(origin_ref={}),                                                     # a cron fire
    _turn(origin_ref={"slot": "2026-10-29T09:00:00Z", "schedule_name": "Daily"}),
    _turn(origin_ref={"thread_id": "1a18"}, caller_context=CONFINED),
    _turn(origin_ref={}, caller_context=CONFINED),
], ids=["email", "phone", "dispatch-key", "keyless", "schedule", "confined-thread",
        "confined-keyless"])
def test_the_cloud_runner_keys_an_agent_turn_as_the_laptop_does(cloud_runner, turn):
    assert cloud_runner._agent_thread_key(turn) == execute._thread_key(turn)
