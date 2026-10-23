"""Host-held results for a Django host: the store, the settings, the sweep, and the
MCP ``resources/read`` handler (``canopy_sdk.host.artifacts`` is the logic).

Install: ``"canopy_sdk.django"`` in ``INSTALLED_APPS`` (already there for the grant)
and ``migrate`` — 0002 adds the ``canopy_host_artifact`` table. Turn it on with
``CANOPY_HOST["ARTIFACTS"] = {"SCHEME": "labs-result"}`` (``canopy_sdk.django.conf``);
``site.json`` then declares the scheme, so canopy keeps the links.

In a tool::

    from canopy_sdk.django import artifacts
    from canopy_sdk.host.artifacts import summarize_rows

    link = artifacts.save("query", {"columns": cols, "rows": rows},
                          tool_call_id=call_id, name="Query result")
    return [TextContent(type="text", text=json.dumps(summarize_rows(cols, rows))), link]

On the MCP server, ``resources/read`` for the scheme, as the reader:

* FastMCP: ``artifacts.mount_fastmcp(mcp)`` (a resource template
  ``<scheme>://{artifact_id}{?page,page_size}``, also listed by
  ``resources/templates/list``);
* any other server: answer ``resources/read`` for a URI under the scheme with
  ``artifacts.resource_handler(uri)`` (MCP ``contents`` dicts) and list
  ``artifacts.template()``.

The owner and the reader are the subject the delegated call runs as: the
``DPoPGate``'s resolved principal (``dpop_gate(app, require_principal=True)``), or
``ARTIFACTS["READER"]`` for a host whose own token verifier resolves it. Sweep with
``manage.py canopy_artifacts_sweep`` (or call ``sweep()``) on a schedule.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from django.db import IntegrityError
from django.utils.module_loading import import_string

from ..host.artifacts import Artifact, ArtifactRefused, Artifacts, current_subject
from ..host.config import HostNotConfigured
from ..resources import ResourceError
from . import conf

log = logging.getLogger("canopy_sdk.django")


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


class DjangoArtifactStore:
    """``canopy_sdk.host.artifacts.ArtifactStore`` over the ``Artifact`` model."""

    @staticmethod
    def _row(a) -> Artifact:
        return Artifact(id=a.artifact_id, scheme=a.scheme, owner=a.owner, kind=a.kind, payload=a.payload,
                        tool_call_id=a.tool_call_id, name=a.name, description=a.description,
                        mime_type=a.mime_type, created_at=a.created_at.timestamp(),
                        expires_at=a.expires_at.timestamp())

    def put(self, artifact: Artifact) -> None:
        from .models import Artifact as Row

        try:
            Row.objects.create(artifact_id=artifact.id, scheme=artifact.scheme, owner=artifact.owner,
                               kind=artifact.kind[:100], tool_call_id=artifact.tool_call_id[:255],
                               name=artifact.name[:200], description=artifact.description[:500],
                               mime_type=artifact.mime_type[:100], payload=artifact.payload,
                               created_at=_dt(artifact.created_at), expires_at=_dt(artifact.expires_at))
        except IntegrityError as exc:  # 144 random bits colliding: say so, never overwrite
            raise RuntimeError("artifact id collision; save again") from exc

    def get(self, scheme: str, artifact_id: str) -> Artifact | None:
        from .models import Artifact as Row

        row = Row.objects.filter(scheme=scheme, artifact_id=artifact_id).first()
        return self._row(row) if row is not None else None

    def set_payload(self, scheme: str, artifact_id: str, payload: Any) -> bool:
        from .models import Artifact as Row

        return Row.objects.filter(scheme=scheme, artifact_id=artifact_id).update(payload=payload) > 0

    def delete_expired(self, now: float) -> int:
        from .models import Artifact as Row

        deleted, _ = Row.objects.filter(expires_at__lte=_dt(now)).delete()
        return deleted


def _hook(settings: dict, name: str):
    value = settings.get(name)
    if not value:
        return None
    return import_string(value) if isinstance(value, str) else value


def artifacts() -> Artifacts:
    """The host's ``Artifacts``, built from ``CANOPY_HOST["ARTIFACTS"]`` now (so
    ``override_settings`` takes effect). ``HostNotConfigured`` while it is unset."""
    settings = conf.artifacts_settings()
    if not settings.get("SCHEME"):
        raise HostNotConfigured("CANOPY_HOST['ARTIFACTS']['SCHEME'] is not set")
    return Artifacts(DjangoArtifactStore(), scheme=settings["SCHEME"],
                     ttl=int(settings.get("TTL") or 24 * 60 * 60),
                     extra_schemes=settings.get("EXTRA_SCHEMES") or (),
                     mime_type=settings.get("MIME_TYPE") or "application/json",
                     may_read=_hook(settings, "MAY_READ"), on_read=_hook(settings, "ON_READ"),
                     notify=((lambda uris: _best_effort(notify_updated, uris))
                             if settings.get("NOTIFY", True) else None))


def current_reader() -> str | None:
    """Who the current MCP request acts for: ``ARTIFACTS["READER"]()`` if set, else
    the ``DPoPGate``'s delegated principal (``require_principal=True``)."""
    reader = _hook(conf.artifacts_settings(), "READER")
    return reader() if reader is not None else current_subject()


