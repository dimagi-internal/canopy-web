"""Two web workers refreshing one connection at once, against real row locks (Review Focus 4).

SQLite has none, so `tests/test_connections_server_tokens.py` replays the interleavings. Skipped
there; CI's `Backend pytest (PostgreSQL)` job runs this.
"""
from __future__ import annotations

import threading
from unittest import mock

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from django.db import connection, connections

from apps.connections import server_key, server_tokens
from apps.connections.providers.salesforce import SalesforceProvider
from tests.fixtures.connection_calls import TokensWorld
from tests.fixtures.connections import FakeSF, _resp, _token_body, sealed_to_owner

pytestmark = [
    pytest.mark.skipif(connection.vendor != "postgresql", reason="row locks need PostgreSQL"),
    pytest.mark.django_db(transaction=True),
]


def test_two_workers_one_lease_one_provider_call():
    server_key.use_software_key_for_tests(ec.generate_private_key(ec.SECP256R1()))
    server_tokens._forget_all()
    try:
        world = TokensWorld()
        conn = world.shared(sealed_to_owner({"agent": world.eva}, world.person, app=world.app, server_use=True),
                            agents=[world.eva], canopy_web=server_key.thumbprint())
        # The first at the provider until the second has found the lease held and started waiting.
        waiting = threading.Event()
        real = SalesforceProvider.refresh

        def refresh(self, token):
            assert waiting.wait(20)
            return real(self, token)

        def sleep(seconds):
            waiting.set()
            threading.Event().wait(0.2)

        got, failures = [], []

        def worker():
            try:
                got.append(server_tokens.access_token(conn, sleep=sleep)[0])
            except BaseException as e:  # noqa: BLE001 — carried back to the test's own thread
                failures.append(e)
            finally:
                connections.close_all()

        with FakeSF(token=[_resp(body=_token_body(access_token="00D!new", refresh_token=None))]) as sf, \
                mock.patch.object(SalesforceProvider, "refresh", refresh):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(40)
        assert failures == []
        assert got == ["00D!new", "00D!new"] and len(sf.token_calls()) == 1
    finally:
        server_key.use_software_key_for_tests(None)
        server_tokens._forget_all()
