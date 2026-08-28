from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0036_userprofile_discord_username"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="discord_username_is_public",
            field=models.BooleanField(default=False),
        ),
    ]
