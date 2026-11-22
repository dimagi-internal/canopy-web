"""Bundle pages: an app page made of static files (apps/workspace_apps/bundles.py) — the manifest,
the upload, the sandboxed serving and its CSP, and the overlay's limits on them."""
from __future__ import annotations

import base64
import copy
from datetime import timedelta

import pytest
from django.test import Client, RequestFactory, override_settings
from django.utils import timezone

from apps.workspace_apps import bundles
from apps.workspace_apps import manifest as mf
from apps.workspace_apps import overlay as ov
from apps.workspace_apps.models import AppBundle, AppBundleBlob, AppInstallation, AppRoleGrant
from tests.fixtures.workspace_apps import J, _client

pytestmark = pytest.mark.django_db

DEV_ID = "lkbpnpoflndilnhefmhjpmdibdcopcfo"
HTML = "<!doctype html><html><head><title>Kanban</title></head><body><div id=b></div>" \
       "<script src=\"../shared/app.js\"></script></body></html>"
FILES = [
    {"path": "kanban/kanban.html", "text": HTML},
    {"path": "sidepanel/sidepanel.html", "text": "<html><body>side</body></html>"},
    {"path": "shared/app.js", "text": "canopy.ready.then(({app}) => app.query('note'))"},
    {"path": "shared/logo.png", "content_base64": base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()},
]
APP = {
    "key": "sales", "name": "Sales",
    "collections": {"note": {"fields": {"body": {"type": "text"}}}},
    "pages": [
        {"slug": "sidepanel", "title": "Side panel", "kind": "bundle", "entry": "sidepanel/sidepanel.html"},
        {"slug": "kanban", "title": "Kanban", "kind": "bundle", "entry": "kanban/kanban.html"},
        {"slug": "notes", "title": "Notes", "code": "function View(){return <Card>n</Card>}"},
    ],
}


def _install(world, m=None, files=FILES, user="admin"):
    body = {"manifest": m or APP}
    if files is not None:
        body["files"] = files
    return _client(world[user]).post("/api/workspace-apps/dimagi/", body, content_type=J)


def _installed(world):
    r = _install(world)
    assert r.status_code == 201, r.content
    return r.json()


def _page(out, slug):
    return next(p for p in out["pages"] if p["slug"] == slug)


def _origins() -> list[str]:
    return bundles.host_origins(RequestFactory().get("/"))


def _directives(csp: str) -> dict[str, str]:
    return {d.split(" ", 1)[0]: d.split(" ", 1)[1] if " " in d else "" for d in csp.split("; ")}


# --- the manifest -------------------------------------------------------------------------------


def test_install_with_files_gives_each_bundle_page_its_url(world):
    out = _installed(world)
    kanban = _page(out, "kanban")
    m = AppInstallation.objects.get(key="sales").manifest
    assert kanban["kind"] == "bundle" and kanban["entry"] == "kanban/kanban.html" and kanban["code"] == ""
    assert kanban["bundle_url"] == f"/app-bundles/{m['bundle']['id']}/kanban/kanban.html"
    assert m["bundle"]["files"] == sorted(f["path"] for f in FILES)
    assert _page(out, "notes")["kind"] == "jsx" and _page(out, "notes")["bundle_url"] == ""


def test_an_app_without_bundle_pages_keeps_its_shape():
    plain = copy.deepcopy(APP)
    plain["pages"] = plain["pages"][2:]
    assert "bundle" not in mf.validate(plain)


def test_the_bundle_is_the_servers_not_the_authors(world):
    """A hand-written `bundle` (another app's id, say) is dropped: with no files, the pages have none."""
    other = _installed(world)
    bundle_id = AppInstallation.objects.get(key="sales").manifest["bundle"]["id"]
    forged = {**copy.deepcopy(APP), "key": "copy",
              "bundle": {"id": bundle_id, "size": 1, "files": sorted(f["path"] for f in FILES)}}
    r = _install(world, forged, files=None)
    assert r.status_code == 422 and "needs the app's files" in r.json()["detail"]
    assert other  # the first install is untouched


