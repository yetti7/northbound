"""Reading Groups listing presentation only; Team/Challenge styles are reused unchanged."""
import tempfile
from pathlib import Path
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from .models import ReadingGroup, Membership


class GroupListingBrandingTests(TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        override=override_settings(MEDIA_ROOT=directory.name);override.enable();self.addCleanup(override.disable)
        self.user=get_user_model().objects.create_user('group-card-reader','group-card@example.invalid','test-password')
        self.group=ReadingGroup.objects.create(name='Visible Reading Group',slug='visible-card',branding_mode='banner',timezone='America/New_York')
        Membership.objects.create(group=self.group,user=self.user,display_name='Reader')
        self.client.force_login(self.user)

    def artwork(self,logo=True,banner=True):
        if logo:self.group.logo.save('group-logo.png',ContentFile(b'fixture'),save=False)
        if banner:self.group.banner.save('group-banner.png',ContentFile(b'fixture'),save=False)
        self.group.save()

    def test_banner_and_single_logo_use_exact_team_card_classes(self):
        self.artwork();response=self.client.get(reverse('dashboard'))
        self.assertContains(response,'css/team_identity.css')
        self.assertContains(response,'class="team-card-identity has-banner"')
        self.assertContains(response,'class="team-card-banner"')
        self.assertContains(response,'class="team-card-logo"',count=1)
        self.assertContains(response,self.group.logo.url,count=1)
        self.assertContains(response,self.group.banner.url,count=1)
        self.assertNotContains(response,'class="community-logos"')
        self.assertContains(response,'Visible Reading Group');self.assertContains(response,'America/New_York')
        self.assertContains(response,'class="status-dot"')
        self.assertContains(response,'href="'+reverse('group-detail',args=[self.group.slug])+'"')
        self.assertEqual(self.client.get(reverse('group-detail',args=[self.group.slug])).status_code,200)

    def test_missing_artwork_fallbacks(self):
        for logo,banner in ((False,False),(True,False),(False,True)):
            with self.subTest(logo=logo,banner=banner):
                self.group.logo='';self.group.banner='';self.group.save()
                self.artwork(logo,banner)
                response=self.client.get(reverse('dashboard'))
                self.assertContains(response,'Visible Reading Group')
                self.assertContains(response,'class="team-card-logo"',count=int(logo))
                self.assertContains(response,'class="team-card-banner"',count=int(banner))
                self.assertContains(response,'class="team-card-identity',count=int(logo or banner))
                self.assertNotContains(response,'src=""')

    def test_missing_files_are_not_rendered_as_broken_images(self):
        self.artwork()
        Path(self.group.logo.path).unlink();Path(self.group.banner.path).unlink()
        response=self.client.get(reverse('dashboard'))
        self.assertNotContains(response,'class="team-card-identity')
        self.assertNotContains(response,self.group.logo.url)
        self.assertNotContains(response,self.group.banner.url)
        self.assertContains(response,'Visible Reading Group')

    def test_compact_branding_mode_keeps_existing_banner_visibility_rule(self):
        self.artwork();self.group.branding_mode='logo';self.group.save()
        response=self.client.get(reverse('dashboard'))
        self.assertContains(response,'class="team-card-logo"')
        self.assertNotContains(response,'class="team-card-banner"')

    def test_existing_group_scope_and_empty_state_are_preserved(self):
        ReadingGroup.objects.create(name='Unrelated Group',slug='unrelated-card')
        response=self.client.get(reverse('dashboard'));self.assertNotContains(response,'Unrelated Group')
        Membership.objects.filter(group=self.group,user=self.user).update(is_active=False)
        response=self.client.get(reverse('dashboard'))
        self.assertContains(response,'You do not belong to a reading group yet.')
        self.assertNotContains(response,'Visible Reading Group')
