"""A new route must not rename an existing MCP tool.

An operationId is a view function's own name, and it is also the route's MCP tool
name and its `generated.ts` key (`apps/api/api.py::CanopyNinjaAPI`). When two
modules share a view name, BOTH are qualified by their app — so adding
`def list_views` in a new app silently turns the saved task views' existing
`list_views` tool into `agents_tasks_list_views`, and every agent calling the old
name breaks. Nothing errors: the schema stays unique, `generated.ts` regenerates
cleanly, and the only trace is a few deleted lines in a large generated diff.

So the names shared today are pinned here. A new collision fails with the fix:
give the new view a name of its own.
"""
from __future__ import annotations

from apps.api.api import api

#: View names already shared between modules, each published under its qualified
#: operationId. Only ever shrinks: removing a collision is fine, adding one is not.
SHARED_TODAY = frozenset({
    "declare_page_actions", "declare_page_state", "disconnect_app", "get_run", "get_session",
    "list_connected_apps", "list_sessions", "list_turns", "resolve_page_action", "rotate_token", "send",
})


def test_no_new_view_name_is_shared_between_modules():
    api.get_openapi_schema()  # binds the routers the name rule reads
    new = sorted(api._shared_view_names() - SHARED_TODAY)
    assert not new, (
        f"view name(s) {new} now exist in more than one module, which renames the EXISTING route's "
        "MCP tool and generated.ts key to an app-qualified one. Rename the new view function "
        "(e.g. list_dynamic_views, not list_views)."
    )
