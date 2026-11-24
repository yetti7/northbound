from django.test import TestCase, TransactionTestCase
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.core.exceptions import PermissionDenied
from . import test_branding as fixture
from .branding import community_branding, team_branding, update_branding, TeamBrandingForm


class IndependentBrandingTests(TestCase):
    setUp = fixture.BrandingTests.setUp
    setUpFixture = fixture.BrandingTests.setUpFixture
    make_book = fixture.BrandingTests.make_book
    save = fixture.BrandingTests.save

    def assets(self):
        self.save(logo=fixture.upload(), banner=fixture.upload())
        self.group.refresh_from_db()
        self.month.group = self.group
        self.team.month = self.month

    def test_independent_modes_inherit_raw_assets_and_preserve_overrides(self):
        self.assets()
        self.month.branding_mode = 'banner_only'
        art = community_branding(self.admin, self.group, self.month)
        self.assertFalse(art['group_logo']); self.assertTrue(art['group_banner'])
        self.team.branding_mode = 'banner'
        art = team_branding(self.admin, self.team)
        self.assertTrue(art['effective_logo']); self.assertTrue(art['effective_banner'])
        self.group.branding_mode = 'banner_only'
        self.assertEqual(team_branding(self.admin, self.team), art)
        self.team.branding_mode = 'logo'
        self.assertFalse(team_branding(self.admin, self.team)['effective_banner'])
        self.save(target=self.team, logo=fixture.upload())
        self.team.refresh_from_db(); self.team.month = self.month
        self.assertEqual(team_branding(self.admin, self.team)['effective_logo'], self.team.logo.url)

    def test_legacy_blank_is_retained_until_explicit_save_without_fourth_mode(self):
        self.assets()
        self.team.hide_inherited_logo = True
        self.team.branding_legacy_visibility = True
        self.team.save()
        art = team_branding(self.admin, self.team)
        self.assertFalse(art['effective_logo']); self.assertFalse(art['effective_banner'])
        self.assertEqual(len(TeamBrandingForm().fields['branding_mode'].choices), 3)
        self.assertNotIn('hide_inherited_logo', TeamBrandingForm().fields)
        form, saved = update_branding(actor=self.admin,target=self.team,data={'branding_mode':'logo'},files={})
        self.assertTrue(saved, form.errors)
        self.team.refresh_from_db(); self.team.month = self.month
        self.assertFalse(self.team.branding_legacy_visibility)
        self.assertTrue(team_branding(self.admin,self.team)['effective_logo'])

    def test_missing_artwork_and_banner_showcase(self):
        self.assets(); self.team.branding_mode = 'banner_only'
        self.assertFalse(team_branding(self.admin,self.team)['effective_logo'])
        self.assertFalse(team_branding(self.admin,self.team)['showcase'])
        self.save(target=self.team,banner=fixture.upload())
        self.team.refresh_from_db(); self.team.month = self.month; self.team.branding_mode='banner_only'
        self.assertTrue(team_branding(self.admin,self.team)['showcase'])
        self.team.logo='missing.png'; self.group.logo='missing.png'; self.month.logo='missing.png'
        self.team.branding_mode='logo'
        self.assertFalse(team_branding(self.admin,self.team)['effective_logo'])
        with self.assertRaises(PermissionDenied):
            update_branding(actor=self.reader,target=self.group,data={'branding_mode':'banner'},files={})


class BrandingModeMigrationTests(TransactionTestCase):
    def test_backfill_preserves_assets_gradients_and_legacy_blank(self):
        executor=MigrationExecutor(connection); old=[('core','0086_platform_discord_override')]
        try:
            executor.migrate(old); apps=executor.loader.project_state(old).apps
            group=apps.get_model('core','ReadingGroup').objects.create(name='Legacy',slug='legacy-brand',branding_mode='logo',logo='logo.png',banner='banner.png',gradient_enabled=True)
            month=apps.get_model('core','ChallengeMonth').objects.create(group=group,name='Old')
            team=apps.get_model('core','Team').objects.create(month=month,name='Blank',hide_inherited_logo=True)
            executor=MigrationExecutor(connection); target=[('core','0087_independent_branding_modes')]
            executor.migrate(target); apps=executor.loader.project_state(target).apps
            migrated=apps.get_model('core','Team').objects.get(pk=team.pk)
            self.assertEqual(migrated.branding_mode,'logo'); self.assertTrue(migrated.branding_legacy_visibility)
            self.assertTrue(migrated.hide_inherited_logo)
            self.assertEqual(apps.get_model('core','ChallengeMonth').objects.get(pk=month.pk).branding_mode,'logo')
            group=apps.get_model('core','ReadingGroup').objects.get(pk=group.pk)
            self.assertEqual((group.logo.name,group.banner.name,group.gradient_enabled),('logo.png','banner.png',True))
        finally:
            executor=MigrationExecutor(connection); executor.migrate(executor.loader.graph.leaf_nodes())
