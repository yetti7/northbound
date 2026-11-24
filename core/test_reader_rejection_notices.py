from copy import copy
from django.core.exceptions import ValidationError, PermissionDenied
from django.test import TestCase
from django.utils import timezone
from . import test_review_queue_grouping as fixture
from .models import (ReaderRejectionNotice,BookSubmission,ThemeClaim,ModifierProvenance,PromptClaim,
                     ImmersiveRequest,ScavengerPhotoSubmission,AuditEvent)
from .matching_resolution import review_submission_with_matches
from .immersive import review_immersive,request_immersive
from .prompt_scoring import review_prompts
from .photo_review import review_photo_submission,photo_review_version
from .photo_submissions import create_photo_submission
from .reader_rejections import acknowledge,outstanding,record

REASON='The supplied evidence does not satisfy this qualification.'


class RejectionNoticeTests(TestCase):
    setUp=fixture.ReviewQueueGroupingTests.setUp
    token=fixture.ReviewQueueGroupingTests.token
    prompt=fixture.ReviewQueueGroupingTests.prompt
    settings=fixture.ReviewQueueGroupingTests.settings
    pending=fixture.ReviewQueueGroupingTests.pending

    def reject_photo(self,reason=REASON):
        return review_photo_submission(photo_id=self.photo.pk,actor=self.actor,decision='rejected',
            version=photo_review_version(self.photo,self.actor),rejection_reason=reason)

    def test_theme_rejection_preserves_parent_pages_other_qualifications_and_credit(self):
        from .models import MonthTheme
        claim=self.book.theme_claims.first()
        retained=MonthTheme.objects.create(month=self.month,name='Retained positive award',starts_on=self.month.starts_on,ends_on=self.month.ends_on,bonus_percentage=25)
        other=ThemeClaim.objects.create(submission=self.book,theme=retained)
        other.status='approved'
        review_submission_with_matches(self.book,[other],self.actor,timezone.now())
        review_prompts(self.book,self.actor,{'bingo':'qualified','scavenger':'qualified'})
        before=BookSubmission.objects.get(pk=self.book.pk)
        credits=list(ModifierProvenance.objects.values());prompts=list(PromptClaim.objects.values())
        self.assertEqual({row['source_type'] for row in credits},{'theme_bonus','bingo_prompt','scavenger_prompt'})
        review_submission_with_matches(self.book,[type(claim)(pk=claim.pk,status='rejected')],self.actor,timezone.now(),theme_reasons={claim.pk:REASON})
        self.book.refresh_from_db()
        self.assertEqual((self.book.status,self.book.approved_pages,self.book.final_scored_pages),(before.status,before.approved_pages,before.final_scored_pages))
        other.refresh_from_db();self.assertEqual(other.status,'approved')
        self.assertEqual(list(PromptClaim.objects.values()),prompts)
        self.assertEqual(list(ModifierProvenance.objects.values()),credits)
        self.assertEqual(ImmersiveRequest.objects.get(submission=self.book).status,'pending')
        n=ReaderRejectionNotice.objects.get();self.assertEqual(n.reader_id,self.reader.user_id)
        self.assertEqual(n.theme_claim_id,claim.pk);self.assertIn(claim.theme.name,n.target_label)
        self.assertEqual(n.reason,REASON)

    def test_book_rejection_one_notice_no_automatic_child_notices_and_no_duplicate(self):
        self.second.status='rejected'
        review_submission_with_matches(self.second,[],self.actor,timezone.now(),rejection_reason=REASON)
        review_submission_with_matches(self.second,[],self.actor,timezone.now(),rejection_reason=REASON)
        self.assertEqual(ReaderRejectionNotice.objects.count(),1)
        self.assertEqual(ReaderRejectionNotice.objects.get().category,'book')

    def test_backend_reasons_required_for_all_categories_and_transactions_rollback(self):
        self.second.status='rejected'
        with self.assertRaises(ValidationError):review_submission_with_matches(self.second,[],self.actor,timezone.now(),rejection_reason='  ')
        theme=self.book.theme_claims.first();theme.status='rejected'
        with self.assertRaises(ValidationError):review_submission_with_matches(self.book,[theme],self.actor,timezone.now())
        claim=ImmersiveRequest.objects.get(submission=self.book)
        with self.assertRaises(ValidationError):review_immersive(request=claim,actor=self.actor,decision='rejected',reason=' ')
        for kind in ['bingo','scavenger']:
            with self.assertRaises(ValidationError):review_prompts(self.book,self.actor,{kind:'rejected'},rejection_reasons={kind:' '})
        with self.assertRaises(ValidationError):self.reject_photo(' ')
        self.assertFalse(ReaderRejectionNotice.objects.exists())
        with self.assertRaises(ValidationError):
            review_prompts(self.book,self.actor,{'bingo':'rejected','scavenger':'rejected'},rejection_reasons={'bingo':REASON})
        self.assertFalse(ReaderRejectionNotice.objects.exists())
        self.second.refresh_from_db();self.assertEqual(self.second.status,'pending')

    def test_independent_immersive_and_prompt_rejections_and_idempotent_events(self):
        claim=ImmersiveRequest.objects.get(submission=self.book)
        review_immersive(request=claim,actor=self.actor,decision='rejected',reason=REASON)
        for kind in ['bingo','scavenger']:
            review_prompts(self.book,self.actor,{kind:'rejected'},rejection_reasons={kind:REASON})
            review_prompts(self.book,self.actor,{kind:'rejected'},rejection_reasons={kind:REASON})
        self.book.refresh_from_db();self.assertEqual(self.book.status,'approved');self.assertEqual(self.book.approved_pages,368)
        self.assertEqual(set(ReaderRejectionNotice.objects.values_list('category',flat=True)),{'immersive','bingo','scavenger'})
        n=ReaderRejectionNotice.objects.get(category='immersive')
        record(category='immersive',target=claim,book=self.book,actor=self.actor,reason=REASON,label=n.target_label,event=n.event)
        self.assertEqual(ReaderRejectionNotice.objects.count(),3)

    def test_acknowledgment_is_private_idempotent_and_does_not_change_decision_or_score(self):
        books_before=list(BookSubmission.objects.values())
        self.reject_photo();n=ReaderRejectionNotice.objects.get()
        self.assertEqual(list(BookSubmission.objects.values()),books_before)
        with self.assertRaises(PermissionDenied):acknowledge(self.actor,n.pk)
        before=list(ScavengerPhotoSubmission.objects.values()),list(ModifierProvenance.objects.values())
        acknowledge(self.reader.user,n.pk);n.refresh_from_db();stamp=n.acknowledged_at
        acknowledge(self.reader.user,n.pk);n.refresh_from_db();self.assertEqual(n.acknowledged_at,stamp)
        self.assertFalse(outstanding(self.reader.user).exists())
        self.assertEqual(before,(list(ScavengerPhotoSubmission.objects.values()),list(ModifierProvenance.objects.values())))

    def test_explicit_photo_retry_resolution_and_new_rejection(self):
        self.reject_photo();notice=ReaderRejectionNotice.objects.get()
        replacement=create_photo_submission(month_id=self.month.pk,actor=self.reader.user,
            prompt_id=self.photo_prompt.pk,reader_confirmed=True,replacement_notice=notice.pk)
        notice.refresh_from_db();self.assertEqual(notice.resolution_type,'resubmitted')
        self.assertEqual(notice.replacement_key,f'photo:{replacement.pk}')
        review_photo_submission(photo_id=replacement.pk,actor=self.actor,decision='rejected',
            version=photo_review_version(replacement,self.actor),rejection_reason=REASON)
        self.assertEqual(outstanding(self.reader.user).count(),1)
        self.assertEqual(ReaderRejectionNotice.objects.count(),2)

    def test_immersive_retry_resolution_requires_exact_target_and_does_not_guess(self):
        claim=ImmersiveRequest.objects.get(submission=self.book)
        review_immersive(request=claim,actor=self.actor,decision='rejected',reason=REASON)
        n=ReaderRejectionNotice.objects.get()
        with self.assertRaises(ValidationError):
            request_immersive(submission=self.second,actor=self.reader.user,replacement_notice=n.pk)
        self.assertFalse(ImmersiveRequest.objects.filter(submission=self.second).exists())
        request_immersive(submission=self.book,actor=self.reader.user,replacement_notice=n.pk)
        self.assertFalse(outstanding(self.reader.user).exists())

    def test_historical_state_and_deselection_do_not_create_notices(self):
        BookSubmission.objects.filter(pk=self.second.pk).update(status='rejected')
        self.assertFalse(ReaderRejectionNotice.objects.exists())
        from .prompt_reservations import replace_selections
        replace_selections(submission=self.book,actor=self.actor,selections={'bingo':None})
        self.assertFalse(ReaderRejectionNotice.objects.exists())



