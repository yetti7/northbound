from django.db import migrations, models


FORWARD_STATUS_MAP = {
    "open": "active",
    "closed": "finalizing",
    "finalized": "completed",
}

REVERSE_STATUS_MAP = {
    "open_registration": "draft",
    "active": "open",
    "finalizing": "closed",
    "completed": "finalized",
}


def migrate_phase_one_lifecycle(apps, schema_editor):
    ChallengeMonth = apps.get_model("core", "ChallengeMonth")
    for old_status, new_status in FORWARD_STATUS_MAP.items():
        ChallengeMonth.objects.filter(status=old_status).update(status=new_status)


def restore_phase_one_lifecycle(apps, schema_editor):
    ChallengeMonth = apps.get_model("core", "ChallengeMonth")
    for new_status, old_status in REVERSE_STATUS_MAP.items():
        ChallengeMonth.objects.filter(status=new_status).update(status=old_status)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0032_retire_review_submissions_capability"),
    ]

    operations = [
        migrations.AddField(
            model_name="challengemonth",
            name="description",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="challengemonth",
            name="registration_closes_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="challengemonth",
            name="registration_opens_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="challengemonth",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "Draft"),
                    ("open_registration", "Open Registration"),
                    ("active", "Active"),
                    ("finalizing", "Finalizing"),
                    ("completed", "Completed"),
                    ("archived", "Archived"),
                ],
                default="draft",
                max_length=17,
            ),
        ),
        migrations.RunPython(migrate_phase_one_lifecycle, restore_phase_one_lifecycle),
    ]
