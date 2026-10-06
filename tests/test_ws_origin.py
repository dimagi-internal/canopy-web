"""Which web pages may open a canopy socket (apps/realtime/origin.py).

ALLOWED_HOSTS is `*` on labs (the load balancer's health check uses the
container's IP as Host), so Channels' host-based origin check let every web
page open a socket. The check is now by origin.
"""
import pytest
from django.utils import timezone

from apps.realtime.origin import OriginValidator
from apps.tokens.models import AppCredential

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]


@pytest.fixture
def prod(settings):
    settings.DEBUG = False
    settings.CANOPY_PUBLIC_BASE_URL = "https://canopy.test"
    settings.CANOPY_IDENTITY_BASE_URL = "https://canopy.test"
    settings.CANOPY_FORMER_BASE_URLS = ["https://labs.test/canopy"]


async def _open(origin):
    """(reached the app, close code sent)."""
    reached, sent = [], []

    async def app(scope, receive, send):
        reached.append(True)

    async def receive():
        return {"type": "websocket.connect"}

    async def send(msg):
        sent.append(msg)

    headers = [(b"origin", origin.encode())] if origin is not None else []
    await OriginValidator(app)({"type": "websocket", "headers": headers}, receive, send)
    return bool(reached), [m.get("code") for m in sent if m["type"] == "websocket.close"]


async def test_canopys_own_addresses_are_allowed(prod):
    assert (await _open("https://canopy.test"))[0]
    assert (await _open("https://labs.test"))[0]   # a former address: runners and old pages


async def test_an_unknown_web_page_is_refused(prod):
    assert await _open("https://evil.test") == (False, [403])


async def test_a_connected_sites_origin_is_allowed_until_revoked(prod, default_workspace):
    from asgiref.sync import sync_to_async

    app = await sync_to_async(AppCredential.objects.create)(
        name="site", workspace=default_workspace, allowed_frame_origins=["https://site.test"])
    assert (await _open("https://site.test"))[0]
    app.revoked_at = timezone.now()
    await sync_to_async(app.save)()
    assert not (await _open("https://site.test"))[0]


async def test_a_client_with_no_origin_goes_on_to_the_token_check(prod):
    """Not a browser — a script or the e2e checks. Cross-site socket hijacking
    is a browser attack; these authenticate by token alone."""
    assert (await _open(None))[0]
