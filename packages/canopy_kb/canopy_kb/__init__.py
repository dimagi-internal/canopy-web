"""A knowledge base: build a cited library from a curator's bundle, query it, and
describe it as MCP tools. Django-free and stdlib-only.

The engine (`kb.py`) is the same as the canopy CLI's `orchestrator/kb.py`; keep the
two in step until the CLI depends on this package instead of carrying its copy.
"""
from canopy_kb.kb import KB, KBError, build

__all__ = ["KB", "KBError", "build"]
