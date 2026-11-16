"""Initial Theme decisions preserve approved book evidence and frozen awards."""
from datetime import date
from unittest.mock import patch

from django.core.exceptions import ValidationError, PermissionDenied
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from . import test_submission_hotfix as fixture
from . import test_submission_editing as editing
from .models import (BookSubmission, ThemeClaim, MonthTheme, ModifierProvenance, AuditEvent,
    Team, TeamAssignment, ProgressCheckpoint, ProgressCheckpointResult)
from .submission_review_matching import review_token, apply_unified_review
from .score_aggregation import challenge_score_totals, challenge_team_totals
from .submission_activity import submission_timeline


class PendingThemeReviewTests(TestCase):
    actor = editing.SubmissionEditingTests.actor

    def setUp(self):
        fixture.SubmissionHotfixTests.setUp(self)
        self.reviewer = self.actor('team_leader')
        self.book = BookSubmission.objects.create(month=self.month, participant=self.reader,
            title='Synthetic Approved Book', author='Author', book_format='ebook',
            completed_on=date(2026,11,14), submitted_pages=384, approved_pages=384,
            status='approved', reviewed_by=self.reviewer, reviewed_at=timezone.now())
        self.theme = MonthTheme.objects.create(month=self.month,name='Synthetic Week 2',
            starts_on=date(2026,11,10),ends_on=date(2026,11,16),bonus_percentage=25)
        self.claim = ThemeClaim.objects.create(submission=self.book,theme=self.theme)
        self.client.force_login(self.reviewer)
        self.url=reverse('submission-review',args=[self.month.group.slug,self.month.pk,self.book.pk])
        self.queue=reverse('review-queue',args=[self.month.group.slug,self.month.pk])
        self.original={k:getattr(self.book,k) for k in ['status','approved_pages','submitted_pages','title','author',
            'book_format','catalog_book_id','catalog_edition_id','scoring_catalog_edition_id',
            'reviewed_by_id','reviewed_at','completed_on','verification_method']}

    def payload(self, theme_status, **changes):
        page=self.client.get(self.url)
        claims=list(self.book.theme_claims.order_by('pk'))
        data={'status':'approved','approved_pages':'384','verification_url':self.book.verification_url,
            'review_notes':self.book.review_notes,'matching_version':page.context['matching_form']['matching_version'].value(),
            'claims-TOTAL_FORMS':str(len(claims)),'claims-INITIAL_FORMS':str(len(claims)),
            'claims-MIN_NUM_FORMS':'0','claims-MAX_NUM_FORMS':'1000'}
        for i,claim in enumerate(claims):
            data.update({f'claims-{i}-id':str(claim.pk),f'claims-{i}-submission':str(self.book.pk),
                         f'claims-{i}-status':theme_status if claim.pk==self.claim.pk else claim.status})
        return {**data,**changes}

    def assert_book_preserved(self):
        self.book.refresh_from_db()
        self.assertEqual({k:getattr(self.book,k) for k in self.original},self.original)

    def test_approve_384_page_book_pending_25_percent_theme(self):
        self.assertContains(self.client.get(self.queue),self.theme.name)
        page=self.client.get(self.url)
        self.assertTrue(page.context['form'].fields['approved_pages'].disabled)
        self.assertFalse(page.context['claim_formset'].forms[0].fields['status'].disabled)
        self.assertEqual(self.client.post(self.url,self.payload('approved')).status_code,302)
        self.assert_book_preserved();self.claim.refresh_from_db()
        self.assertEqual((self.claim.status,self.claim.approved_bonus_pages,self.claim.award_basis_pages,self.claim.award_percentage),('approved',96,384,25))
        self.assertEqual((self.book.bonus_pages,self.book.final_scored_pages),(96,480))
        self.assertEqual(challenge_score_totals(month=self.month)[self.reader.pk]['total_pages'],480)
        self.assertEqual(challenge_team_totals(month=self.month,team_ids=[self.team.pk])[self.team.pk],480)
        self.assertEqual(ModifierProvenance.objects.filter(source_reference=f'theme_claim:{self.claim.pk}',amount=96,is_active=True).count(),1)
        self.assertEqual(AuditEvent.objects.filter(action='submission.theme_approved',object_id=str(self.book.pk)).count(),1)
        self.assertFalse(AuditEvent.objects.filter(action='submission.approved',object_id=str(self.book.pk)).exists())
        self.assertNotContains(self.client.get(self.queue),self.theme.name)
        timeline=submission_timeline(self.reviewer,self.book)
        self.assertIn('Theme approved',[card['label'] for card in timeline['cards']])

    def test_autoapproved_book_review_attribution_is_not_rewritten(self):
        BookSubmission.objects.filter(pk=self.book.pk).update(reviewed_by=None)
        self.original['reviewed_by_id']=None
        self.assertEqual(self.client.post(self.url,self.payload('approved')).status_code,302)
        self.assert_book_preserved()
        self.assertFalse(AuditEvent.objects.filter(action='submission.approved').exists())

    def test_end_date_is_inclusive_and_later_review_still_qualifies(self):
        from datetime import datetime
        BookSubmission.objects.filter(pk=self.book.pk).update(completed_on=self.theme.ends_on)
        self.original['completed_on']=self.theme.ends_on
        late=timezone.make_aware(datetime(2026,11,25,12))
        with patch('django.utils.timezone.now',return_value=late):
            self.assertEqual(self.client.post(self.url,self.payload('approved')).status_code,302)
        self.assert_book_preserved();self.claim.refresh_from_db()
        self.assertEqual((self.claim.status,self.claim.approved_bonus_pages),('approved',96))

    def test_rejection_leaves_book_pages_and_totals_unchanged(self):
        self.assertEqual(self.client.post(self.url,self.payload('rejected')).status_code,302)
        self.assert_book_preserved();self.claim.refresh_from_db()
        self.assertEqual((self.claim.status,self.claim.approved_bonus_pages),('rejected',0))
        self.assertEqual(self.book.final_scored_pages,384)
        self.assertFalse(ModifierProvenance.objects.filter(submission=self.book).exists())
        self.assertEqual(AuditEvent.objects.filter(action='submission.theme_rejected').count(),1)
        self.assertNotContains(self.client.get(self.queue),self.theme.name)

    def test_repeated_stale_and_reloaded_requests_never_double_award(self):
        data=self.payload('approved')
        self.assertEqual(self.client.post(self.url,data).status_code,302)
        self.client.post(self.url,data)
        self.client.post(self.url,self.payload('approved'))
        self.assert_book_preserved()
        self.assertEqual(self.book.final_scored_pages,480)
        self.assertEqual(ModifierProvenance.objects.filter(submission=self.book).count(),1)
        self.assertEqual(AuditEvent.objects.filter(action='submission.theme_approved').count(),1)
        self.assertFalse(AuditEvent.objects.filter(action='submission.approved').exists())

    def test_approved_pages_status_and_finalized_decision_remain_protected(self):
        for changes in [{'approved_pages':'500'},{'status':'pending'},{'status':'rejected'}]:
            self.assertEqual(self.client.post(self.url,self.payload('approved',**changes)).status_code,200)
        self.assert_book_preserved();self.claim.refresh_from_db();self.assertEqual(self.claim.status,'pending')
        self.client.post(self.url,self.payload('approved'))
        page=self.client.get(self.url)
        self.assertTrue(page.context['claim_formset'].forms[0].fields['status'].disabled)
        self.assertEqual(self.client.post(self.url,self.payload('rejected')).status_code,200)
        self.claim.refresh_from_db();self.assertEqual(self.claim.status,'approved')
        proposed=ThemeClaim.objects.get(pk=self.claim.pk);proposed.status='rejected'
        self.book.refresh_from_db()
        with self.assertRaisesMessage(ValidationError,'Finalized Theme'):
            apply_unified_review(self.book,[proposed],self.reviewer,timezone.now(),version=review_token(self.book,self.reviewer),confirmations=[])

    def test_unauthorized_and_transferred_reviewer_cannot_apply(self):
        data=self.payload('approved')
        self.client.force_login(self.reader.user);self.assertEqual(self.client.post(self.url,data).status_code,403)
        self.client.force_login(self.reviewer)
        TeamAssignment.objects.filter(month=self.month,participant=self.reader).update(ended_at=timezone.now())
        self.assertEqual(self.client.post(self.url,data).status_code,403)
        self.claim.refresh_from_db();self.assertEqual(self.claim.status,'pending')

    def test_direct_service_rejects_identity_changes(self):
        token=review_token(self.book,self.reviewer);self.book.title='Unapproved identity change'
        with self.assertRaisesMessage(ValidationError,'signed Edit Book'):
            apply_unified_review(self.book,[],self.reviewer,timezone.now(),version=token,confirmations=[])
        self.assert_book_preserved()

    def test_unrelated_claims_immersive_matching_and_checkpoints_untouched(self):
        other_theme=MonthTheme.objects.create(month=self.month,name='Frozen',starts_on=self.month.starts_on,ends_on=self.month.ends_on,bonus_percentage=10)
        frozen=ThemeClaim.objects.create(submission=self.book,theme=other_theme,status='approved',approved_bonus_pages=30,
            award_finalized=True,award_basis_pages=300,award_bonus_pages=30,award_percentage=10,award_rule='percentage',award_version=1)
        source=ModifierProvenance.objects.create(month=self.month,participant=self.reader,submission=self.book,
            source_type='theme_bonus',source_reference=f'theme_claim:{frozen.pk}',source_label='Frozen',amount=30,effective_date=self.book.completed_on,is_active=True)
        checkpoint=ProgressCheckpoint.objects.create(month=self.month,scheduled_at=timezone.now(),target_basis='fixed',fixed_target_pages=100)
        result=ProgressCheckpointResult.objects.create(checkpoint=checkpoint,participant=self.reader,evaluated_at=timezone.now(),
            threshold_percentage=25,progress_basis='base',target_basis='fixed',target_pages=100,required_pages=25,progress_pages=384,outcome='met')
        before=(ThemeClaim.objects.filter(pk=frozen.pk).values().get(),ModifierProvenance.objects.filter(pk=source.pk).values().get(),ProgressCheckpointResult.objects.filter(pk=result.pk).values().get())
        with patch('core.immersive.synchronize_awards',side_effect=AssertionError('No unrelated Immersive synchronization')),patch('core.botm_matching.synchronize_submission',side_effect=AssertionError('No matching synchronization')):
            self.assertEqual(self.client.post(self.url,self.payload('approved')).status_code,302)
        after=(ThemeClaim.objects.filter(pk=frozen.pk).values().get(),ModifierProvenance.objects.filter(pk=source.pk).values().get(),ProgressCheckpointResult.objects.filter(pk=result.pk).values().get())
        self.assertEqual(before,after);self.book.refresh_from_db();self.assertEqual(self.book.final_scored_pages,510)

    def test_failure_rolls_back_claim_award_and_audit_atomically(self):
        with patch('core.scoring.refresh_submission_score',side_effect=ValidationError('Synthetic failure')):
            self.assertEqual(self.client.post(self.url,self.payload('approved')).status_code,200)
        self.claim.refresh_from_db();self.assertEqual(self.claim.status,'pending')
        self.assertFalse(AuditEvent.objects.filter(action='submission.theme_approved').exists())
        self.assertFalse(ModifierProvenance.objects.filter(submission=self.book).exists())
