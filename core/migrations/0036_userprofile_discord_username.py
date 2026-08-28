from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0035_durable_participation_history"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="discord_username",
            field=models.CharField(blank=True, max_length=100),
        ),
    ]
