"""Run one pass of the off-request clock by hand (apps/common/ticks.py).

The web process runs it on its own every TICK_BASE_S; this is for an operator
or a debugging session. `--list` prints the registered jobs and their intervals.
"""
from django.core.management.base import BaseCommand

from apps.common import ticks


class Command(BaseCommand):
    help = "Run every registered off-request job that is due, once."

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true", help="list the jobs instead of running them")

    def handle(self, *args, **opts):
        if opts["list"]:
            for name, job in ticks.jobs().items():
                self.stdout.write(f"{name}\tevery {job.interval():g}s\tdedupe={job.dedupe}")
            return
        ran = ticks.tick()
        self.stdout.write("ran: " + (", ".join(ran) or "nothing due"))
