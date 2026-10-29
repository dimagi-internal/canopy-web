"""Keep request detail out of the logs: no client address, no concrete path, no query.

canopy-web's logs land in CloudWatch, where anyone debugging the service reads
them. A concrete path or query string is not safe there: `/share/<token>` and a
walkthrough's or storyboard's link carry the bearer token that opens them, a
WebSocket's `?ticket=` is a live credential, and a client address identifies a
person. What a log line may say about a request is its method, its route
TEMPLATE (`api/w/<str:ws>/sessions/<str:session_id>`, never the values),
its status and its timing.

Three things used to write more than that, and `RedactRequestDetail` closes each:

* uvicorn's access log — one line per request with the client address and the
  full path and query. Off in the Dockerfile (`--no-access-log`) and silenced in
  LOGGING; this filter drops any record that still reaches it.
* uvicorn's own `"WebSocket <path?query>" [accepted]` lines, logged on
  `uvicorn.error` with the client address. Rewritten to the folded path.
* `django.request` (every 4xx/5xx: `"Not Found: /share/<token>"`) and anything
  else that passes `extra={"request": request}` — the JSON formatter would
  otherwise serialize the request object, path and query included.

`path_template` is the one way to name a request's path in a log line.
"""
from __future__ import annotations

import logging
import re

#: A path segment kept as written: a lowercase word (letters, dots, dashes,
#: underscores; no digits). Everything else — ids, slugs with digits, tokens,
#: file names — is folded. Tokens are `secrets.token_urlsafe`, so an all-lowercase
#: letter-only one is vanishingly rare, and a fold costs only detail.
_WORD = re.compile(r"^[a-z][a-z._-]{0,39}$")
FOLDED = "<x>"

#: uvicorn's WebSocket handshake lines: `'%s - "WebSocket %s" …'` with args
#: (client address, path with query string, [status]).
_UVICORN_WS = '"WebSocket %s"'


def path_template(path, request=None) -> str:
    """The route template for a request's path, safe to log.

    The matched Django route when there is one (`request.resolver_match.route`
    names its parameters, never their values); otherwise the path with its query
    dropped and every segment that is not a plain word folded to `<x>`.
    """
    match = getattr(request, "resolver_match", None) if request is not None else None
    route = getattr(match, "route", None)
    if route:
        return "/" + route.lstrip("/")
    path = str(path or "").split("?", 1)[0].split("#", 1)[0]
    return "/".join(s if (not s or _WORD.match(s)) else FOLDED for s in path.split("/"))


def _request_path(request) -> str:
    return str(getattr(request, "path", "") or "")


class RedactRequestDetail(logging.Filter):
    """Drop access-log records; rewrite request detail in the rest (see module doc)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "uvicorn.access" or record.name.startswith("uvicorn.access."):
            return False
        try:
            self._scrub(record)
        except Exception:  # noqa: BLE001 — a log line must never fail on its scrub
            record.msg, record.args = "log record withheld: request detail could not be scrubbed", None
        return True

    def _scrub(self, record: logging.LogRecord) -> None:
        if (record.name.startswith("uvicorn") and isinstance(record.msg, str)
                and _UVICORN_WS in record.msg and isinstance(record.args, tuple)
                and len(record.args) >= 2):
            record.args = ("-", path_template(record.args[1]), *record.args[2:])
            return

        request = getattr(record, "request", None)
        if request is None or isinstance(request, str):
            return
        template = path_template(_request_path(request), request)
        if isinstance(record.args, tuple) and record.args:
            concrete = {_request_path(request)}
            full = getattr(request, "get_full_path", None)
            if callable(full):
                concrete.add(full())
            record.args = tuple(template if (isinstance(a, str) and a in concrete) else a
                                for a in record.args)
        # The JSON formatter writes every `extra=` attribute; the request object's
        # repr carries its full path and query string.
        record.request = f"{getattr(request, 'method', '')} {template}".strip()
