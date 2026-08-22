from django.db import migrations, models


def backfill_scores(apps, schema_editor):
    BookSubmission = apps.get_model("core", "BookSubmission")
    BookSubmission.objects.filter(status="approved", approved_pages__isnull=False).update(
        final_scored_pages=models.F("approved_pages")
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0018_booksubmission_bonus_pages_and_more")]

    operations = [migrations.RunPython(backfill_scores, migrations.RunPython.noop)]
