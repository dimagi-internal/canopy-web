"""Django wiring for the HOST half of the canopy SDK (optional; needs Django).

Add ``"canopy_sdk.django"`` to ``INSTALLED_APPS`` (app label ``canopy_host``),
set ``CANOPY_HOST`` (see ``canopy_sdk.django.conf``), run ``migrate``, and mount
what you need:

* ``canopy_sdk.django.views.token_endpoint`` — the jwt-bearer grant; or
  ``jwt_bearer_view(fallback)`` to put it in front of an existing token view
  (django-oauth-toolkit's ``TokenView``), leaving every other grant to it;
* ``views.jwks`` — the host's public key(s), for canopy to verify with;
* ``views.panel_token`` — the endpoint the widget mints through;
* ``views.site_document`` — ``site.json``, what this site asks of canopy's
  agents (``SITE_NEEDS``), signed; and ``canopy_sdk.django.site_agents.current()`` —
  canopy's signed list of the agents this site may show, cached, failing closed;
* ``canopy_sdk.django.asgi.dpop_gate(app)`` — wrap the MCP ASGI app;
* ``canopy_sdk.django.execution.verify_execution(request)`` — canopy's signed
  ``Canopy-Execution`` statement on a tool call, before releasing real data;
* ``{% load canopy_host %}{% canopy_panel resource=... %}`` — the panel.

Nothing here assumes the host is not canopy itself; every URL and id comes from
``CANOPY_HOST``.
"""