@pytest.mark.parametrize("change, why", [
    ({"entry": "shared/app.js"}, "must be an .html file"),
    ({"entry": "missing.html"}, "not one of the bundle's files"),
    ({"entry": "../kanban/kanban.html"}, "no empty, '.' or '..' part"),
    ({"audience": "public", "role": "public"}, "a bundle page is a member page"),
    ({"code": "function View(){return null}"}, "has no code, slots or sources"),
    ({"slots": ["side"]}, "has no code, slots or sources"),
    ({"kind": "iframe"}, "must be 'jsx'"),
])
def test_a_bad_bundle_page_is_refused_naming_why(world, change, why):
    m = copy.deepcopy(APP)
    m["pages"][1] = {**m["pages"][1], **change}
    r = _install(world, m)
    assert r.status_code == 422 and why in r.json()["detail"], r.content


def test_a_jsx_page_has_no_entry(world):
    m = copy.deepcopy(APP)
    m["pages"][2]["entry"] = "kanban/kanban.html"
    r = _install(world, m)
    assert r.status_code == 422 and "only a bundle page" in r.json()["detail"]


def test_a_store_template_takes_no_files(world):
    r = _client(world["admin"]).post("/api/workspace-apps/dimagi/", {"template": "hiring", "files": FILES}, content_type=J)
    assert r.status_code == 422 and "your own `manifest`" in r.json()["detail"]


def test_installing_needs_the_integrations_permission(world):
    assert _install(world, user="editor").status_code == 403


# --- paths and limits ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "../etc/passwd.js", "/abs.js", "a//b.js", "a/./b.js", ".hidden.js", "a/.git/x.js", "a\\b.js",
    "run.exe", "noext", "a/" * 13 + "x.js", "x" * 201 + ".js", "%2e%2e/x.js", "a b.js", "",
])
def test_a_bad_path_is_refused(path):
    with pytest.raises(ValueError):
        bundles.check_path("p", path)


@pytest.mark.parametrize("path", ["index.html", "kanban/kanban.html", "lib/@scope/x-y_z.min.js", "fonts/Inter.woff2"])
def test_a_good_path_is_kept(path):
    assert bundles.check_path("p", path) == path


def test_two_paths_differing_only_in_case_are_one_file(world):
    r = _install(world, files=[*FILES, {"path": "Shared/App.js", "text": "x"}])
    assert r.status_code == 422 and "given twice" in r.json()["detail"]


def test_a_file_over_the_limit_is_refused(world, monkeypatch):
    monkeypatch.setattr(bundles, "MAX_FILE_BYTES", 10)
    r = _install(world)
    assert r.status_code == 422 and "is over" in r.json()["detail"]


def test_a_bundle_over_the_limit_is_refused_and_stores_nothing(world, monkeypatch):
    monkeypatch.setattr(bundles, "MAX_BUNDLE_BYTES", 100)
    r = _install(world)
    assert r.status_code == 422 and "the bundle is" in r.json()["detail"]
    assert not AppBundle.objects.exists() and not AppBundleBlob.objects.exists()


def test_each_file_gives_exactly_one_kind_of_content(world):
    r = _install(world, files=[{"path": "kanban/kanban.html", "text": "x", "content_base64": "eA=="}])
    assert r.status_code == 422 and "exactly one" in r.json()["detail"]
    r = _install(world, files=[{"path": "kanban/kanban.html", "content_base64": "not base64!"}])
    assert r.status_code == 422 and "not base64" in r.json()["detail"]


def test_a_bundle_is_content_addressed():
    a = bundles.build(copy.deepcopy(FILES))
    assert bundles.build(copy.deepcopy(FILES))["id"] == a["id"]
    changed = copy.deepcopy(FILES)
    changed[2]["text"] += ";"
    b = bundles.build(changed)
    assert b["id"] != a["id"]
    # Unchanged files are stored once, shared by both bundles.
    assert AppBundleBlob.objects.count() == len(FILES) + 1


# --- uploading in batches -----------------------------------------------------------------------


def test_files_sent_ahead_are_named_by_hash(world):
    r = _client(world["admin"]).post("/api/workspace-app-bundles/dimagi/blobs", {"files": FILES[:2]}, content_type=J)
    assert r.status_code == 201, r.content
    sent = {b["path"]: b["sha256"] for b in r.json()}
    files = [{"path": p, "sha256": sha} for p, sha in sent.items()] + FILES[2:]
    assert _install(world, files=files).status_code == 201


