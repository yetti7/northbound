import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0034_challenge_schedule_foundation_correction"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="monthenrollment",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="monthenrollment",
            name="origin",
            field=models.CharField(
                choices=[("legacy", "Legacy"), ("self", "Self-registration"), ("staff", "Staff")],
                default="legacy",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="monthenrollment",
            name="inactive_reason",
            field=models.CharField(
                blank=True,
                choices=[("withdrawn", "Withdrawn"), ("removed", "Removed")],
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="monthenrollment",
            name="inactivated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="monthenrollment",
            name="inactivated_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="inactivated_month_enrollments",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="teamassignment",
            name="one_team_per_participant_per_month",
        ),
        migrations.AddField(
            model_name="teamassignment",
            name="assigned_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name="teamassignment",
            name="assigned_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="created_team_assignments",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="teamassignment",
            name="ended_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="teamassignment",
            name="ended_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="ended_team_assignments",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddConstraint(
            model_name="teamassignment",
            constraint=models.UniqueConstraint(
                fields=("month", "participant"),
                condition=Q(ended_at__isnull=True),
                name="one_current_team_per_participant_per_month",
            ),
        ),
        migrations.AlterField(
            model_name="teamassignment",
            name="assigned_at",
            field=models.DateTimeField(auto_now_add=True, null=True),
        ),
    ]
