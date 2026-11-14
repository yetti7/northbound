import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0067_challenge_schedule_state")]

    operations = [
        migrations.AddField(
            model_name="monthenrollment",
            name="credited_team",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.RESTRICT,
                related_name="credited_enrollments", to="core.team",
            ),
        ),
        migrations.AddField(
            model_name="monthenrollment",
            name="awaiting_scoring_team",
            field=models.BooleanField(default=False, editable=False),
        ),
    ]
