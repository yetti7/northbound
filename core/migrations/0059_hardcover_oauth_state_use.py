from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0058_hardcover_read_occurrence_sync")]
    operations = [
        migrations.CreateModel(
            name="HardcoverOAuthStateUse",
            fields=[
                ("state_hash", models.CharField(editable=False, max_length=64, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("completed_or_cancelled", models.BooleanField(default=False)),
            ],
        ),
    ]
