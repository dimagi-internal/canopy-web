"""The embed shell, and the framing policy that makes it possible at all.

Every canopy page currently refuses to be framed: `XFrameOptionsMiddleware` is
enabled with no `X_FRAME_OPTIONS` override, so Django's `DENY` default applies.
An iframe widget is therefore impossible until one route opts out — and opting
out is the whole risk, because a page exempt from `X-Frame-Options` with no
`frame-ancestors` to replace it is frameable by ANY site.

So the property under test is not "the route is frameable". It is "the route is
frameable by exactly the origins an admin registered, and refuses to be served
at all otherwise". The fail-closed direction is the one that matters: a bug that
blocks a legitimate host costs an error message, a bug that serves an
unrestricted shell costs clickjacking on every deployment that embeds it.
"""

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.tokens.models import AppCredential
from tests.site_tenant import host_workspace

pytestmark = pytest.mark.django_db

LABS = "https://labs.connect.dimagi.com"


def _app(name="connect-labs", *, origins=(LABS,)):
    admin = User.objects.create_user(f"a-{name}", f"a-{name}@dimagi.com", "pw")
    cred = AppCredential.create_credential(name=name, created_by=admin,
                                                 workspace=host_workspace())
    if origins:
        cred.allowed_frame_origins = list(origins)
        cred.save(update_fields=["allowed_frame_origins"])
    return cred


def _get(app_name="connect-labs"):
    return Client().get(f"/embed/chat?app={app_name}")


@pytest.fixture
def built_frontend(settings, tmp_path):
    """Stand in a built frontend so the shell renders its real body.

    CI's backend job does not run `npm run build`, so `_embed_assets()` finds
    no manifest and `embed_chat` serves its "not built" page instead — which is
    correct behaviour and made three tests below pass locally (where a build
    existed) and fail in CI. The manifest shape mirrors a real vite build,
    including CSS hoisted onto a SHARED chunk rather than the entry, because
    that is the case the resolver exists to handle.
    """
    import json as _json
    from apps.tokens import views_embed

    vite = tmp_path / ".vite"
    vite.mkdir()
    (vite / "manifest.json").write_text(_json.dumps({
        "src/embed/main.tsx": {
            "file": "assets/embed-TEST01.js",
            "isEntry": True,
            "imports": ["_shared-TEST02.js"],
        },
        "_shared-TEST02.js": {
            "file": "assets/shared-TEST02.js",
            "css": ["assets/shared-TEST03.css"],
        },
    }))
    settings.FRONTEND_DIST_DIR = tmp_path
    # The manifest read is lru_cached for the life of the process, so a test
    # that changes the setting must drop it or it reads a neighbour's.
    views_embed._manifest.cache_clear()
    yield tmp_path
    views_embed._manifest.cache_clear()

# --- the framing policy ---------------------------------------------------


def test_serves_the_shell_with_frame_ancestors_for_the_registered_origin():
    _app()
    r = _get()
    assert r.status_code == 200
    assert r["Content-Security-Policy"] == f"frame-ancestors {LABS}"


def test_x_frame_options_is_not_sent_on_this_route():
    """DENY and frame-ancestors together is a contradiction, and DENY wins in
    enough browsers to make the widget simply not load."""
    _app()
    assert "X-Frame-Options" not in _get()


def test_every_other_page_still_refuses_framing():
    """The exemption must be this route only — a blanket change would make the
    whole app clickjackable."""
    _app()
    r = Client().get("/health/")
    assert r.get("X-Frame-Options") == "DENY"


def test_several_registered_origins_are_all_listed():
    _app(origins=[LABS, "https://labs-staging.dimagi.com"])
    csp = _get()["Content-Security-Policy"]
    assert csp == f"frame-ancestors {LABS} https://labs-staging.dimagi.com"


# --- fail closed ----------------------------------------------------------


def test_an_app_with_no_registered_origins_is_refused_outright():
    """THE dangerous state. Serving an XFO-exempt page with an empty
    frame-ancestors list would leave it frameable by anyone, so there is no
    shell to serve until an origin exists."""
    _app(origins=())
    assert _get().status_code == 404


def test_an_unknown_app_is_refused():
    assert _get("no-such-app").status_code == 404


def test_a_missing_app_parameter_is_refused():
    _app()
    assert Client().get("/embed/chat").status_code == 404


def test_a_revoked_credential_is_refused():
    app = _app()
    AppCredential.objects.filter(pk=app.pk).update(revoked_at=timezone.now())
    assert _get().status_code == 404


def test_a_malformed_origin_cannot_reach_the_header():
    """Defence in depth: the write path validates, and the read path sanitises,
    so a row inserted by other means (a shell, a fixture, a future migration)
    cannot produce a permissive or broken header. `*` is the case that matters —
    it would re-open framing to everyone.
    """
    app = _app()
    AppCredential.objects.filter(pk=app.pk).update(
        allowed_frame_origins=["*", "not-an-origin", f"{LABS}/with/path", LABS]
    )
    csp = _get()["Content-Security-Policy"]
    assert csp == f"frame-ancestors {LABS}"


