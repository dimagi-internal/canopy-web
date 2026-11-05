"""Every route the canopy CLI calls still exists here.

canopy (the CLI and plugin every agent runs) calls ~160 routes on this API, and its own
tests drive them through a fake transport — so a route removed or renamed here kept
passing there until a real call 404'd in an agent's turn. `PATCH /api/agents/{slug}/
device-binding` was removed while `canopy vault binding` still sent it.

`tests/fixtures/canopy_cli_routes.json` is canopy's own list of what it sends
(`orchestrator.canopy_web_routes routes`, read from canopy's source). This test checks it
against the schema this branch builds, so the PR that removes a route canopy calls is
the one that fails. Refresh the list with `uv run python scripts/refresh_canopy_cli_routes.py`.

Removing a route on purpose: change canopy first (or in step), refresh the list, then
merge this. canopy checks the other direction against its vendored copy of our schema.

Vendored rather than fetched from canopy's main at CI time: a fetch would make every
PR here fail the moment canopy's main starts calling a route this repo has not shipped
yet (or whenever GitHub is unreachable from the merge queue) — a failure no PR here
caused. A vendored list fails only the PR whose diff removes a route.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "canopy_cli_routes.json"
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _norm(path: str) -> str:
    return re.sub(r"\{[^{}]*\}", "{}", path)


def _served() -> dict:
    from apps.api.api import api

    out: dict = {}
    for path, ops in api.get_openapi_schema()["paths"].items():
        for method in ops:
            if method.upper() in METHODS:
                out.setdefault(method.upper(), set()).add(_norm(path))
    return out


def _matches(call: str, route: str) -> bool:
    # canopy writes a value it only knows at run time as `{}`, which fills any one
    # segment; a literal canopy sends never counts as filling a route parameter.
    a, b = call.split("/"), route.split("/")
    return len(a) == len(b) and all(x == y or x == "{}" for x, y in zip(a, b))


def _is_served(method: str, path: str, served: dict) -> bool:
    routes = served.get(method, set())
    forms = [path]
    if method == "GET" and not path.endswith("/"):
        forms.append(path + "/")   # APPEND_SLASH answers a GET with a 301 canopy follows
    return any(f in routes or any(_matches(f, r) for r in routes) for f in forms)


def test_every_route_canopy_calls_is_served():
    data = json.loads(FIXTURE.read_text())
    known = {(a["method"], a["path"]) for a in data.get("allowed_missing", [])}
    served = _served()
    gone = [c for c in data["calls"]
            if (c["method"], c["path"]) not in known
            and not _is_served(c["method"], c["path"], served)]
    assert not gone, (
        "This change removes or renames API routes the canopy CLI still calls "
        f"(canopy {data.get('source', {}).get('commit', '?')[:12]}):\n"
        + "\n".join(f"  {c['method']} {c['path']}  <- canopy {', '.join(c['where'])}" for c in gone)
        + "\nKeep the route, or change canopy first and refresh the list "
          "(scripts/refresh_canopy_cli_routes.py).")


def test_the_list_is_canopys_extractor_output():
    data = json.loads(FIXTURE.read_text())
    assert len(data["calls"]) >= 100
    assert all(c["method"] in METHODS and c["path"].startswith("/api/") for c in data["calls"])