from django.test import TransactionTestCase
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


class RejectionCorrectionTests(TransactionTestCase):
    setUp=RejectionNoticeTests.setUp
    token=RejectionNoticeTests.token
    prompt=RejectionNoticeTests.prompt
    settings=RejectionNoticeTests.settings
    pending=RejectionNoticeTests.pending

    def test_exact_recipient_correction_resolves_and_later_rejection_is_new(self):
        from .models import ChallengeStaffAssignment
        from .submission_corrections import correction_version,preview_correction,apply_previewed_correction
        ChallengeStaffAssignment.objects.create(month=self.month,membership=self.reader,role='team_leader',team=self.team)
        actor=self.reader.user
        review_prompts(self.book,self.actor,{'bingo':'rejected'},rejection_reasons={'bingo':REASON})
        review_prompts(self.book,actor,{'bingo':'qualified'})
        self.assertFalse(outstanding(actor).exists())
        self.assertEqual(ReaderRejectionNotice.objects.get().resolution_type,'corrected')
        changes={'prompt_decisions':{'bingo':'rejected'},'prompt_rejection_reasons':{'bingo':REASON}}
        version=correction_version(self.book,actor)
        preview=preview_correction(submission=self.book,actor=actor,version=version,changes=changes)
        self.assertEqual(ReaderRejectionNotice.objects.count(),1)
        apply_previewed_correction(submission=self.book,actor=actor,version=version,changes=changes,preview_token=preview['token'],confirm_approved=True)
        self.assertEqual(outstanding(actor).count(),1)
        self.assertEqual(ReaderRejectionNotice.objects.count(),2)