def test_an_app_whose_only_origins_are_malformed_is_refused():
    """Sanitising down to an empty list must land in the same refusal as never
    having registered one, not in an empty directive."""
    app = _app()
    AppCredential.objects.filter(pk=app.pk).update(allowed_frame_origins=["*"])
    assert _get().status_code == 404


# --- what the shell is, and is not ----------------------------------------


def test_the_shell_is_not_the_app_and_pulls_in_no_service_worker():
    """A widget that loaded the SPA shell would register canopy's service
    worker inside a third-party page and drag the whole app in with it — the
    shape of issue #345, where an iframe rendered the entire SPA inside
    itself."""
    _app()
    body = _get().content.decode()
    assert "serviceWorker" not in body
    assert "registerSW" not in body
    assert '<div id="root"' not in body


def test_the_shell_loads_the_in_frame_bundle():
    """The shell is a loader, not the app. The `ready` handshake moved into the
    bundle (frontend/src/embed/hostLink.ts) so the protocol has ONE
    implementation rather than one inline here and one in TypeScript."""
    _app()
    body = _get().content.decode()
    # Either a module script for the built bundle, or the honest "not built"
    # page — never a silently blank frame, which a host cannot tell from a
    # broken token.
    assert 'type="module"' in body or "npm run build" in body


def test_the_shell_is_never_cached():
    """It names a build's assets and carries a per-app policy; a cached copy is
    the "do I have to hard-refresh?" bug with a stale CSP attached."""
    _app()
    assert "no-cache" in _get()["Cache-Control"]


def test_the_shell_is_reachable_without_logging_in():
    """The token arrives later over postMessage, so the document itself is
    anonymous — a login redirect here would mean the widget could never load
    for anyone."""
    _app()
    r = _get()
    assert r.status_code == 200
    assert "accounts/google" not in r.get("Location", "")


# --- the origin registry has to be operable, and refuse typos --------------


def _run(*args):
    from io import StringIO
    from django.core.management import call_command
    out = StringIO()
    call_command("grant_app_frame_origin", "--workspace", "site-host", *args, stdout=out)
    return out.getvalue()


def test_command_registers_an_origin_and_the_shell_starts_serving():
    _app(origins=())
    assert _get().status_code == 404          # nothing registered yet
    _run("--name", "connect-labs", "--origin", LABS)
    r = _get()
    assert r.status_code == 200
    assert r["Content-Security-Policy"] == f"frame-ancestors {LABS}"


def test_command_refuses_a_wildcard():
    """The one input that would undo the whole exemption."""
    from django.core.management.base import CommandError
    _app(origins=())
    for bad in ("*", "https:", "https://*.dimagi.com"):
        with pytest.raises(CommandError, match="not a valid frame origin"):
            _run("--name", "connect-labs", "--origin", bad)
    assert _get().status_code == 404


def test_command_refuses_a_path_because_frame_ancestors_would_ignore_it():
    """Storing a path would imply a narrower grant than is actually made."""
    from django.core.management.base import CommandError
    _app(origins=())
    with pytest.raises(CommandError, match="not a valid frame origin"):
        _run("--name", "connect-labs", "--origin", f"{LABS}/only/here")


def test_command_refuses_an_injected_second_directive():
    from django.core.management.base import CommandError
    _app(origins=())
    with pytest.raises(CommandError, match="not a valid frame origin"):
        _run("--name", "connect-labs", "--origin", f"{LABS}; default-src *")


def test_command_allows_localhost_for_development():
    _app(origins=())
    _run("--name", "connect-labs", "--origin", "http://localhost:8000")
    assert _get()["Content-Security-Policy"] == "frame-ancestors http://localhost:8000"


def test_command_warns_when_removing_the_last_origin_breaks_the_widget():
    _app()
    out = _run("--name", "connect-labs", "--origin", LABS, "--remove")
    assert "404 until one is added" in out
    assert _get().status_code == 404


def test_command_is_idempotent():
    _app(origins=())
    _run("--name", "connect-labs", "--origin", LABS)
    assert "no change" in _run("--name", "connect-labs", "--origin", LABS)


def test_command_list_flags_a_stored_value_that_is_being_ignored():
    """The column and the served policy can disagree (a row written by other
    means); --list says so rather than implying the grant is in force."""
    app = _app()
    AppCredential.objects.filter(pk=app.pk).update(allowed_frame_origins=[LABS, "*"])
    out = _run("--name", "connect-labs", "--list")
    assert "IGNORED" in out


# --- the loader script a host hard-codes -----------------------------------


def test_widget_js_is_served_from_the_build():
    r = Client().get("/embed/widget.js")
    # Either the real artifact (a dev box that has built it) or the honest 503
    # below — never a 404, which in someone else's page is undebuggable.
    assert r.status_code in (200, 503)


def test_widget_js_says_what_to_run_when_it_is_not_built(settings, tmp_path):
    settings.FRONTEND_DIST_DIR = tmp_path
    r = Client().get("/embed/widget.js")
    assert r.status_code == 503
    assert "npm run build:widget" in r.content.decode()


