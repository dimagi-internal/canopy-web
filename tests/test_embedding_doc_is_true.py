"""The embedding guide's setup instructions have to stay true.

`docs/architecture/embedding-a-canopy-agent.md` is a handoff document: someone
integrating a host product follows it without being able to check it against the
code. Every command, flag and URL in it was verified by hand once, which is
exactly the kind of verification that rots — a renamed flag leaves a document
that reads perfectly and does not work.

This is the same argument `frontend/src/guide/coverage.test.ts` makes for the
in-app guide ("a test can read this … prose in another repo can do neither"),
applied to the one doc that is consumed by people outside this repo's review
loop.

Deliberately checks only the CHECKABLE claims — commands, flags, routes, field
names. The prose and the reasoning are not testable and are not tested; this
stops the document lying about mechanics, not about judgement.
"""

from pathlib import Path

import pytest
from django.core.management import get_commands, load_command_class
from django.urls import resolve
from django.urls.exceptions import Resolver404

DOC = Path(__file__).resolve().parent.parent / "docs" / "architecture" / "embedding-a-canopy-agent.md"


@pytest.fixture(scope="module")
def doc() -> str:
    """The doc with whitespace collapsed.

    Phrases in a markdown file are line-wrapped, so a naive substring match
    fails on any claim long enough to span a wrap — which is every claim worth
    asserting. Commands and routes contain no spaces, so collapsing is safe for
    them too.
    """
    assert DOC.exists(), f"the embedding guide is missing: {DOC}"
    return " ".join(DOC.read_text().split())


def test_the_doc_exists_and_is_indexed_in_claude_md(doc):
    """An unindexed doc is an unread doc: CLAUDE.md is the index loaded into
    every session, and it is where api-surface.md and mcp-surface.md live."""
    claude = (DOC.parent.parent.parent / "CLAUDE.md").read_text()
    assert "embedding-a-canopy-agent.md" in claude


@pytest.mark.parametrize(
    "command,flags",
    [
        ("create_app_credential", ["--name", "--domains"]),
        ("grant_app_frame_origin", ["--name", "--origin", "--list"]),
        ("grant_app_agent", ["--name", "--agent", "--list"]),
    ],
)
def test_documented_commands_exist_with_the_documented_flags(doc, command, flags):
    """A renamed flag turns the setup section into instructions that fail."""
    assert command in doc, f"{command} is no longer mentioned — update this test or the doc"
    assert command in get_commands(), f"the doc names a command that does not exist: {command}"

    parser = load_command_class("apps.tokens", command).create_parser("manage.py", command)
    available = {
        option for action in parser._actions for option in action.option_strings
    }
    missing = [f for f in flags if f not in available]
    assert not missing, f"{command} no longer accepts {missing}, but the doc uses them"


@pytest.mark.parametrize(
    "route",
    [
        "/api/auth/contact-token",
        "/api/embed/agents",
        "/embed/chat",
        "/embed/widget.js",
    ],
)
def test_documented_routes_resolve(doc, route):
    """Every URL a host is told to call must actually be served."""
    assert route in doc, f"{route} is no longer in the doc — update this test or the doc"
    try:
        resolve(route)
    except Resolver404:  # pragma: no cover - the assertion is the message
        pytest.fail(f"the doc tells a host to call {route}, which resolves to nothing")


def test_the_documented_page_exists_and_the_admin_is_no_longer_the_path(doc):
    """The doc sends a human to a product page, not the Django admin.

    `resolve()` cannot check the page itself — every unknown path falls through
    to the SPA catch-all, so it would pass for a route that does not exist. The
    frontend router is the authority for that, and the API it calls is the
    authority for the rest.
    """
    assert "/w/<workspace>/connected-apps" in doc

    router = (DOC.parent.parent.parent / "frontend" / "src" / "router.tsx").read_text()
    assert "/w/:workspace/connected-apps" in router, (
        "the doc sends a human to a page the router does not declare"
    )

    # The surface behind it must be real, and mounted where the client expects.
    resolve("/api/workspaces/w1/connected-apps")

    # The old door must not still be advertised: it is read-only now, so
    # following the doc there would be a dead end with no explanation.
    assert "/admin/tokens/appcredential/add/" not in doc


