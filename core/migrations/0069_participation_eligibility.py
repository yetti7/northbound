import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0068_enrollment_scoring_team'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='booksubmission',
            name='acceptance_claims',
            field=models.JSONField(blank=True, default=dict, editable=False),
        ),
        migrations.AddField(
            model_name='booksubmission',
            name='accepted_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='booksubmission',
            name='accepted_completed_on',
            field=models.DateField(blank=True, editable=False, null=True),
        ),
        migrations.CreateModel(
            name='ParticipationPeriod',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('started_at', models.DateTimeField(editable=False)),
                ('start_reason', models.CharField(choices=[('enrollment', 'Initial enrollment'), ('team_assignment', 'Eligible team assignment'), ('observed', 'Existing eligibility first observed')], editable=False, max_length=16)),
                ('ended_at', models.DateTimeField(blank=True, editable=False, null=True)),
                ('end_reason', models.CharField(blank=True, choices=[('withdrawn', 'Withdrawn'), ('removed', 'Removed')], editable=False, max_length=12)),
                ('enrollment', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='participation_periods', to='core.monthenrollment')),
            ],
        ),
        migrations.AddField(
            model_name='booksubmission',
            name='participation_period',
            field=models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='submissions', to='core.participationperiod'),
        ),
        migrations.AddConstraint(
            model_name='booksubmission',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('accepted_at__isnull', True), ('accepted_completed_on__isnull', True), ('participation_period__isnull', True)), models.Q(('accepted_at__isnull', False), ('accepted_completed_on__isnull', False), ('participation_period__isnull', False)), _connector='OR'), name='submission_acceptance_fields_together'),
        ),
        migrations.AddConstraint(
            model_name='participationperiod',
            constraint=models.UniqueConstraint(condition=models.Q(('ended_at__isnull', True)), fields=('enrollment',), name='one_open_participation_period'),
        ),
        migrations.AddConstraint(
            model_name='participationperiod',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('end_reason', ''), ('ended_at__isnull', True)), models.Q(('end_reason__in', ['withdrawn', 'removed']), ('ended_at__gte', models.F('started_at')), ('ended_at__isnull', False)), _connector='OR'), name='participation_period_valid_end'),
        ),
    ]
