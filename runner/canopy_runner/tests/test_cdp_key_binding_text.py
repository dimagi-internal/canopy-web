"""A chat message that is just "?", "!" or "/" must land in the session as text.

Turn e4fb721c (2026-10-20, failure investigation 2): Jonathan's canopy-web chat
message was a single "?". Claude Code reads one keystroke into an empty prompt as a
key binding — "?" opens the shortcuts panel and types nothing — so the message
never reached the session and each recovery retype only toggled the panel.
Measured on a raw pty against claude v2.1.304: "?", "!" and "/" alone are bindings
("/" then Enter would run the first slash command); with a leading space each is
plain text.

`typed_text.mjs` holds the rule; these tests drive it through node, plus a static
guard that the open-send `submit` types through it.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

CDP = Path(__file__).resolve().parents[1] / "canopy_runner" / "cdp"
MODULE = CDP / "typed_text.mjs"
SIDECAR = CDP / "emdash_control.mjs"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def as_typed(body):
    script = (
        f"const m = await import({json.dumps(MODULE.as_uri())});"
        f"process.stdout.write(JSON.stringify(m.asTyped({json.dumps(body)})));"
    )
    out = subprocess.run(["node", "--input-type=module", "-e", script],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@needs_node
@pytest.mark.parametrize("char", ["?", "!", "/"])
def test_a_lone_key_binding_is_typed_after_a_space(char):
    assert as_typed(char) == " " + char


@needs_node
def test_surrounding_whitespace_does_not_hide_it():
    assert as_typed(" ?\n") == " ?"


@needs_node
@pytest.mark.parametrize("body", ["/ace:turn", "!ls", "?? what", "why?", "#", "hello", ""])
def test_everything_else_is_typed_exactly_as_sent(body):
    """A slash command and shell mode are meant as such; only a bare binding changes."""
    assert as_typed(body) == body


def _submit_src() -> str:
    src = SIDECAR.read_text(encoding="utf-8")
    start = src.index("const submit = async")
    return src[start:src.index("if (clearFirst)", start)]


def test_open_send_submit_types_through_as_typed():
    assert "insertText(asTyped(body))" in _submit_src()
    assert "import { asTyped } from './typed_text.mjs';" in SIDECAR.read_text(encoding="utf-8")


def test_the_recheck_still_compares_the_original_body():
    """The composer then shows " ?"; squash() drops the space, so the swallowed-Enter
    recheck still recognises it as ours."""
    body = _submit_src()
    assert "stillOurs(body, after.typed)" in body
