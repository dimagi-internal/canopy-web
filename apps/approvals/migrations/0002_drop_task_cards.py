from django.db import migrations


class Migration(migrations.Migration):
    """Approval cards were first derived from board tasks (a suggested task with `on_approve`).
    They are now explicit requests with a fixed kind (0003), so the task-keyed tables go. Only
    test cards ever used them."""

    dependencies = [("approvals", "0001_initial")]

    operations = [
        migrations.DeleteModel(name="ApprovalDecision"),
        migrations.DeleteModel(name="ApprovalDelivery"),
    ]
