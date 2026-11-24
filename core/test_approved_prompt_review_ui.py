from django.test import TestCase
from django.urls import reverse
from . import test_reader_rejection_notices as fixture
from .models import ReaderRejectionNotice,ModifierProvenance,BookSubmission,PromptClaim,ImmersiveRequest
from .submission_review_matching import review_token,apply_unified_review
from django.utils import timezone
from django.core.exceptions import ValidationError


class ApprovedPromptReviewTests(TestCase):
    setUp=fixture.RejectionNoticeTests.setUp
    token=fixture.RejectionNoticeTests.token
    prompt=fixture.RejectionNoticeTests.prompt
    settings=fixture.RejectionNoticeTests.settings
    pending=fixture.RejectionNoticeTests.pending

    def payload(self,**extra):
        self.client.force_login(self.actor)
        self.url=reverse('submission-review',args=[self.group.slug,self.month.pk,self.book.pk])
        response=self.client.get(self.url);self.assertEqual(response.status_code,200)
        data={'status':'approved','approved_pages':self.book.approved_pages,
              'matching_version':response.context['matching_form']['matching_version'].value(),
              'claims-TOTAL_FORMS':self.book.theme_claims.count(),'claims-INITIAL_FORMS':self.book.theme_claims.count(),
              'claims-MIN_NUM_FORMS':0,'claims-MAX_NUM_FORMS':1000}
        for i,claim in enumerate(self.book.theme_claims.all()):
            data.update({f'claims-{i}-id':claim.pk,f'claims-{i}-status':claim.status})
        data.update(extra);return data

    def test_initial_approved_book_prompt_decisions_preserve_approval_and_other_awards(self):
        before=BookSubmission.objects.get(pk=self.book.pk)
        data=self.payload(prompt_bingo='qualified',prompt_scavenger='rejected',prompt_reason_scavenger=fixture.REASON)
        response=self.client.post(self.url,data);self.assertEqual(response.status_code,302)
        self.book.refresh_from_db()
        self.assertEqual((self.book.status,self.book.approved_pages,self.book.reviewed_by_id,self.book.reviewed_at),(before.status,before.approved_pages,before.reviewed_by_id,before.reviewed_at))
        self.assertEqual(ModifierProvenance.objects.get(source_type='bingo_prompt').amount,25)
        self.assertFalse(ModifierProvenance.objects.filter(source_type='scavenger_prompt').exists())
        self.assertEqual(ReaderRejectionNotice.objects.get().category,'scavenger')
        self.assertEqual(ImmersiveRequest.objects.get(submission=self.book).status,'pending')
        # Browser retry of the old token cannot duplicate awards/notices.
        self.client.post(self.url,data)
        self.assertEqual(ModifierProvenance.objects.filter(source_type='bingo_prompt').count(),1)
        self.assertEqual(ReaderRejectionNotice.objects.count(),1)

    def test_blank_reason_and_out_of_scope_post_leave_no_side_effects(self):
        data=self.payload(prompt_bingo='rejected',prompt_reason_bingo=' ')
        self.assertContains(self.client.post(self.url,data),'Explain the reason')
        self.assertFalse(ReaderRejectionNotice.objects.exists())
        self.client.force_login(self.users['moderator'])
        self.assertEqual(self.client.post(self.url,data).status_code,403)
        self.assertTrue(PromptClaim.objects.filter(submission=self.book,state='reserved').exists())

    def test_earned_claim_cannot_be_reversed_through_initial_review(self):
        from .prompt_scoring import review_prompts
        review_prompts(self.book,self.actor,{'bingo':'qualified'})
        with self.assertRaises(ValidationError):
            apply_unified_review(self.book,[],self.actor,timezone.now(),version=review_token(self.book,self.actor),
                confirmations=[],prompt_decisions={'bingo':'rejected'},prompt_rejection_reasons={'bingo':fixture.REASON})
        self.assertTrue(ModifierProvenance.objects.get(source_type='bingo_prompt').is_active)
