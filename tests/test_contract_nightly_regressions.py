"""Regressions for what the nightly contract fuzz (tests/contract/) found.

That suite only runs nightly against a live server, so each server error it
finds gets a plain test here that runs on every PR. One test per finding: a
malformed request is the caller's mistake and answers 4xx, never 500.
"""
from __future__ import annotations

import datetime as dt
import json
import typing
import uuid

import jsonschema
import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings

from apps.api.api import api
from apps.api.openapi_doc import NO_NUL_PATTERN
from apps.api.renderers import OrjsonRenderer
from apps.common.pg_datetime import OffsetSafeDatetimeDumper
from apps.common.schemas import TRIMMED_PATTERN
from apps.harness.models import Runner
from apps.harness.schemas.runners import RunnerIn
from apps.tokens.schemas import MAX_TTL_DAYS
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()

BIGGER_THAN_INT64 = 2**63


@pytest.fixture
def client(db):
    user = User.objects.create_user(username="fuzz", email="fuzz@dimagi.com", password="pw")
    c = Client()
    c.force_login(user)
    return c


def test_issue_with_an_overlong_confidence_is_a_422_not_a_500(client):
    # `confidence` is a 20-character column; the schema now says so.
    body = {"repo": "o/r", "number": 1, "title": "t", "confidence": "a" * 500}
    r = client.post("/api/issues/", data=json.dumps(body), content_type="application/json")
    assert r.status_code == 422


def test_issue_number_past_its_integer_column_is_a_422_not_a_500(client):
    body = {"repo": "o/r", "number": BIGGER_THAN_INT64, "title": "t"}
    r = client.post("/api/issues/", data=json.dumps(body), content_type="application/json")
    assert r.status_code == 422


def test_an_id_too_big_for_the_database_is_a_400_not_a_500(client):
    # Ninja's `int` is unbounded; the database is not. One handler covers every route.
    r = client.get(f"/api/events/?limit={BIGGER_THAN_INT64}")
    assert r.status_code in (200, 400), r.content
    r = client.delete(f"/api/workspaces/x/members/{BIGGER_THAN_INT64}/")
    assert r.status_code < 500, r.content


def test_noise_audit_with_a_non_uuid_session_is_a_422_not_a_500(client):
    r = client.get("/api/canopy-sessions/noise?session_id=null")
    assert r.status_code == 422


def test_issue_list_offset_past_int64_is_an_empty_page_not_a_500(client):
    r = client.get(f"/api/issues/?offset={BIGGER_THAN_INT64}&limit=100")
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_token_ttl_past_the_ceiling_is_a_422_not_a_500(client):
    r = client.post("/api/tokens/", data=json.dumps({"label": "x", "ttl_days": BIGGER_THAN_INT64}),
                    content_type="application/json")
    assert r.status_code == 422


def test_token_ttl_at_the_ceiling_still_mints(client):
    r =client.post("/api/tokens/", data=json.dumps({"label": "x", "ttl_days": MAX_TTL_DAYS}),
                    content_type="application/json")
    assert r.status_code == 201


def test_multipart_without_a_boundary_is_a_400_not_a_500(db):
    # The public upload route parses the body before anything else can refuse it.
    r = Client().post("/api/public-apps/ws/key/files", data=b"--x\r\n",
                      content_type="multipart/form-data")
    assert r.status_code == 400
    assert r["Content-Type"] == "application/problem+json"


@override_settings(REQUIRE_AUTH=True)
def test_unauthenticated_resolve_matches_its_declared_401_schema(db):
    """The login middleware's 401 must fit what the resolve route declares for 401."""
    r = Client().get("/api/agents/x/credentials/resolve")
    assert r.status_code == 401

    spec = api.get_openapi_schema()
    op = spec["paths"]["/api/agents/{slug}/credentials/resolve"]["get"]
    declared = op["responses"][401]["content"]["application/json"]["schema"]
    # Round-trip through JSON so the schema is what /api/openapi.json serves.
    components = json.loads(json.dumps(spec["components"]))
    jsonschema.validate(r.json(), {**declared, "components": components})


