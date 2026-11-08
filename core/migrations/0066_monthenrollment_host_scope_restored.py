from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0065_reader_intent_and_matching_evidence")]

    operations = [
        migrations.AddField(
            model_name="monthenrollment",
            name="host_scope_restored",
            field=models.BooleanField(default=False, editable=False),
        ),
    ]
