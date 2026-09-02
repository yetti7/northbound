from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0054_hardcover_oauth_application_foundation")]

    operations = [
        migrations.AddField(
            model_name="readerhardcoverconnection",
            name="encrypted_refresh_token",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="readerhardcoverconnection",
            name="access_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="readerhardcoverconnection",
            name="granted_scopes",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="readerhardcoverconnection",
            name="refreshed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="readerhardcoverconnection",
            name="reconnect_required",
            field=models.BooleanField(default=False),
        ),
    ]
