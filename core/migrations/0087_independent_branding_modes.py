from django.db import migrations, models

MODES = [('logo', 'Logo Only'), ('banner_only', 'Banner Only'), ('banner', 'Logo and Banner')]


def preserve_presentation(apps, schema_editor):
    alias = schema_editor.connection.alias
    Group = apps.get_model('core', 'ReadingGroup')
    Challenge = apps.get_model('core', 'ChallengeMonth')
    Team = apps.get_model('core', 'Team')
    for group in Group.objects.using(alias).all().iterator():
        Challenge.objects.using(alias).filter(group_id=group.pk).update(branding_mode=group.branding_mode)
        for team in Team.objects.using(alias).filter(month__group_id=group.pk).iterator():
            mode = group.branding_mode
            if mode == 'banner' and team.hide_inherited_logo and not team.logo:
                mode = 'banner_only'
            Team.objects.using(alias).filter(pk=team.pk).update(
                branding_mode=mode, branding_legacy_visibility=team.hide_inherited_logo)


class Migration(migrations.Migration):
    dependencies = [('core', '0086_platform_discord_override')]
    operations = [
        migrations.AlterField('readinggroup', 'branding_mode', models.CharField(max_length=16, choices=MODES, default='logo')),
        migrations.AddField('challengemonth', 'branding_mode', models.CharField(max_length=16, choices=MODES, default='logo')),
        migrations.AddField('team', 'branding_mode', models.CharField(max_length=16, choices=MODES, default='logo')),
        migrations.AddField('team', 'branding_legacy_visibility', models.BooleanField(default=False, editable=False)),
        migrations.AlterField('team', 'hide_inherited_logo', models.BooleanField(default=False, editable=False)),
        migrations.RunPython(preserve_presentation, migrations.RunPython.noop),
    ]
