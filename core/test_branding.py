from io import BytesIO
from pathlib import Path
import tempfile
from PIL import Image
from django.test import TestCase,override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from . import test_recaps as fixture
from .models import Membership,AuditEvent,BookSubmission,ModifierProvenance,ChallengeStaffAssignment
from .branding import BrandingForm,can_manage_branding,update_branding,community_branding,image_asset
from .recaps import build_recap


def upload(name='../../unsafe.png',fmt='PNG',size=(1400,600)):
    output=BytesIO();Image.new('RGB',size,'purple').save(output,format=fmt)
    return SimpleUploadedFile(name,output.getvalue(),content_type='image/'+fmt.lower())


class BrandingTests(TestCase):
    setUpFixture=fixture.RecapTests.setUp
    status=fixture.RecapTests.status
    make_book=fixture.RecapTests.make_book

    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.settings_context=override_settings(MEDIA_ROOT=self.directory.name);self.settings_context.enable();self.addCleanup(self.settings_context.disable)
        self.setUpFixture()
        # The participating Host fixture is a Group Owner; separate roles here.
        Membership.objects.filter(pk=self.members[self.host.pk].pk).update(role='member')
        self.owner=self.other;Membership.objects.filter(pk=self.members[self.other.pk].pk).update(role='owner')
        self.mod=self.leader;Membership.objects.filter(pk=self.members[self.leader.pk].pk).update(role='moderator')

    def save(self,target=None,actor=None,**files):
        return update_branding(actor=actor or self.admin,target=target or self.group,data={'branding_mode':'logo'},files=files)

    def test_group_permissions_readonly_moderator_and_unauthorized_posts(self):
        url=reverse('group-branding',args=[self.group.slug])
        for user,status in [(self.admin,200),(self.owner,200),(self.mod,200),(self.host,403),(self.reader,403),(self.outsider,403)]:
            self.client.force_login(user);response=self.client.get(url);self.assertEqual(response.status_code,status)
            if user==self.mod:self.assertNotContains(response,'type="file"')
        Membership.objects.filter(pk=self.members[self.leader.pk].pk).update(permission_overrides={'manage_group_settings':True})
        self.client.force_login(self.mod)
        self.assertEqual(self.client.post(url,{'logo':upload()}).status_code,403)
        with self.assertRaises(PermissionDenied):self.save(actor=self.mod,logo=upload())
        self.assertFalse(self.group.logo)

    def test_challenge_staff_permissions_and_terminal_lifecycle_at_service_boundary(self):
        self.assertTrue(can_manage_branding(self.admin,self.month))
        self.assertTrue(can_manage_branding(self.owner,self.month))
        self.assertTrue(can_manage_branding(self.host,self.month))
        self.assertFalse(can_manage_branding(self.reader,self.month))
        self.assertFalse(can_manage_branding(self.mod,self.month))
        Membership.objects.filter(pk=self.members[self.leader.pk].pk).update(permission_overrides={'manage_months':True})
        self.assertTrue(can_manage_branding(self.mod,self.month))
        foreign=reverse('challenge-branding',args=[self.foreign.slug,self.month.pk])
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(foreign).status_code,404)
        for status in ['completed','archived']:
            self.status(status)
            with self.assertRaises(PermissionDenied):self.save(target=self.month,logo=upload())
        self.status('active')
        self.client.force_login(self.host)
        self.assertEqual(self.client.post(reverse('challenge-branding',args=[self.group.slug,self.month.pk]),{'logo':upload()}).status_code,302)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(reverse('challenge-branding',args=[self.group.slug,self.month.pk])).status_code,403)

    def test_upload_resize_replace_clear_and_mode_preservation(self):
        form,saved=self.save(logo=upload(),banner=upload('banner.webp','WEBP'))
        self.assertTrue(saved,form.errors);self.group.refresh_from_db()
        first=self.group.logo.name;banner=self.group.banner.name
        self.assertTrue(first.startswith('branding/readinggroup/'));self.assertNotIn('unsafe',first)
        with Image.open(self.group.logo.path) as image:self.assertEqual(image.size,(800,343))
        self.assertTrue(AuditEvent.objects.filter(action='branding.updated',actor=self.admin,object_id=str(self.group.pk)).exists())
        form,saved=update_branding(actor=self.admin,target=self.group,data={'branding_mode':'banner'},files={})
        self.group.refresh_from_db();self.assertEqual(self.group.logo.name,first);self.assertEqual(self.group.banner.name,banner)
        form,saved=self.save(logo=upload('new.jpeg','JPEG'));self.group.refresh_from_db();self.assertNotEqual(self.group.logo.name,first)
        form,saved=update_branding(actor=self.admin,target=self.group,data={'branding_mode':'logo','logo-clear':'on'},files={})
        self.group.refresh_from_db();self.assertFalse(self.group.logo);self.assertEqual(self.group.banner.name,banner)

    def test_contents_size_dimension_validation(self):
        for data in [b'<svg xmlns="http://www.w3.org/2000/svg"/>',b'not an image']:
            form=BrandingForm({'branding_mode':'logo'},{'logo':SimpleUploadedFile('fake.png',data)},instance=self.group);self.assertFalse(form.is_valid())
        too_big=SimpleUploadedFile('huge.png',b'x'*(5*1024*1024+1))
        form=BrandingForm({'branding_mode':'logo'},{'logo':too_big},instance=self.group);self.assertFalse(form.is_valid())
        for file in [upload(fmt='GIF'),upload(size=(6001,1)),upload(size=(4100,4100))]:
            form=BrandingForm({'branding_mode':'logo'},{'logo':file},instance=self.group);self.assertFalse(form.is_valid())
        for fmt in ['PNG','JPEG','WEBP']:
            form=BrandingForm({'branding_mode':'logo'},{'logo':upload(fmt=fmt)},instance=self.group);self.assertTrue(form.is_valid(),form.errors)

    def test_branding_mode_inheritance_fallback_and_missing_media(self):
        self.save(logo=upload(),banner=upload());self.group.refresh_from_db()
        art=community_branding(self.reader,self.group,self.month)
        self.assertTrue(art['group_logo']);self.assertFalse(art.get('challenge_logo'));self.assertNotIn('group_banner',art)
        self.group.branding_mode='banner';self.group.save(update_fields=['branding_mode'])
        self.save(target=self.month,logo=upload(),banner=upload());self.month.refresh_from_db()
        art=community_branding(self.reader,self.group,self.month)
        self.assertTrue(all(art.get(key) for key in ['group_logo','group_banner','challenge_logo','challenge_banner']))
        Path(self.month.logo.path).unlink();self.assertFalse(community_branding(self.reader,self.group,self.month)['challenge_logo'])
        self.assertEqual(community_branding(self.outsider,self.group,self.month),{})

    def test_recap_privacy_single_context_and_multiple_groups(self):
        self.save(logo=upload());self.group.refresh_from_db()
        self.save(target=self.month,logo=upload());self.month.refresh_from_db()
        own=build_recap(viewer=self.reader,group_id=self.group.pk,challenge_id=self.month.pk)
        self.assertTrue(own['branding']['group_logo']);self.assertTrue(own['branding']['challenge_logo'])
        self.assertEqual(build_recap(viewer=self.reader)['branding'],{})
        self.assertEqual(build_recap(viewer=self.admin)['branding'],{})
        member=self.members[self.reader.pk];member.is_active=False;member.save()
        historical=build_recap(viewer=self.reader,challenge_id=self.month.pk)
        self.assertEqual(historical['branding'],{})
        self.client.force_login(self.reader)
        response=self.client.get(reverse('recap-wrapped'),{'challenge':self.month.pk})
        self.assertNotContains(response,'/media/branding/');self.assertContains(response,'northbound-header-dark.png')

    def test_rendering_and_no_scoring_side_effects(self):
        before=list(BookSubmission.objects.values());modifiers=list(ModifierProvenance.objects.values())
        self.save(logo=upload(),banner=upload());self.group.refresh_from_db()
        self.save(target=self.month,logo=upload());self.month.refresh_from_db()
        self.client.force_login(self.reader)
        for name,args in [('group-detail',[self.group.slug]),('month-list',[self.group.slug]),('month-detail',[self.group.slug,self.month.pk])]:
            response=self.client.get(reverse(name,args=args));self.assertEqual(response.status_code,200);self.assertContains(response,'data-community-image')
        for name in ['recap-dashboard','recap-wrapped']:
            response=self.client.get(reverse(name),{'group':self.group.pk,'challenge':self.month.pk});self.assertEqual(response.status_code,200);self.assertContains(response,self.month.logo.url)
        self.assertEqual(before,list(BookSubmission.objects.values()));self.assertEqual(modifiers,list(ModifierProvenance.objects.values()))

    def test_field_allowlist_noop_and_stale_lifecycle_revalidation(self):
        name=self.group.name;audits=AuditEvent.objects.count()
        form,saved=update_branding(actor=self.admin,target=self.group,data={'branding_mode':'logo','name':'Not authorized','join_code':'FAKE'},files={})
        self.assertFalse(saved);self.assertEqual(AuditEvent.objects.count(),audits)
        self.group.refresh_from_db();self.assertEqual(self.group.name,name)
        # A previously fetched active record must not bypass the current lock.
        from .models import ChallengeMonth
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status='completed')
        with self.assertRaises(PermissionDenied):self.save(target=self.month,logo=upload())
        self.assertFalse(ChallengeMonth.objects.get(pk=self.month.pk).logo)

    def test_failed_file_contents_hide_without_breaking_page(self):
        self.save(logo=upload());self.group.refresh_from_db()
        Path(self.group.logo.path).write_bytes(b'corrupt fixture')
        self.client.force_login(self.reader)
        response=self.client.get(reverse('group-detail',args=[self.group.slug]))
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'data-community-image')
        self.assertContains(response,'js/community_branding.js')
