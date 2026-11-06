"""The words a person reads say read mode, connected site and profile, never door, ask or capability.

`door` and `ask` (as the name of read mode) were a second vocabulary for the same things, and the
agent sense of `capability` (one entry of an agent's published document) is now a connected site, read
mode or a profile (`docs/superpowers/specs/2026-11-05-turn-context-design.md` § 2, § 9). The frontend's
half is `frontend/src/turnContextWords.test.ts`.

Guarded here, the text canopy-web writes for a person or an AI to read:
* every string the server builds outside docstrings (refusals, `why`, errors, labels, notices): a
  literal with a space in it is prose;
* the OpenAPI document, whose descriptions are also the MCP tools' descriptions;
* API field names: none may be new with these words in it.

What may still say them, and nothing else:
* the stored and wire names of section 9, each behind its one constant (`READ_MODE_STAMP`, the
  document's `capabilities` key, `Turn.capability`, `AgentGrant.capability`, `AdmissionRule.capability`,
  `use:<name>`), and the field names that existed before this guard;
* leased machine capabilities and a runner's capabilities (`MACHINE_CAPABILITY_MODULES`), a different
  concept that keeps its name.
"""
from __future__ import annotations

import ast
import re

import pytest

from tests._source import ROOT

pytestmark = pytest.mark.guard

DOOR = re.compile(r"\bdoors?\b", re.I)
# `ask` as a NAME: read mode's old name, a published entry, an agent's `:ask` command. Not the verb, and
# not "an ask" meaning a request (a task's, an investigation's, a review's), which is another noun.
ASK = re.compile(r"(?i)\b(?:use|capability|sensitive):ask\b|['\"]ask['\"]|/[\w-]+:ask\b"
                 r"|\bask (?:capability|capabilities|profile|door|mode|entry|line)\b"
                 r"|\b(?:published|stamped|own|confined|default) (?:`ask`|ask\b)(?! (?:for|to|about)\b)")
CAPABILITY = re.compile(r"\bcapabilit(?:y|ies)\b", re.I)

#: Section 9's stored and wire names, written as a person sees them quoted. Removed before checking.
WIRE_NAMES = re.compile(r"`(?:capabilities|capability|Turn\.capability|AgentGrant\.capability|"
                        r"AdmissionRule\.capability|capability:<?[\w-]*>?)`")

#: Phrases that name a leased machine capability wherever they appear. Removed before checking.
MACHINE_PHRASES = re.compile(r"(?i)\b(?:machine|cloud|leased) capabilit(?:y|ies)\b|\bcapability (?:grants?|leases?|sweep)\b")

#: Leased machine capabilities and a runner's own capabilities: another concept, which keeps its name.
MACHINE_CAPABILITY_MODULES = (
    "apps/capabilities/",
    "apps/harness/models/runner.py",
    "apps/harness/schemas/runners.py",
    "apps/harness/api_runners_fleet.py",
    "apps/harness/machine_pairing.py",
    "apps/harness/api_runners.py",
    "apps/harness/routing_machine.py",
    "apps/harness/runner_requirements.py",
    "apps/harness/api_project_opt_in.py",
    "apps/slack/status.py",
    "apps/system/",            # the plugin catalog's own kind names (`/api/system`)
    "apps/storyboards/",       # a share link's capability: what the link grants
    "apps/workspace_apps/",    # an app action's capability: the permission it needs
    "apps/agents/move.py",     # moving the machine capabilities' rows
    "apps/agents/health.py",   # an agent's leased machine capabilities
)

#: OpenAPI paths of the machine-capability routes above (their text is about leased machines, a runner's own
#: capabilities, the plugin catalog, a share link's or an app action's capability).
MACHINE_PATHS = re.compile(r"/capabilit|/system/|/storyboards|/narratives|/workspace-apps|"
                           r"/workspaces/[^/]+/apps|/app/|/harness/runners/\{runner_id\}$")

