from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('core', '0082_personal_tbr_custom_cover')]
    operations = [migrations.AddField(
        model_name='challengemonth', name='discord_moderator_opt_in',
        field=models.BooleanField(default=False),
    )]
