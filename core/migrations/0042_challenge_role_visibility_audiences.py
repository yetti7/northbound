from django.db import migrations, models


OLD_TO_CHALLENGE_ROLE = {
    "nobody": "hosts",
    "owner": "hosts",
    "moderator": "hosts",
    "team_leader": "hosts_leaders",
    "team_members": "team_members",
    "everybody": "everybody",
}


def migrate_visibility_audiences(apps, schema_editor):
    ChallengeMonth = apps.get_model("core", "ChallengeMonth")
    for old_value, new_value in OLD_TO_CHALLENGE_ROLE.items():
        ChallengeMonth.objects.filter(team_standings_visibility=old_value).update(
            team_standings_visibility=new_value,
        )
        ChallengeMonth.objects.filter(reader_scores_visibility=old_value).update(
            reader_scores_visibility=new_value,
        )


def restore_previous_audiences(apps, schema_editor):
    ChallengeMonth = apps.get_model("core", "ChallengeMonth")
    reverse_mapping = {
        "hosts": "owner",
        "hosts_floaters": "nobody",
        "hosts_leaders": "team_leader",
        "team_members": "team_members",
        "everybody": "everybody",
    }
    for new_value, old_value in reverse_mapping.items():
        ChallengeMonth.objects.filter(team_standings_visibility=new_value).update(
            team_standings_visibility=old_value,
        )
        ChallengeMonth.objects.filter(reader_scores_visibility=new_value).update(
            reader_scores_visibility=old_value,
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0041_challenge_competition_visibility")]

    operations = [
        migrations.RunPython(migrate_visibility_audiences, restore_previous_audiences),
        migrations.AlterField(
            model_name="challengemonth",
            name="team_standings_visibility",
            field=models.CharField(
                choices=[
                    ("hosts", "Hosts only"),
                    ("hosts_floaters", "Hosts + Floaters"),
                    ("hosts_leaders", "Hosts + Team Leaders"),
                    ("team_members", "Team Members"),
                    ("everybody", "Everybody"),
                ],
                default="hosts",
                max_length=14,
            ),
        ),
        migrations.AlterField(
            model_name="challengemonth",
            name="reader_scores_visibility",
            field=models.CharField(
                choices=[
                    ("hosts", "Hosts only"),
                    ("hosts_floaters", "Hosts + Floaters"),
                    ("hosts_leaders", "Hosts + Team Leaders"),
                    ("team_members", "Team Members"),
                    ("everybody", "Everybody"),
                ],
                default="hosts",
                max_length=14,
            ),
        ),
    ]