def test_an_unknown_hash_is_refused(world):
    r = _install(world, files=[{"path": "kanban/kanban.html", "sha256": "0" * 64}, *FILES[1:]])
    assert r.status_code == 422 and "no uploaded file" in r.json()["detail"]


def test_uploading_needs_a_manager_or_an_apps_maintainer(world):
    body = {"files": FILES[:1]}
    assert _client(world["viewer"]).post("/api/workspace-app-bundles/dimagi/blobs", body, content_type=J).status_code == 403
    assert _client(world["outsider"]).post("/api/workspace-app-bundles/dimagi/blobs", body, content_type=J).status_code == 404
    _installed(world)
    AppRoleGrant.objects.create(installation=AppInstallation.objects.get(key="sales"), user=world["viewer"],
                                role="maintainer", granted_by=world["admin"])
    assert _client(world["viewer"]).post("/api/workspace-app-bundles/dimagi/blobs", body, content_type=J).status_code == 201


def test_an_unused_upload_is_swept_and_a_used_one_kept(world):
    _installed(world)
    stray = bundles.store_blobs([{"path": "x.js", "text": "stray"}])[0]["sha256"]
    AppBundleBlob.objects.update(created_at=timezone.now() - timedelta(days=2))
    assert bundles.sweep_unused_blobs() == 1
    assert not AppBundleBlob.objects.filter(sha256=stray).exists()
    assert AppBundleBlob.objects.count() == len(FILES)


# --- serving ------------------------------------------------------------------------------------


def test_the_entry_is_served_sandboxed_with_canopys_client_first(world):
    url = _page(_installed(world), "kanban")["bundle_url"]
    r = Client().get(url)
    assert r.status_code == 200 and r["Content-Type"] == "text/html; charset=utf-8"
    body = r.content.decode()
    # The client goes in right after <head>, ahead of the page's own scripts.
    head = body.index("<head>") + len("<head>")
    assert body[head:].startswith("<script nonce=")
    assert body.index("/app-bundles/client.js?v=") < body.index("../shared/app.js")
    assert "window.CANOPY_DV = { origins: [" in body
    assert r["Cache-Control"] == "no-store" and r["X-Content-Type-Options"] == "nosniff"


def test_the_policy_allows_only_the_bundle_and_canopys_client(world):
    url = _page(_installed(world), "kanban")["bundle_url"]
    bundle_id = url.split("/")[2]
    d = _directives(Client().get(url)["Content-Security-Policy"])
    own = " ".join(f"{o}/app-bundles/{bundle_id}/" for o in _origins())
    client = [f"{o}/app-bundles/client.js" for o in _origins()]
    assert d["sandbox"] == "allow-scripts allow-forms"
    assert d["default-src"] == "'none'"
    script = d["script-src"].split()
    assert script[0].startswith("'nonce-") and script[1:] == [*client, *own.split()]
    assert not {"'unsafe-eval'", "'unsafe-inline'", "*", "'self'"} & set(script)
    assert d["connect-src"] == own
    assert d["img-src"] == f"{own} data: blob:"
    assert d["style-src"] == f"{own} 'unsafe-inline'"
    for name in ("object-src", "frame-src", "worker-src", "form-action", "base-uri"):
        assert d[name] == "'none'"


def test_a_nonce_is_fresh_each_time(world):
    url = _page(_installed(world), "kanban")["bundle_url"]
    nonces = {_directives(Client().get(url)["Content-Security-Policy"])["script-src"].split()[0] for _ in range(3)}
    assert len(nonces) == 3


@override_settings(BROWSER_EXTENSION_IDS=[DEV_ID])
def test_the_extension_may_frame_it_as_an_ancestor(world):
    url = _page(_installed(world), "sidepanel")["bundle_url"]
    d = _directives(Client().get(url)["Content-Security-Policy"])
    assert d["frame-ancestors"].split() == [*_origins(), f"chrome-extension://{DEV_ID}"]