def test_the_doc_matches_whether_actions_actually_work(doc):
    """The doc must not describe a capability the code does not have, or warn
    about a gap that has been closed.

    This started life as "the gap is still disclosed" and fired exactly once,
    the moment the frame gained `runAction` — which is what it was for. It now
    pins the other direction: actions work, so the doc says so, and if the
    frame ever stops calling them the doc has to stop claiming it.
    """
    frame_app = DOC.parent.parent.parent / "frontend" / "src" / "embed" / "EmbedApp.tsx"
    agent_can_act = frame_app.exists() and "runAction" in frame_app.read_text()
    claims_it_works = "Agent actions work" in doc
    warns_it_does_not = "cannot yet call a host action" in doc

    if agent_can_act:
        assert claims_it_works, "the frame calls runAction but the doc does not say actions work"
        assert not warns_it_does_not, "the gap is closed but the doc still warns about it"
    else:
        assert warns_it_does_not, "the frame cannot act, and the doc must say so"


def test_every_internal_link_points_at_a_real_heading():
    """The doc routes the reader by anchor ("see §7"), so a renumbered section
    silently sends them nowhere. Cheap to check, invisible when broken."""
    import re

    text = DOC.read_text()
    headings = re.findall(r"^#{2,3} (.+)$", text, re.M)

    def slug(heading: str) -> str:
        s = re.sub(r"[^\w\s-]", "", heading.lower())
        return re.sub(r"\s+", "-", s.strip())

    available = {slug(h) for h in headings}
    used = set(re.findall(r"\]\(#([^)]+)\)", text))
    assert not (used - available), f"broken anchors: {sorted(used - available)}"


def test_the_step_numbering_matches_the_checklist():
    """The doc's promise is "do these in order". Sections that drift out of
    step with the checklist break that, and it happened once while writing it.
    """
    import re

    text = DOC.read_text()
    # Checklist rows link to their own section: | [3](#3-add-one-backend-endpoint) | …
    checklist = re.findall(r"\|\s*\[(\d+)\]\(#(\d+)-[^)]*\)", text)
    for shown, anchored in checklist:
        assert shown == anchored, (
            f"checklist row {shown} links to section {anchored} — the numbering drifted"
        )
    assert len(checklist) == 6, f"expected 6 steps, found {len(checklist)}"


def test_no_reserved_name_or_setting_survives_in_the_doc(doc):
    """§11 used to tell you to name the app after `EMBED_SELF_APP`.

    The setting is gone — canopy's own panel is a column an owner ticks, not a
    name that has to match a deployment value. A doc still naming the setting
    would send someone looking for something that no longer exists, and the
    failure it used to cause was silent on both sides.
    """
    from django.conf import settings

    assert "EMBED_SELF_APP" not in doc
    assert not hasattr(settings, "EMBED_SELF_APP")

    cfn = (DOC.parent.parent.parent / "deploy" / "aws" / "canopy-web.cfn.yaml").read_text()
    assert "EMBED_SELF_APP" not in cfn


def test_the_self_embed_section_asks_for_nothing(doc):
    """§11 is one tick on the ordinary form, and that is the claim worth pinning.

    Every input it would otherwise collect is a fact about canopy rather than a
    decision, and each fails closed and silently when got wrong. If this section
    ever grows a form of its own again, canopy has become a special case again.
    """
    section = " ".join(
        DOC.read_text().split("## 11. Reference: canopy embedding its own pages")[-1].split()
    )
    assert "Show this panel on canopy's own pages" in section
    assert "not a special one" in section
    # A field table is exactly what this section exists not to have.
    assert "| **Name** |" not in section
