from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0069_participation_eligibility'),
    ]

    operations = [
        migrations.AddField(
            model_name='booksubmission',
            name='late_approval_evidence',
            field=models.JSONField(blank=True, default=dict, editable=False),
        ),
    ]
