"""A sensitive conversation's words (`Message.sealed`) are sealed to a key canopy-web
does not hold (`canopy_sessions.conversation_keys`), and the ciphertext has ONE way out:
the gate in `apps/harness/sensitivity.py` (`reads_content` → `readable`,
`sealed_call_for`), which hands it only to the conversation's own people, whose browsers
decrypt it. Anything else that reaches for `sealed` — a new review, a search, an export,
another AI's door — fails here, so a new reader is closed until it asks the gate.
Writers, the fork that copies a conversation whole, and the one-time re-seal of rows
from before keys were per conversation are named below.

docs/architecture/tenancy.md § Sensitive conversations.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
MESSAGE_IMPORTS = ("canopy_sessions.models import", "from .models import")

#: path -> why it may touch `sealed`.
ALLOWED = {
    "apps/harness/sensitivity.py": "THE gate: seal, reads_content, readable, sealed_call_for",
    "apps/canopy_sessions/models.py": "the field",
    "apps/canopy_sessions/send.py": "writes the person's words",
    "apps/canopy_sessions/transcript_ingest.py": "writes the transcript's and the ledger's rows",
    "apps/canopy_sessions/placement.py": "a fork copies the same conversation whole",
    "apps/canopy_sessions/serializers.py": "puts the gate's output (`readable_sealed`) on the "
                                           "socket's row",
    "apps/canopy_sessions/conversation_keys.py": "re-seals rows written under the field key "
                                                 "to their conversation's key (the migration), and "
                                                 "seals the rows an ordinary conversation kept when "
                                                 "it becomes sensitive (`seal_existing`)",
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


def test_canopy_web_never_opens_the_words():
    """No module that touches `sealed` decrypts anything, but the one-time re-seal of
    rows from before keys were per conversation: canopy-web writes a sensitive
    conversation and cannot read it back (spec 2026-10-23 §2)."""
    openers = [rel for rel in ALLOWED if rel != "apps/canopy_sessions/conversation_keys.py"
               and ("decrypt_secret" in (ROOT / rel).read_text() or "_open(" in (ROOT / rel).read_text())]
    assert not openers, f"decrypts a sensitive conversation's words: {openers}"
