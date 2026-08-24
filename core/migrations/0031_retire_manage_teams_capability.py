from django.db import migrations


def remove_manage_teams_overrides(apps, schema_editor):
    Membership = apps.get_model("core", "Membership")
    for membership in Membership.objects.iterator():
        overrides = dict(membership.permission_overrides or {})
        if "manage_teams" not in overrides:
            continue
        overrides.pop("manage_teams")
        membership.permission_overrides = overrides
        membership.save(update_fields=["permission_overrides"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0030_alter_challengestaffassignment_role"),
    ]

    operations = [
        migrations.RunPython(remove_manage_teams_overrides, migrations.RunPython.noop),
    ]
