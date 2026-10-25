from django.db import migrations, models


def backfill(apps, schema_editor):
    """Approximate the size of rows stored before sizes were recorded: text + html + their
    attachments. The raw MIME object's size was never kept, so it is left out."""
    MailMessage = apps.get_model("mail", "MailMessage")
    MailAttachment = apps.get_model("mail", "MailAttachment")
    att: dict[int, int] = {}
    for message_id, size in MailAttachment.objects.values_list("message_id", "size").iterator():
        att[message_id] = att.get(message_id, 0) + (size or 0)
    batch = []
    for m in MailMessage.objects.only("pk", "text", "html").iterator(chunk_size=500):
        m.size_bytes = (len((m.text or "").encode()) + len((m.html or "").encode())
                        + att.get(m.pk, 0))
        batch.append(m)
        if len(batch) >= 500:
            MailMessage.objects.bulk_update(batch, ["size_bytes"])
            batch = []
    if batch:
        MailMessage.objects.bulk_update(batch, ["size_bytes"])


class Migration(migrations.Migration):

    dependencies = [
        ("mail", "0006_agent_addresses"),
    ]

    operations = [
        migrations.AddField(
            model_name="mailmessage",
            name="size_bytes",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AlterField(
            model_name="mailattachment",
            name="s3_key",
            field=models.CharField(blank=True, default="", max_length=512),
        ),
        migrations.AddField(
            model_name="mailattachment",
            name="kept",
            field=models.BooleanField(default=True),
        ),
        migrations.AddIndex(
            model_name="mailmessage",
            index=models.Index(fields=["workspace", "mailbox", "created_at"], name="mail_message_mailbox_age"),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
