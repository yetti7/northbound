from django.db import migrations, models


LEGACY_TO_COMPETITION = {
    "owner": "owner",
    "staff": "moderator",
    "everyone": "everybody",
}


def migrate_legacy_visibility(apps, schema_editor):
    ChallengeMonth = apps.get_model("core", "ChallengeMonth")
    for legacy, replacement in LEGACY_TO_COMPETITION.items():
        ChallengeMonth.objects.filter(team_stats_visibility=legacy).update(
            team_standings_visibility=replacement,
            reader_scores_visibility=replacement,
        )


def restore_legacy_visibility(apps, schema_editor):
    ChallengeMonth = apps.get_model("core", "ChallengeMonth")
    competition_to_legacy = {value: key for key, value in LEGACY_TO_COMPETITION.items()}
    for month in ChallengeMonth.objects.all().iterator():
        month.team_stats_visibility = competition_to_legacy.get(
            month.team_standings_visibility
            if month.team_standings_visibility == month.reader_scores_visibility
            else "",
            "owner",
        )
        month.save(update_fields=["team_stats_visibility"])


class Migration(migrations.Migration):
    dependencies = [("core", "0040_challenge_creation_handoff")]

    operations = [
        migrations.AddField(
            model_name="challengemonth",
            name="team_standings_visibility",
            field=models.CharField(
                choices=[
                    ("nobody", "Nobody"),
                    ("owner", "Owner only"),
                    ("moderator", "Moderator + Owner"),
                    ("team_leader", "Team Leader"),
                    ("team_members", "Team Members"),
                    ("everybody", "Everybody"),
                ],
                default="owner",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="challengemonth",
            name="reader_scores_visibility",
            field=models.CharField(
                choices=[
                    ("nobody", "Nobody"),
                    ("owner", "Owner only"),
                    ("moderator", "Moderator + Owner"),
                    ("team_leader", "Team Leader"),
                    ("team_members", "Team Members"),
                    ("everybody", "Everybody"),
                ],
                default="owner",
                max_length=12,
            ),
        ),
        migrations.RunPython(migrate_legacy_visibility, restore_legacy_visibility),
        migrations.RemoveField(model_name="challengemonth", name="team_stats_visibility"),
    ]
