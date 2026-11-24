from datetime import date
from django.core.exceptions import PermissionDenied
from django.test import TestCase, TransactionTestCase
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse
from . import test_participating_host_scope as scope_fixture
from . import test_prompt_foundations as prompt_fixture
from .models import ChallengeMonth, BookSubmission, TeamAssignment, MonthEnrollment, Membership, ModifierProvenance
from .permissions import can_review_submission, restricted_host_team_ids
from .immersive import request_immersive, review_immersive, review_queue_requests
from .prompt_reservations import replace_selections
from .prompt_scoring import review_prompts
from .photo_submissions import create_photo_submission
from .photo_review import review_photo_submission, photo_review_version


class HostReviewAccessTests(TestCase):
    setUp=scope_fixture.ParticipatingHostScopeTests.setUp
    member=scope_fixture.ParticipatingHostScopeTests.member
    book=scope_fixture.ParticipatingHostScopeTests.book
    url=scope_fixture.ParticipatingHostScopeTests.url
    approve_payload=scope_fixture.ParticipatingHostScopeTests.approve_payload

    def access(self,value):
        ChallengeMonth.objects.filter(pk=self.month.pk).update(host_review_access=value)
        self.month.refresh_from_db()
        for b in [self.own_book,self.other_book]:b.month=self.month

    def test_matrix_scope_changes_and_no_team(self):
        for mode in ['restricted','unrestricted']:
            self.access(mode)
            self.assertTrue(can_review_submission(self.host.user,self.own_book))
            self.assertEqual(can_review_submission(self.host.user,self.other_book),mode=='unrestricted')
            self.assertTrue(can_review_submission(self.admin.user,self.other_book))
        self.access('restricted')
        TeamAssignment.objects.filter(month=self.month,participant=self.host).delete()
        self.assertEqual(restricted_host_team_ids(self.host.user,self.month),frozenset())
        self.assertFalse(can_review_submission(self.host.user,self.own_book))
        TeamAssignment.objects.create(month=self.month,participant=self.host,team=self.b)
        self.assertTrue(can_review_submission(self.host.user,self.other_book))
        MonthEnrollment.objects.filter(pk=self.enrollment.pk).update(is_active=False)
        self.assertIsNone(restricted_host_team_ids(self.host.user,self.month))
        self.access(None);self.assertEqual(restricted_host_team_ids(self.host.user,self.month),frozenset())

    def test_direct_review_posts_self_and_other_team(self):
        self.access('restricted');self.client.force_login(self.host.user)
        self.assertEqual(self.client.post(self.url('submission-review',self.other_book.pk),{'status':'approved','approved_pages':123}).status_code,403)
        response=self.client.post(self.url('submission-review',self.own_book.pk),self.approve_payload(self.own_book))
        self.assertEqual(response.status_code,302);self.own_book.refresh_from_db();self.assertEqual(self.own_book.status,'approved')
        self.access('unrestricted')
        self.assertEqual(self.client.post(self.url('submission-review',self.other_book.pk),self.approve_payload(self.other_book)).status_code,302)
        self.client.force_login(self.leader.user)
        self.assertEqual(self.client.get(self.url('submission-review',self.other_book.pk)).status_code,403)

    def test_setting_authorization_legacy_display_and_audit(self):
        url=self.url('challenge-host-list');self.client.force_login(self.host.user)
        self.assertEqual(self.client.post(url,{'action':'host_review_access','host_review_access':'unrestricted'}).status_code,403)
        self.client.force_login(self.member('organizer',group_role='owner').user)
        self.assertContains(self.client.get(url),'Legacy behavior retained')
        self.assertEqual(self.client.post(url,{'action':'host_review_access','host_review_access':'restricted'}).status_code,302)
        self.month.refresh_from_db();self.assertEqual(self.month.host_review_access,'restricted')


class HostQualificationSelfReviewTests(TestCase):
    setUp=prompt_fixture.PromptFoundationTests.setUp
    token=prompt_fixture.PromptFoundationTests.token
    prompt=prompt_fixture.PromptFoundationTests.prompt
    settings=prompt_fixture.PromptFoundationTests.settings

    def test_self_review_preserves_eligibility_and_other_roles(self):
        actor=self.users['participating'];member=self.members['participating']
        ChallengeMonth.objects.filter(pk=self.month.pk).update(immersive_enabled=True)
        self.month.refresh_from_db()
        book=BookSubmission.objects.create(month=self.month,participant=member,title='Self review',author='A',book_format='ebook',completed_on=date(2027,1,3),submitted_pages=100,status='approved',approved_pages=100)
        request=request_immersive(submission=book,actor=actor)
        self.assertTrue(review_queue_requests(actor=actor,month=self.month).filter(pk=request.pk).exists())
        review_immersive(request=request,actor=actor,decision='approved')
        self.assertEqual(ModifierProvenance.objects.get(source_type='immersive').amount,100)
        photo_prompt=self.prompt(kind='scavenger',number=2,submission_type='photo')
        for kind in ['bingo','scavenger']:
            p=self.prompt(kind=kind);self.settings(kind=kind,enabled=True)
            replace_selections(submission=book,actor=actor,selections={kind:p.pk});review_prompts(book,actor,{kind:'qualified'})
        p=photo_prompt
        photo=create_photo_submission(month_id=self.month.pk,actor=actor,prompt_id=p.pk,reader_confirmed=True)
        review_photo_submission(photo_id=photo.pk,actor=actor,decision='approved',reviewer_verified=True,version=photo_review_version(photo,actor))
        from .immersive import can_review_immersive
        leader_book=BookSubmission.objects.create(month=self.month,participant=self.members['leader'],title='Leader',author='A',book_format='ebook',completed_on=date(2027,1,3),submitted_pages=100)
        self.assertFalse(can_review_immersive(submission=leader_book,actor=self.users['leader']))
        ChallengeMonth.objects.filter(pk=self.month.pk).update(host_review_access=None)
        book.month.refresh_from_db()
        self.assertFalse(can_review_immersive(submission=book,actor=actor))


class HostAccessMigrationTests(TransactionTestCase):
    def test_existing_null_new_restricted(self):
        executor=MigrationExecutor(connection);executor.migrate([('core','0088_scheduled_report_cleanup')])
        try:
            apps=executor.loader.project_state([('core','0088_scheduled_report_cleanup')]).apps
            group=apps.get_model('core','ReadingGroup').objects.create(name='Legacy',slug='legacy-host')
            month=apps.get_model('core','ChallengeMonth').objects.create(group=group,name='Existing')
            executor=MigrationExecutor(connection);executor.migrate([('core','0089_host_review_access')])
            self.assertIsNone(ChallengeMonth.objects.get(pk=month.pk).host_review_access)
            self.assertEqual(ChallengeMonth.objects.create(group_id=group.pk,name='New').host_review_access,'restricted')
        finally:MigrationExecutor(connection).migrate([('core','0089_host_review_access')])
