"""The canopy-web FastMCP server instance.

Auth model — per-user **Personal Access Token** (the house pattern, matching
connect-labs):

  * `CanopyPATVerifier` resolves a per-user PAT to the Django user; tools run
    AS that user. It replaces the old single shared `CANOPY_MCP_BEARER`.

The MCP surface is intended only for people who can already log into
canopy-web — and PAT enforces exactly that, because minting a PAT *requires*
logging in (the `canopy-web-pat-mint` browser flow). So no separate OAuth
flow is needed to gate access: logging in is how you get a token. (This also
matches connect-labs, which is PAT-only.)

Tools are registered as a side effect of importing `apps.mcp.tools`.

Beyond those static tools, `PageActionProvider` contributes the actions
declared by whatever pages the CALLER currently has open — computed per
request, because they differ per user and vanish when a tab closes. See
`apps/mcp/page_tools.py`.

The module exposes:
  * `mcp`            — the FastMCP instance (auth attached)
  * `build_http_app()` — builds the Streamable-HTTP ASGI app (called
    once from config/asgi.py at mount time)
"""
from __future__ import annotations

import logging

from fastmcp import FastMCP

from .auth import CanopyPATVerifier

logger = logging.getLogger(__name__)

mcp = FastMCP("canopy-web", auth=CanopyPATVerifier())

# Registering tools is a side effect of importing the tools package.
from . import tools  # noqa: E402,F401
from .page_tools import PageActionProvider  # noqa: E402

# Dynamic tools: the actions of the pages the caller has open right now.
# Registered AFTER the static tools deliberately — FastMCP resolves static
# components ahead of providers, so a page can never capture a canopy tool
# name even if the `page_` prefix were dropped.
mcp.add_provider(PageActionProvider())


def build_http_app():
    """Build the Streamable-HTTP ASGI app for mounting at /api/mcp/.

    `path="/"` because config/asgi.py mounts this app under the
    /api/mcp prefix; the MCP endpoint then lives at /api/mcp/.
    """
    return mcp.http_app(path="/", transport="streamable-http")
