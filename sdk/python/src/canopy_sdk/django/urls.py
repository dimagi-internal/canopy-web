"""``path("canopy/", include("canopy_sdk.django.urls"))``.

The token endpoint is here for a host with no OAuth server of its own. A host
that runs one (django-oauth-toolkit) mounts ``views.jwt_bearer_view`` on its
existing token URL instead, so canopy discovers ONE token endpoint from the
host's RFC 8414 metadata. The ``.well-known`` documents must be served at the
site root (RFC 8414 / 9728), so they are not in this include; mount
``views.authorization_server_metadata_view`` and
``views.protected_resource_metadata_view`` there if the host serves none.
"""
from django.urls import path

from . import views

app_name = "canopy_host"

urlpatterns = [
    path("token/", views.token_endpoint, name="token"),
    path("jwks.json", views.jwks, name="jwks"),
    path("panel-token/", views.panel_token, name="panel_token"),
]
