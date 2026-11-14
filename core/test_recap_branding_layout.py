from django.urls import reverse
from . import test_branding as fixture


class RecapBrandingLayoutTests(fixture.TestCase):
    setUp=fixture.BrandingTests.setUp
    setUpFixture=fixture.BrandingTests.setUpFixture
    status=fixture.BrandingTests.status
    make_book=fixture.BrandingTests.make_book
    save=fixture.BrandingTests.save

    def page(self,wrapped=False):
        self.client.force_login(self.reader)
        return self.client.get(reverse('recap-wrapped' if wrapped else 'recap-dashboard'),{'group':self.group.pk,'challenge':self.month.pk})
    def opening(self,response):
        return response.content.decode().split('class="wrapped-slide wrapped-slide-opening"',1)[1].split('</article>',1)[0]

    def test_inherited_group_art_integrates_with_header_and_keeps_filters(self):
        self.save(logo=fixture.upload());self.group.refresh_from_db()
        response=self.page();self.assertContains(response,'recap-identity has-community');self.assertContains(response,self.group.logo.url)
        self.assertContains(response,'recap-community-logo');self.assertContains(response,'data-recap-filters');self.assertContains(response,'View Wrapped')
        self.assertContains(response,'Update Recap')

    def test_challenge_precedence_does_not_change_authorized_export_payload(self):
        self.save(logo=fixture.upload());self.save(target=self.month,logo=fixture.upload());self.group.refresh_from_db();self.month.refresh_from_db()
        response=self.page();self.assertContains(response,self.month.logo.url);self.assertNotContains(response,self.group.logo.url)
        wrapped=self.page(True);opening=self.opening(wrapped)
        self.assertNotIn('/media/',opening)
        data=wrapped.context['export_data']['branding_options']['art']
        self.assertEqual(data['group']['logo'],self.group.logo.url);self.assertEqual(data['challenge']['logo'],self.month.logo.url)
        self.assertIn('wrapped-opening-northbound',opening);self.assertNotContains(wrapped,'type="checkbox" checked')
        self.assertContains(wrapped,'Download Summary');self.assertContains(wrapped,'data-wrapped-next')

    def test_logo_and_banner_modes_preserve_precedence_and_fallback(self):
        self.save(logo=fixture.upload(),banner=fixture.upload());self.group.refresh_from_db()
        response=self.page();self.assertNotContains(response,'recap-community-banner')
        self.group.branding_mode='banner';self.group.save(update_fields=['branding_mode'])
        response=self.page();self.assertContains(response,self.group.banner.url)
        self.save(target=self.month,logo=fixture.upload(),banner=fixture.upload());self.month.refresh_from_db()
        response=self.page();self.assertContains(response,self.month.banner.url);self.assertNotContains(response,self.group.banner.url)
        self.month.banner='';self.month.save(update_fields=['banner'])
        self.assertContains(self.page(),self.group.banner.url)

    def test_missing_artwork_has_no_empty_identity_container_and_keeps_northbound(self):
        self.group.logo='missing.png';self.group.save(update_fields=['logo'])
        response=self.page();self.assertNotContains(response,'class="recap-community-art"');self.assertNotContains(response,'recap-identity has-community')
        opening=self.opening(self.page(True));self.assertIn('northbound-header-dark.png',opening);self.assertNotIn('wrapped-opening-branding has-community',opening)

    def test_historical_self_access_does_not_reveal_community_art(self):
        self.save(logo=fixture.upload());member=self.members[self.reader.pk];member.is_active=False;member.save()
        self.client.force_login(self.reader)
        response=self.client.get(reverse('recap-wrapped'),{'challenge':self.month.pk})
        self.assertNotContains(response,'/media/branding/');self.assertNotContains(response,'recap-community-logo')

    def test_banner_uses_overlay_hook_only_when_authorized_banner_is_available(self):
        self.save(logo=fixture.upload(),banner=fixture.upload());self.group.refresh_from_db()
        self.assertNotContains(self.page(),'class="recap-community-art has-banner"')
        self.group.branding_mode='banner';self.group.save(update_fields=['branding_mode'])
        self.assertContains(self.page(),'class="recap-community-art has-banner"',count=1)
        self.assertNotContains(self.page(True),'class="recap-community-art has-banner"')
        self.group.logo='';self.group.save(update_fields=['logo'])
        response=self.page();self.assertContains(response,'recap-community-banner');self.assertNotContains(response,'recap-community-logo')
        self.group.banner='missing.png';self.group.save(update_fields=['banner'])
        self.assertNotContains(self.page(),'class="recap-community-art has-banner"')

    def test_overlay_css_is_scoped_and_does_not_crop_logos(self):
        from pathlib import Path
        from django.conf import settings
        css=(Path(settings.BASE_DIR)/'static/css/recap.css').read_text()
        self.assertIn('.recap-community-art.has-banner .recap-community-banner{position:absolute;',css)
        self.assertIn('.recap-community-art.has-banner::before',css)
        self.assertIn('.recap-community-art.has-banner .recap-community-logo{max-height:140px;max-width:100%;object-fit:contain',css)
