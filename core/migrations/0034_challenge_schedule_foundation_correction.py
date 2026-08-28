from django.db import migrations, models


def replace_open_registration_with_upcoming(apps, schema_editor):
    ChallengeMonth = apps.get_model("core", "ChallengeMonth")
    ChallengeMonth.objects.filter(status="open_registration").update(status="upcoming")


def restore_open_registration_value(apps, schema_editor):
    ChallengeMonth = apps.get_model("core", "ChallengeMonth")
    ChallengeMonth.objects.filter(status="upcoming").update(status="open_registration")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0033_challenge_lifecycle_identity_foundation"),
    ]

    operations = [
        migrations.AddField(
            model_name="challengemonth",
            name="auto_close_registration",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="challengemonth",
            name="auto_complete_challenge",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="challengemonth",
            name="auto_end_challenge",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="challengemonth",
            name="auto_open_registration",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="challengemonth",
            name="auto_start_challenge",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="challengemonth",
            name="ends_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="challengemonth",
            name="final_announcement_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="challengemonth",
            name="registration_is_open",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="challengemonth",
            name="starts_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="challengemonth",
            name="late_entry_deadline",
            field=models.DateField(blank=True, editable=False, null=True),
        ),
        migrations.AlterField(
            model_name="challengemonth",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "Draft"),
                    ("upcoming", "Upcoming"),
                    ("active", "Active"),
                    ("finalizing", "Finalizing"),
                    ("completed", "Completed"),
                    ("archived", "Archived"),
                ],
                default="draft",
                max_length=17,
            ),
        ),
        migrations.RunPython(replace_open_registration_with_upcoming, restore_open_registration_value),
    ]
