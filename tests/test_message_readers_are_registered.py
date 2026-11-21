"""Every module that reads a session's transcript rows is placed here, with why.

canopy's learning goes through the brain (`apps/brain`): what it may read, who agreed, and who sees the result.
Every OTHER way a server module touches `canopy_sessions.Message` is the chat itself, or the plumbing around it,
and is listed so that a new reader is a decision made in a PR, not an accident: a module that starts reading the
transcript fails this test until it is placed, and a learning read belongs in the brain (`brain.reading`), not in
a new module here. A listed module that no longer reads fails too, so the table cannot hide slack.

Pure `ast`, like the other structural guards."""
from __future__ import annotations

import ast

import pytest

from tests._source import ROOT

pytestmark = pytest.mark.guard

CHAT = "the chat itself: its ingest, ACL-gated reads, socket, send and lifecycle"
INGEST = "a runner's transcript ingest and the rows canopy keeps from it"
NOTIFY = "a notification or relay of the chat to the person (or channel) it already belongs to"
PLUMBING = "plumbing that moves, deletes, seals or annotates rows and reads no meaning from them"
SHARING = "the session's own person sharing it: their act, scrubbed on the way out"
BRAIN = "the brain's one learning read: eligible, scrubbed, recorded (`apps/brain/reading.py`)"
PRESENCE = "asks whether any row exists; reads no words"

READERS: dict[str, str] = {
    "apps/agents/credentials/secret_requests.py": PLUMBING,
    "apps/agents/move.py": PLUMBING,
    "apps/brain/lineage.py": ("a learning run's own conversation, for the links its turn reported making (URLs only, "
                              "stored as ids and links, never a word: `lineage.record_reported`)"),
    "apps/brain/reading.py": BRAIN,
    "apps/canopy_sessions/access.py": CHAT,
    "apps/canopy_sessions/activity.py": INGEST,
    "apps/canopy_sessions/api_read.py": CHAT,
    "apps/canopy_sessions/consumers.py": CHAT,
    "apps/canopy_sessions/content_deletion.py": PLUMBING,
    "apps/canopy_sessions/conversation_keys.py": PLUMBING,
    "apps/canopy_sessions/exports.py": SHARING,
    "apps/canopy_sessions/maintenance.py": PLUMBING,
    "apps/canopy_sessions/placement.py": CHAT,
    "apps/canopy_sessions/private_attachments.py": PLUMBING,
    "apps/canopy_sessions/send.py": CHAT,
    "apps/canopy_sessions/serializers.py": CHAT,
    "apps/canopy_sessions/services.py": CHAT,
    "apps/canopy_sessions/transcript_ingest.py": INGEST,
    "apps/canopy_sessions/transcript_read.py": CHAT,
    "apps/contacts/session_review_asks.py": PRESENCE,
    "apps/harness/api_session_sync.py": INGEST,
    "apps/harness/sensitivity.py": PLUMBING,
    "apps/harness/session_sync.py": INGEST,
    "apps/host_grants/management/commands/view_read_probe.py": "an operator's probe command, run by hand",
    "apps/mcp_apps_host/mcp_apps_views.py": PLUMBING,
    "apps/push/services.py": NOTIFY,
    "apps/retention/services.py": PLUMBING,
    "apps/session_sharing/api.py": SHARING,
    "apps/slack/relay.py": NOTIFY,
}


#: The core read's functions (`transcript_read`): a module outside the chat app calling one reads the words.
CORE_READS = frozenset({"read", "view_rows", "view_messages"})


def _reads_transcript_rows(tree: ast.Module, rel: str) -> bool:
    """Imports `Message` from the chat app (by absolute or, inside it, relative import), calls the core read
    (`transcript_read.read` and its views), or a module of the chat app's session surface uses a session's
    `.messages` rows."""
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in CORE_READS
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "transcript_read"):
            return True
        if isinstance(node, ast.ImportFrom) and any(a.name == "Message" for a in node.names):
            if (node.module or "").startswith("apps.canopy_sessions") or (
                    node.level and rel.startswith("apps/canopy_sessions/")):
                return True
    if rel.startswith(("apps/canopy_sessions/", "apps/contacts/session_review", "apps/session_sharing/")):
        return any(isinstance(n, ast.Attribute) and n.attr == "messages" for n in ast.walk(tree))
    return False


def readers() -> set[str]:
    found = set()
    for path in sorted((ROOT / "apps").rglob("*.py")):
        rel = str(path.relative_to(ROOT))
        if "/migrations/" in rel or "/tests/" in rel:
            continue
        if _reads_transcript_rows(ast.parse(path.read_text()), rel):
            found.add(rel)
    return found


def test_every_module_that_reads_transcript_rows_is_placed():
    found = readers()
    assert found - set(READERS) == set(), (
        "these read a session's transcript rows and are not placed in READERS: place each with the reason it "
        "may, or move a learning read into apps/brain")
    assert set(READERS) - found == set(), "these no longer read transcript rows: remove them from READERS"


def test_only_the_brain_reads_for_learning_and_it_is_one_module():
    assert [m for m, why in READERS.items() if why == BRAIN] == ["apps/brain/reading.py"]
