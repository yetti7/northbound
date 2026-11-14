from datetime import date
from unittest.mock import patch
from django.core.exceptions import ValidationError, PermissionDenied
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from . import test_prompt_reservations as fixture
from .models import (BookSubmission,PromptClaim,PromptCategory,PromptCompletion,ModifierProvenance,AuditEvent,
    TeamAssignment,MonthEnrollment,ChallengeMonth,RecoveryOperation)
from .prompt_scoring import review_prompts,pending_submission_ids
from .prompt_reservations import replace_selections
from .submission_corrections import correction_version,correct_submission,preview_correction,apply_previewed_correction
from .score_aggregation import challenge_score_totals,challenge_team_totals


@override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=True,NORTHBOUND_HARDCOVER_WRITEBACK_ENABLED=False)
class PromptScoringTests(TransactionTestCase):
    setUp=fixture.PromptReservationTests.setUp
    setUp_fixture=fixture.PromptReservationTests.setUp_fixture
    token=fixture.PromptReservationTests.token
    prompt=fixture.PromptReservationTests.prompt
    settings=fixture.PromptReservationTests.settings
    pending=fixture.PromptReservationTests.pending
    reserve=fixture.PromptReservationTests.reserve
    payload=fixture.PromptReservationTests.payload
    hardcover=fixture.PromptReservationTests.hardcover
    tbr=fixture.PromptReservationTests.tbr

    def approve(self,book):
        book.status='approved';book.approved_pages=368;book.save();return book
    def total(self):
        return challenge_score_totals(month=self.month,participant_ids=[self.reader.pk])[self.reader.pk]['total_pages']
    def review(self,book=None,**decisions):
        return review_prompts(book or self.book,self.actor,decisions)
    def correct(self,book=None,**changes):
        book=book or self.book;version=correction_version(book,self.actor)
        preview=preview_correction(submission=book,actor=self.actor,version=version,changes=changes)
        return apply_previewed_correction(submission=book,actor=self.actor,version=version,changes=changes,
            preview_token=preview['token'],confirm_approved=True)
    def void(self,book=None):
        book=book or self.book
        from .recovery_scoring import _set_locked_submission_removed
        from django.db import transaction
        with transaction.atomic():
            return _set_locked_submission_removed(book,removed=True,actor=self.actor,reason='Test void',action='submission.removed')
    def restore(self,book=None):
        from .recovery_scoring import restore_submission_as_staff,staff_restore_confirmation
        book=book or self.book;book.refresh_from_db()
        return restore_submission_as_staff(submission=book,actor=self.actor,confirmation=staff_restore_confirmation(book,self.actor))

    def test_independent_decisions_and_stable_scoring(self):
        self.reserve(bingo=self.bingo[0].pk,scavenger=self.scavenger[0].pk)
        self.review(bingo='qualified',scavenger='rejected');self.book.refresh_from_db()
        self.assertEqual(self.book.status,'approved');self.assertEqual(self.book.approved_pages,368)
        self.assertEqual(self.book.final_scored_pages,393);self.assertEqual(self.total(),393)
        self.assertEqual(dict(self.book.prompt_claims.values_list('category__kind','state')),{'bingo':'qualified','scavenger':'rejected'})
        rows=list(ModifierProvenance.objects.values());audits=AuditEvent.objects.count()
        self.review(bingo='qualified');self.assertEqual(list(ModifierProvenance.objects.values()),rows);self.assertEqual(AuditEvent.objects.count(),audits)
        self.reserve(self.pending(),scavenger=self.scavenger[0].pk)

    def test_zero_point_qualification_and_zero_completion(self):
        for p in self.bingo:p.points=0;p.save()
        for p in self.bingo:
            book=self.book if p==self.bingo[0] else self.approve(self.pending())
            self.reserve(book,bingo=p.pk);self.review(book,bingo='qualified')
        self.assertFalse(ModifierProvenance.objects.exists())
        self.assertTrue(PromptCompletion.objects.get().is_active)
        self.assertEqual(PromptClaim.objects.filter(state='qualified').count(),3)

    def test_frozen_points_and_description_future_awards_only(self):
        self.reserve(bingo=self.bingo[0].pk);self.review(bingo='qualified')
        p=self.bingo[0];p.points=90;p.description='Corrected wording';p.save()
        self.book.refresh_from_db();self.assertEqual(self.book.final_scored_pages,393)
        self.assertEqual(self.book.prompt_claims.get().earning_evidence['amount'],25)
        book=self.approve(self.pending());self.bingo[1].points=90;self.bingo[1].save()
        self.reserve(book,bingo=self.bingo[1].pk);self.review(book,bingo='qualified')
        self.assertEqual(ModifierProvenance.objects.get(submission=book).amount,90)

    def complete(self):
        PromptCategory.objects.filter(kind='bingo').update(completion_bonus=100)
        books=[]
        for p in self.bingo:
            book=self.book if not books else self.approve(self.pending())
            self.reserve(book,bingo=p.pk);self.review(book,bingo='qualified');books.append(book)
        return books

    def test_completion_once_reversal_and_reconfirmation_frozen(self):
        books=self.complete();award=PromptCompletion.objects.get();evidence=award.evidence
        self.assertEqual(self.total(),3*368+75+100)
        PromptCategory.objects.filter(kind='bingo').update(completion_bonus=900)
        self.correct(books[-1],prompt_decisions={'bingo':'rejected'})
        award.refresh_from_db();self.assertFalse(award.is_active);self.assertEqual(self.total(),3*368+50)
        self.correct(books[-1],prompt_decisions={'bingo':'qualified'})
        award.refresh_from_db();self.assertTrue(award.is_active);self.assertEqual(award.bonus_amount_snapshot,100);self.assertEqual(award.evidence,evidence)
        self.assertEqual(ModifierProvenance.objects.count(),4);self.assertEqual(self.total(),3*368+75+100)

    def test_earned_replacement_requires_preview_and_removes_completion(self):
        books=self.complete();book=books[-1]
        with self.assertRaises(ValidationError):replace_selections(submission=book,actor=self.actor,selections={'bingo':None})
        with self.assertRaises(ValidationError):correct_submission(submission=book,actor=self.actor,version=correction_version(book,self.actor),changes={'bingo_prompt_id':None})
        self.correct(book,bingo_prompt_id=None)
        self.assertFalse(book.prompt_claims.get().is_current);self.assertFalse(PromptCompletion.objects.get().is_active)
        self.assertEqual(self.total(),3*368+50)
        self.reserve(book,bingo=self.bingo[-1].pk);self.review(book,bingo='qualified')
        self.assertEqual(self.total(),3*368+75+100)

    def test_review_earned_correction_without_preview_rejected(self):
        self.reserve(bingo=self.bingo[0].pk);self.review(bingo='qualified')
        before=list(ModifierProvenance.objects.values())
        with self.assertRaises(ValidationError):self.review(bingo='rejected')
        self.assertEqual(list(ModifierProvenance.objects.values()),before)

    def test_stale_preview_has_no_writes(self):
        self.reserve(bingo=self.bingo[0].pk);self.review(bingo='qualified')
        version=correction_version(self.book,self.actor);changes={'prompt_decisions':{'bingo':'rejected'}}
        before=list(ModifierProvenance.objects.values());audits=AuditEvent.objects.count()
        preview=preview_correction(submission=self.book,actor=self.actor,version=version,changes=changes)
        self.assertEqual(list(ModifierProvenance.objects.values()),before);self.assertEqual(AuditEvent.objects.count(),audits)
        p=self.bingo[0];p.points=80;p.save()
        with self.assertRaises(ValidationError):apply_previewed_correction(submission=self.book,actor=self.actor,version=version,changes=changes,preview_token=preview['token'],confirm_approved=True)
        self.assertEqual(list(ModifierProvenance.objects.values()),before)

    def test_rejected_book_deactivates_only_its_prompt_credit(self):
        self.reserve(bingo=self.bingo[0].pk,scavenger=self.scavenger[0].pk);self.review(bingo='qualified',scavenger='qualified')
        self.book.status='rejected';self.book.save()
        self.assertFalse(ModifierProvenance.objects.filter(is_active=True).exists());self.assertEqual(self.total(),0)
        self.assertEqual(set(self.book.prompt_claims.values_list('state',flat=True)),{'rejected'})

    def test_void_restore_preserves_frozen_sources(self):
        self.complete();rows=list(ModifierProvenance.objects.values_list('pk','amount','effective_date'))
        self.void();self.assertEqual(self.total(),2*368+50)
        self.restore();self.assertEqual(self.total(),3*368+75+100)
        self.assertEqual(list(ModifierProvenance.objects.values_list('pk','amount','effective_date')),rows)

    def test_restoration_conflict_fails_atomically(self):
        self.reserve(bingo=self.bingo[0].pk);self.review(bingo='qualified');self.void()
        other=self.pending();self.reserve(other,bingo=self.bingo[0].pk)
        with self.assertRaises(ValidationError):self.restore()
        self.book.refresh_from_db();self.assertTrue(self.book.is_removed);self.assertFalse(ModifierProvenance.objects.get().is_active)

    def test_manual_game_reward_does_not_qualify_or_complete(self):
        ModifierProvenance.objects.create(month=self.month,participant=self.reader,source_type='game_reward',source_reference='manual',amount=100,effective_date=date(2027,1,3),is_active=True)
        self.reserve(bingo=self.bingo[0].pk);self.review(bingo='qualified')
        self.assertFalse(PromptCompletion.objects.exists());self.assertEqual(self.total(),493)
        self.assertEqual(ModifierProvenance.objects.get(source_type='game_reward').amount,100)

    def test_team_transfer_uses_existing_aggregation(self):
        self.reserve(bingo=self.bingo[0].pk);self.review(bingo='qualified')
        assignment=TeamAssignment.objects.get(participant=self.reader,ended_at__isnull=True)
        assignment.ended_at=timezone.now();assignment.save()
        TeamAssignment.objects.create(month=self.month,participant=self.reader,team=self.other_team)
        self.assertEqual(challenge_team_totals(month=self.month,team_ids=[self.team.pk,self.other_team.pk]),{self.team.pk:0,self.other_team.pk:393})
        self.assertEqual(ModifierProvenance.objects.count(),1)

    def test_team_scoped_review_authority(self):
        self.reserve(bingo=self.bingo[0].pk)
        review_prompts(self.book,self.users['participating'],{'bingo':'qualified'})
        assignment=TeamAssignment.objects.get(participant=self.reader,ended_at__isnull=True);assignment.ended_at=timezone.now();assignment.save();TeamAssignment.objects.create(month=self.month,participant=self.reader,team=self.other_team)
        book=self.approve(self.pending());self.reserve(book,bingo=self.bingo[1].pk)
        for actor in [self.users['participating'],self.users['leader'],self.users['reader'],self.users['outsider']]:
            with self.assertRaises(PermissionDenied):review_prompts(book,actor,{'bingo':'qualified'})
        review_prompts(book,self.users['floater'],{'bingo':'qualified'})

    def test_tbr_conflict_and_scavenger_compatibility(self):
        entry=self.tbr()
        from .personal_tbr_matching import synchronize_submission
        self.book.title=entry.title_snapshot;self.book.save();synchronize_submission(self.book)
        self.assertTrue(self.book.personal_tbr_matches.filter(is_qualifying=True).exists())
        with self.assertRaises(ValidationError):self.reserve(bingo=self.bingo[0].pk)
        self.reserve(scavenger=self.scavenger[0].pk);self.review(scavenger='qualified')
        self.assertTrue(self.book.personal_tbr_matches.filter(is_qualifying=True).exists())

    def test_hardcover_autoapproval_queue_and_explicit_review(self):
        self.assertEqual(self.client.post(self.url,self.payload(catalog_selection=self.hardcover(),bingo_prompt=self.bingo[0].pk)).status_code,302)
        book=BookSubmission.objects.latest('pk');self.assertEqual(book.status,'approved');self.assertIn(book.pk,list(pending_submission_ids(self.month).values_list('submission_id',flat=True)))
        self.client.force_login(self.actor)
        url=reverse('submission-review',args=[self.group.slug,self.month.pk,book.pk]);response=self.client.get(url)
        self.assertContains(response,'name="prompt_bingo"')
        data={'status':'approved','approved_pages':368,'matching_version':response.context['matching_form']['matching_version'].value(),'prompt_bingo':'qualified'}
        response=self.client.post(url,data);self.assertEqual(response.status_code,302)
        self.assertEqual(book.prompt_claims.get().state,'qualified')
        self.assertNotIn(book.pk,list(pending_submission_ids(self.month).values_list('submission_id',flat=True)))

    def test_disabled_gate_and_november_deny_awards(self):
        self.reserve(bingo=self.bingo[0].pk)
        with override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=False):
            with self.assertRaises(ValidationError):self.review(bingo='qualified')
            self.assertFalse(pending_submission_ids(self.month).exists())
        ChallengeMonth.objects.filter(pk=self.month.pk).update(prompt_automation_excluded=True)
        with self.assertRaises(ValidationError):self.review(bingo='qualified')
        self.assertFalse(ModifierProvenance.objects.exists())

    def test_legacy_refresh_does_not_award_reserved_prompts(self):
        self.reserve(bingo=self.bingo[0].pk)
        from .scoring import refresh_submission_score
        from django.db import transaction
        with transaction.atomic():refresh_submission_score(self.book)
        self.assertEqual(self.total(),368);self.assertFalse(ModifierProvenance.objects.exists())


    def test_both_earned_decisions_corrected_in_one_preview(self):
        self.reserve(bingo=self.bingo[0].pk,scavenger=self.scavenger[0].pk);self.review(bingo='qualified',scavenger='qualified')
        self.correct(prompt_decisions={'bingo':'rejected','scavenger':'rejected'})
        self.assertEqual(self.total(),368);self.assertFalse(ModifierProvenance.objects.filter(is_active=True).exists())

    def test_book_identity_change_requires_explicit_prompt_reversal(self):
        self.reserve(bingo=self.bingo[0].pk);self.review(bingo='qualified')
        with self.assertRaises(ValidationError):self.correct(title='Different book')
        self.book.refresh_from_db();self.assertEqual(self.book.title,'Preserve this book')
        self.correct(title='Different book',prompt_decisions={'bingo':'rejected'})
        self.assertEqual(self.total(),368)

    def test_pending_book_cannot_receive_prompt_points(self):
        book=self.pending();self.reserve(book,bingo=self.bingo[0].pk)
        with self.assertRaises(ValidationError):self.review(book,bingo='qualified')
        self.assertEqual(book.prompt_claims.get().state,'reserved');self.assertFalse(ModifierProvenance.objects.exists())

    def test_theme_and_botm_remain_independent(self):
        from .models import BotmBook,MonthTheme,ThemeClaim
        from .botm_matching import synchronize_submission
        from .scoring import refresh_submission_score
        from django.db import transaction
        self.month.botm_enabled=True;self.month.save()
        BotmBook.objects.create(month=self.month,position=1,title_snapshot=self.book.title,author_snapshot=self.book.author,page_count_snapshot=368,bonus_pages=50)
        synchronize_submission(self.book)
        theme=MonthTheme.objects.create(month=self.month,name='Theme',starts_on=self.month.starts_on,ends_on=self.month.ends_on,bonus_pages=20)
        ThemeClaim.objects.create(submission=self.book,theme=theme,status='approved',approved_bonus_pages=20)
        with transaction.atomic():refresh_submission_score(self.book)
        protected=list(ModifierProvenance.objects.order_by('pk').values())
        self.reserve(bingo=self.bingo[0].pk,scavenger=self.scavenger[0].pk);self.review(bingo='qualified',scavenger='qualified')
        self.assertTrue(self.book.botm_matches.get().is_qualifying)
        for row in protected:self.assertEqual(ModifierProvenance.objects.filter(pk=row['id']).values().get(),row)
        self.assertEqual(self.total(),488)

    def test_attention_and_queue_include_only_actionable_scoped_prompts(self):
        from .review_attention import challenge_attention_count
        self.reserve(bingo=self.bingo[0].pk,scavenger=self.scavenger[0].pk)
        self.assertEqual(challenge_attention_count(self.users['participating'],self.month),2)
        assignment=TeamAssignment.objects.get(participant=self.reader,ended_at__isnull=True)
        assignment.ended_at=timezone.now();assignment.save();TeamAssignment.objects.create(month=self.month,participant=self.reader,team=self.other_team)
        self.assertEqual(challenge_attention_count(self.users['participating'],self.month),0)
        self.assertEqual(challenge_attention_count(self.users['floater'],self.month),2)
        self.client.force_login(self.actor);url=reverse('review-queue',args=[self.group.slug,self.month.pk])
        self.assertContains(self.client.get(url),'prompt · needs confirmation',count=2)
        self.review(bingo='qualified',scavenger='rejected')
        self.assertEqual(challenge_attention_count(self.actor,self.month),0)

    def test_editor_renders_signed_prompt_credit_preview(self):
        self.reserve(bingo=self.bingo[0].pk);self.review(bingo='qualified');self.client.force_login(self.actor)
        url=reverse('submission-edit',args=[self.group.slug,self.month.pk,self.book.pk])
        response=self.client.get(url);self.assertContains(response,'name="prompt_decision_bingo"')
        data={'title':self.book.title,'author':self.book.author,'completed_on':'2027-01-03','submitted_pages':368,'version':response.context['form']['version'].value(),'bingo_prompt':self.bingo[0].pk,'prompt_decision_bingo':'rejected'}
        response=self.client.post(url,data)
        self.assertContains(response,'Preview before confirmation');self.assertContains(response,'Bingo qualification')
        self.assertEqual(self.total(),393)


    def test_completion_state_change_invalidates_correction_preview(self):
        books=self.complete();book=books[-1];changes={'prompt_decisions':{'bingo':'rejected'}}
        version=correction_version(book,self.actor)
        preview=preview_correction(submission=book,actor=self.actor,version=version,changes=changes)
        PromptCompletion.objects.filter(category__kind='bingo').update(is_active=False)
        before=list(ModifierProvenance.objects.values())
        with self.assertRaises(ValidationError):apply_previewed_correction(submission=book,actor=self.actor,version=version,changes=changes,preview_token=preview['token'],confirm_approved=True)
        self.assertEqual(list(ModifierProvenance.objects.values()),before)

    def test_completion_provenance_ownership_mismatch_fails_closed(self):
        books=self.complete()
        ModifierProvenance.objects.filter(source_type='bingo_completion').update(submission=self.book)
        before=list(ModifierProvenance.objects.values())
        with self.assertRaises(ValidationError):self.correct(books[-1],prompt_decisions={'bingo':'rejected'})
        self.assertEqual(list(ModifierProvenance.objects.values()),before)


