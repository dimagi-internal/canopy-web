"""No route lets a member of workspace A read or change workspace B by naming B's object.

The cross-tenant bugs found so far all had one shape: a body reference, path id or list
filter naming another workspace's object, sent by someone who is a member only of their
own, and a route that checked the caller's workspace but not the object's. The other
tenancy guards check invariants (guests, personal projects, the sole authorizer); this
one tries the attack, on every operation in the real OpenAPI schema, so a route added
next year is swept without anyone remembering to list it.

The world (`tests/fixtures/two_tenant_world.py`) is two mirrored tenants in different
organisations. Alice owns A and nothing else. For each operation the sweep sends:

- the CONTROL: every reference parameter (path, query, body) names A's object; and
- one VARIANT per reference parameter: that one parameter names B's twin.

Each must leave B untouched — no row of B, and no row anywhere pointing at B, created,
changed or deleted — and must not answer with B's words (the canary every B row
carries). A 403/404/422 passes; so does a 2xx that ignored the reference. Each request
runs in a savepoint that is rolled back, so variants never see each other.

An operation that is cross-workspace on purpose is listed in `CROSS_WORKSPACE_BY_DESIGN`
with why. `test_the_sweep_reaches_the_routes` keeps the sweep honest: a harness whose
controls all 404 would pass while testing nothing.
"""
from __future__ import annotations

import contextlib
import os
import re
from urllib.parse import urlencode

import pytest
from django.db import connection, transaction
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.test.utils import CaptureQueriesContext

from tests.fixtures import two_tenant_world as tw

pytestmark = [pytest.mark.guard, pytest.mark.django_db]

#: operationId -> why it may reach B. Keep each reason specific enough to re-check.
CROSS_WORKSPACE_BY_DESIGN: dict[str, str] = {
    # Cross-workspace by current design, decision pending: TODOS.md § "Dispatch and origin
    # issues across workspaces". Take these out when that decision lands.
    "upsert_issue": "issues surface: decision pending (TODOS.md)",
    "list_issues": "issues surface: decision pending (TODOS.md)",
    "get_issue": "issues surface: decision pending (TODOS.md)",
    "delete_issue": "issues surface: decision pending (TODOS.md)",
    # A bearer link: the share token IS the grant, to anyone holding it, member or not
    # (`apps/session_sharing/api.py`). Naming a token is not naming B's object by id.
    "public_share_view": "link share: the token is the grant, by design",
}

#: (operationId, variant) -> why that one cross-tenant reference may write a row labelled B.
CROSS_WORKSPACE_VARIANTS: dict[tuple[str, str], str] = {
    # The person searches their OWN HCP entries, filtered by the workspace they name; the
    # PersonAuditEvent it appends is their own log (every reader filters by `person`), so the
    # B label is the caller's words about themselves, not a write into B.
    ("hcp_v2_queryContext", "query:workspace"): "the caller's own audit log, labelled by their filter",
}

#: Set to a file path to log every request's status (how to see what the sweep reaches).
_REPORT = os.environ.get("TWO_TENANT_REPORT", "")


_WRITE = re.compile(r'\s*(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)\s+"?(\w+)"?', re.I)


class _Rollback(Exception):
    pass


def _client(user) -> Client:
    c = Client(raise_request_exception=False)
    c.force_login(user)
    return c


def _body(resp) -> str:
    if getattr(resp, "streaming", False):
        return b"".join(resp.streaming_content).decode("utf-8", "replace")
    return resp.content.decode("utf-8", "replace")


def _send(client, method, url, query, body, files):
    if method == "get":
        return client.get(url, query)
    full = f"{url}?{urlencode(query, doseq=True)}" if query else url
    if files is not None:
        data = {k: SimpleUploadedFile(*v) if isinstance(v, tuple) else v for k, v in files.items()}
        return getattr(client, method)(full, data)
    return getattr(client, method)(full, body if body is not None else {}, content_type="application/json")


def _writes_b_scoped_table(queries) -> bool:
    tables = tw.b_scoped_tables()
    return any((m := _WRITE.match(q["sql"])) and m.group(1) in tables for q in queries)


def _try(client, world, method, url, query, body, files):
    """Send one request inside a savepoint that is rolled back; return (status, body text,
    B's fingerprint as the request left it — None when it wrote no table a B row can live
    in). The fingerprint is most of the sweep's cost, and most variants are refusals that
    write nothing but the caller's own session row."""
    out = {}
    with contextlib.suppress(_Rollback):
        with transaction.atomic():
            with CaptureQueriesContext(connection) as queries:
                resp = _send(client, method, url, query, body, files)
            out["status"], out["text"], out["after"] = resp.status_code, _body(resp), None
            if _writes_b_scoped_table(queries.captured_queries):
                try:
                    out["after"] = tw.b_snapshot(world)
                except Exception as exc:  # a broken transaction: the request left no readable state
                    out["after"] = {"<unreadable after request>": type(exc).__name__}
            raise _Rollback
    return out["status"], out["text"], out["after"]


