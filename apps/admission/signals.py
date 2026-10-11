"""A new workspace starts with the default admission rules (`defaults.py`)."""
from __future__ import annotations

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.workspaces.models import Workspace


@receiver(post_save, sender=Workspace, dispatch_uid="admission.seed_new_workspace")
def seed_new_workspace(sender, instance, created, raw=False, **kwargs):
    # A post_save rather than a call in each create path: workspaces are made
    # by the API, the default-workspace bootstrap, fixtures and the admin, and a
    # path that forgot would leave a workspace silently without the default.
    if not created or raw:
        return
    from . import defaults

    defaults.seed(instance.pk)
