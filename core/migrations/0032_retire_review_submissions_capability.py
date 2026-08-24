from django.db import migrations


def remove_review_submissions_overrides(apps, schema_editor):
    Membership = apps.get_model("core", "Membership")
    for membership in Membership.objects.iterator():
        overrides = dict(membership.permission_overrides or {})
        if "review_submissions" not in overrides:
            continue
        overrides.pop("review_submissions")
        membership.permission_overrides = overrides
        membership.save(update_fields=["permission_overrides"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0031_retire_manage_teams_capability"),
    ]

    operations = [
        migrations.RunPython(remove_review_submissions_overrides, migrations.RunPython.noop),
    ]
