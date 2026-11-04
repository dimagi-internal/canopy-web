"""Run the connections sweep once (`keeper.sweep`), as the off-request clock does."""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.connections import keeper


class Command(BaseCommand):
    help = ("Delete revoking connections no holder confirmed within seven days (or that no cloud box holds), "
            "and removed connection apps with no connection left.")

    def handle(self, *args, **options) -> None:
        out = keeper.sweep()
        self.stdout.write(" ".join(f"{k}={v}" for k, v in out.items()))