#: API field names with these words that existed before this guard. None may be added: a new field
#: says `connected_site`, `read_mode` or `profile`. (Machine capabilities and stored names keep theirs.)
EXISTING_FIELDS = frozenset({
    # leased machine capabilities and runners
    "AgentCapabilitiesIn.capabilities", "AgentCapabilitiesOut.capabilities", "CapabilityGrantIn.capability",
    "CapabilityGrantOut.capability", "CapabilityLeaseIn.capability", "CapabilityLeaseOut.capability",
    "CapabilityOrphansOut.capability", "CapabilitySlotsOut.capability", "CapabilitySpendRow.capability",
    "AgentHealthOut.capabilities", "PairingRunnerIn.capabilities", "RunnerPairByRoleIn.capabilities",
    "RunnerOut.capabilities", "RunnerIn.capabilities", "RunnerCapabilitiesIn.capabilities",
    # the A2A protocol's own field, and a storyboard share link's grant
    "A2AAgentCardOut.capabilities", "StoryboardListItemOut.capability", "StoryboardOut.capability",
    "StoryboardIn.capability", "StoryboardPatchIn.capability", "NarrativeReadOut.capability",
    # stored: `AdmissionRule.capability` (section 9)
    "AdmissionRuleOut.capability", "AdmissionRuleIn.capability", "AdmissionRulePatch.capability",
    # `ask` meaning a request (a task filter, a meeting's or an investigation's request, a review request)
    "DecisionOut.review_ask", "DecisionIn.review_ask", "MeetingIn.ask", "MeetingFollowUpIn.ask",
    "FailureInvestigationOut.ask", "HandOffInvestigationIn.ask", "SessionReviewOfferOut.ask",
    "AgentTaskDetailOut.ask_body", "AgentTaskDetailOut.ask_closed_at", "AgentTaskDetailOut.ask_kind",
    "AgentTaskDetailOut.ask_open", "AgentTaskIn.ask_body", "AgentTaskIn.ask_kind", "AgentTaskOut.ask_body",
    "AgentTaskOut.ask_closed_at", "AgentTaskOut.ask_kind", "AgentTaskOut.ask_open", "TaskCreateIn.ask_body",
    "TaskCreateIn.ask_kind", "TaskDetailOut.ask_body", "TaskDetailOut.ask_closed_at", "TaskDetailOut.ask_kind",
    "TaskDetailOut.ask_open", "TriageOut.ask_kind",
})


#: Agent-sense fields that were renamed after this guard was written: the roster's and the topology's
#: `capabilities`, now `profiles` (read mode or connected sites, by name). Never to come back, under
#: any schema.
RETIRED_FIELDS = frozenset({"AgentAccessRowOut.capabilities", "AgentEdgeOut.capabilities"})


def _hits(text: str, *, capability: bool = True) -> list[str]:
    text = MACHINE_PHRASES.sub("", WIRE_NAMES.sub("", text))
    pats = (DOOR, ASK, CAPABILITY) if capability else (DOOR, ASK)
    return [m.group(0) for p in pats for m in p.finditer(text)]


