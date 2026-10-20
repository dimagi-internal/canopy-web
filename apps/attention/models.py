from django.conf import settings
from django.db import models


class Hidden(models.Model):
    """Something a person has taken off their own list, without deciding it.

    Not a decline, not a close, not a "done": the task, chat or request is
    unchanged and nobody is told. It only leaves THIS person's list, until
    something new happens on it (its `last_activity_at` passes `hidden_at`) or,
    when `until` is set, until then — whichever comes first.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="hidden_attention")
    key = models.CharField(max_length=200)
    hidden_at = models.DateTimeField()
    until = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "key"], name="attention_hidden_user_key")]
