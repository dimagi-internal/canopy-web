"""Signals the event log sends. A lower layer says what happened; whoever cares listens."""
from django.dispatch import Signal

# Sent with: sender=None, runner=<Runner>, item=<dict> (the posted event) for each event a
# box's runner token recorded that is the first of its (source, key) today. The harness
# listens to tell the people who can fix a box's credential problem (`credential_notify`).
runner_event_first_today = Signal()