def test_widget_js_is_javascript_and_revalidates(settings, tmp_path):
    embed = tmp_path / "embed"
    embed.mkdir()
    (embed / "widget.js").write_text("var canopy=(function(){return{}})();")
    settings.FRONTEND_DIST_DIR = tmp_path

    r = Client().get("/embed/widget.js")

    assert r.status_code == 200
    assert "javascript" in r["Content-Type"]
    # Fixed filename, so it cannot be content-hashed: a host must pick up a new
    # loader without editing their script tag.
    assert r["Cache-Control"] == "no-cache"


def test_widget_js_needs_no_login(settings, tmp_path):
    """It is loaded by a <script> tag on a third-party page, where a redirect to
    Google would simply mean the widget never appears."""
    embed = tmp_path / "embed"
    embed.mkdir()
    (embed / "widget.js").write_text("var canopy={};")
    settings.FRONTEND_DIST_DIR = tmp_path

    r = Client().get("/embed/widget.js")

    assert r.status_code == 200
    assert "accounts" not in r.get("Location", "")


def test_widget_js_is_not_app_scoped(settings, tmp_path):
    """No ?app= — the loader is one plain script for every host; `canopy.init`
    picks the app at call time, and the framing policy is enforced on the shell."""
    embed = tmp_path / "embed"
    embed.mkdir()
    (embed / "widget.js").write_text("var canopy={};")
    settings.FRONTEND_DIST_DIR = tmp_path

    assert Client().get("/embed/widget.js").status_code == 200


def test_the_shell_hands_over_the_origins_the_frame_must_validate_against(built_frontend):
    """The server is the only party that can say this.

    The frame is framed only by registered origins (frame-ancestors), but that
    constrains who may EMBED it, not who may postMessage AT it — any window
    with a handle can post. So the frame validates `event.origin` against a
    list, and the list has to come from here: a list the host supplied would
    authenticate the very party being authenticated.
    """
    _app(origins=[LABS, "https://labs-staging.dimagi.com"])
    body = _get().content.decode()
    assert "window.CANOPY_EMBED" in body
    assert LABS in body
    assert "https://labs-staging.dimagi.com" in body


def test_the_shell_json_encodes_injected_values(built_frontend):
    """These land inside a <script> block. An app name or origin carrying a
    quote would otherwise close the string and inject."""
    app = _app()
    AppCredential.objects.filter(pk=app.pk).update(name='ev"il')
    body = Client().get('/embed/chat?app=ev"il').content.decode()
    # json.dumps escapes it; an f-string would have emitted a bare quote.
    assert 'ev\\"il' in body or 'ev\"il' in body
    assert '{ app: ev"il' not in body


def test_the_shell_tells_the_frame_which_app_it_is(built_frontend):
    """The in-frame app needs to know, and it cannot read the query string of a
    URL the host controls any more safely than the server can hand it over."""
    _app()
    body = _get().content.decode()
    assert "window.CANOPY_EMBED" in body
    assert "connect-labs" in body


# ---- the panel's light/dark (`?theme=`, set by the loader from theme.mode) ----
#
# Rendered by the SERVER so the panel is in the right mode from its first
# paint. Applied later by the frame's JS instead, a light host would see the
# panel flash dark on every page view.

def _html_tag(body: str) -> str:
    start = body.index("<html")
    return body[start:body.index(">", start) + 1]


def _shell(built_frontend, query=""):
    if not AppCredential.objects.filter(name="connect-labs").exists():
        _app()
    return Client().get(f"/embed/chat?app=connect-labs{query}").content.decode()


def test_no_theme_is_the_historical_dark_default(built_frontend):
    """Every host that existed before theming must see exactly what it saw."""
    assert 'class="dark"' in _html_tag(_shell(built_frontend))


def test_a_light_host_gets_a_light_shell(built_frontend):
    tag = _html_tag(_shell(built_frontend, "&theme=light"))
    assert 'class=""' in tag and "dark" not in tag


def test_auto_starts_dark_and_lets_the_browser_decide_before_anything_paints(built_frontend):
    body = _shell(built_frontend, "&theme=auto")
    assert 'class="dark"' in _html_tag(body)
    # In <head>, BEFORE the stylesheet — after it, the wrong mode has painted.
    head = body[:body.index("</head>")]
    assert "prefers-color-scheme: dark" in head
    assert head.index("prefers-color-scheme") < head.index("<style>")


def test_an_unrecognised_theme_cannot_reach_the_attribute(built_frontend):
    """The value lands in an HTML attribute. It is looked up in an allowlist,
    never echoed."""
    body = _shell(built_frontend, '&theme=%22%3E%3Cscript%3Ealert(1)%3C/script%3E')
    assert "alert(1)" not in body
    assert 'class="dark"' in _html_tag(body)


def test_only_auto_carries_the_media_query_script(built_frontend):
    assert "prefers-color-scheme" not in _shell(built_frontend, "&theme=light")
    assert "prefers-color-scheme" not in _shell(built_frontend, "&theme=dark")