from . import test_late_approval as late_fixture

@override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=True,NORTHBOUND_HARDCOVER_WRITEBACK_ENABLED=False)
class HistoricalPromptTests(TransactionTestCase):
    activate=late_fixture.LateApprovalTests.activate
    assign=late_fixture.LateApprovalTests.assign
    depart=late_fixture.LateApprovalTests.depart
    submit=late_fixture.LateApprovalTests.submit
    approve=late_fixture.LateApprovalTests.approve

    def setUp(self):
        late_fixture.LateApprovalTests.setUp(self)
        from .prompt_configuration import save_prompt,save_category,configuration_token
        self.prompt=save_prompt(month_id=self.month.pk,kind='bingo',actor=self.owner,version=configuration_token(self.month,'bingo',self.owner),values={'number':1,'description':'Historical prompt','points':25,'is_active':True})[0]
        save_category(month_id=self.month.pk,kind='bingo',actor=self.owner,version=configuration_token(self.month,'bingo',self.owner),values={'enabled':True,'completion_bonus':100,'exclude_automation':False})

    def reserved(self):
        book=self.submit();replace_selections(submission=book,actor=self.owner,selections={'bingo':self.prompt.pk});return book
    def qualify(self,book):review_prompts(book,self.owner,{'bingo':'qualified'})
    def void(self,book):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(reverse('submission-remove',args=[self.group.slug,self.month.pk,book.pk]),{'reason':'Historical prompt fixture'}).status_code,302)
        book.refresh_from_db()
    def request(self,book):
        from .recovery import RecoveryRequest
        from .recovery_scoring import submission_recovery_label
        label=submission_recovery_label(book)
        return RecoveryRequest(actor=self.owner,tier=2,action='submission.restore',target_type='BookSubmission',target_id=str(book.pk),target_label=label,reason='Verify historical prompt credit',required_confirmation=label,supplied_confirmation=label,group=self.group,challenge=self.month)
    def snapshot(self):
        return {m.__name__:list(m.objects.order_by('pk').values()) for m in [BookSubmission,PromptClaim,PromptCompletion,ModifierProvenance,AuditEvent,RecoveryOperation]}

    def test_late_qualification_uses_captured_rates_not_changed_values(self):
        book=self.reserved();self.depart();self.approve(book)
        self.prompt.points=500;self.prompt.save();PromptCategory.objects.filter(pk=self.prompt.category_id).update(completion_bonus=900)
        self.qualify(book)
        self.assertEqual(ModifierProvenance.objects.get(source_type='bingo_prompt').amount,25)
        self.assertEqual(PromptCompletion.objects.get().bonus_amount_snapshot,100)
        self.assertEqual(challenge_team_totals(month=self.month,team_ids=[self.team.pk]),{self.team.pk:525})
        self.assertFalse(MonthEnrollment.objects.get(pk=self.enrollment.pk).is_active)

    def test_late_without_evidence_fails_closed(self):
        book=self.reserved();self.depart()
        BookSubmission.objects.filter(pk=book.pk).update(status='approved',approved_pages=400,late_approval_evidence={})
        before=self.snapshot()
        with self.assertRaises(ValidationError):self.qualify(book)
        self.assertEqual(self.snapshot(),before)

    def test_advanced_historical_preview_rolls_back_and_restores_frozen_credit(self):
        from .recovery_scoring import preview_submission_restoration,set_submission_removed
        book=self.reserved();self.approve(book);self.qualify(book);self.void(book);self.depart()
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status='completed');book.refresh_from_db()
        before=self.snapshot();preview,token=preview_submission_restoration(submission=book,actor=self.owner)
        self.assertEqual(self.snapshot(),before);self.assertEqual(preview['reader_score_after']['total_pages'],525)
        operation=set_submission_removed(submission=book,removed=False,recovery_request=self.request(book),preview_token=token)
        self.assertEqual(operation.after_state,preview);self.assertTrue(PromptCompletion.objects.get().is_active)
        self.assertEqual(AuditEvent.objects.filter(action='prompt.lifecycle_changed').latest('pk').actor_id,self.owner.pk)
        self.assertEqual(ModifierProvenance.objects.filter(is_active=True).count(),2)

    def test_missing_or_manually_voided_prompt_source_prevents_recovery(self):
        from .recovery_scoring import preview_submission_restoration
        book=self.reserved();self.approve(book);self.qualify(book);self.void(book)
        ModifierProvenance.objects.filter(source_type='bingo_prompt').update(voided_at=timezone.now())
        before=self.snapshot()
        with self.assertRaises(ValidationError):preview_submission_restoration(submission=book,actor=self.owner)
        self.assertEqual(self.snapshot(),before)

    def test_generic_provenance_recovery_cannot_reprice_prompt_sources(self):
        from .recovery_scoring import void_provenance
        book=self.reserved();self.approve(book);self.qualify(book)
        row=ModifierProvenance.objects.get(source_type='bingo_prompt');before=self.snapshot()
        with self.assertRaises(ValidationError):void_provenance(provenance=row,recovery_request=self.request(book))
        self.assertEqual(self.snapshot(),before)

    def test_late_approved_book_supports_independent_unified_prompt_review(self):
        from .submission_review_matching import apply_unified_review,review_token
        book=self.reserved();self.depart();book=self.approve(book)
        original=(book.reviewed_by_id,book.reviewed_at,book.approved_pages)
        result=apply_unified_review(book,[],self.owner,timezone.now(),version=review_token(book,self.owner),confirmations=[],prompt_decisions={'bingo':'qualified'})
        self.assertEqual(result.final_scored_pages,425)
        self.assertEqual((result.reviewed_by_id,result.reviewed_at,result.approved_pages),original)
        self.assertEqual(PromptCompletion.objects.get().bonus_amount_snapshot,100)
