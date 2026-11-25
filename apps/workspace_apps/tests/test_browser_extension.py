"""The browser extension: the manifest's `browser` key, the apps-for-a-tab route, and
the framing of the panel and the sandbox frame it nests (docs/architecture/browser-extension.md)."""
from __future__ import annotations

import copy

import pytest
from django.test import Client, override_settings

from apps.workspace_apps import browser
from apps.workspace_apps import manifest as mf
from apps.workspace_apps import template_diff
from apps.workspace_apps.models import AppInstallation
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.workspace_apps import J, _client, _user

pytestmark = pytest.mark.django_db

DEV_ID = "lkbpnpoflndilnhefmhjpmdibdcopcfo"
GMAIL = "https://mail.google.com/mail/u/0/#inbox/FMfcgz"

APP = {
    "key": "sales", "name": "Sales",
    "roles": {"seller": {"capability": "content.write"}},
    "collections": {"note": {"fields": {"body": {"type": "text"}}}},
    "pages": [
        {"slug": "gmail-panel", "title": "Gmail", "code": "function View(){return <Card>hi</Card>}"},
        {"slug": "sellers", "title": "Sellers", "role": "seller", "code": "function View(){return <Card>s</Card>}"},
        {"slug": "jobs", "audience": "public", "role": "public", "code": "function View(){return <Card>p</Card>}"},
    ],
    "browser": [
        {"page": "gmail-panel", "matches": ["https://mail.google.com/*"], "adapter": "gmail",
         "capabilities": ["read_open_email"], "events": ["email_opened"]},
        {"page": "sellers", "matches": ["https://mail.google.com/mail/*"], "adapter": "gmail",
         "capabilities": ["compose_recipients"], "events": []},
    ],
}


def _with(**changes):
    m = copy.deepcopy(APP)
    m["browser"] = [{**m["browser"][0], **changes}]
    return m


def _install(world, m=None, ws="dimagi"):
    r = _client(world["admin"]).post(f"/api/workspace-apps/{ws}/", {"manifest": m or APP}, content_type=J)
    assert r.status_code == 201, r.content
    return AppInstallation.objects.get(workspace_id=ws, key=(m or APP)["key"])


def _apps(user, url=GMAIL):
    r = _client(user).get("/api/browser/apps", {"url": url})
    assert r.status_code == 200, r.content
    return r.json()


# --- the manifest ------------------------------------------------------------------------


def test_a_valid_declaration_is_kept_and_an_app_without_one_keeps_its_shape():
    m = mf.validate(APP)
    assert m["browser"][0] == {"page": "gmail-panel", "matches": ["https://mail.google.com/*"], "adapter": "gmail",
                               "capabilities": ["read_open_email"], "events": ["email_opened"]}
    plain = {k: v for k, v in APP.items() if k != "browser"}
    assert "browser" not in mf.validate(plain)


def test_gmail_v1_2_capabilities_are_accepted():
    caps = ["read_open_email", "read_open_thread", "compose_recipients", "start_compose", "fill_compose"]
    assert mf.validate(_with(capabilities=caps))["browser"][0]["capabilities"] == caps


def test_every_adapters_acts_are_among_its_capabilities_and_gmails_are_the_compose_writers():
    for adapter in browser.ADAPTERS.values():
        assert set(adapter.get("acts", ())) <= set(adapter["capabilities"])
    assert set(browser.ADAPTERS["gmail"]["acts"]) == {"start_compose", "fill_compose"}
    assert browser.acts_of("gmail", ["read_open_thread", "fill_compose"]) == ["fill_compose"]
    assert browser.acts_of("nope", ["fill_compose"]) == []


@pytest.mark.parametrize("change, why", [
    ({"page": "nope"}, "member page"),
    ({"page": "jobs"}, "member page"),
    ({"adapter": "outlook"}, "adapter"),
    ({"capabilities": ["send_email"]}, "not offered"),
    ({"events": ["email_deleted"]}, "not offered"),
    ({"matches": ["http://mail.google.com/*"]}, "https match pattern"),
    ({"matches": ["https://*/*"]}, "https match pattern"),
    ({"matches": ["https://evil.example.com/*"]}, "not a host of this adapter"),
    ({"matches": ["https://*.google.com/*"]}, "not a host of this adapter"),
    ({"matches": ["https://mail.google.com.evil.com/*"]}, "not a host of this adapter"),
    ({"matches": ["https://mail.google.com:8443/*"]}, "https match pattern"),
    ({"matches": []}, "match patterns"),
    ({"extra": 1}, "unknown keys"),
])
def test_a_bad_declaration_is_refused_naming_why(change, why):
    with pytest.raises(ValueError, match=why):
        mf.validate(_with(**change))


