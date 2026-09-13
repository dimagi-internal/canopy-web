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

pytestmark = pytest.mark.django_db

LABS = "https://labs.connect.dimagi.com"


def _app(name="connect-labs", *, origins=(LABS,)):
    admin = User.objects.create_user(f"a-{name}", f"a-{name}@dimagi.com", "pw")
    raw, cred = AppCredential.create_credential(
        name=name, domains=["dimagi.com"], created_by=admin,
    )
    if origins:
        cred.allowed_frame_origins = list(origins)
        cred.save(update_fields=["allowed_frame_origins"])
    return cred


def _get(app_name="connect-labs"):
    return Client().get(f"/embed/chat?app={app_name}")


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


def test_the_shell_announces_itself_to_the_parent():
    """First half of the credential handshake: the host cannot know when to
    send a token until the frame says it exists."""
    _app()
    body = _get().content.decode()
    assert "canopy-widget" in body
    assert "postMessage" in body


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
    call_command("grant_app_frame_origin", *args, stdout=out)
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
