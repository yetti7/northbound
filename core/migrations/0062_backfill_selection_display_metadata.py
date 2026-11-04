from django.db import migrations
from django.db.models import F


def backfill_display_metadata(apps, schema_editor):
    for model_name in ("PersonalTBRBook", "BotmBook"):
        selection = apps.get_model("core", model_name)
        for display, snapshot in (
            ("display_title", "title_snapshot"),
            ("display_author", "author_snapshot"),
            ("display_cover_url", "cover_url_snapshot"),
            ("display_page_count", "page_count_snapshot"),
        ):
            selection.objects.using(schema_editor.connection.alias).filter(
                **{f"{display}__isnull": True}
            ).update(**{display: F(snapshot)})


class Migration(migrations.Migration):
    dependencies = [("core", "0061_selection_display_metadata")]
    operations = [migrations.RunPython(backfill_display_metadata, migrations.RunPython.noop)]
