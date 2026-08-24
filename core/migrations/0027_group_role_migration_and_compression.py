from django.db import migrations, models


LEGACY_ADMIN_ADDITIONAL_CAPABILITIES = (
    "manage_group_settings",
    "manage_participants",
    "manage_months",
    "manage_teams",
)


def compress_group_roles(apps, schema_editor):
    Membership = apps.get_model("core", "Membership")

    for membership in Membership.objects.filter(role="admin").iterator():
        overrides = dict(membership.permission_overrides or {})
        for capability in LEGACY_ADMIN_ADDITIONAL_CAPABILITIES:
            overrides.setdefault(capability, True)
        membership.role = "moderator"
        membership.permission_overrides = overrides
        membership.save(update_fields=["role", "permission_overrides"])

    Membership.objects.filter(role="game_manager").update(role="member")
    Membership.objects.filter(role="reader").update(role="member")


def restore_compatible_legacy_values(apps, schema_editor):
    """Restore a valid legacy baseline without guessing former admin/game roles."""
    Membership = apps.get_model("core", "Membership")
    Membership.objects.filter(role="member").update(role="reader")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0026_platformsettings"),
    ]

    operations = [
        migrations.RunPython(compress_group_roles, restore_compatible_legacy_values),
        migrations.AlterField(
            model_name="membership",
            name="role",
            field=models.CharField(
                choices=[
                    ("owner", "Group owner"),
                    ("moderator", "Moderator"),
                    ("member", "Member"),
                ],
                default="member",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="readinggroup",
            name="access_code_visibility",
            field=models.CharField(
                choices=[
                    ("owner", "Group owners only"),
                    ("staff", "Owners and moderators"),
                    ("members", "All group members"),
                ],
                default="owner",
                max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name="challengemonth",
            name="team_stats_visibility",
            field=models.CharField(
                choices=[
                    ("everyone", "Everyone in the group"),
                    ("staff", "Owners and moderators"),
                    ("owner", "Group owners only"),
                ],
                default="owner",
                max_length=10,
            ),
        ),
    ]
