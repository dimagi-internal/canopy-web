"""`canopy_runner/inbox_rules.py` is a VERBATIM copy of canopy's
`src/orchestrator/inbox_rules.py` — the fleet's one inbound-email table (canopy#679).

The runner cannot import canopy, so the table is vendored rather than shared. This pin is
the drift tripwire: canopy's `tests/test_inbox_rules.py` pins the SAME hash, so editing
either copy fails that repo's suite until both pins move together. Do not edit the rules
here — edit them in canopy, copy the file over, and update both pins.
"""
import hashlib
from pathlib import Path

from canopy_runner import inbox_rules

#: Must equal canopy's tests/test_inbox_rules.py::VENDORED_SHA256.
CANOPY_SHA256 = "69a23cb125fd4b7b882248e40d2ce5d4a0477a72dc8facdb4f94897ff90b8b9a"


def test_vendored_table_is_byte_identical_to_canopys():
    digest = hashlib.sha256(Path(inbox_rules.__file__).read_bytes()).hexdigest()
    assert digest == CANOPY_SHA256, (
        "canopy_runner/inbox_rules.py no longer matches the pinned canopy copy. Edit the "
        "table in canopy (src/orchestrator/inbox_rules.py), copy it here verbatim, and "
        f"update this pin and canopy's VENDORED_SHA256 together (now {digest}).")


def test_vendored_table_is_stdlib_only():
    src = Path(inbox_rules.__file__).read_text()
    assert "from orchestrator" not in src and "import orchestrator" not in src
