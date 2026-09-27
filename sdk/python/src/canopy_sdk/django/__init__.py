"""Django wiring for the HOST half of the canopy SDK (optional; needs Django).

Add ``"canopy_sdk.django"`` to ``INSTALLED_APPS`` (app label ``canopy_host``),
set ``CANOPY_HOST`` (see ``canopy_sdk.django.conf``), run ``migrate``, and mount
what you need:

* ``canopy_sdk.django.views.token_endpoint`` — the jwt-bearer grant; or
  ``jwt_bearer_view(fallback)`` to put it in front of an existing token view
  (django-oauth-toolkit's ``TokenView``), leaving every other grant to it;
* ``views.jwks`` — the host's public key(s), for canopy to verify with;
* ``views.panel_token`` — the endpoint the widget mints through;
* ``canopy_sdk.django.asgi.dpop_gate(app)`` — wrap the MCP ASGI app;
* ``{% load canopy_host %}{% canopy_panel resource=... %}`` — the panel.

Nothing here assumes the host is not canopy itself; every URL and id comes from
``CANOPY_HOST``.
"""