def test_a_task_raised_by_a_turn_that_does_not_exist_is_a_422_not_a_500(client):
    # A person filing (no agent) named an unknown turn: the FK insert used to 500.
    user = User.objects.get(username="fuzz")
    ws = Workspace.objects.create(slug="fuzz", display_name="Fuzz", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    body = {"title": "t", "workspace": "fuzz", "raised_by": str(uuid.uuid4())}
    r = client.post("/api/tasks/", data=json.dumps(body), content_type="application/json")
    assert r.status_code == 422, r.content


@pytest.mark.parametrize("query", ["priority=%C2%B3", "priority=1,%C2%B3", "milestone=%C2%B2"])
def test_a_superscript_digit_in_a_task_filter_is_not_a_500(client, query):
    # "³".isdigit() is True but int("³") raises: the count badge used to 500.
    user = User.objects.get(username="fuzz")
    ws = Workspace.objects.create(slug="fuzz", display_name="Fuzz", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    for path in ("/api/tasks/count/", "/api/tasks/"):
        r = client.get(f"{path}?{query}")
        assert r.status_code == 200, (path, r.content)


@override_settings(VAPID_PUBLIC_KEY="", VAPID_PRIVATE_KEY="")
def test_push_without_keys_says_so_with_a_503(client):
    # Unconfigured push is the deployment's state, not a fault: 503, as designed.
    # The nightly configures throwaway keys so it reaches the real path instead.
    assert client.get("/api/push/vapid-public-key").status_code == 503


@override_settings(VAPID_PUBLIC_KEY="pub", VAPID_PRIVATE_KEY="priv")
def test_push_with_keys_serves_the_public_key(client):
    r = client.get("/api/push/vapid-public-key")
    assert r.status_code == 200 and r.json()["public_key"] == "pub"


# --- round 4 (#2598): schema-valid requests the server refused or failed ------


def _member_of_fuzz():
    user = User.objects.get(username="fuzz")
    ws = Workspace.objects.create(slug="fuzz", display_name="Fuzz", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    return user


def test_a_utc_offset_postgres_cannot_parse_is_written_as_utc():
    # ISO 8601 and Python allow ±23:59; PostgreSQL reads ±15:59. Same instant, in UTC.
    when = dt.datetime(2026, 1, 1, 7, 21, tzinfo=dt.timezone(dt.timedelta(hours=17, minutes=10)))
    dumped = bytes(OffsetSafeDatetimeDumper(dt.datetime).dump(when)).decode()
    assert dumped == "2025-12-31 14:11:00+00:00"
    usual = when.replace(tzinfo=dt.timezone(dt.timedelta(hours=5)))
    assert bytes(OffsetSafeDatetimeDumper(dt.datetime).dump(usual)).decode().endswith("+05:00")


def test_an_integer_past_64_bits_in_stored_json_renders_instead_of_500():
    body = OrjsonRenderer().render(None, {"payload": {"n": 2**70}}, response_status=200)
    assert json.loads(body) == {"payload": {"n": 2**70}}


def test_a_century_of_since_minutes_is_every_row_not_an_overflow(client):
    for path in ("/api/events/", "/api/events/setting-changes", "/api/events/mcp-calls"):
        r = client.get(f"{path}?since_minutes={BIGGER_THAN_INT64}")
        assert r.status_code == 200, (path, r.content)


def test_a_task_filter_number_no_column_holds_matches_nothing(client):
    _member_of_fuzz()
    r = client.get(f"/api/tasks/?priority={BIGGER_THAN_INT64}&milestone={BIGGER_THAN_INT64}")
    assert r.status_code == 200 and r.json() == [], r.content


def test_an_upload_line_that_is_json_but_not_a_record_is_skipped(client):
    _member_of_fuzz()
    f = SimpleUploadedFile("t.jsonl", b'1\n"x"\n{"type": "user", "message": {"content": "hi"}}\n')
    r = client.post("/api/w/fuzz/sessions/upload", {"file": f})
    assert r.status_code == 201, r.content


def test_an_upload_record_of_the_wrong_shape_is_a_422_not_a_500(client):
    _member_of_fuzz()
    f = SimpleUploadedFile("t.jsonl", b'{"type": "assistant", "message": 5}\n')
    r = client.post("/api/sessions/upload", {"file": f})
    assert r.status_code == 422, r.content


def test_the_runner_kinds_in_the_schema_are_the_models():
    kinds = set(typing.get_args(RunnerIn.model_fields["kind"].annotation))
    assert kinds == {k for k, _ in Runner.KIND_CHOICES}


def test_request_text_is_documented_nul_free_and_refusals_as_problem_json():
    schema = json.loads(json.dumps(api.get_openapi_schema(), default=str))
    components = schema["components"]["schemas"]
    # A free-text request string says it holds no NUL; one with its own rule keeps it.
    assert components["EventIn"]["properties"]["source"]["pattern"] == NO_NUL_PATTERN
    # A trimmed (StrictModel) required string says it is not blank or padded.
    assert components["OriginIssueIn"]["properties"]["title"]["pattern"] == TRIMMED_PATTERN
    # A declared refusal is documented in the media type it is sent as.
    walkthrough = schema["paths"]["/api/walkthroughs/"]["post"]["responses"]
    assert list(walkthrough["422"]["content"]) == ["application/problem+json"]
    # An operation that takes free-form JSON declares the database's 400.
    assert "400" in schema["paths"]["/api/events/"]["post"]["responses"]
    # ...and one that takes none does not.
    assert "400" not in schema["paths"]["/api/tokens/"]["post"]["responses"]


@override_settings(VAPID_PUBLIC_KEY="pub", VAPID_PRIVATE_KEY="priv")
@pytest.mark.parametrize("endpoint", ["https://[unbalanced/x", "https://é\u008f.example/x"])
def test_a_push_endpoint_no_url_parser_or_resolver_reads_is_a_422_not_a_500(client, endpoint):
    body = {"endpoint": endpoint, "p256dh": "k", "auth": "a"}
    r = client.post("/api/push/subscribe", data=json.dumps(body), content_type="application/json")
    assert r.status_code == 422, r.content
