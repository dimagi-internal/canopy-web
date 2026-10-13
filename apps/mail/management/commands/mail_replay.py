from botocore.exceptions import ClientError
from django.core.management.base import BaseCommand, CommandError

from apps.mail import inbound, ses


class Command(BaseCommand):
    help = (
        "Re-handle a raw inbound message still in S3 (incoming/<key>, kept 7 days) through the "
        "same path as a live SES notification: parse, resolve each recipient, deliver to the "
        "agent or forward. Safe to repeat: agent mail dedupes per mailbox on Message-ID and a "
        "forward is sent at most once per recipient, so a copy that already arrived is "
        "reported as `duplicate`, never delivered twice. Recipients default to the To/Cc/"
        "Delivered-To addresses on one of our mail domains; --recipient overrides (needed "
        "for a Bcc recipient). Prints one outcome line per recipient."
    )

    def add_arguments(self, parser):
        parser.add_argument("key", help="the S3 object key, e.g. incoming/abc123")
        parser.add_argument("--recipient", action="append", default=[], metavar="ADDR",
                            help="deliver to this address only (repeatable)")
        parser.add_argument("--dry-run", action="store_true",
                            help="show where each recipient's copy would go; deliver nothing")

    def handle(self, *args, key, recipient, dry_run, **opts):
        try:
            out = inbound.replay(key, recipient, dry_run=dry_run)
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        except ses.MailOff as exc:
            raise CommandError(str(exc)) from None
        except inbound.Transient as exc:
            self._print(exc.outcomes)
            raise CommandError("a transient failure stopped some recipients; run it again") from None
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "error")
            if code in ("NoSuchKey", "404"):
                raise CommandError(f"{key} is not in the bucket (incoming/ objects expire after 7 days)") from None
            raise CommandError(f"S3 refused: {code}") from None
        self._print(out)

    def _print(self, outcomes):
        for o in outcomes:
            self.stdout.write(f"{o['recipient']}: {o['outcome']}")
