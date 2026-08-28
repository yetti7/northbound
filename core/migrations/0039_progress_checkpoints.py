import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0038_challenge_registration_questions")]

    operations = [
        migrations.CreateModel(
            name="ProgressCheckpoint",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("scheduled_at", models.DateTimeField()),
                ("threshold_percentage", models.PositiveSmallIntegerField(default=25, validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(100)])),
                ("progress_basis", models.CharField(choices=[("base", "Base Pages"), ("total", "Total Pages")], default="base", max_length=8)),
                ("target_basis", models.CharField(choices=[("previous_average", "Previous Monthly Average"), ("fixed", "Fixed Target")], default="previous_average", max_length=20)),
                ("fixed_target_pages", models.PositiveIntegerField(blank=True, null=True)),
                ("position", models.PositiveSmallIntegerField(default=1)),
                ("evaluation_state", models.CharField(choices=[("pending", "Pending"), ("evaluated", "Evaluated"), ("skipped", "Skipped")], default="pending", max_length=10)),
                ("evaluated_at", models.DateTimeField(blank=True, null=True)),
                ("month", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="progress_checkpoints", to="core.challengemonth")),
            ],
            options={"ordering": ["position", "scheduled_at", "pk"]},
        ),
        migrations.CreateModel(
            name="ProgressCheckpointResult",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("evaluated_at", models.DateTimeField()),
                ("threshold_percentage", models.PositiveSmallIntegerField()),
                ("progress_basis", models.CharField(choices=[("base", "Base Pages"), ("total", "Total Pages")], max_length=8)),
                ("target_basis", models.CharField(choices=[("previous_average", "Previous Monthly Average"), ("fixed", "Fixed Target")], max_length=20)),
                ("target_pages", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("required_pages", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("progress_pages", models.PositiveIntegerField(default=0)),
                ("outcome", models.CharField(choices=[("met", "Met threshold"), ("below", "Below threshold"), ("not_evaluated", "Not Evaluated")], max_length=16)),
                ("checkpoint", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="results", to="core.progresscheckpoint")),
                ("participant", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="progress_checkpoint_results", to="core.membership")),
            ],
            options={"ordering": ["checkpoint__scheduled_at", "participant__display_name", "pk"]},
        ),
        migrations.AddConstraint(
            model_name="progresscheckpoint",
            constraint=models.UniqueConstraint(fields=("month", "position"), name="unique_progress_checkpoint_position"),
        ),
        migrations.AddConstraint(
            model_name="progresscheckpointresult",
            constraint=models.UniqueConstraint(fields=("checkpoint", "participant"), name="one_progress_result_per_checkpoint_reader"),
        ),
    ]
