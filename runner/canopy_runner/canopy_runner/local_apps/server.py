"""The server calls a local app needs, and the local stub that replaces them in dev.

Run calls use the runner's own token and only the `/runners/{id}/local-apps/` routes.
`submit` is the one exception: publishing is something a person or an agent does, so it
takes their token and never the runner's.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .. import client as client_mod
from ..client import ClientError
from . import package, state

SUBMIT_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 60
UNREACHABLE = 0
# The run or the app no longer exists on the server: there is nothing to wait out.
GONE_STATUSES = (404, 410)


class Refused(Exception):
    """The server said no, and its words are for the person."""


class Unreachable(Exception):
    """No usable answer: a network fault or a 5xx."""


class RunGone(Exception):
    """A heartbeat answered 404 or 410: the app was removed or the run is gone."""


def _detail(exc: ClientError) -> str:
    try:
        body = json.loads(exc.body or b"{}")
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    return str(body.get("detail") or body.get("title") or "")


def _quoted(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


class RunnerServer:
    def __init__(self, client, runner_id: str):
        self.client = client
        self.prefix = f"/runners/{runner_id}/local-apps"

    def list_apps(self) -> list:
        # Asked from a menu and a prompt: one short try, not three.
        try:
            _, body = self.client._call("GET", f"{self.prefix}/", retry=False)
        except ClientError as exc:
            raise Unreachable(str(exc)) from exc
        apps = (body or {}).get("apps") if isinstance(body, dict) else None
        if not isinstance(apps, list):
            raise Unreachable("canopy's list of apps made no sense")
        return [a for a in apps if isinstance(a, dict)]

    def package(self, key: str, version: str, *, max_bytes: int = package.MAX_PACKED) -> bytes:
        url = (f"{self.client.base_url}/api/harness{self.prefix}/{_quoted(key)}/package"
               f"?version={_quoted(version)}")
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {self.client.token}")
        client_mod._identify(req)
        try:
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
                data = resp.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404, 409, 410):
                raise Refused(f"canopy won't give out {key} {version} (HTTP {exc.code})") from exc
            raise Unreachable(f"downloading {key} -> HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise Unreachable(f"downloading {key} -> {exc}") from exc
        if len(data) > max_bytes:
            raise package.PackageError("the download is larger than a package may be")
        return data

    def start_run(self, key: str, version: str) -> str:
        # Not re-sent: a retry after the server made the run would make a second one.
        try:
            _, body = self.client._call("POST", f"{self.prefix}/{_quoted(key)}/runs/",
                                        {"version": version}, retry=False)
        except ClientError as exc:
            if exc.status and exc.status < 500:
                raise Refused(_detail(exc) or f"canopy refused (HTTP {exc.status})") from exc
            raise Unreachable(str(exc)) from exc
        run_id = (body or {}).get("run_id") if isinstance(body, dict) else None
        if not run_id:
            raise Unreachable("canopy started the run but named no run id")
        return str(run_id)

    def heartbeat(self, run_id: str, run_state: str, counts: dict) -> dict:
        try:
            _, body = self.client._call("POST", f"{self.prefix}/runs/{_quoted(run_id)}/heartbeat",
                                        {"state": run_state, "counts": counts}, retry=False)
        except ClientError as exc:
            if exc.status in GONE_STATUSES:
                raise RunGone(str(exc)) from exc
            raise Unreachable(str(exc)) from exc
        return body if isinstance(body, dict) else {}

    def finish(self, run_id: str, reason: str, counts: dict) -> None:
        self.client._call("POST", f"{self.prefix}/runs/{_quoted(run_id)}/finish",
                          {"reason": reason, "counts": counts}, retry=False)

    def canopy(self, run_id: str, op: str, args: dict) -> tuple[int, dict]:
        """(status, body). Never raises for an answer, so an app sees what the server
        said. Not re-sent here: the app decides, and only it knows what's safe to repeat."""
        try:
            _, answer = self.client._call("POST", f"{self.prefix}/runs/{_quoted(run_id)}/canopy",
                                          {"op": op, "args": args}, retry=False)
        except ClientError as exc:
            if exc.status is None:
                return UNREACHABLE, {"detail": "could not reach canopy"}
            return exc.status, {"detail": _detail(exc) or f"HTTP {exc.status}"}
        if not isinstance(answer, dict):
            return UNREACHABLE, {"detail": "canopy's answer made no sense"}
        body = answer.get("body")
        return int(answer.get("status") or 0), body if isinstance(body, dict) else {}


class ServerLink:
    """One run's calls, to the real server."""

    beats = True

    def __init__(self, server: RunnerServer, run_id: str):
        self.server, self.run_id = server, run_id

    def heartbeat(self, run_state: str, counts: dict) -> dict:
        return self.server.heartbeat(self.run_id, run_state, counts)

    def canopy(self, op: str, args: dict) -> tuple[int, dict]:
        return self.server.canopy(self.run_id, op, args)

    def finish(self, reason: str, counts: dict) -> None:
        self.server.finish(self.run_id, reason, counts)


class StubLink:
    """A dev-installed app's Canopy. Answers plausibly and writes down what would have
    been sent, in a private file, so nothing leaves the computer."""

    beats = False

    def __init__(self, key: str):
        self.key = key

    def heartbeat(self, run_state: str, counts: dict) -> dict:
        return {"stop": False, "reason": ""}

    def canopy(self, op: str, args: dict) -> tuple[int, dict]:
        state.ensure_dir(self.key)
        state.append_private_line(state.would_send_file(self.key),
                                  {"at": time.time(), "op": op, "args": args})
        if op == "sessions.create":
            session_id = str(uuid.uuid4())
            return 201, {"id": session_id, "url": f"http://localhost/unpublished/sessions/{session_id}"}
        if op == "sessions.send":
            return 200, {"turn_id": str(uuid.uuid4())}
        if op == "turns.status":
            return 200, {"status": "done"}
        if op == "meetings.save":
            return 200, {"id": str(uuid.uuid4())}
        return 400, {"detail": f"unknown op {op}"}

    def finish(self, reason: str, counts: dict) -> None:
        return None


def person_token() -> str:
    """A person's or agent's token for submitting. Never the runner's."""
    token = os.environ.get("CANOPY_WEB_PAT", "").strip()
    if not token:
        raise Refused("submit needs a person's or an agent's token: set CANOPY_WEB_PAT. "
                      "The runner's own token cannot publish.")
    return token


def submit(base_url: str, token: str, packed: bytes, note: str = "", *, workspace: str = "") -> dict:
    url = f"{base_url.rstrip('/')}/api/local-apps/submissions/"
    body = {"package_b64": base64.b64encode(packed).decode("ascii"), "note": note}
    if workspace:
        body["workspace"] = workspace
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    client_mod._identify(req)
    try:
        with urllib.request.urlopen(req, timeout=SUBMIT_TIMEOUT) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        wrapped = ClientError(str(exc), status=exc.code, body=exc.read()[:4096])
        raise Refused(_detail(wrapped) or f"canopy refused the submission (HTTP {exc.code})") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise Unreachable(f"could not reach canopy: {exc}") from exc
