"""Database-backed stores for the host half: shared across workers, fail closed."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.db import IntegrityError, transaction

from ..stores import IssuedToken, Replayed, jti_key


def _dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, timezone.utc)


class DjangoJtiStore:
    """``JtiStore`` over ``SeenJti``: a unique constraint makes single use hold
    across every worker; all-or-nothing inside one transaction."""

    FLOOR = timedelta(minutes=5)

    def consume(self, entries):
        from .models import SeenJti

        now = datetime.now(timezone.utc)
        rows = [SeenJti(key=jti_key(kind, jti), expires_at=max(_dt(exp), now) + self.FLOOR)
                for kind, jti, exp in entries]
        try:
            with transaction.atomic():
                for row in rows:
                    row.save(force_insert=True)
        except IntegrityError as exc:
            raise Replayed() from exc

    def prune(self):
        from .models import SeenJti

        SeenJti.objects.filter(expires_at__lt=datetime.now(timezone.utc)).delete()


class DjangoTokenStore:
    def save(self, token: IssuedToken) -> None:
        from .models import DelegatedToken

        DelegatedToken.objects.create(
            token_checksum=token.token_checksum, subject=token.subject, client_id=token.client_id,
            actor=token.actor, scope=token.scope, cnf_jkt=token.cnf_jkt, grant_jti=token.grant_jti,
            expires_at=_dt(token.expires_at), created_at=_dt(token.created_at),
        )

    def get(self, token_checksum: str) -> IssuedToken | None:
        from .models import DelegatedToken

        row = DelegatedToken.objects.filter(token_checksum=token_checksum).first()
        if row is None:
            return None
        return IssuedToken(
            token_checksum=row.token_checksum, subject=row.subject, client_id=row.client_id,
            actor=row.actor, scopes=tuple(row.scope.split()), cnf_jkt=row.cnf_jkt,
            grant_jti=row.grant_jti, expires_at=row.expires_at.timestamp(),
            created_at=row.created_at.timestamp(),
        )

    def prune(self) -> None:
        from .models import DelegatedToken

        DelegatedToken.objects.filter(expires_at__lt=datetime.now(timezone.utc)).delete()
