"""Focused regressions for the v1.0.8 consolidated-review blockers."""
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import test_immersive as immersive_fixture
from . import test_recovery_scoring as recovery_fixture
from .immersive import review_queue_requests, review_immersive, withdraw_immersive
from .models import (ImmersiveRequest, RegistrationAgreementAcceptance, BookSubmission,
    ChallengeMonth, ChallengeStaffAssignment, ModifierProvenance, ThemeClaim,
    MonthEnrollment, TeamAssignment, RecoveryOperation, ProgressCheckpoint, ProgressCheckpointResult)
from .recovery_scoring import purge_submission, submission_purge_impact, submission_recovery_label
from .recovery_domain import challenge_purge_impact
from .review_attention import challenge_attention_count, needs_attention_summary


class RecoveryProtectedRecordTests(TestCase):
    setUp=recovery_fixture.RecoveryScoringTests.setUp
    request=recovery_fixture.RecoveryScoringTests.request

    def snapshot(self):
        return {model._meta.label:list(model.objects.order_by('pk').values()) for model in (
            BookSubmission,ModifierProvenance,ThemeClaim,MonthEnrollment,TeamAssignment,
            ImmersiveRequest,RegistrationAgreementAcceptance)}

    def test_submission_preview_and_purge_preserve_immersive_history(self):
        for status in ('pending','approved','rejected','withdrawn','revoked'):
            claim=ImmersiveRequest.objects.create(submission=self.submission,requester=self.reader_user,
                captured_percent=100,status=status)
            preview=submission_purge_impact(self.submission)
            self.assertEqual({item.label:item.count for item in preview.items}['Blocking Immersive requests'],1)
            self.assertIn('Purge blocked',preview.warnings[0])
            before=self.snapshot()
            with self.assertRaisesMessage(ValidationError,'Purge blocked'):
                purge_submission(submission=self.submission,recovery_request=self.request(
                    action='submission.purge',target=self.submission,tier=3,password='owner-password-481!'))
            self.assertEqual(self.snapshot(),before)
            claim.delete()  # Synthetic rows only; exercise each protected status.
        claim=ImmersiveRequest.objects.create(submission=self.submission,requester=self.reader_user,captured_percent=50)
        self.client.force_login(self.root)
        url=reverse('recovery-submission-purge',args=[self.submission.pk])
        self.assertContains(self.client.get(url),'Blocking Immersive requests')
        before=self.snapshot()
        response=self.client.post(url,{'reason':'Synthetic repair','confirmation':submission_recovery_label(self.submission),
            'current_password':'owner-password-481!'})
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Purge blocked')
        self.assertEqual(self.snapshot(),before)
        self.assertFalse(RecoveryOperation.objects.filter(result='succeeded',action='submission.purge').exists())

    def test_challenge_preview_and_purge_preserve_acceptance_and_immersive(self):
        acceptance=RegistrationAgreementAcceptance.objects.create(month=self.month,reader=self.reader,
            enrollment=self.enrollment,agreement_text='Accepted wording',agreement_version='v1')
        preview=challenge_purge_impact(self.month)
        self.assertEqual({item.label:item.count for item in preview.items}['Blocking Registration Agreement acceptances'],1)
        self.assertIn('Purge blocked',preview.warnings[0])
        self.client.force_login(self.root)
        url=reverse('recovery-challenge-purge',args=[self.month.pk])
        self.assertContains(self.client.get(url),'Blocking Registration Agreement acceptances')
        before=self.snapshot()
        response=self.client.post(url,{'reason':'Synthetic repair','confirmation':self.month.name,'current_password':'owner-password-481!'})
        self.assertEqual(response.status_code,200);self.assertContains(response,'Purge blocked')
        self.assertEqual(self.snapshot(),before)
        # Both new dependency types are reflected independently in the preview.
        ImmersiveRequest.objects.create(submission=self.submission,requester=self.reader_user,captured_percent=50)
        self.assertEqual({item.label:item.count for item in challenge_purge_impact(self.month).items}['Blocking Immersive requests'],1)
        self.client.force_login(self.reader_user)
        self.assertEqual(self.client.post(url,{'reason':'Attempt'}).status_code,403)

    def test_acceptance_enrollment_dependency_is_also_reported(self):
        record=RegistrationAgreementAcceptance.objects.create(month=self.month,reader=self.reader,
            enrollment=self.enrollment,agreement_text='Accepted wording',agreement_version='v1')
        other=ChallengeMonth.objects.create(group=self.group,name='Other Challenge')
        # Recovery must recognize both protected FKs even in inconsistent data.
        RegistrationAgreementAcceptance.objects.filter(pk=record.pk).update(month=other)
        preview=challenge_purge_impact(self.month)
        self.assertEqual({item.label:item.count for item in preview.items}['Blocking Registration Agreement acceptances'],1)
        self.client.force_login(self.root)
        before=self.snapshot()
        response=self.client.post(reverse('recovery-challenge-purge',args=[self.month.pk]),
            {'reason':'Synthetic repair','confirmation':self.month.name,'current_password':'owner-password-481!'})
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Purge blocked')
        self.assertEqual(self.snapshot(),before)

    def test_challenge_purge_is_blocked_by_immersive_without_acceptance(self):
        ImmersiveRequest.objects.create(submission=self.submission,requester=self.reader_user,captured_percent=50)
        before=self.snapshot()
        self.client.force_login(self.root)
        response=self.client.post(reverse('recovery-challenge-purge',args=[self.month.pk]),
            {'reason':'Synthetic repair','confirmation':self.month.name,'current_password':'owner-password-481!'})
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Purge blocked')
        self.assertEqual(self.snapshot(),before)