def test_a_template_update_changing_the_declaration_needs_an_admin():
    old, new = mf.validate(APP), mf.validate(_with(capabilities=["read_open_email", "compose_recipients"]))
    assert any("browser tabs" in c for c in template_diff.changes(old, new))
    assert not any("browser tabs" in c for c in template_diff.changes(old, mf.validate(APP)))


def test_a_template_update_adding_an_act_says_so_and_a_read_does_not():
    old = mf.validate(APP)
    acts = template_diff.changes(old, mf.validate(_with(capabilities=["read_open_email", "fill_compose"])))
    assert any("change what is on the browser tab" in c and "gmail fill_compose" in c for c in acts)
    reads = template_diff.changes(old, mf.validate(_with(capabilities=["read_open_email", "read_open_thread"])))
    assert reads and not any("change what is on the browser tab" in c for c in reads)
    # Already declared before: no new act, only the general change.
    had = mf.validate(_with(capabilities=["fill_compose"]))
    again = template_diff.changes(had, mf.validate(_with(capabilities=["fill_compose", "read_open_thread"])))
    assert again and not any("change what is on the browser tab" in c for c in again)


@pytest.mark.parametrize("url, hit", [
    ("https://mail.google.com/mail/u/0/#inbox", True),
    ("https://mail.google.com/", True),
    ("https://MAIL.google.com/mail?x=1", True),
    ("http://mail.google.com/mail/", False),
    ("https://mail.google.com.evil.com/", False),
    ("https://evil.com/?https://mail.google.com/", False),
    ("https://mail.google.com:8443/mail/", False),
    ("https://user:pw@mail.google.com/mail/", False),
    ("not a url", False),
])
def test_matching_is_chromes_rule(url, hit):
    assert browser.matches("https://mail.google.com/*", url) is hit


def test_a_path_pattern_matches_the_path_and_query_never_the_fragment():
    assert browser.matches("https://mail.google.com/mail/*", "https://mail.google.com/mail/u/0")
    assert not browser.matches("https://mail.google.com/mail/*", "https://mail.google.com/other#/mail/")
    assert browser.matches("https://mail.google.com/*inbox*", "https://mail.google.com/x?view=inbox")


def test_a_subdomain_pattern_needs_an_adapter_that_allows_it(monkeypatch):
    monkeypatch.setitem(browser.ADAPTERS, "docs", {"hosts": ("*.example.com",), "capabilities": (), "events": ()})
    assert browser.check_pattern("x", "https://*.example.com/*", ("*.example.com",))
    assert browser.matches("https://*.example.com/*", "https://a.example.com/")
    assert browser.matches("https://*.example.com/*", "https://example.com/")
    assert not browser.matches("https://*.example.com/*", "https://badexample.com/")


# --- GET /api/browser/apps -----------------------------------------------------------------


def test_a_member_gets_the_pages_for_their_roles(world):
    _install(world)
    viewer = _apps(world["viewer"])
    assert [(a["app_key"], a["page_slug"]) for a in viewer] == [("sales", "gmail-panel")]
    assert viewer[0] == {"workspace": "dimagi", "workspace_name": "Dimagi", "app_key": "sales", "app_name": "Sales",
                         "page_slug": "gmail-panel", "page_title": "Gmail", "adapter": "gmail",
                         "capabilities": ["read_open_email"], "acts": [], "events": ["email_opened"],
                         "matches": ["https://mail.google.com/*"]}
    # An editor also holds `seller`, so both pages match the Gmail tab.
    assert {a["page_slug"] for a in _apps(world["editor"])} == {"gmail-panel", "sellers"}


def test_each_row_says_which_of_its_capabilities_are_acts(world):
    m = _with(capabilities=["read_open_thread", "start_compose", "fill_compose"])
    _install(world, m)
    row = next(a for a in _apps(world["viewer"]) if a["page_slug"] == "gmail-panel")
    assert row["capabilities"] == ["read_open_thread", "start_compose", "fill_compose"]
    assert row["acts"] == ["start_compose", "fill_compose"]


def test_a_url_that_matches_nothing_lists_nothing(world):
    _install(world)
    for url in ("https://www.google.com/", "http://mail.google.com/mail/", "", "x" * 3000):
        assert _apps(world["viewer"], url) == []


def test_a_switched_off_app_is_not_offered(world):
    inst = _install(world)
    inst.enabled = False
    inst.save()
    assert _apps(world["viewer"]) == []
    assert _apps(world["admin"]) == []  # even to those who manage it: the panel only uses apps


