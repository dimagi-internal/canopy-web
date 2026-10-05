"""StripScriptName ASGI middleware — strips the /canopy prefix so the inner
Starlette (MCP at /api/mcp) + Django routers see unprefixed paths."""
import pytest

from config.asgi_prefix import SCOPE_KEY, StripScriptName


class _Recorder:
    """A trivial ASGI app that records the scope path it was called with."""
    def __init__(self):
        self.seen_path = None
        self.seen_root = None

    async def __call__(self, scope, receive, send):
        self.seen_path = scope.get("path")
        self.seen_root = scope.get("root_path")


async def _call(mw, path, scope_type="http"):
    rec = mw.app
    await mw({"type": scope_type, "path": path, "raw_path": path.encode()}, None, None)
    return rec.seen_path


@pytest.mark.asyncio
async def test_strips_prefix_from_http_path():
    mw = StripScriptName(_Recorder(), "/canopy")
    assert await _call(mw, "/canopy/api/me") == "/api/me"
    assert await _call(mw, "/canopy/api/mcp") == "/api/mcp"


@pytest.mark.asyncio
async def test_bare_prefix_becomes_root():
    mw = StripScriptName(_Recorder(), "/canopy")
    assert await _call(mw, "/canopy") == "/"
    assert await _call(mw, "/canopy/") == "/"


@pytest.mark.asyncio
async def test_non_matching_path_untouched():
    mw = StripScriptName(_Recorder(), "/canopy")
    # a path that doesn't start with the prefix is passed through verbatim
    assert await _call(mw, "/canopyfoo/x") == "/canopyfoo/x"
    assert await _call(mw, "/other") == "/other"


@pytest.mark.asyncio
async def test_strips_on_websocket_too():
    mw = StripScriptName(_Recorder(), "/canopy")
    assert await _call(mw, "/canopy/ws/sessions/abc", scope_type="websocket") == "/ws/sessions/abc"


@pytest.mark.asyncio
async def test_empty_prefix_is_noop():
    mw = StripScriptName(_Recorder(), "")
    assert await _call(mw, "/canopy/api/me") == "/canopy/api/me"


@pytest.mark.asyncio
async def test_a_stripped_scope_is_marked_and_an_unstripped_one_is_not():
    # apps/common/legacy_prefix.py tells the old address from the new one by
    # this mark — and it must not be root_path, which Starlette and Channels
    # both expect `path` to start with.
    seen = {}

    async def app(scope, receive, send):
        seen["mark"] = scope.get(SCOPE_KEY)
        seen["root"] = scope.get("root_path")

    mw = StripScriptName(app, "/canopy")
    await mw({"type": "http", "path": "/canopy/api/me", "raw_path": b"/canopy/api/me"}, None, None)
    assert seen == {"mark": "/canopy", "root": None}
    await mw({"type": "http", "path": "/api/me", "raw_path": b"/api/me"}, None, None)
    assert seen["mark"] is None
