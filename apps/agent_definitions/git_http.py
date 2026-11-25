"""A stored agent's repository over git's smart HTTP: clone and fetch only, never push.

    GET  /git/<workspace>/<slug>.git/info/refs?service=git-upload-pack
    POST /git/<workspace>/<slug>.git/git-upload-pack

A bare Django view, not a Ninja route: its addresses are what `git` asks for, it speaks
git's pkt-line protocol rather than JSON, and it must not become an MCP tool. It
authenticates itself (so `LoginRequiredMiddleware` admits `/git/`, and CSRF does not
apply — nothing here writes):

* the credential is a canopy token, sent as HTTP Basic (any username, the token as the
  password: what git's credential helpers send) or as `Authorization: Bearer <token>`
  (what the canopy CLI passes with `-c http.extraHeader`). A browser session is not one;
* only two kinds are taken: a PAT (a person's, or an agent's own login — a turn's
  login included) and a runner's own token. Every other kind — a contact's, a
  conversation's caller token (which is what a confined turn holds), a site's
  delegated token, an act-as-you token — is refused like no token at all: 401 with
  `WWW-Authenticate: Basic realm="canopy"`, so git asks for one;
* a person reads the repository of an agent in a workspace they are a member of; an
  agent's login reads its OWN agent's only; a runner reads the repository of an agent
  its box may hold (`runner_may_hold_agent`, as the archive and the credential resolve
  ask), and a `computer` runner none. Anything else is 404, as for a repository that
  does not exist;
* a runner is served `main` and the `r<N>` tags; a person and the agent's login also
  `draft` and `imported` (dulwich only sends what a served ref reaches);
* `git-receive-pack` is answered with a refusal for everyone, before any credential is
  read: canopy is the only writer, through `canopy agent definition edit`.

The token is read here, used for one lookup, and never logged or echoed. The pack is
streamed: dulwich's upload-pack runs on a worker thread over a repository read into
memory first, and the response yields what it writes as it writes it.
Doc: `docs/architecture/stored-agents.md` § Reading over git.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import gzip
import io
import logging
import queue
import threading
from collections.abc import AsyncIterator, Iterator

from django.core.handlers.asgi import ASGIRequest
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from dulwich.protocol import ReceivableProtocol, pkt_line
from dulwich.server import DictBackend, UploadPackHandler

from apps.agents import definition_git as git
from apps.agents import definition_store as store
from apps.agents.holding import runner_may_hold_agent
from apps.agents.models import Agent
from apps.harness.models import Runner
from apps.tokens import principal as P
from apps.tokens.models import client_label
from apps.workspaces import services as wsvc

logger = logging.getLogger(__name__)

UPLOAD_PACK = "git-upload-pack"
RECEIVE_PACK = "git-receive-pack"
PUSH_REFUSED = ("pushes are refused: canopy is the only writer of a stored agent's repository. "
                "Propose a change with `canopy agent definition edit`; a person who administers "
                "the agent publishes it")
#: The largest upload-pack request (wants and haves) taken, once ungzipped.
MAX_REQUEST_BYTES = 8 * 1024 * 1024


def _unauthorized() -> HttpResponse:
    resp = HttpResponse("Authenticate with a canopy token: as the password of HTTP Basic (any "
                        "username), or as `Authorization: Bearer <token>`.\n", status=401,
                        content_type="text/plain; charset=utf-8")
    resp["WWW-Authenticate"] = 'Basic realm="canopy"'
    return resp


def _not_found() -> HttpResponse:
    return HttpResponse("repository not found\n", status=404, content_type="text/plain; charset=utf-8")


def _raw_token(request: HttpRequest) -> str:
    header = request.META.get("HTTP_AUTHORIZATION", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer":
        return value.strip()
    if scheme.lower() == "basic":
        try:
            decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
        except (binascii.Error, ValueError):
            return ""
        _user, _, password = decoded.partition(":")
        return password.strip()
    return ""


class _Reader:
    """Who is reading: a person (`user`), an agent's login (`user` + `agent_login`), or a
    runner (`runner`)."""

    def __init__(self, *, user=None, runner=None):
        self.user = user
        self.runner = runner


def _reader(request: HttpRequest) -> _Reader | None:
    raw = _raw_token(request)
    if not raw:
        return None
    found = P.resolve_bearer(raw, accept=frozenset({P.PAT, P.RUNNER_KEY}), client=client_label(
        request.META.get("HTTP_USER_AGENT", ""), request.META.get("HTTP_X_CANOPY_CLIENT", "")))
    if found is None or found.user is None or not getattr(found.user, "is_active", False):
        return None
    if found.kind == P.RUNNER_KEY:
        return _Reader(user=found.user, runner=found.token.runner)
    return _Reader(user=found.user)


def _may_read(reader: _Reader, agent: Agent) -> bool:
    if reader.runner is not None:
        return reader.runner.kind != Runner.COMPUTER and runner_may_hold_agent(reader.runner, agent)
    logins = list(Agent.objects.filter(user_id=reader.user.pk).values_list("pk", flat=True))
    if logins:
        # An agent's login (its own, or a turn's) reads its own repository and no other.
        return agent.pk in logins
    return agent.workspace_id in wsvc.member_scope(reader.user)


def _served_refs(agent: Agent, reader: _Reader) -> dict[str, str]:
    refs = git.refs_of(agent.pk)
    if reader.runner is not None:
        # A box runs what a person published: never a draft, and it needs no imported history.
        return {n: s for n, s in refs.items() if n == git.MAIN or n.startswith(git.TAG_PREFIX)}
    return refs


def _agent(ws: str, slug: str) -> Agent | None:
    agent = Agent.objects.filter(slug=slug, workspace_id=ws).first()
    if agent is None or not store.has_repository(agent):
        return None
    return agent


def _push_refused_advertisement() -> HttpResponse:
    body = (pkt_line(f"# service={RECEIVE_PACK}\n".encode()) + pkt_line(None)
            + pkt_line(f"ERR {PUSH_REFUSED}\n".encode()))
    resp = HttpResponse(body, content_type=f"application/x-{RECEIVE_PACK}-advertisement")
    resp["Cache-Control"] = "no-cache"
    return resp


def _body(request: HttpRequest) -> bytes | None:
    data = request.body
    if request.META.get("HTTP_CONTENT_ENCODING", "").lower() == "gzip":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
                data = gz.read(MAX_REQUEST_BYTES + 1)
        except (OSError, EOFError):
            return None
    return None if len(data) > MAX_REQUEST_BYTES else data


def _producer(repo, body: bytes, *, advertise: bool) -> tuple[threading.Thread, queue.Queue]:
    """Run dulwich's upload-pack for `repo` on a worker thread; what it writes is put on
    the queue as it is written, then None. The repository is already in memory, so the
    thread touches no database connection."""
    out: queue.Queue = queue.Queue()

    def write(data: bytes) -> int:
        out.put(bytes(data))
        return len(data)

    def run() -> None:
        try:
            proto = ReceivableProtocol(io.BytesIO(body).read, write)
            handler = UploadPackHandler(DictBackend({b"/": repo}), [b"/"], proto, stateless_rpc=True,
                                        advertise_refs=advertise)
            if advertise:
                proto.write_pkt_line(f"# service={UPLOAD_PACK}\n".encode())
                proto.write_pkt_line(None)
            handler.handle()
        except Exception:  # noqa: BLE001 — the client sees a truncated response; we see why
            logger.exception("git upload-pack failed")
        finally:
            out.put(None)

    thread = threading.Thread(target=run, name="git-upload-pack", daemon=True)
    return thread, out


def _sync_chunks(thread: threading.Thread, out: queue.Queue) -> Iterator[bytes]:
    thread.start()
    while (chunk := out.get()) is not None:
        yield chunk


async def _async_chunks(thread: threading.Thread, out: queue.Queue) -> AsyncIterator[bytes]:
    thread.start()
    while (chunk := await asyncio.to_thread(out.get)) is not None:
        yield chunk


def _stream(request: HttpRequest, repo, body: bytes, *, advertise: bool, content_type: str) -> StreamingHttpResponse:
    thread, out = _producer(repo, body, advertise=advertise)
    chunks = _async_chunks(thread, out) if isinstance(request, ASGIRequest) else _sync_chunks(thread, out)
    resp = StreamingHttpResponse(chunks, content_type=content_type)
    resp["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
    resp["Expires"] = "Fri, 01 Jan 1980 00:00:00 GMT"
    resp["Pragma"] = "no-cache"
    return resp


@csrf_exempt
def serve(request: HttpRequest, ws: str, slug: str, rest: str) -> HttpResponse:
    service = request.GET.get("service", "")
    if rest == RECEIVE_PACK or (rest == "info/refs" and service == RECEIVE_PACK):
        # No credential is read and no repository looked up: a push is refused to everyone.
        if request.method == "GET":
            return _push_refused_advertisement()
        return HttpResponse(PUSH_REFUSED + "\n", status=403, content_type="text/plain; charset=utf-8")
    if rest == "info/refs":
        if request.method not in ("GET", "HEAD"):
            return HttpResponse(status=405)
        if service != UPLOAD_PACK:
            # The dumb protocol is not served: it would hand out objects without a walk.
            return HttpResponse("only git's smart HTTP protocol is served (git 1.6.6 or later)\n", status=403,
                                content_type="text/plain; charset=utf-8")
    elif rest == UPLOAD_PACK:
        if request.method != "POST":
            return HttpResponse(status=405)
        if request.META.get("CONTENT_TYPE", "").split(";")[0].strip() != f"application/x-{UPLOAD_PACK}-request":
            return HttpResponse("expected application/x-git-upload-pack-request\n", status=415,
                                content_type="text/plain; charset=utf-8")
    else:
        return _not_found()

    reader = _reader(request)
    if reader is None:
        return _unauthorized()
    agent = _agent(ws, slug)
    if agent is None or not _may_read(reader, agent):
        return _not_found()
    repo = git.serving_repo(store.object_store(agent).preload(), _served_refs(agent, reader))

    if rest == "info/refs":
        return _stream(request, repo, b"", advertise=True,
                       content_type=f"application/x-{UPLOAD_PACK}-advertisement")
    body = _body(request)
    if body is None:
        return HttpResponse("the request is too large or not valid gzip\n", status=413,
                            content_type="text/plain; charset=utf-8")
    return _stream(request, repo, body, advertise=False, content_type=f"application/x-{UPLOAD_PACK}-result")