def owner_for(user=None) -> str | None:
    """The owner of what this call saves: the delegated subject, else ``user``'s
    subject as this host signs it (``SUBJECT_FOR_USER``)."""
    subject = current_reader()
    if subject:
        return str(subject)
    if user is not None and getattr(user, "is_authenticated", False):
        return conf.subject_for_user(user)
    return None


def save(kind: str, payload: Any, *, owner: str | None = None, user=None, tool_call_id: str = "",
         ttl: int | None = None, scheme: str | None = None, name: str = "", description: str = "",
         mime_type: str | None = None) -> dict:
    """Keep ``payload`` for the person this call runs as and return the MCP
    ``resource_link`` to put in the tool result beside the facts."""
    who = owner or owner_for(user)
    if not who:
        raise ValueError("no owner: pass owner= or user=, or save inside a delegated MCP request")
    return artifacts().save(who, kind, payload, tool_call_id=tool_call_id, ttl=ttl, scheme=scheme,
                            name=name, description=description, mime_type=mime_type)


def read(uri: str, reader: str | None = None) -> Artifact:
    """The artifact, as ``reader`` (default: the current request's) may see it, or
    ``ArtifactRefused``."""
    return artifacts().read(uri, reader if reader is not None else current_reader())


def resource_handler(uri: str, reader: str | None = None) -> list[dict]:
    """``resources/read`` for one of this host's artifact URIs: the MCP ``contents``,
    read as ``reader`` (default: the current request's). Raises ``ArtifactRefused``
    — answer it as an MCP error whose message is ``str(exc)`` (``"<code>: <why>"``)."""
    return artifacts().contents(uri, reader if reader is not None else current_reader())


def template() -> dict:
    """The ``resources/templates/list`` entry for this host's artifacts."""
    return artifacts().template()


def update(uri: str, payload: Any) -> bool:
    """Replace an artifact's payload and tell canopy, so subscribed Views re-read."""
    return artifacts().update(uri, payload)


def sweep() -> int:
    """Delete expired artifacts (all schemes); how many went."""
    import time

    return DjangoArtifactStore().delete_expired(time.time())


def notify_updated(uris) -> int:
    """POST canopy a signed "these changed" (``canopy_sdk.resources.notify_updated``),
    signed with ``SIGNING_KEY`` as ``APP_NAME`` and addressed to ``CANOPY_BASE_URL``.
    Raises ``ResourceError``; ``update`` calls it best-effort."""
    config = conf.get_host_config()
    if not (config.canopy_base_url and config.app_name):
        raise ResourceError("not_configured", "CANOPY_HOST needs CANOPY_BASE_URL and APP_NAME to notify canopy")
    from ..resources import notify_updated as send

    return send(config.canopy_base_url, config.app_name, config.signing_key, list(uris),
                workspace=config.workspace, agent="" if config.workspace else conf.agent_slug())


def _best_effort(fn, uris) -> None:
    try:
        fn(uris)
    except Exception as exc:  # noqa: BLE001 - a View shows its last read until it reads again
        log.warning("could not tell canopy %d resource(s) changed: %s", len(uris), exc)


def mount_fastmcp(server, *, name: str = "results", description: str = "") -> object:
    """Register this host's artifacts on a FastMCP server: a resource template
    ``<scheme>://{artifact_id}{?page,page_size}`` that answers ``resources/read``
    as the reader (``current_reader``), with each refusal an MCP error. Returns
    the registered function. Needs FastMCP with RFC 6570 query templates (2.12+)."""
    settings = conf.artifacts_settings()
    if not settings.get("SCHEME"):
        raise HostNotConfigured("CANOPY_HOST['ARTIFACTS']['SCHEME'] is not set")
    from asgiref.sync import sync_to_async

    try:
        from fastmcp.exceptions import ResourceError as McpResourceError
    except ImportError:  # pragma: no cover - older FastMCP
        McpResourceError = ValueError  # noqa: N806
    scheme = artifacts().scheme
    mime = settings.get("MIME_TYPE") or "application/json"

    def read_sync(uri: str, reader: str | None) -> list[dict]:
        return resource_handler(uri, reader)

    async def artifact(artifact_id: str, page: str = "", page_size: str = ""):
        query = "&".join(f"{k}={v}" for k, v in (("page", page), ("page_size", page_size)) if v)
        uri = f"{scheme}://{artifact_id}" + (f"?{query}" if query else "")
        reader = current_reader()  # read here: contextvars do not cross into the thread below
        try:
            contents = await sync_to_async(read_sync, thread_sensitive=True)(uri, reader)
        except ArtifactRefused as exc:
            raise McpResourceError(str(exc)) from None
        return _fastmcp_result(contents)

    artifact.__name__ = f"{scheme.replace('-', '_').replace('.', '_').replace('+', '_')}_artifact"
    return server.resource(f"{scheme}://{{artifact_id}}{{?page,page_size}}", name=name, mime_type=mime,
                           description=description or "A result a tool produced, kept for the "
                                                      "person it ran as.")(artifact)


def _fastmcp_result(contents: list[dict]):
    try:
        from fastmcp.resources import ResourceContent, ResourceResult
    except ImportError:  # pragma: no cover - older FastMCP: text only, no _meta
        return contents[0]["text"]
    return ResourceResult(contents=[ResourceContent(content=c["text"], mime_type=c["mimeType"],
                                                    meta=c.get("_meta")) for c in contents])