def test_a_non_member_sees_none_of_the_workspaces_apps(world):
    _install(world)
    assert _apps(world["outsider"]) == []


def test_apps_come_from_every_workspace_the_caller_is_in(world):
    _install(world)
    WorkspaceMembership.objects.create(user=world["viewer"], workspace_id="other", role=WorkspaceMembership.VIEWER)
    other = {**copy.deepcopy(APP), "key": "crm", "name": "CRM"}
    r = _client(world["outsider"]).post("/api/workspace-apps/other/", {"manifest": other}, content_type=J)
    assert r.status_code == 201, r.content
    assert {(a["workspace"], a["app_key"]) for a in _apps(world["viewer"])} == {("dimagi", "sales"), ("other", "crm")}


def test_a_guest_gets_only_the_app_and_role_their_scope_names(world):
    _install(world)
    guest = _user("guest")
    WorkspaceMembership.objects.create(user=guest, workspace=world["ws"], role=WorkspaceMembership.GUEST,
                                       scope={"apps": {"sales": "seller"}})
    # The guest holds `seller` (the sellers page), never `member` (the gmail-panel page).
    assert [a["page_slug"] for a in _apps(guest)] == ["sellers"]
    WorkspaceMembership.objects.filter(user=guest).update(scope={"apps": {}})
    assert _apps(guest) == []


def test_signed_out_is_a_401():
    assert Client().get("/api/browser/apps", {"url": GMAIL}).status_code == 401


# --- framing -----------------------------------------------------------------------------


@pytest.fixture()
def built(settings, tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><html><head><title>c</title></head><body></body></html>")
    settings.FRONTEND_DIST_DIR = tmp_path
    return tmp_path


def test_the_panel_is_framable_only_by_the_allowed_extensions(built, settings):
    settings.REQUIRE_AUTH = True
    r = Client().get("/ext/panel/")  # signed out: it must render, to offer the sign-in
    assert r.status_code == 200
    assert "X-Frame-Options" not in r
    assert r["Content-Security-Policy"] == f"frame-ancestors chrome-extension://{DEV_ID}"
    assert f'name="canopy-ext-origins" content="[&quot;chrome-extension://{DEV_ID}&quot;]"' in r.content.decode()


def test_with_no_extension_allowed_there_is_no_panel(built):
    with override_settings(BROWSER_EXTENSION_IDS=[]):
        assert Client().get("/ext/panel/").status_code == 404


def test_every_other_page_keeps_deny(built, world):
    c = _client(world["viewer"])
    for path in ("/w/dimagi/agents", "/ext/signed-in/", "/ext/panel/x"):
        r = c.get(path)
        assert r.get("X-Frame-Options") == "DENY", path


def test_only_the_panel_itself_is_open_signed_out(built, settings):
    settings.REQUIRE_AUTH = True
    assert Client().get("/ext/panel/").status_code == 200
    for path in ("/ext/signed-in/", "/ext/panel/x"):
        r = Client().get(path)
        assert r.status_code == 302 and "/accounts/" in r["Location"], path


def test_the_sandbox_frame_allows_the_extension_as_an_ancestor_and_nothing_else_changes():
    with override_settings(BROWSER_EXTENSION_IDS=[]):
        before = Client().get("/dynamic-views/frame/")["Content-Security-Policy"]
    after = Client().get("/dynamic-views/frame/")["Content-Security-Policy"]
    directives = dict(d.strip().split(" ", 1) for d in after.split(";"))
    assert directives["sandbox"] == "allow-scripts"  # never allow-same-origin
    assert directives["connect-src"] == "'none'"
    assert f"chrome-extension://{DEV_ID}" in directives["frame-ancestors"].split()
    # Only frame-ancestors differs, by exactly the extension origin; the frame's
    # accepted message origins (its inline script) never include it.
    strip = lambda csp: [d for d in csp.split("; ") if not d.startswith(("frame-ancestors", "script-src"))]  # noqa: E731
    assert strip(before) == strip(after)
    assert after.split("frame-ancestors ")[1].split() == [*before.split("frame-ancestors ")[1].split(),
                                                           f"chrome-extension://{DEV_ID}"]


def test_a_bad_extension_id_fails_at_boot():
    import os
    import subprocess
    import sys

    env = {**os.environ, "BROWSER_EXTENSION_IDS": "not-an-id"}
    run = subprocess.run([sys.executable, "-c", "import config.settings.base"], env=env, capture_output=True, text=True)
    assert run.returncode != 0 and "not a Chrome extension id" in run.stderr
