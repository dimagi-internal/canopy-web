from django.conf import settings
from django.db import models


class LetGo(models.Model):
    """A person setting something aside without deciding it.

    Not a decline and not a "done": the agent is told nothing and the thing is
    unchanged. It only leaves THIS person's attention, until something new
    happens on it (its `last_activity_at` passes `let_go_at`) or, when `until`
    is set, until then — whichever comes first.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="let_gos")
    key = models.CharField(max_length=200)
    let_go_at = models.DateTimeField()
    until = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "key"], name="attention_letgo_user_key")]
