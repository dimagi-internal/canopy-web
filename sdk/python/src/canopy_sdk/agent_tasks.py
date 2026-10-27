"""Call a canopy agent inline and get a structured, schema-validated result back.

A page's backend (or any script) hands an agent a task — a name, its input and the
JSON Schema of the answer it needs — and waits for the result::

    from canopy_sdk.agent_tasks import AgentTasks

    tasks = AgentTasks("https://canopy.dimagi.com", token=os.environ["CANOPY_PAT"])
    result = tasks.run("bailey", "gmail-triage.review", {"emails": emails}, SCHEMA)

canopy queues one turn for the agent; the task is done only when the agent submits a
result that validates against the schema (never because its turn ended). ``run``
raises ``AgentTaskFailed`` when the task failed or was cancelled and
``AgentTaskTimeout`` when it did not finish in time. The token is a canopy personal
access token (sent as ``Authorization: Bearer``) of someone who may run a turn for
the agent. Contract: canopy-web ``docs/architecture/agent-tasks.md``.

Standard library only, through ``canopy_sdk.fetch`` (https, address-pinned, no
redirects). Pass ``transport`` to use something else — a test double, or a plain
``http://localhost`` canopy in development.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

from . import fetch

#: The statuses a task ends in.
TERMINAL = frozenset({"done", "failed", "cancelled"})
#: The server's long-poll ceiling (``GET /api/agent-tasks/{id}?wait_s=``).
MAX_WAIT_S = 20
#: The largest answer read (a result may be up to 1 MiB of JSON; the rest is the envelope).
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
#: How long ``run`` waits beyond the task's own timeout before giving up: the server
#: fails the task at its deadline, so this is only slack for the last poll.
WAIT_SLACK_S = 60

_monotonic = time.monotonic

#: ``transport(method, url, body, headers, timeout) -> (status, raw body)``.
Transport = Callable[[str, str, "bytes | None", dict, float], "tuple[int, bytes]"]


class AgentTaskError(Exception):
    """canopy refused a request (``status`` is the HTTP status, ``detail`` its RFC 7807
    detail, ``errors`` the ``{path, message}`` list of a refused result), or could not
    be reached (``status`` is None)."""

    def __init__(self, message: str, *, status: int | None = None, detail: str = "",
                 errors: list | None = None, task: Mapping | None = None):
        super().__init__(message)
        self.status = status
        self.detail = detail
        self.errors = list(errors or [])
        self.task = dict(task or {})


class AgentTaskFailed(AgentTaskError):
    """The task ended ``failed`` or ``cancelled``; ``task["error"]`` says why."""


class AgentTaskTimeout(AgentTaskError):
    """The task had not ended when the wait ran out (it may still finish)."""


def _default_transport(method: str, url: str, body: bytes | None, headers: dict,
                       timeout: float) -> tuple[int, bytes]:
    try:
        status, raw, _ = fetch._request(method, url, body=body, headers=headers, timeout=timeout,
                                        max_bytes=MAX_RESPONSE_BYTES)
    except fetch.FetchError as exc:
        raise AgentTaskError(str(exc)) from exc
    return status, raw


class AgentTasks:
    """A client for ``/api/agent-tasks/`` on one canopy, with one token.

    ``workspace`` (a slug) sends every call through ``/api/w/<workspace>/``, for a
    token whose person belongs to workspaces in more than one organisation."""

    def __init__(self, base_url: str, token: str, *, workspace: str = "",
                 transport: Transport | None = None, request_timeout_s: float = 30):
        if not base_url or not token:
            raise ValueError("AgentTasks needs canopy's base URL and a token")
        root = base_url.rstrip("/") + "/api"
        if workspace:
            root += f"/w/{quote(workspace, safe='')}"
        self._root = root + "/agent-tasks/"
        self._token = token
        self._transport = transport or _default_transport
        self._timeout = request_timeout_s

    # --- the routes ------------------------------------------------------------

    def create(self, agent: str, name: str, input: Any, output_schema: Mapping, *,
               instructions: str = "", routing: str = "prefer_local", timeout_s: int = 900,
               parent: Mapping | None = None) -> dict:
        """Create a task (``POST /api/agent-tasks/``); returns it, ``status`` queued."""
        body: dict[str, Any] = {"agent": agent, "name": name, "input": input,
                                "output_schema": dict(output_schema), "instructions": instructions,
                                "routing": routing, "timeout_s": int(timeout_s)}
        if parent:
            body["parent"] = dict(parent)
        return self._call("POST", self._root, body)

    def get(self, task_id: str, wait_s: int = 0) -> dict:
        """Read a task. ``wait_s`` (0–20) long-polls until its status changes."""
        wait_s = max(0, min(int(wait_s), MAX_WAIT_S))
        url = f"{self._root}{quote(str(task_id), safe='')}"
        if wait_s:
            url += f"?wait_s={wait_s}"
        return self._call("GET", url, None, timeout=self._timeout + wait_s)

    def cancel(self, task_id: str) -> dict:
        """Stop waiting for a task (its creator, or the agent's admins)."""
        return self._call("POST", f"{self._root}{quote(str(task_id), safe='')}/cancel", {})

    # --- waiting ---------------------------------------------------------------

    def wait(self, task_id: str, timeout_s: float) -> dict:
        """Long-poll until the task ends; returns it (whatever its ending). Raises
        ``AgentTaskTimeout`` if it has not ended within ``timeout_s``."""
        deadline = _monotonic() + timeout_s
        while True:
            remaining = deadline - _monotonic()
            task = self.get(task_id, wait_s=int(max(0, min(MAX_WAIT_S, remaining))))
            if task.get("status") in TERMINAL:
                return task
            if _monotonic() >= deadline:
                raise AgentTaskTimeout(f"agent task {task_id} has not finished after {timeout_s:g}s",
                                       task=task)

    def run(self, agent: str, name: str, input: Any, output_schema: Mapping, instructions: str = "",
            timeout_s: int = 900, **create_kwargs) -> Any:
        """Create a task, wait for it, and return its ``result``. Raises
        ``AgentTaskFailed`` (failed or cancelled; ``.task["error"]`` says why) or
        ``AgentTaskTimeout``."""
        task = self.create(agent, name, input, output_schema, instructions=instructions,
                           timeout_s=timeout_s, **create_kwargs)
        task = self.wait(task["id"], timeout_s + WAIT_SLACK_S)
        if task["status"] != "done":
            raise AgentTaskFailed(f"agent task {task['id']} {task['status']}: {task.get('error') or 'no reason given'}",
                                  task=task)
        return task.get("result")

    # --- HTTP ------------------------------------------------------------------

    def _call(self, method: str, url: str, body: Mapping | None, *, timeout: float | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        status, raw = self._transport(method, url, data, headers, timeout or self._timeout)
        try:
            doc = json.loads(raw or b"{}")
        except (ValueError, UnicodeDecodeError):
            doc = {}
        if not 200 <= status < 300:
            doc = doc if isinstance(doc, dict) else {}
            detail = str(doc.get("detail") or doc.get("title") or "")[:500]
            raise AgentTaskError(f"{method} {url} answered {status}" + (f": {detail}" if detail else ""),
                                 status=status, detail=detail, errors=doc.get("errors"))
        if not isinstance(doc, dict):
            raise AgentTaskError(f"{method} {url} did not answer with a JSON object", status=status)
        return doc
