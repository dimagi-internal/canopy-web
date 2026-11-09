"""The local page's server refuses anyone but its own page. Needs aiohttp (the `mac` extra);
skipped without it, as in CI."""
import asyncio
import json

import pytest

pytest.importorskip("aiohttp")
from aiohttp import WSServerHandshakeError  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from canopy_listen import serve, web  # noqa: E402

PORT = 7351
HOST = {"Host": f"127.0.0.1:{PORT}"}
MINE = {**HOST, "Origin": f"http://127.0.0.1:{PORT}"}


@pytest.fixture(autouse=True)
def _homes(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPY_LISTEN_HOME", str(tmp_path / "listen"))
    monkeypatch.setenv("CANOPY_LISTEN_CANOPY_HOME", str(tmp_path / "canopy"))
    monkeypatch.setenv("CANOPY_LISTEN_RUNNER", "/usr/bin/false")


def test_the_page_answers_only_itself():
    calls = []

    async def go():
        s = serve.Serve(run=lambda argv: calls.append(argv[1:]) or (0, ""), name="Test Mac")
        async with TestClient(TestServer(web.make_app(s, PORT))) as client:
            page = await client.get("/", headers=HOST)
            assert page.status == 200 and "Canopy Listen" in await page.text()
            assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
            assert (await client.get("/api/live", headers={"Host": f"evil.example:{PORT}"})).status == 403

            body = {"input": "browser-tab", "notice_accepted": True}
            assert (await client.post("/api/start", json=body, headers=HOST)).status == 403
            evil = {**HOST, "Origin": "https://evil.example"}
            assert (await client.post("/api/start", json=body, headers=evil)).status == 403
            assert (await client.get("/api/meetings", headers=evil)).status == 403
            with pytest.raises(WSServerHandshakeError):
                await client.ws_connect("/ws/audio?input=x", headers=evil)
            assert calls == []

            started = await client.post("/api/start", json=body, headers=MINE)
            assert started.status == 200
            input_id = (await started.json())["input"]
            ws = await client.ws_connect(f"/ws/audio?input={input_id}", headers=MINE)
            await ws.send_bytes(b"\x01\x00" * 1600)
            await ws.send_str(json.dumps({"type": "end"}))
            await ws.receive()
            inp = s.hub.get(input_id)
            assert inp.ended and inp.buffered == 3200 and inp.producer
            while s.controller.tasks:
                await asyncio.gather(*list(s.controller.tasks))
            assert calls == [["app", "start", "meeting-assistant", "--accept-notice"]]

    asyncio.run(go())
