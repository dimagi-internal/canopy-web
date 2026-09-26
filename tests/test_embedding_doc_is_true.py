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

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "architecture" / "embedding-a-canopy-agent.md"


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
        ("create_app_credential", ["--name"]),
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
    assert "/w/<workspace>/settings/connected-apps" in doc

    router = (DOC.parent.parent.parent / "frontend" / "src" / "router.tsx").read_text()
    # The page is a SECTION of the workspace settings route now, so the URL the
    # doc gives is a parent plus a child segment — neither half alone proves the
    # page exists.
    assert "path: '/w/:workspace/settings'" in router, (
        "the doc sends a human to a page the router does not declare"
    )
    assert "path: 'connected-apps'" in router, (
        "the doc sends a human to a settings section the router does not declare"
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


# --- the 2026-09-16 page contract -------------------------------------------
#
# The doc's value is that it is PINNED: a host team reads it once and builds
# against it, so a claim that quietly stops being true is worse than no claim.
# These cover what §5, §5a and §7 now promise.


def test_the_documented_page_state_api_exists(doc):
    """§5 tells a host to call `setPageState`. It must be on the handle."""
    src = (REPO / "frontend/packages/canopy-widget/src/index.ts").read_text()

    assert "setPageState" in doc
    assert "setPageState(state: Record<string, unknown>): void" in src


def test_the_documented_selection_fields_are_the_ones_the_server_reads(doc):
    """§5 tells a host to send `visible_ids` + `backing_tool` + `resource`.

    `resource` is the one canopy keys invalidation on, so a doc naming a
    different spelling would produce pages that are never refreshed — and
    nothing at runtime would say so.
    """
    helper = (REPO / "frontend/src/widget/pageState.ts").read_text()
    invalidation = (REPO / "apps/canopy_sessions/invalidation.py").read_text()

    for field in ("visible_ids", "backing_tool", "resource"):
        assert field in doc, f"§5 no longer documents {field}"
        assert field in helper, f"describeSelection no longer emits {field}"
    assert "page_state__resource" in invalidation, "invalidation no longer keys on `resource`"


def test_the_documented_size_cap_is_the_real_one(doc):
    """§5 promises 8 KiB. A host sizes its payload against that number."""
    from apps.canopy_sessions import page_state

    assert page_state.MAX_STATE_BYTES == 8192
    assert "8 KiB" in doc


def test_the_documented_invalidation_hook_exists(doc):
    """§5a tells a host to pass `onInvalidate` to `canopy.init`.

    This used to assert the doc named `useResource` — a React hook that lives
    inside canopy-web's OWN frontend and is exported by no package. A host
    following the doc could not have called it, and this test pinned that. So
    it now checks the option against the widget a host actually loads: it is a
    declared option, and the widget really calls it when canopy says a
    resource moved.
    """
    widget = (REPO / "frontend/packages/canopy-widget/src/index.ts").read_text()

    assert "onInvalidate" in doc
    assert "onInvalidate?: (resource: string) => void" in widget
    assert "options.onInvalidate?.(" in widget
    # And the host-facing guide no longer offers the in-repo hook as its API.
    assert "useResource(" not in doc


def test_every_documented_widget_method_is_real(doc):
    """§4 lists what `canopy.init` returns. A method a host calls that does not
    exist fails in their browser, not here."""
    import re

    widget = (REPO / "frontend/packages/canopy-widget/src/index.ts").read_text()
    iface = widget[widget.index("export interface CanopyWidget {"):]
    iface = iface[: iface.index("\n}")]
    real = set(re.findall(r"^\s+(\w+)\(", iface, re.M))

    section = doc[doc.index("The returned object has"):]
    section = section[: section.index("## 5.")]
    documented = set(re.findall(r"`(\w+)\(\)`", section))

    assert documented, "the method list moved; point this test at it"
    assert documented <= real, f"documented but not on the widget: {sorted(documented - real)}"
    assert "setPageState" in documented, "the primary API is missing from the list"


def test_the_documented_agui_ingress_route_exists(doc):
    """§5 offers `run-input` as the one-call alternative."""
    from django.urls import resolve

    assert "run-input" in doc
    assert resolve("/api/canopy-sessions/00000000-0000-0000-0000-000000000000/run-input")


def test_the_doc_no_longer_teaches_dismissInsights_as_a_page_action(doc):
    """§7 uses it as the WORKED EXAMPLE of what not to do, so the name may
    appear — but never as something a host should register."""
    assert "widget.registerAction('dismissInsights'" not in doc
    assert not (REPO / "frontend/src/pages/insightsDismissAction.ts").exists()


def test_the_replacement_server_tool_is_actually_served(doc):
    """§7 says the mutation moved to `dismiss_insights`. Asserted against the
    MOUNTED server, not the module — `page_tools.py` had ten passing tests and
    no import."""
    import asyncio

    from apps.mcp.server import mcp

    assert "dismiss_insights" in doc
    assert "dismiss_insights" in {t.name for t in asyncio.run(mcp._list_tools())}


def test_the_documented_read_tool_is_actually_served(doc):
    """§5 says the agent re-reads the page with `current_page`."""
    import asyncio

    from apps.mcp.server import mcp

    assert "current_page" in doc
    assert "current_page" in {t.name for t in asyncio.run(mcp._list_tools())}


def test_the_doc_does_not_still_claim_context_is_a_snapshot(doc):
    """§9 listed "Context is a snapshot" as a known limit. It was fixed, and a
    limits list that names a solved problem sends host teams designing around
    something that no longer exists."""
    limits = doc[doc.index("## 9. Reference: known limits"):]
    assert "~~**Context is a snapshot.**~~" in limits, "the correction was dropped"


def test_the_documented_agui_socket_contract_is_the_real_one(doc):
    """The doc tells a host the query flag, the CUSTOM prefix and the metadata
    key a lossy frame rides under. Each is a literal in code a host cannot see,
    so each is pinned to that literal rather than trusted."""
    import json as _json

    from apps.canopy_sessions import agui
    from apps.canopy_sessions.consumers import AGUI_PROTOCOL

    assert f"?protocol={AGUI_PROTOCOL}" in doc
    assert f"`CUSTOM` events named `{agui.CUSTOM_PREFIX}<event>`" in doc
    assert f"`metadata.{agui.METADATA_KEY}.frame`" in doc

    # And the frames the doc names as carrying the original really do.
    for frame in (
        {"event": "session.state", "data": {"messages": []}},
        {"event": "chat.stream_error", "data": {"message_id": "m1", "detail": "x"}},
    ):
        [event] = [agui.encode(e) for e in agui.project(frame, thread_id="t")]
        assert event["metadata"][agui.METADATA_KEY]["frame"] == frame, _json.dumps(event)

    # "canopy-ui >= 0.9" is only true if the version that ships this says so.
    pkg = _json.loads((REPO / "frontend" / "packages" / "canopy-ui" / "package.json").read_text())
    major, minor = (int(x) for x in pkg["version"].split(".")[:2])
    assert (major, minor) >= (0, 9)


def test_the_documented_history_filters_are_real_parameters(doc):
    """§5b tells a host it can filter its history by `resource` and
    `page_path`. Those are query parameter NAMES a host cannot discover, so
    they are pinned to the route's signature rather than trusted."""
    import inspect

    from apps.canopy_sessions.api import list_sessions

    params = inspect.signature(list_sessions).parameters
    for name in ("resource", "page_path"):
        assert f"?{name}=" in doc, f"§5b no longer documents ?{name}="
        assert name in params, f"the doc offers ?{name}= but list_sessions has no such parameter"


def test_the_doc_states_the_host_boundary_on_history(doc):
    """The leak #823 closed — a delegated token listing another host's
    conversations — is a property a host team should be able to rely on, so
    the doc has to say it, and say the supplied value is ignored."""
    assert "on your host, and only yours" in doc
    assert "An `embed_app` you pass is ignored" in doc


def test_every_documented_theme_variable_and_part_is_real(doc):
    """§4's theming section tells a host to set `--canopy-*` variables and style
    `::part()`s. One the launcher does not read is a host's brand colour silently
    doing nothing — the exact failure theming exists to end."""
    import re

    chrome = (REPO / "frontend/packages/canopy-widget/src/chrome.ts").read_text()
    section = doc[doc.index("### Match it to your brand"):]
    section = section[: section.index("**Pick a mode:**")]

    documented_vars = set(re.findall(r"(--canopy-[a-z-]+)\s*:", section))
    read_vars = set(re.findall(r"var\((--canopy-[a-z-]+)", chrome))
    assert documented_vars, "the variable list moved; point this test at it"
    assert documented_vars <= read_vars, (
        f"documented but never read by the launcher: {sorted(documented_vars - read_vars)}"
    )

    documented_parts = set(re.findall(r"::part\((\w+)\)", section))
    real_parts = set(re.findall(r"setAttribute\('part', '(\w+)'\)", chrome))
    assert documented_parts == real_parts, (
        f"documented parts {sorted(documented_parts)} vs real {sorted(real_parts)}"
    )

    theme = (REPO / "frontend/packages/canopy-widget/src/theme.ts").read_text()
    iface = theme[theme.index("export interface WidgetTheme {"):]
    iface = iface[: iface.index("\n}")]
    real_fields = set(re.findall(r"^\s+(\w+)\?:", iface, re.M))
    # The RAW file here: the `doc` fixture collapses whitespace, which removes
    # the line starts this pattern anchors on.
    raw = DOC.read_text()
    raw = raw[raw.index("### Match it to your brand"):]
    block = raw[raw.index("theme: {"):]
    block = block[: block.index("},")]
    documented_fields = set(re.findall(r"^\s+(?://\s*)?(\w+):", block, re.M))
    assert documented_fields == real_fields, (
        f"theme fields documented {sorted(documented_fields)} vs real {sorted(real_fields)}"
    )


# --- host grant contract v1 (2026-09-26) ------------------------------------
#
# §8a hands a host team a wire contract they build against in another repo, so
# every name in it is pinned to the code that reads it.


CONTRACT = REPO / "docs" / "architecture" / "host-grant-contract.md"


@pytest.mark.parametrize("route", ["/oauth/client.json", "/oauth/jwks.json"])
def test_the_client_identity_routes_resolve_and_are_public(doc, route):
    """canopy's client_id IS the first URL; a host fetches both before it
    trusts anything, so they must resolve and must not sit behind a login."""
    from apps.common.middleware import _is_public

    assert route in doc and route in CONTRACT.read_text()
    resolve(route)
    assert _is_public(route), f"{route} must be reachable without a login"


def test_the_contract_is_published_beside_the_guide_and_linked(doc):
    assert CONTRACT.exists(), "docs/architecture/host-grant-contract.md is missing"
    assert "(host-grant-contract.md)" in doc


def test_the_documented_arrival_field_is_the_one_the_endpoint_reads(doc):
    """§8a tells a host to send `id_jag` beside `assertion`."""
    from apps.tokens.contact_api import ContactTokenIn, ContactTokenOut

    assert "`id_jag`" in doc
    assert "id_jag" in ContactTokenIn.model_fields
    assert "host_grant" in ContactTokenOut.model_fields


def test_the_documented_wire_constants_are_the_ones_canopy_uses(doc):
    """The grant type, the ID-JAG typ, the client-assertion type and the
    metadata document's fields are literals a host implements against."""
    from apps.tokens import client_identity, host_grants

    contract = CONTRACT.read_text()
    assert client_identity.JWT_BEARER_GRANT in doc and client_identity.JWT_BEARER_GRANT in contract
    assert host_grants.ID_JAG_TYP in doc and host_grants.ID_JAG_TYP in contract
    assert client_identity.CLIENT_ASSERTION_TYPE in contract
    meta = client_identity.client_metadata()
    for key in ("client_id", "client_name", "jwks_uri", "token_endpoint_auth_method",
                "grant_types", "dpop_bound_access_tokens"):
        assert f'"{key}"' in contract and key in meta
    assert meta["token_endpoint_auth_method"] == "private_key_jwt"
    assert "Canopy-Actor" in doc and "Canopy-Actor" in contract


def test_the_gateway_tools_the_doc_names_are_served():
    """§8a says the agent calls `site_tools` / `site_call`. Asserted against the
    MOUNTED server."""
    import asyncio

    from apps.mcp.server import mcp

    text = DOC.read_text()
    names = {t.name for t in asyncio.run(mcp._list_tools())}
    for tool in ("site_tools", "site_call"):
        assert tool in text and tool in names
    # The retired canopy-minted assertion is gone from the server and the doc's
    # instructions (it may be named once, as history).
    assert "act_on_behalf_of_caller" not in names
    assert "/api/tokens/on-behalf-of/jwks" not in text
