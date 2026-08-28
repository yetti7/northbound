from django.db import migrations, models
from django.utils import timezone


def mark_existing_host_assignments_seen(apps, schema_editor):
    ChallengeStaffAssignment = apps.get_model("core", "ChallengeStaffAssignment")
    ChallengeStaffAssignment.objects.filter(role="host").update(
        host_assignment_notice_seen_at=timezone.now(),
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0039_progress_checkpoints")]

    operations = [
        migrations.AddField(
            model_name="challengestaffassignment",
            name="host_assignment_notice_seen_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(mark_existing_host_assignments_seen, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="challengemonth",
            name="ends_on",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="challengemonth",
            name="starts_on",
            field=models.DateField(blank=True, null=True),
        ),
    ]
