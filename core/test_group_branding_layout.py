from django.urls import reverse
from django.test import TestCase
from . import test_branding as fixture


class GroupBrandingLayoutTests(TestCase):
    setUp = fixture.BrandingTests.setUp
    setUpFixture = fixture.BrandingTests.setUpFixture
    make_book = fixture.BrandingTests.make_book
    save = fixture.BrandingTests.save
    # Only the new focused cases are collected here; reuse disposable setup.
    def test_identity_modes_and_missing_artwork(self):
        self.client.force_login(self.reader)
        url=reverse('group-detail',args=[self.group.slug])
        response=self.client.get(url)
        self.assertContains(response,'group-identity-main')
        self.assertNotContains(response,'group-identity-logo')
        self.save(logo=fixture.upload(),banner=fixture.upload());self.group.refresh_from_db()
        response=self.client.get(url);self.assertContains(response,'group-identity-logo');self.assertNotContains(response,self.group.banner.url)
        self.assertContains(response,'<h1>'+self.group.name+'</h1>',html=True)
        self.group.branding_mode='banner';self.group.save(update_fields=['branding_mode'])
        response=self.client.get(url);self.assertContains(response,'has-banner');self.assertContains(response,self.group.banner.url)
        self.assertContains(response,'active member');self.assertContains(response,'visible Challenge')

    def test_clear_group_settings_navigation_and_saved_state(self):
        self.save(logo=fixture.upload(),banner=fixture.upload());self.group.refresh_from_db()
        self.client.force_login(self.admin);url=reverse('group-branding',args=[self.group.slug])
        response=self.client.get(url)
        for label in ['Group Logo','Group Banner','Replace Image','Remove Image','Back to Group Settings','Cancel','Header preview']:
            self.assertContains(response,label)
        self.assertNotContains(response,'Currently:');self.assertNotContains(response,'Change:')
        self.assertNotContains(response,'View current image')
        self.assertContains(response,'href="'+reverse('group-edit',args=[self.group.slug])+'"',count=2)
        old=self.group.logo.name
        response=self.client.post(url,{'branding_mode':'banner'})
        self.assertEqual(response.status_code,302);self.group.refresh_from_db();self.assertEqual(self.group.logo.name,old)
        self.assertEqual(self.client.post(url,{'branding_mode':'banner','logo':fixture.upload('new.png')}).status_code,302)
        self.group.refresh_from_db();self.assertNotEqual(self.group.logo.name,old)
        self.assertEqual(self.client.post(url,{'branding_mode':'logo','logo-clear':'on'}).status_code,302)
        self.group.refresh_from_db();self.assertFalse(self.group.logo);self.assertTrue(self.group.banner)
        self.client.force_login(self.mod)
        readonly=self.client.get(url)
        self.assertNotContains(readonly,'type="file"')
        self.assertContains(readonly,self.group.banner.url)
        self.assertContains(readonly,'Back to Group')

    def test_invalid_post_preserves_mode_removal_and_errors(self):
        self.save(logo=fixture.upload());self.group.refresh_from_db()
        self.client.force_login(self.admin)
        response=self.client.post(reverse('group-branding',args=[self.group.slug]),{'branding_mode':'banner','logo-clear':'on','banner':fixture.SimpleUploadedFile('bad.png',b'invalid')})
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'name="logo-clear" value="on" checked')
        self.assertContains(response,'value="banner" selected')
        self.assertContains(response,'banner-errors')
        self.group.refresh_from_db();self.assertTrue(self.group.logo)

    def test_logo_shapes_preserve_asset_and_challenge_presentation(self):
        self.client.force_login(self.admin)
        for size in [(2400,500),(1000,1000),(600,1200)]:
            self.save(logo=fixture.upload(size=size));self.group.refresh_from_db()
            self.assertContains(self.client.get(reverse('group-detail',args=[self.group.slug])),self.group.logo.url)
        response=self.client.get(reverse('challenge-branding',args=[self.group.slug,self.month.pk]))
        self.assertTemplateUsed(response,'core/branding_settings.html')
        self.assertNotContains(response,'data-group-branding-form')
        response=self.client.get(reverse('month-detail',args=[self.group.slug,self.month.pk]))
        self.assertNotContains(response,'group-identity-main')
