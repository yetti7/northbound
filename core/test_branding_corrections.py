"""Focused saved-header previews and presentation-only branding corrections."""
from pathlib import Path
from django.test import TestCase
from django.urls import reverse
from . import test_branding as fixture
from .branding import BrandingForm


class BrandingCorrectionTests(TestCase):
    setUp = fixture.BrandingTests.setUp
    setUpFixture = fixture.BrandingTests.setUpFixture
    make_book = fixture.BrandingTests.make_book
    save = fixture.BrandingTests.save

    def page(self, route, *extra, actor=None):
        self.client.force_login(actor or self.admin)
        return self.client.get(reverse(route, args=[self.group.slug, *extra]))

    def preview(self, response):
        self.assertContains(response, 'Header preview')
        html = response.content.decode()
        self.assertLess(html.index('Save Branding'), html.index('Header preview'))
        return html.split('<section class="group-branding-preview">', 1)[1]

    def test_challenge_settings_heading_compact_access_and_controls(self):
        page = self.page('challenge-settings', self.month.pk)
        html = page.content.decode()
        self.assertLess(html.index('<h1>Challenge Settings</h1>'), html.index('>Branding Settings</a>'))
        self.assertNotContains(page, 'community-branding')
        self.assertNotContains(page, '<h2>Branding</h2>')
        for label in ('Core Configuration', 'Signup Questions', 'Challenge Announcement', 'Competition Visibility'):
            self.assertContains(page, label)
        # The link keeps exactly the existing branding permission policy.
        from .branding import can_view_branding_settings
        for actor in (self.admin, self.owner, self.host, self.mod, self.reader):
            page = self.page('challenge-settings', self.month.pk, actor=actor)
            endpoint = self.page('challenge-branding', self.month.pk, actor=actor)
            allowed = can_view_branding_settings(actor, self.month)
            self.assertEqual(endpoint.status_code, 200 if allowed else 403)
            if page.status_code == 200:
                self.assertEqual('>Branding Settings</a>' in page.content.decode(), allowed)

    def test_saved_previews_follow_per_asset_inheritance_and_display_mode(self):
        self.save(logo=fixture.upload(), banner=fixture.upload())
        self.group.refresh_from_db()
        self.group.branding_mode = 'banner'; self.group.save(update_fields=['branding_mode'])
        routes = [('challenge-branding', (self.month.pk,)), ('team-branding', (self.month.pk, self.team.pk))]
        for route, args in routes:
            preview = self.preview(self.page(route, *args))
            self.assertIn(self.group.logo.url, preview)
            self.assertIn(self.group.banner.url, preview)
        self.save(target=self.month, logo=fixture.upload())
        self.month.refresh_from_db()
        for route, args in routes:
            preview = self.preview(self.page(route, *args))
            self.assertIn(self.month.logo.url, preview)
            self.assertNotIn(self.group.logo.url, preview)
            self.assertIn(self.group.banner.url, preview)
        self.team.logo.save('team.png', fixture.upload(), save=True)
        self.team.banner.save('team-banner.png', fixture.upload(), save=True)
        preview = self.preview(self.page('team-branding', self.month.pk, self.team.pk))
        self.assertIn(self.team.logo.url, preview); self.assertIn(self.team.banner.url, preview)
        self.assertIn('<h1>'+self.team.name+'</h1>', preview)
        self.group.branding_mode = 'logo'; self.group.save(update_fields=['branding_mode'])
        for route, args in routes:
            preview = self.preview(self.page(route, *args))
            self.assertNotIn('challenge-identity-banner', preview)
        self.team.logo = 'missing.png'; self.team.save(update_fields=['logo'])
        preview = self.preview(self.page('team-branding', self.month.pk, self.team.pk))
        self.assertIn(self.month.logo.url, preview); self.assertNotIn('missing.png', preview)

    def test_empty_and_readonly_previews_and_existing_unsaved_asset_hooks(self):
        for route, args in [('challenge-branding', (self.month.pk,)), ('team-branding', (self.month.pk, self.team.pk))]:
            response = self.page(route, *args)
            preview = self.preview(response)
            self.assertNotIn('class="challenge-identity-logo"', preview)
            self.assertNotIn('class="challenge-identity-banner"', preview)
            for kind in ('logo', 'banner'):
                self.assertContains(response, 'data-branding-preview="'+kind+'"')
        type(self.month).objects.filter(pk=self.month.pk).update(status='completed')
        response = self.page('team-branding', self.month.pk, self.team.pk)
        self.assertContains(response, 'Header preview'); self.assertContains(response, 'Read-only')
        self.assertNotContains(response, 'Save Branding</button>')

    def test_presentation_labels_keep_values_and_model_state(self):
        from .models import ReadingGroup
        self.assertEqual(list(BrandingForm(instance=self.group).fields['branding_mode'].choices),
                         [('logo', 'Compact Header'), ('banner', 'Banner Header')])
        self.assertEqual(list(ReadingGroup._meta.get_field('branding_mode').choices),
                         [('logo', 'Logo Only'), ('banner', 'Logo + Banner')])
        for value, label in [('logo', 'Compact Header'), ('banner', 'Banner Header')]:
            form = BrandingForm({'branding_mode': value}, instance=self.group)
            self.assertTrue(form.is_valid(), form.errors); self.assertEqual(form.cleaned_data['branding_mode'], value)
            self.group.branding_mode = value; self.group.save(update_fields=['branding_mode'])
            response = self.page('challenge-branding', self.month.pk)
            self.assertContains(response, label); self.assertNotContains(response, 'Logo Only'); self.assertNotContains(response, 'Logo + Banner')
            self.assertContains(self.page('group-branding'), '>'+label+'</option>')

    def test_challenge_cover_keeps_logo_and_other_header_styles(self):
        css = (Path(__file__).resolve().parent.parent/'static/css/challenge_identity.css').read_text()
        banner = css.split('.challenge-identity-banner{', 1)[1].split('}', 1)[0]
        self.assertIn('object-fit:cover', banner)
        self.assertIn('.challenge-identity:not(.team-identity) .challenge-identity-banner{object-position:center top}', css)
        self.assertIn('object-fit:contain', css.split('.challenge-identity-logo{', 1)[1].split('}', 1)[0])
