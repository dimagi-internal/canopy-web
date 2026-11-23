"""A host site's surfaces: the parts of ONE site that serve different people.

A site declares them in its signed `site.json` (`surfaces`, `canopy_sdk.site.SiteSurface`):
each says whether its conversations hold the site's sensitive data, the MCP resource a
contact's subject-bound calls go to, and the scheme of its people's records. A site with none
is one surface, itself. Connect Labs is the example: its pages (staff, act-as-you grants,
sensitive) are the site itself, and `workers` (Connect Workers relayed over PersonalID, not
sensitive) is a surface of it, signed with the same key.

An agent's host-site entry names the surface it serves (`surface:`,
`published_profiles.surface_of`). The rules, each asked here and nowhere else:

* **A contact holds a conversation only on a surface the site declares `sensitive: false`**
  (`for_contact`, asked by `contact_api.start_session`), reached through a `contact_scoped`
  entry that names it. That conversation is not sensitive, so the arrival must carry no
  `sensitive_data` requirement; a site that declares surfaces offers a contact no subject
  tools through an entry that names none.
* **A contact's subject-bound calls go to its surface's resource, never the site's own**
  (`subject_resource`, asked by `host_gateway._subject_context`): the conversation's stamped
  surface must still be the entry's, still declared and not sensitive, and its resource on the
  origin of the site's registered MCP server, which only the workspace owner sets. So a contact
  never reaches the site's pages' tools, and the site cannot point canopy at another host.

Read LIVE from the verified, cached document (`site_needs.needs_for`), like `person_scheme`;
every rule fails closed when the document cannot be read.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from . import site_needs

#: Where a session records the surface its conversation is held on: server-owned
#: (`canopy_sessions.services.SERVER_OWNED_METADATA`), stamped by `contact_api.start_session`.
SESSION_KEY = "host_surface"


class SurfaceRefused(Exception):
    """A contact's conversation or call a surface rule refuses. `code` names which."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def declared(app) -> tuple | None:
    """The site's surfaces as canopy last verified its `site.json`, or None when it has no
    verifiable document (an empty tuple: it declares none)."""
    if app is None:
        return None
    document = site_needs.needs_for(app).document
    return None if document is None else document.surfaces


def _named(app, name: str):
    surfaces = declared(app)
    if surfaces is None:
        raise SurfaceRefused("surface_unknown", f"canopy cannot read {app.name}'s site.json, so it "
                                                f"cannot tell what its {name!r} surface holds")
    surface = next((s for s in surfaces if s.name == name), None)
    if surface is None:
        raise SurfaceRefused("surface_unknown", f"{app.name} declares no surface {name!r} in its site.json")
    if surface.sensitive:
        raise SurfaceRefused("surface_sensitive", f"{app.name}'s {name!r} surface holds its sensitive "
                                                  "data, which a contact cannot hold a conversation over")
    return surface


def for_contact(app, agent):
    """The surface a contact's new conversation with `agent` on `app` is held on (a
    `canopy_sdk.site.SiteSurface`), or None for the site itself. Raises `SurfaceRefused` for
    one a contact may not hold: a surface the site does not declare, or declares sensitive, or
    a `contact_scoped` entry naming none on a site that declares surfaces."""
    from apps.agents import published_profiles  # noqa: PLC0415 — as `host_gateway`: loads with a call

    name = published_profiles.surface_of(agent, app.name)
    if not name:
        if published_profiles.contact_scoped(agent, app.name) and declared(app):
            raise SurfaceRefused("no_surface", f"{app.name} serves its contacts on a surface of its own; "
                                               f"{agent.slug}'s entry for it names none")
        return None
    return _named(app, name)


def subject_resource(app, cap: dict, session) -> str:
    """The MCP resource a contact's subject-bound call goes to, for the turn's entry `cap` in
    `session`: the entry's surface's, or the site's own registered resource when neither the
    entry nor the site names a surface. Raises `SurfaceRefused`."""
    name = str((cap or {}).get("surface") or "")
    held = str(((getattr(session, "metadata", None) or {}).get(SESSION_KEY)) or "")
    if name != held:
        raise SurfaceRefused("surface_changed", f"this conversation is held on {held or app.name!r}, "
                                                f"and the agent's entry now serves {name or app.name!r}")
    site_resource = (app.host_mcp_resource or "").strip()
    if not name:
        if declared(app):
            raise SurfaceRefused("no_surface", f"{app.name} serves its contacts on a surface of its own, "
                                               "and this conversation is held on none")
        return site_resource
    surface = _named(app, name)
    if not surface.mcp_resource:
        raise SurfaceRefused("no_resource", f"{app.name}'s {name!r} surface names no MCP resource")
    if not site_resource or _origin(surface.mcp_resource) != _origin(site_resource):
        raise SurfaceRefused("foreign_resource", f"{app.name}'s {name!r} surface names a resource "
                                                 "off the origin of its registered MCP server")
    return surface.mcp_resource


def person_scheme_for(app, record_uri: str = "") -> str:
    """The person scheme a `record_uri` from `app` is checked under: the declared scheme it
    starts with (the site's own `person_scheme` or one of its surfaces'), else the site's own
    (so the refusal names it). "" when the site declares none."""
    facts = site_needs.site_facts_of(app)
    own = facts.person_scheme if facts is not None else ""
    schemes = [own, *(s.person_scheme for s in (declared(app) or ()))]
    uri = str(record_uri or "").lower()
    match = next((sc for sc in schemes if sc and uri.startswith(f"{sc}://")), "")
    return match or own or next((sc for sc in schemes if sc), "")


def _origin(url: str) -> str:
    parts = urlsplit(url or "")
    return f"{parts.scheme}://{parts.netloc}".lower()