def _prose(path) -> list[tuple[int, str]]:
    """Every string literal in a module that is prose (holds a space), docstrings left out."""
    tree = ast.parse(path.read_text())
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docs.add(id(first.value))
    return [(n.lineno, n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs
            and " " in n.value.strip()]


def _server_modules():
    for path in sorted((ROOT / "apps").rglob("*.py")):
        rel = str(path.relative_to(ROOT))
        if "/migrations/" in rel or "/tests/" in rel:
            continue
        yield rel, path


def server_text_hits() -> list[str]:
    out = []
    for rel, path in _server_modules():
        machine = rel.startswith(MACHINE_CAPABILITY_MODULES)
        for line, text in _prose(path):
            if found := _hits(text, capability=not machine):
                out.append(f"{rel}:{line}: {found} in {text.strip()[:90]!r}")
    return out


def _openapi() -> dict:
    from apps.api.api import api  # noqa: PLC0415 — builds the whole API

    return api.get_openapi_schema()


def openapi_text_hits(doc: dict) -> list[str]:
    out = []

    def walk(node, where: str, machine: bool):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{where}/{key}", machine)
        elif isinstance(node, list):
            for value in node:
                walk(value, where, machine)
        elif isinstance(node, str) and where.rsplit("/", 1)[-1] in ("description", "summary"):
            if found := _hits(node, capability=not machine):
                out.append(f"{where}: {found} in {node.strip()[:90]!r}")

    for path, item in doc.get("paths", {}).items():
        walk(item, path, bool(MACHINE_PATHS.search(path)))
    for name, schema in doc.get("components", {}).get("schemas", {}).items():
        walk(schema, name, "Capabilit" in name or name.startswith(("Runner", "Storyboard", "Narrative")))
    return out


def field_names(doc: dict) -> set[str]:
    out = set()
    for name, schema in doc.get("components", {}).get("schemas", {}).items():
        for field in (schema.get("properties") or {}):
            if re.search(r"door|(?:^|_)asks?(?:_|$)|capabilit", field, re.I):
                out.add(f"{name}.{field}")
    return out


def test_server_text_says_read_mode_and_connected_site():
    hits = server_text_hits()
    assert not hits, "say read mode, connected site or profile (spec § 2, § 9):\n" + "\n".join(hits)


@pytest.mark.django_db
def test_api_text_says_read_mode_and_connected_site():
    hits = openapi_text_hits(_openapi())
    assert not hits, ("an API description (and so an MCP tool's) says door, ask or capability:\n"
                      + "\n".join(hits))


@pytest.mark.django_db
def test_no_new_api_field_is_named_after_them():
    new = field_names(_openapi()) - EXISTING_FIELDS
    assert not new, f"new API fields named door/ask/capability (say connected_site, read_mode, profile): {sorted(new)}"


@pytest.mark.django_db
def test_the_renamed_agent_sense_fields_stay_gone():
    doc = _openapi()
    schemas = doc["components"]["schemas"]
    assert not RETIRED_FIELDS & EXISTING_FIELDS
    for field in RETIRED_FIELDS:
        name, _old = field.split(".")
        assert set(schemas[name]["properties"]) >= {"profiles"} and _old not in schemas[name]["properties"], field
    # Nor does any schema bring the word back as a field, even one not checked by name here.
    assert not {f for f in field_names(doc) if f.endswith(".capabilities")} - EXISTING_FIELDS


def test_the_topology_basis_says_profiles():
    from apps.agents import topology  # noqa: PLC0415

    bases = (topology.OWNER, topology.WS_OWNER, topology.ADMIN, topology.EDITOR, topology.NO_INTERFACE,
             topology.PROFILES, topology.NOTHING, topology.NOT_MEMBER, topology.NO_LOGIN)
    assert topology.PROFILES == "profiles"
    assert not [b for b in bases if CAPABILITY.search(b) or DOOR.search(b)], bases


def test_the_guard_catches_each_word():
    assert _hits("this line opens one named capability") == ["capability"]
    assert _hits("use the 'connect' door") == ["door"]
    assert _hits("Read mode and doors") == ["doors"]
    assert _hits("confined to the caller capability 'ask'") == ["'ask'", "capability"]
    assert _hits("a `use:ask` line") == ["use:ask"]
    assert _hits("started by /hal:ask") == ["/hal:ask"]
    assert _hits("the agent's published ask") == ["published ask"]
    assert _hits("its confined `ask` path") == ["confined `ask`"]
    # The verb, a stored name in backticks, and machine capabilities (when allowed) are not hits.
    assert _hits("ask its owner to set it") == []
    assert _hits("Ask a coach to review this one session") == []
    assert _hits("The ask on the card: approve it") == []      # a request, another noun
    assert _hits("the document's `capabilities` key") == []
    assert _hits("a leased capability", capability=False) == []
    assert field_names({"components": {"schemas": {"X": {"properties": {
        "doors": {}, "door_sites": {}, "capability": {}, "review_ask": {}, "connected_site": {}, "task": {}}}}}}) \
        == {"X.doors", "X.door_sites", "X.capability", "X.review_ask"}
