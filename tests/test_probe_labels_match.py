"""The runner page reads each credential's probe by the label the BOX probes it
under. Two sources — `cloud_runner._BUNDLE_SLOTS` / `_login_label` on the box,
`PROBE_LABEL` / `loginProbeLabel` in RunnerCredentials.tsx — so they are pinned
equal here: a label that drifts on one side leaves its dot reading "set" for a
credential the box has proven dead.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TSX = ROOT / "frontend/src/components/supervisor/RunnerCredentials.tsx"


def _cloud_runner():
    spec = importlib.util.spec_from_file_location("cloud_runner_labels", ROOT / "runner/ec2/cloud_runner.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["cloud_runner_labels"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_slot_labels_match_the_box():
    src = TSX.read_text()
    block = re.search(r"export const PROBE_LABEL[^=]*=\s*\{(.*?)\}", src, re.S).group(1)
    page = dict(re.findall(r"(\w+):\s*'([^']+)'", block))
    box = {slot: label for label, _var, slot in _cloud_runner()._BUNDLE_SLOTS}
    assert page == box


def test_login_labels_match_the_box():
    src = TSX.read_text()
    fn = re.search(r"export function loginProbeLabel.*?\n\}", src, re.S).group(0)
    cr = _cloud_runner()
    assert f"'{cr._login_label('default')}'" in fn
    assert cr._login_label("a44d94c3") == f"{cr.BOX_LOGIN_LABEL}:a44d94c3"
    assert f"`{cr.BOX_LOGIN_LABEL}:${{key}}`" in fn
