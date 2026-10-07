"""The retired nouns cannot creep back into the API (and so the MCP tools)."""
import re

from apps.api.api import api

BANNED = re.compile(r"(^|[_/-])(items?|work[_-]?products?|commands?|decisions?)($|[_/-])")

#: Pre-existing names elsewhere in the API that use one of the words in its
#: ordinary sense, not as the retired task-system noun. Each needs a reason.
#: Paths and operation ids have no exemptions; these are all schema names.
ALLOWED: dict[str, str] = {
    "WalkthroughListItemOut": "walkthroughs: one row of a list, not a task-system item",
    "StoryboardListItemOut": "storyboards: one row of a list",
    "NarrationItemOut": "storyboards: one narration line of a storyboard",
    "ReviewListItemOut": "reviews: one row of a list",
    "NarrativeListItemOut": "DDD runs: one row of the narratives list",
    "SessionListItemOut": "session sharing: one row of a list",
    "ArcItemIn": "session sharing: a session placed in an arc",
    "ArcItemOut": "session sharing: a session placed in an arc",
    "ArcListItemOut": "session sharing: one row of the arcs list",
    "CapabilityItemOut": "system: one row of the capabilities list",
    "SlackCommandsOut": "slack: the Slack app's slash commands",
    "Decision": "DDD runs: an entry in a run's auditable decisions log",
    "GateDecisionIn": "agent runs: closing a gate on a run step",
    "TransferDecisionIn": "canopy sessions: approving or declining a session transfer",
}


def _hits():
    schema = api.get_openapi_schema()
    hits = [p for p in schema["paths"] if BANNED.search(p.lower())]
    ops = [op["operationId"] for p in schema["paths"].values() for op in p.values()
           if isinstance(op, dict) and "operationId" in op]
    hits += [o for o in ops if BANNED.search(o.lower())]
    hits += [n for n in schema.get("components", {}).get("schemas", {})
             if re.search(r"Item(In|Out)|WorkProduct|Command|Decision", n)]
    return hits


def test_no_retired_noun_in_paths_or_operations():
    assert [h for h in _hits() if h not in ALLOWED] == []


def test_allowed_list_has_no_stale_entries():
    # An exemption nothing needs any more is a hole waiting to be reused.
    hits = set(_hits())
    assert [name for name in ALLOWED if name not in hits] == []