class ImmersiveAttentionTests(TestCase):
    setUp=immersive_fixture.ImmersiveTests.setUp
    actor=immersive_fixture.ImmersiveTests.actor
    submission=immersive_fixture.ImmersiveTests.submission
    enable=immersive_fixture.ImmersiveTests.enable
    request=immersive_fixture.ImmersiveTests.request
    approve=immersive_fixture.ImmersiveTests.approve

    def clear_notices(self):
        ChallengeStaffAssignment.objects.filter(month=self.month).update(host_assignment_notice_seen_at=timezone.now())

    def test_active_and_completed_include_only_actionable_immersive(self):
        self.enable();claim=self.request();self.clear_notices()
        for stage in ('active','finalizing','completed'):
            ChallengeMonth.objects.filter(pk=self.month.pk).update(status=stage);self.month.refresh_from_db()
            self.assertEqual(challenge_attention_count(self.host,self.month),1)
            self.assertEqual(needs_attention_summary(self.host)['total'],1)
        # Completed book reviews and historical checkpoints must not reappear.
        self.submission(status='pending')
        checkpoint=ProgressCheckpoint.objects.create(month=self.month,scheduled_at=timezone.now(),fixed_target_pages=100,target_basis='fixed')
        ProgressCheckpointResult.objects.create(checkpoint=checkpoint,participant=self.reader,evaluated_at=timezone.now(),
            threshold_percentage=25,progress_basis='base',target_basis='fixed',target_pages=100,required_pages=25,progress_pages=0,outcome='below')
        self.assertEqual(needs_attention_summary(self.host)['total'],1)
        self.approve(claim)
        self.assertEqual(needs_attention_summary(self.host)['total'],0)
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status='archived');self.month.refresh_from_db()
        self.assertEqual(challenge_attention_count(self.host,self.month),0)

    def test_team_scope_group_owner_and_host_permissions(self):
        self.enable();claim=self.request();leader=self.actor('team_leader');owner=self.actor('owner');self.clear_notices()
        for actor in (self.host,owner,leader):self.assertEqual(needs_attention_summary(actor)['total'],1)
        self.assertEqual(needs_attention_summary(self.reader.user)['total'],0)
        moderator=self.actor('moderator');self.assertEqual(needs_attention_summary(moderator)['total'],0)
        TeamAssignment.objects.filter(participant=self.reader).update(ended_at=timezone.now())
        self.assertEqual(needs_attention_summary(leader)['total'],0)
        self.assertEqual(needs_attention_summary(self.host)['total'],1)
        self.assertEqual(needs_attention_summary(owner)['total'],1)
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status='completed')
        self.month.refresh_from_db()
        self.assertEqual(needs_attention_summary(owner)['total'],1)
        self.assertEqual(needs_attention_summary(self.host)['total'],1)
        self.assertEqual(needs_attention_summary(leader)['total'],0)
        self.client.force_login(owner)
        response=self.client.get(reverse('needs-attention'))
        self.assertContains(response,'Open Review Queue')
        self.assertContains(response,'Immersive review')

    def test_unapproved_books_and_reviewed_requests_do_not_count(self):
        self.enable();self.clear_notices()
        pending=self.submission(status='pending');self.request(pending)
        self.assertEqual(challenge_attention_count(self.host,self.month),1)  # Book only.
        rejected=self.submission(status='rejected');self.request(rejected)
        self.assertEqual(challenge_attention_count(self.host,self.month),1)
        for decision in ('rejected','withdrawn','revoked'):
            claim=self.request()
            if decision=='withdrawn':withdraw_immersive(request=claim,actor=self.reader.user)
            else:
                if decision=='revoked':self.approve(claim)
                review_immersive(request=claim,actor=self.host,decision=decision,reason='Synthetic review')
        self.assertFalse(review_queue_requests(actor=self.host,month=self.month).exists())
        self.assertEqual(challenge_attention_count(self.host,self.month),1)
        approved=self.submission();claim=self.request(approved)
        approved.is_removed=True;approved.save(update_fields=['is_removed'])
        self.assertEqual(challenge_attention_count(self.host,self.month),1)
