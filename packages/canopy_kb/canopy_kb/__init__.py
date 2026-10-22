"""A knowledge base: build a cited library from a curator's bundle, query it, and
describe it as MCP tools. Django-free and stdlib-only.

This package is the one engine: canopy-web serves it, and the canopy CLI's
`canopy kb build` / `canopy kb mcp` import it (the CLI no longer carries a copy).
"""
from canopy_kb import records
from canopy_kb.kb import KB, KBError, apply, build

__all__ = ["KB", "KBError", "apply", "build", "records"]
