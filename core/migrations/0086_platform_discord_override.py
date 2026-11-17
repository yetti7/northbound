from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('core', '0085_account_identity')]
    operations = [migrations.AddField(
        model_name='platformsettings', name='discord_notifications_override',
        field=models.BooleanField(null=True, default=None, blank=True),
    )]
