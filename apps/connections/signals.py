"""Receivers: a connection app deleted without `services.remove_app`.

Deleting a workspace deletes its apps, and their connections with them
(CASCADE), without revoking anything at the provider. This records each such
connection as `disconnected` (`during=app_deleted`, no `by`) before it goes, so
the audit trail is not silent about it. No network call: it runs inside someone
else's delete.
"""
from __future__ import annotations

from django.db.models.signals import pre_delete
from django.dispatch import receiver

from apps.workspaces.models import Workspace

from . import services
from .models import ConnectionApp


@receiver(pre_delete, sender=ConnectionApp, dispatch_uid="connections.app_deleted")
def record_connections_going_with_an_app(sender, instance: ConnectionApp, origin=None, **kwargs) -> None:
    # `origin` is what the delete was called on: the workspace, when it is the one going.
    going = isinstance(origin, Workspace) and origin.pk == instance.workspace_id
    services.app_deleted(instance, workspace_going=going)
