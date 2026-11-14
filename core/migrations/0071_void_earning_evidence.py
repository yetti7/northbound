from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0070_late_approval_evidence'),
    ]

    operations = [
        migrations.AddField(
            model_name='booksubmission',
            name='void_earning_evidence',
            field=models.JSONField(blank=True, default=dict, editable=False),
        ),
    ]