def _ops():
    return [pytest.param(m, p, op, id=op["operationId"]) for m, p, op in tw.operations()
            if op["operationId"] not in CROSS_WORKSPACE_BY_DESIGN]


@pytest.fixture(autouse=True)
def _no_step_up(monkeypatch):
    """A passkey step-up is not a tenant gate; without this, every secret route stops at 403."""
    from apps.passkeys import stepup
    monkeypatch.setattr(stepup, "require_recent_step_up", lambda request, *a, **k: None)


@pytest.fixture(autouse=True)
def _drive_in_memory(monkeypatch, settings):
    """Uploads (walkthroughs, narrative sources) store to an in-memory Drive, so they get past
    storage to the references they carry."""
    from apps.walkthroughs import storage
    from tests.fixtures.fake_drive import FakeDriveClient
    drive = FakeDriveClient()
    monkeypatch.setattr(storage, "get_drive_client", lambda: drive)
    settings.CANOPY_DRIVE_ROOT_FOLDER_ID = drive.root_id


@pytest.fixture()
def world():
    return tw.make_world()


@pytest.mark.parametrize("method,path,op", _ops())
def test_a_reference_to_b_reaches_nothing_of_b(world, method, path, op):
    comps = tw.schema()["components"]["schemas"]
    client = _client(world["alice"])
    *_, refs = tw.request_parts(method, path, op, comps, world, {})
    before = None  # taken once, after a rollback, and only if some variant wrote near B
    problems = []
    variants = [(None, {})]
    for ref in dict.fromkeys(refs):
        variants.append((ref, {ref: "b"}))
        if ref.startswith("body") and len(refs) > 1:
            variants.append((f"{ref} alone", {ref: "b", tw.OTHERS: tw.OMIT}))
    for variant, sides in variants:
        url, query, body, files, _ = tw.request_parts(method, path, op, comps, world, sides)
        status, text, after = _try(client, world, method, url, query, body, files)
        changed = []
        if after is not None:
            # Every variant is rolled back, so the state now IS the state it started from.
            before = before or tw.b_snapshot(world)
            changed = tw.changed(before, after)
        if _REPORT:
            with open(_REPORT, "a") as fh:
                detail = text[:300].replace("\n", " ") if status in (400, 404, 409, 422) else ""
                fh.write(f"{op['operationId']}\t{variant or 'control'}\t{status}\t{method} {url}\t{body or files}\t{detail}\n")
        label = f"{method.upper()} {url} {query or ''} {body or ''} [{status}] ({variant or 'control'})"
        if tw.CANARY in text:
            problems.append(f"{label}: answered with B's words")
        if changed and (op["operationId"], variant) not in CROSS_WORKSPACE_VARIANTS:
            problems.append(f"{label}: changed B: {changed}")
    assert not problems, "A member of A reached B:\n" + "\n".join(problems)


# ---- the sweep is reaching something --------------------------------------------------

#: The routes the cross-tenant bugs so far went through, and their neighbours: each one's
#: CONTROL (A's own objects) must succeed, or its variants test nothing.
MUST_REACH = {
    # tasks: project / parent references
    "create_task", "create_tasks", "patch_task", "patch_task_by_key", "bulk_tasks", "act_on_task_by_key",
    # session sharing and arcs
    "session_sharing_list_sessions", "session_sharing_get_session", "patch_session", "upload_session",
    "list_arcs", "get_arc", "create_arc", "patch_arc",
    # narratives, runs, walkthroughs (free-text run_id / narrative_slug)
    "list_narratives", "get_narrative", "set_narrative_visibility", "runs_get_run", "get_run_release",
    "list_walkthroughs", "get_walkthrough", "patch_walkthrough", "create_review", "list_reviews", "get_review",
    "list_narrative_sources", "delete_run", "delete_narrative", "upload_walkthrough", "upload_narrative_source",
    # the runner protocol
    "resolve_session", "record_session", "post_session_stream",
    # the activity feed
    "list_timeline",
}

#: How many controls must succeed in all. Raise it when the world grows; never lower it to
#: make a red build green — find out which route stopped answering instead.
MIN_REACHED = 450


def test_the_sweep_reaches_the_routes(world):
    comps = tw.schema()["components"]["schemas"]
    client = _client(world["alice"])
    reached, missed = set(), {}
    for method, path, op in tw.operations():
        url, query, body, files, _ = tw.request_parts(method, path, op, comps, world, {})
        with contextlib.suppress(_Rollback):
            with transaction.atomic():
                status = _send(client, method, url, query, body, files).status_code
                raise _Rollback
        if status < 300:
            reached.add(op["operationId"])
        else:
            missed[op["operationId"]] = status
    not_reached = {op: missed.get(op) for op in MUST_REACH - reached}
    assert not not_reached, f"these controls no longer succeed, so their variants test nothing: {not_reached}"
    assert len(reached) >= MIN_REACHED, f"only {len(reached)} controls succeed (floor {MIN_REACHED})"
