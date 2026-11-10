"""The local page's server: http://127.0.0.1:<port>, never another interface.

aiohttp (in the `mac` extra) because it is one dependency for HTTP and WebSockets both;
everything this does is a call into serve.Serve, whose logic is tested without it.

Every request must be addressed to this server by name (Host), and anything that changes
something, or opens a socket, must carry this page's own Origin: so neither another website
nor a DNS-rebinding page can start a recording, read a transcript, or pair a phone.
"""
from __future__ import annotations

import json
from pathlib import Path

from aiohttp import WSMsgType, web

from .serve import MAX_AUDIO_MESSAGE, Serve, allowed

PAGE = Path(__file__).with_name("page.html")


def make_app(serve: Serve, port: int) -> web.Application:
    @web.middleware
    async def local_only(request: web.Request, handler):
        upgrade = request.headers.get("Upgrade", "").lower() == "websocket"
        needs_origin = upgrade or request.method not in ("GET", "HEAD")
        if not allowed(request.headers.get("Host"), request.headers.get("Origin"), port, needs_origin=needs_origin):
            return web.json_response({"error": "this server only answers its own page"}, status=403)
        response = await handler(request)
        if not upgrade:
            here = f"127.0.0.1:{port} localhost:{port}"
            ws = " ".join(f"ws://{h}" for h in here.split())
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
                f"script-src 'unsafe-inline' blob:; worker-src blob:; connect-src 'self' {ws}; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Cache-Control"] = "no-store"
        return response

    async def body_of(request: web.Request) -> dict:
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType()
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest() from None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest()
        return body

    def reply(result) -> web.Response:
        status, data = result if isinstance(result, tuple) else (200, result)
        return web.json_response(data, status=status)

    async def page(request):
        return web.Response(text=PAGE.read_text(), content_type="text/html")

    async def status(request):
        return reply(serve.api_status())

    async def live(request):
        try:
            after = int(request.query.get("after", "0"))
        except ValueError:
            after = 0
        return reply(serve.api_live(after))

    async def start(request):
        return reply(serve.api_start(await body_of(request)))

    async def end(request):
        await body_of(request)
        return reply(await serve.api_end())

    async def discard(request):
        await body_of(request)
        return reply(serve.api_discard())

    async def dismiss(request):
        await body_of(request)
        return reply(serve.api_dismiss())

    async def rename(request):
        return reply(serve.api_rename(await body_of(request)))

    async def config(request):
        return reply(serve.api_config(await body_of(request)))

    async def pair(request):
        await body_of(request)
        return reply(serve.api_pair())

    async def pair_status(request):
        return reply(serve.api_pair_status(request.match_info["id"]))

    async def phones(request):
        return reply(serve.api_phones())

    async def remove_phone(request):
        return reply(serve.api_remove_phone(request.match_info["id"]))

    async def meetings(request):
        return reply(serve.api_meetings())

    async def meeting(request):
        return reply(serve.api_meeting(request.match_info["id"]))

    async def delete_meeting(request):
        return reply(serve.api_delete_meeting(request.match_info["id"]))

    async def audio(request):
        """A browser tab's (or the browser microphone's) audio: binary int16 16 kHz mono."""
        inp = serve.hub.get(request.query.get("input"))
        if inp is None or inp.producer or inp.ended or inp.source not in ("browser-tab", "browser-mic"):
            return web.json_response({"error": "no such input"}, status=404)
        ws = web.WebSocketResponse(max_msg_size=MAX_AUDIO_MESSAGE, heartbeat=20)
        await ws.prepare(request)
        inp.producer = True
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    inp.push(msg.data)
                elif msg.type == WSMsgType.TEXT:
                    try:
                        if json.loads(msg.data).get("type") == "end":
                            break
                    except (ValueError, AttributeError):
                        pass
                else:
                    break
        finally:
            inp.end()  # the tab stopped sharing, or the page closed: the meeting ends and is kept
        return ws

    app = web.Application(middlewares=[local_only], client_max_size=64 * 1024)
    app.add_routes([
        web.get("/", page),
        web.get("/api/status", status),
        web.get("/api/live", live),
        web.post("/api/start", start),
        web.post("/api/end", end),
        web.post("/api/discard", discard),
        web.post("/api/dismiss", dismiss),
        web.post("/api/rename", rename),
        web.post("/api/config", config),
        web.post("/api/pair", pair),
        web.get("/api/pair/{id}", pair_status),
        web.get("/api/phones", phones),
        web.delete("/api/phones/{id}", remove_phone),
        web.get("/api/meetings", meetings),
        web.get("/api/meetings/{id}", meeting),
        web.delete("/api/meetings/{id}", delete_meeting),
        web.get("/ws/audio", audio),
    ])
    return app


async def start(serve: Serve, port: int) -> web.AppRunner:
    runner = web.AppRunner(make_app(serve, port), access_log=None)  # URLs can carry ids; keep the log quiet
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner
