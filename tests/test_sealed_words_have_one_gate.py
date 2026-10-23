"""A sensitive conversation's words (`Message.sealed`) have ONE reader: the gate in
`apps/harness/sensitivity.py` (`reads_content` → `unseal`). Anything else that reaches
for them — a new review, a search, an export, another AI's door — fails here, so a new
reader is closed until it asks the gate. Writers, the fork that copies a conversation
whole, and the View lookup that derives which host tool a call reached are named below.

docs/architecture/tenancy.md § Sensitive conversations.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
MESSAGE_IMPORTS = ("canopy_sessions.models import", "from .models import")

#: path -> why it may touch `sealed`.
ALLOWED = {
    "apps/harness/sensitivity.py": "THE gate: seal, reads_content, unseal",
    "apps/canopy_sessions/models.py": "the field",
    "apps/canopy_sessions/send.py": "writes the person's words",
    "apps/canopy_sessions/transcript_ingest.py": "writes the transcript's and the ledger's rows",
    "apps/canopy_sessions/placement.py": "a fork copies the same conversation whole",
    "apps/mcp_apps_host/mcp_apps_views.py": "derives which View a call has; its arguments "
                                            "reach a viewer only behind reads_content",
}


def _touches_sealed(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "sealed":
            lines.append(node.lineno)
        elif isinstance(node, ast.Name) and node.id == "sealed":
            lines.append(node.lineno)
        elif isinstance(node, ast.keyword) and node.arg and (
                node.arg == "sealed" or node.arg.startswith("sealed__")):
            lines.append(node.value.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and (
                node.value == "sealed" or node.value.startswith("sealed__")):
            lines.append(node.lineno)
    return lines


def test_only_the_gate_reads_a_sensitive_conversations_words():
    offenders = []
    for path in sorted((ROOT / "apps").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if "/migrations/" in rel or "/tests/" in rel or rel in ALLOWED:
            continue
        source = path.read_text()
        # Only modules that deal in chat Messages: `sealed` means other things elsewhere
        # (a vault value sealed to its readers).
        if "Message" not in source or not any(m in source for m in MESSAGE_IMPORTS):
            continue
        offenders += [f"{rel}:{line}" for line in _touches_sealed(ast.parse(source))]
    assert not offenders, (
        "read a sensitive conversation's words through sensitivity.readable / reads_content, "
        f"never Message.sealed directly: {offenders}")


def test_the_allowlist_is_current():
    for rel in ALLOWED:
        assert _touches_sealed(ast.parse((ROOT / rel).read_text())), f"{rel} no longer touches sealed"
