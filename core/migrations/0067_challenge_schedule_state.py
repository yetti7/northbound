from django.db import migrations, models
from django.utils import timezone


def mark_elapsed_registration_events(apps, schema_editor):
    challenges = apps.get_model("core", "ChallengeMonth").objects.using(schema_editor.connection.alias)
    now = timezone.now()
    challenges.filter(auto_open_registration=True, registration_opens_at__lte=now).update(
        registration_open_handled=True,
    )
    challenges.filter(auto_close_registration=True, registration_closes_at__lte=now).update(
        registration_close_handled=True,
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0066_monthenrollment_host_scope_restored")]

    operations = [
        migrations.AddField(
            model_name="challengemonth",
            name="registration_open_handled",
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.AddField(
            model_name="challengemonth",
            name="registration_close_handled",
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.AddField(
            model_name="challengemonth",
            name="held_schedule_milestones",
            field=models.JSONField(default=list, blank=True, editable=False),
        ),
        migrations.RunPython(mark_elapsed_registration_events, migrations.RunPython.noop),
    ]