def test_other_files_are_immutable_and_loadable_from_the_opaque_frame(world):
    url = _page(_installed(world), "kanban")["bundle_url"]
    base = url.rsplit("/", 2)[0]
    js = Client().get(f"{base}/shared/app.js")
    assert js.status_code == 200 and js["Content-Type"] == "text/javascript; charset=utf-8"
    assert js["Cache-Control"] == "public, max-age=31536000, immutable"
    assert js["Access-Control-Allow-Origin"] == "*" and js["Cross-Origin-Resource-Policy"] == "cross-origin"
    assert "nonce" not in js["Content-Security-Policy"] and b"<script" not in js.content
    png = Client().get(f"{base}/shared/logo.png")
    assert png["Content-Type"] == "image/png" and png.content.startswith(b"\x89PNG")


def test_serving_sets_no_cookie_even_for_a_signed_in_viewer(world):
    url = _page(_installed(world), "kanban")["bundle_url"]
    r = _client(world["admin"]).get(url)
    assert r.status_code == 200 and not r.cookies


@pytest.mark.parametrize("tail", [
    "kanban/../shared/app.js", "kanban/%2e%2e/shared/app.js", "missing.js", "shared/", "", "shared/app.js/x.js",
])
def test_anything_but_a_file_of_the_bundle_is_a_404(world, tail):
    url = _page(_installed(world), "kanban")["bundle_url"]
    base = url.rsplit("/", 2)[0]
    assert Client().get(f"{base}/{tail}").status_code == 404


def test_an_unknown_bundle_is_a_404_and_writes_are_refused(world):
    _installed(world)
    assert Client().get(f"/app-bundles/{'0' * 64}/kanban/kanban.html").status_code == 404
    assert Client().get("/app-bundles/not-a-hash/kanban/kanban.html").status_code == 404
    url = AppInstallation.objects.get(key="sales").manifest["bundle"]["id"]
    assert Client().post(f"/app-bundles/{url}/kanban/kanban.html").status_code == 405


def test_the_client_is_served(world):
    r = Client().get("/app-bundles/client.js?v=1")
    assert r.status_code == 200
    assert b"canopy-dynamic-view" in r.content and r["Content-Type"].startswith("text/javascript")


def test_html_without_a_head_still_gets_the_client_first():
    out = bundles._inject(b"<p>hi</p><script src=a.js></script>", "n", ["https://x"], "/c.js")
    assert out.startswith(b'<script nonce="n">') and out.endswith(b"<p>hi</p><script src=a.js></script>")
    out = bundles._inject(b"<!DOCTYPE html><p>hi", "n", ["https://x"], "/c.js")
    assert out.startswith(b'<!DOCTYPE html><script nonce="n">')


def test_an_origin_cannot_break_out_of_the_config_script():
    out = bundles._inject(b"<head>", "n", ["https://x</script><script>alert(1)"], "/c.js")
    assert b"</script><script>alert" not in out


# --- the overlay --------------------------------------------------------------------------------


@pytest.mark.parametrize("op, why", [
    ({"op": "fork_page", "page": "kanban", "code": "function View(){return null}"}, "cannot be forked"),
    ({"op": "fill_slot", "page": "kanban", "slot": "x", "code": "function Fill(){return null}"}, "cannot be forked"),
    ({"op": "add_page", "page": {"slug": "x-board", "kind": "bundle", "entry": "kanban/kanban.html",
                                 "role": "member"}}, "only the template declares bundle pages"),
])
def test_a_workspace_cannot_fork_fill_or_add_a_bundle_page(world, op, why):
    _installed(world)
    m = AppInstallation.objects.get(key="sales").manifest
    _, quarantined = ov.compose(m, [{**op, "id": "o1"}])
    assert quarantined and why in quarantined[0]["reason"]


def test_a_workspace_may_retitle_a_bundle_page(world):
    _installed(world)
    m = AppInstallation.objects.get(key="sales").manifest
    out, quarantined = ov.compose(m, [{"op": "set_page", "page": "kanban", "title": "Board", "id": "o1"}])
    assert not quarantined and next(p for p in out["pages"] if p["slug"] == "kanban")["title"] == "Board"
