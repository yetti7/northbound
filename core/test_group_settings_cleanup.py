from django.test import TestCase,override_settings
from django.urls import reverse
from . import test_branding as fixture
from .models import Membership,AuditEvent,HardcoverConnection
from .branding import BrandingForm,ChallengeBrandingForm

@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class GroupSettingsCleanupTests(TestCase):
    setUp=fixture.BrandingTests.setUp
    setUpFixture=fixture.BrandingTests.setUpFixture
    save=fixture.BrandingTests.save
    make_book=fixture.BrandingTests.make_book
    def page(self,route,user):
        self.client.force_login(user)
        return self.client.get(reverse(route,args=[self.group.slug]))
    def test_overview_preserves_art_and_other_controls_without_shortcuts(self):
        self.save(logo=fixture.upload(),banner=fixture.upload());self.group.refresh_from_db()
        self.group.branding_mode='banner';self.group.save(update_fields=['branding_mode'])
        for user in [self.admin,self.owner,self.mod,self.reader,self.host]:
            response=self.page('group-detail',user)
            self.assertEqual(response.status_code,200)
            self.assertContains(response,'group-identity-logo');self.assertContains(response,self.group.banner.url)
            self.assertNotContains(response,'href="'+reverse('group-branding',args=[self.group.slug])+'"')
            self.assertNotContains(response,'href="'+reverse('group-discord-notifications',args=[self.group.slug])+'"')
        response=self.page('group-detail',self.owner)
        self.assertContains(response,'Group Settings');self.assertContains(response,'Create Challenge');self.assertContains(response,'Access Code')
    def test_settings_has_one_link_per_section_and_no_art_display(self):
        self.save(logo=fixture.upload(),banner=fixture.upload());self.group.refresh_from_db()
        for user in [self.admin,self.owner]:
            response=self.page('group-edit',user)
            self.assertEqual(response.status_code,200)
            for route in ['group-branding','group-discord-notifications']:
                self.assertContains(response,'href="'+reverse(route,args=[self.group.slug])+'"',count=1)
            self.assertNotContains(response,self.group.logo.url);self.assertNotContains(response,self.group.banner.url)
            self.assertNotContains(response,'<h2>Branding</h2>',html=True)
            self.assertEqual(response.context['logical_parent_url'],reverse('group-detail',args=[self.group.slug]))
            self.assertContains(response,'class="back-button contextual-back"',count=1)
    def test_existing_role_and_group_boundaries_remain(self):
        for user in [self.reader,self.host,self.outsider]:
            self.assertEqual(self.page('group-edit',user).status_code,403)
        # Moderator hub access is read-only; existing section permissions remain.
        self.assertEqual(self.page('group-branding',self.mod).status_code,200)
        self.assertEqual(self.page('group-discord-notifications',self.mod).status_code,200)
        for user in [self.reader,self.host,self.outsider]:
            for route in ['group-branding','group-discord-notifications']:
                self.assertEqual(self.page(route,user).status_code,403)
    def test_delegation_does_not_expand_role_specific_section_authority(self):
        member=Membership.objects.get(group=self.group,user=self.reader)
        member.permission_overrides={'manage_group_settings':True};member.save(update_fields=['permission_overrides'])
        response=self.page('group-edit',self.reader);self.assertEqual(response.status_code,200)
        self.assertNotContains(response,'Branding Settings');self.assertNotContains(response,'Discord Notifications')
        mod=Membership.objects.get(group=self.group,user=self.mod)
        mod.permission_overrides={'manage_group_settings':True};mod.save(update_fields=['permission_overrides'])
        response=self.page('group-edit',self.mod);self.assertEqual(response.status_code,200)
        self.assertContains(response,'Branding Settings');self.assertContains(response,'Discord Notifications')
        self.assertFalse(self.page('group-discord-notifications',self.mod).context['editable'])
    def test_section_return_and_cancel_destinations_unchanged(self):
        for route in ['group-branding','group-discord-notifications']:
            page=self.page(route,self.owner)
            self.assertEqual(page.context['logical_parent_url'],reverse('group-edit',args=[self.group.slug]))
            self.assertContains(page,'class="back-button contextual-back"',count=1)
            page=self.page(route,self.mod)
            self.assertEqual(page.context['logical_parent_url'],reverse('group-edit',args=[self.group.slug]))
        response=self.page('group-edit',self.owner)
        self.assertContains(response,'href="'+reverse('group-detail',args=[self.group.slug])+'">Cancel',count=2)
    def test_recommended_dimensions_are_advisory_for_both_forms(self):
        for form in [BrandingForm(instance=self.group),ChallengeBrandingForm(instance=self.month)]:
            self.assertIn('1000 × 1000',form.fields['logo'].help_text)
            self.assertIn('Transparent PNG',form.fields['logo'].help_text)
            self.assertIn('1920 × 480',form.fields['banner'].help_text)
            self.assertIn('away from edges',form.fields['banner'].help_text)
        for route,args in [('group-branding',[self.group.slug]),('challenge-branding',[self.group.slug,self.month.pk])]:
            self.client.force_login(self.admin);page=self.client.get(reverse(route,args=args))
            self.assertContains(page,'1000 × 1000');self.assertContains(page,'1920 × 480')
        form=BrandingForm({'branding_mode':'logo'},files={'logo':fixture.upload(size=(600,240)),'banner':fixture.upload(size=(900,300))},instance=self.group)
        self.assertTrue(form.is_valid(),form.errors)

    def test_moderator_hub_contains_no_editing_or_connection_data_and_rejects_posts(self):
        page=self.page('group-edit',self.mod)
        self.assertEqual(page.status_code,200);self.assertContains(page,'Read-only')
        self.assertContains(page,'Branding Settings');self.assertContains(page,'Discord Notifications')
        self.assertNotIn('<form',page.content.decode().split('<main',1)[1]);self.assertNotContains(page,'API Token');self.assertNotContains(page,'Disconnect')
        self.assertNotContains(page,'name="timezone"');self.assertNotContains(page,'Save')
        original=self.group.name;before=(AuditEvent.objects.count(),HardcoverConnection.objects.count())
        for payload in [{'action':'save_group','name':'Forbidden change'},{'action':'test_existing'},{'action':'save_hardcover','api_token':'not-a-token'}]:
            self.assertEqual(self.client.post(reverse('group-edit',args=[self.group.slug]),payload).status_code,403)
        self.group.refresh_from_db();self.assertEqual(self.group.name,original)
        self.assertEqual(before,(AuditEvent.objects.count(),HardcoverConnection.objects.count()))
        overview=self.page('group-detail',self.mod);self.assertContains(overview,'Group Settings')
        for route in ['group-branding','group-discord-notifications']:
            self.assertEqual(self.client.post(reverse(route,args=[self.group.slug]),{}).status_code,403)

    def test_owner_save_retains_existing_redirect_and_audit(self):
        self.client.force_login(self.owner)
        response=self.client.post(reverse('group-edit',args=[self.group.slug]),{'action':'save_group','name':'Owner edited Group','timezone':'America/New_York'})
        self.assertRedirects(response,reverse('group-detail',args=[self.group.slug]),fetch_redirect_response=False)
        self.group.refresh_from_db();self.assertEqual(self.group.name,'Owner edited Group')
        self.assertTrue(AuditEvent.objects.filter(actor=self.owner,group=self.group,action='group.updated').exists())
