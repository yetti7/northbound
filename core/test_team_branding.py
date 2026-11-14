from pathlib import Path
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from . import test_branding as fixture
from .test_branding import upload
from django.utils import timezone
from .models import (Team, ChallengeMonth, Membership, ChallengeStaffAssignment, AuditEvent,
    BookSubmission, ModifierProvenance, TeamAssignment, MonthEnrollment, UserProfile)
from .branding import (TeamBrandingForm, can_manage_team_branding, can_view_branding_settings,
    update_branding, team_branding)


class TeamBrandingTests(TestCase):
    setUpFixture = fixture.BrandingTests.setUpFixture
    status = fixture.BrandingTests.status
    make_book = fixture.BrandingTests.make_book

    def setUp(self):
        fixture.BrandingTests.setUp(self)
        self.team_leader = self.staff('art-leader', 'team_leader', team=self.team)
        self.floater = self.staff('art-floater', 'floater')

    def staff(self, name, role, team=None):
        user = get_user_model().objects.create_user(name)
        UserProfile.objects.create(user=user)
        member = Membership.objects.create(user=user, group=self.group, role='member')
        if role == 'team_leader':
            MonthEnrollment.objects.create(month=self.month, participant=member)
            TeamAssignment.objects.create(month=self.month, participant=member, team=team)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=member, role=role, team=team)
        return user

    def branding_url(self, team=None):
        team = team or self.team
        return reverse('team-branding', args=[team.month.group.slug, team.month_id, team.pk])

    def save(self, actor=None, team=None, data=None, **files):
        return update_branding(actor=actor or self.admin, target=team or self.team,
            data=data or {}, files=files)

    def test_role_matrix_additive_roles_and_current_team_scope(self):
        for actor in [self.admin, self.owner, self.mod, self.host, self.team_leader]:
            self.assertTrue(can_manage_team_branding(actor, self.team))
        for actor in [self.reader, self.floater, self.outsider]:
            self.assertFalse(can_manage_team_branding(actor, self.team))
        for actor in [self.reader, self.floater, self.outsider]:
            self.client.force_login(actor)
            self.assertEqual(self.client.get(self.branding_url()).status_code,403)
            self.assertEqual(self.client.post(self.branding_url(),{'logo':upload()}).status_code,403)
            with self.assertRaises(PermissionDenied):self.save(actor=actor,logo=upload())
        self.assertEqual(AuditEvent.objects.count(),0)
        self.assertFalse(can_manage_team_branding(self.team_leader, self.team_b))
        # Participating Host and Group Moderator authority is artwork-only and challenge-wide.
        for actor in [self.host, self.mod]:
            self.assertTrue(can_manage_team_branding(actor, self.team_b))
        self.client.force_login(self.team_leader)
        self.assertEqual(self.client.post(self.branding_url(self.team_b), {'logo':upload()}).status_code, 403)
        with self.assertRaises(PermissionDenied):
            self.save(actor=self.team_leader, team=self.team_b, logo=upload())
        member = Membership.objects.get(user=self.floater, group=self.group)
        member.role='moderator';member.save(update_fields=['role'])
        self.assertTrue(can_manage_team_branding(self.floater, self.team))

    def test_lifecycle_and_draft_visibility_at_endpoint_and_service(self):
        for status in ['draft','upcoming','active','finalizing','completed','archived']:
            self.status(status)
            for actor in [self.admin,self.owner,self.host,self.mod,self.team_leader]:
                allowed = status in ['draft','upcoming','active'] and not (status=='draft' and actor in [self.mod,self.team_leader])
                self.assertEqual(can_manage_team_branding(actor,self.team),allowed,(status,actor.pk))
                self.client.force_login(actor)
                response=self.client.post(self.branding_url(),{'logo':upload()})
                self.assertEqual(response.status_code,302 if allowed else 403,(status,actor.pk))
                if not allowed:
                    before=AuditEvent.objects.count()
                    with self.assertRaises(PermissionDenied):self.save(actor=actor,logo=upload())
                    self.assertEqual(AuditEvent.objects.count(),before)
            if status in ['finalizing','completed','archived']:
                page=self.client.get(self.branding_url())
                self.assertContains(page,'Read-only')
                self.assertNotContains(page,'Save Branding')
                self.assertTrue(can_view_branding_settings(self.team_leader,self.team))

    def test_revocation_and_stale_lifecycle_are_rechecked(self):
        self.assertTrue(can_manage_team_branding(self.team_leader,self.team))
        ChallengeStaffAssignment.objects.filter(membership__user=self.team_leader).update(ended_at=timezone.now())
        with self.assertRaises(PermissionDenied):self.save(actor=self.team_leader,logo=upload())
        Membership.objects.filter(user=self.mod,group=self.group).update(is_active=False)
        with self.assertRaises(PermissionDenied):self.save(actor=self.mod,logo=upload())
        self.host.is_active=False;self.host.save(update_fields=['is_active'])
        self.assertFalse(can_manage_team_branding(self.host,self.team))
        stale=self.team
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status='finalizing')
        with self.assertRaises(PermissionDenied):self.save(team=stale,logo=upload())
        self.assertEqual(AuditEvent.objects.count(),0)

    def test_group_and_challenge_url_isolation_and_no_private_fields(self):
        self.client.force_login(self.admin)
        wrong=reverse('team-branding',args=[self.foreign.slug,self.month.pk,self.team.pk])
        self.assertEqual(self.client.get(wrong).status_code,404)
        self.assertEqual(self.client.post(wrong,{'logo':upload()}).status_code,404)
        other=ChallengeMonth.objects.create(group=self.group,name='Other',starts_on=self.month.starts_on,ends_on=self.month.ends_on,status='active')
        wrong=reverse('team-branding',args=[self.group.slug,other.pk,self.team.pk])
        self.assertEqual(self.client.get(wrong).status_code,404)
        self.client.force_login(self.team_leader)
        page=self.client.get(self.branding_url());self.assertContains(page,'1000 × 1000');self.assertContains(page,'1920 × 480')
        self.assertContains(page,'Back to Team');self.assertNotContains(page,'name="branding_mode"')
        self.assertNotContains(page,'978321');self.assertNotContains(page,'Private title')
        self.assertNotContains(page,'Forbidden violet team')

    def test_fields_allowlist_noop_audit_and_no_scoring_side_effects(self):
        models=[BookSubmission,ModifierProvenance,TeamAssignment,MonthEnrollment]
        before={m.__name__:list(m.objects.order_by('pk').values()) for m in models}
        form,saved=self.save(data={'name':'FORGED','month':999,'color':'#ffffff','branding_mode':'banner'})
        self.assertFalse(saved);self.assertEqual(AuditEvent.objects.count(),0)
        form,saved=self.save(actor=self.team_leader,logo=upload())
        self.assertTrue(saved,form.errors);self.team.refresh_from_db()
        self.assertTrue(self.team.logo.name.startswith('branding/team/'))
        self.assertEqual(self.team.name,'Team A');self.assertEqual(self.team.month_id,self.month.pk)
        event=AuditEvent.objects.get(action='branding.updated')
        self.assertEqual((event.actor_id,event.group_id,event.object_type,event.object_id),(self.team_leader.pk,self.group.pk,'Team',str(self.team.pk)))
        self.assertIn('"challenge_id": '+str(self.month.pk),event.summary)
        self.assertIn('"before"',event.summary);self.assertIn('"after"',event.summary)
        self.assertFalse(self.save()[1]);self.assertEqual(AuditEvent.objects.count(),1)
        for model in models:self.assertEqual(list(model.objects.order_by('pk').values()),before[model.__name__])

    def test_upload_formats_validation_replace_clear_and_retained_files(self):
        for fmt in ['PNG','JPEG','WEBP']:
            form,saved=self.save(logo=upload(fmt=fmt))
            self.assertTrue(saved,form.errors)
        for bad in [SimpleUploadedFile('bad.png',b'not an image'),upload('bad.gif','GIF'),
                    SimpleUploadedFile('huge.png',b'x'*(5*1024*1024+1)),upload(size=(6001,1)),upload(size=(4001,4000))]:
            count=AuditEvent.objects.count();form,saved=self.save(logo=bad)
            self.assertFalse(saved);self.assertIn('logo',form.errors);self.assertEqual(AuditEvent.objects.count(),count)
        self.team.refresh_from_db();old=self.team.logo.path
        self.assertTrue(self.save(logo=upload(),banner=upload())[1]);self.assertTrue(Path(old).is_file())
        self.team.refresh_from_db();paths=[self.team.logo.path,self.team.banner.path]
        self.assertTrue(self.save(data={'logo-clear':'on','banner-clear':'on'})[1]);self.team.refresh_from_db()
        self.assertFalse(self.team.logo);self.assertFalse(self.team.banner)
        self.assertTrue(all(Path(p).is_file() for p in paths))

    def test_independent_inheritance_modes_missing_files_and_live_updates(self):
        self.group.logo.save('group.png',upload(),save=True);self.group.banner.save('group-banner.png',upload(),save=True)
        self.month.logo.save('challenge.png',upload(),save=True);self.month.banner.save('challenge-banner.png',upload(),save=True)
        self.group.branding_mode='banner';self.group.save(update_fields=['branding_mode'])
        self.team.month=self.month;self.month.group=self.group
        for own_logo,own_banner in [(False,False),(True,False),(False,True),(True,True)]:
            self.team.logo='';self.team.banner=''
            if own_logo:self.team.logo.save('team.png',upload(),save=False)
            if own_banner:self.team.banner.save('team-banner.png',upload(),save=False)
            self.team.save()
            art=team_branding(self.reader,self.team)
            self.assertEqual(art['effective_logo'],self.team.logo.url if own_logo else self.month.logo.url)
            self.assertEqual(art['effective_banner'],self.team.banner.url if own_banner else self.month.banner.url)
        self.group.branding_mode='logo'
        self.assertEqual(team_branding(self.reader,self.team)['effective_banner'],'')
        self.assertNotIn('team_banner',team_branding(self.reader,self.team))
        self.team.logo='missing';self.team.banner='missing';self.month.logo='';self.month.banner='';self.group.branding_mode='banner'
        self.assertEqual(team_branding(self.reader,self.team)['effective_logo'],self.group.logo.url)
        self.group.logo='';self.group.banner=''
        art=team_branding(self.reader,self.team);self.assertEqual(art['effective_logo'],'');self.assertEqual(art['effective_banner'],'')
        self.assertEqual(team_branding(self.outsider,self.team),{})
        self.team.logo='';self.team.banner='';self.team.save(update_fields=['logo','banner'])
        self.group.logo.save('new-group.png',upload(),save=True)
        self.month.save(update_fields=['logo','banner'])
        fresh=Team.objects.select_related('month__group').get(pk=self.team.pk)
        self.assertEqual(team_branding(self.reader,fresh)['effective_logo'],self.group.logo.url)
        self.assertFalse(fresh.logo)

    def test_identity_survives_rename_archive_and_assignment_change_and_other_team_is_independent(self):
        self.save(logo=upload());self.team.refresh_from_db();logo=self.team.logo.name
        other=ChallengeMonth.objects.create(group=self.group,name='Other',starts_on=self.month.starts_on,ends_on=self.month.ends_on,status='active')
        twin=Team.objects.create(month=other,name=self.team.name)
        self.assertFalse(twin.logo);self.assertFalse(twin.banner)
        self.team.name='Renamed';self.team.is_archived=True;self.team.save(update_fields=['name','is_archived'])
        TeamAssignment.objects.filter(team=self.team).update(ended_at=timezone.now())
        self.team.refresh_from_db();self.assertEqual(self.team.logo.name,logo)
        self.status('completed');self.team.month=self.month
        self.assertTrue(team_branding(self.reader,self.team)['effective_logo'])

    def test_navigation_does_not_grant_team_administration(self):
        self.client.force_login(self.team_leader)
        page=self.client.get(reverse('team-detail',args=[self.group.slug,self.month.pk,self.team.pk]))
        self.assertContains(page,self.branding_url());self.assertNotContains(page,'>Edit Team</a>')
        other=self.client.get(reverse('team-detail',args=[self.group.slug,self.month.pk,self.team_b.pk]))
        self.assertNotContains(other,self.branding_url(self.team_b))
        self.assertEqual(self.client.get(reverse('team-edit',args=[self.group.slug,self.month.pk,self.team.pk])).status_code,403)

    def test_inactive_and_ended_staff_roles_do_not_keep_authority(self):
        for actor in [self.admin,self.owner,self.mod,self.host,self.team_leader]:
            actor.is_active=False
            self.assertFalse(can_manage_team_branding(actor,self.team))
            actor.is_active=True
        for actor in [self.host,self.team_leader]:
            ChallengeStaffAssignment.objects.filter(membership__user=actor,ended_at__isnull=True).update(ended_at=timezone.now())
            with self.assertRaises(PermissionDenied):self.save(actor=actor,logo=upload())
        self.assertEqual(AuditEvent.objects.count(),0)

    def test_service_locks_challenge_before_team_and_ignores_forged_context(self):
        order=[]
        month_lock=ChallengeMonth.objects.select_for_update
        team_lock=Team.objects.select_for_update
        def lock_month(*args,**kwargs):
            order.append('challenge');return month_lock(*args,**kwargs)
        def lock_team(*args,**kwargs):
            order.append('team');return team_lock(*args,**kwargs)
        with patch.object(ChallengeMonth.objects,'select_for_update',side_effect=lock_month), patch.object(Team.objects,'select_for_update',side_effect=lock_team):
            self.assertFalse(self.save()[1])
        self.assertEqual(order,['challenge','team'])
        self.status('completed')
        forged=Team(pk=self.team.pk,month_id=999,name='Forged')
        with self.assertRaises(PermissionDenied):self.save(team=forged,logo=upload())
        self.assertEqual(AuditEvent.objects.count(),0)
