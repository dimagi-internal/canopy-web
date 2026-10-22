"""The runner page reads each credential's probe by the label the BOX probes it
under. Two sources — `cloud_runner._cascade_from_list` / `_login_label` on the
box, `probeLabel` in RunnerCredentials.tsx — so they are pinned
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


def test_list_labels_match_the_box():
    """`probeLabel` on the page, `_cascade_from_list` on the box."""
    src = TSX.read_text()
    fn = re.search(r"export function probeLabel.*?\n\}", src, re.S).group(0)
    cr = _cloud_runner()
    assert f"'{cr._login_label('default')}'" in fn
    assert cr._login_label("a44d94c3") == f"{cr.BOX_LOGIN_LABEL}:a44d94c3"
    assert f"`{cr.BOX_LOGIN_LABEL}:${{e.key}}`" in fn
    assert cr._LIST_KINDS["token"][0] == "token" and "'token'" in fn
    assert cr._LIST_KINDS["api_key"][0] == "api-key" and "'api-key'" in fn
    staged = cr._cascade_from({"cascade": [{"key": "t1", "kind": "token", "value": "x"},
                                           {"key": "k1", "kind": "api_key", "value": "y"}]})
    assert [c[0] for c in staged] == ["token:t1", "api-key:k1"]