class RejectionNoticeMigrationTests(TransactionTestCase):
    def test_historical_rejections_are_not_backfilled(self):
        executor=MigrationExecutor(connection);executor.migrate([('core','0089_host_review_access')])
        try:
            apps=executor.loader.project_state([('core','0089_host_review_access')]).apps
            user=apps.get_model('auth','User').objects.create(username='legacy-rejection')
            group=apps.get_model('core','ReadingGroup').objects.create(name='Legacy',slug='legacy-rejection')
            member=apps.get_model('core','Membership').objects.create(group=group,user=user,display_name='Legacy')
            month=apps.get_model('core','ChallengeMonth').objects.create(group=group,name='Legacy')
            book=apps.get_model('core','BookSubmission').objects.create(month=month,participant=member,title='Old rejection',author='A',book_format='ebook',completed_on='2026-11-01',submitted_pages=100,status='rejected')
            executor=MigrationExecutor(connection);executor.migrate([('core','0090_reader_rejection_notices')])
            self.assertFalse(ReaderRejectionNotice.objects.exists())
            current=BookSubmission.objects.get(pk=book.pk)
            self.assertEqual(current.status,'rejected');self.assertEqual(current.review_notes,'')
        finally:MigrationExecutor(connection).migrate([('core','0090_reader_rejection_notices')])
