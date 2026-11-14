from datetime import date
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError, PermissionDenied
from django.test import TestCase
from django.utils import timezone
from . import test_submission_hotfix as fixture
from . import test_submission_editing as editing
from .models import (BookSubmission, BotmBook, BotmMatch, PersonalTBRMatch, PersonalTBRBook,
    CatalogBook, CatalogEdition, Membership, MonthEnrollment, ChallengeMonth, ModifierProvenance,
    AuditEvent, HardcoverSyncOutbox, TeamAssignment)
from .botm_matching import synchronize_submission as sync_botm
from .personal_tbr_matching import synchronize_submission as sync_tbr
from .matching_resolution import review_submission_with_matches, confirm_suggestion, identity_stamp
from .matching_intent import capture_intent, selection_token, ReaderIntentForm


class MatchingPhase2ATests(TestCase):
    setUp = fixture.SubmissionHotfixTests.setUp
    actor = editing.SubmissionEditingTests.actor

    def submission(self, title='Dreadful Girls – Halloween ’99: A ’90s Slasher Horror Novella with Paranormal Dark Romance',
                   author='Rosalia Kings', status='approved', pages=158, catalog=None, save=True):
        s=BookSubmission(month=self.month,participant=self.reader,title=title,author=author,book_format='ebook',
            completed_on=date(2026,11,3),submitted_pages=pages,
            approved_pages=pages if status=='approved' else None,status=status,catalog_book=catalog)
        if save:s.save()
        return s

    def botm(self, title='Dreadful Girls: Halloween 99', author='Rosalia Kings', **kwargs):
        self.month.botm_enabled=True;self.month.save()
        return BotmBook.objects.create(month=self.month,position=1,title_snapshot=title,
            author_snapshot=author,page_count_snapshot=158,bonus_pages=250,**kwargs)

    def confirm(self,s,actor,domain,match):
        return review_submission_with_matches(s,[],actor,timezone.now(),suggestions={domain:{
            'match_id':match.pk,'fingerprint':match.suggestion_fingerprint}})

    def test_patrice_subtitle_review_applies_250_once_and_preserves_158(self):
        self.botm();s=self.submission();sync_botm(s)
        match=BotmMatch.objects.get(submission=s)
        self.assertEqual((match.status,match.is_qualifying),('pending_review',False))
        s.refresh_from_db();self.assertEqual(s.final_scored_pages,158)
        floater=self.actor('floater')
        result=self.confirm(s,floater,'botm',match)
        self.assertEqual((result.approved_pages,result.bonus_pages,result.final_scored_pages),(158,250,408))
        evidence=BotmMatch.objects.get(pk=match.pk).decision_evidence
        for _ in range(2):sync_botm(s)
        self.assertEqual(ModifierProvenance.objects.filter(submission=s,source_type='botm_book').count(),1)
        self.assertEqual(BotmMatch.objects.get(pk=match.pk).decision_evidence,evidence)
        self.assertEqual(AuditEvent.objects.filter(action='botm.match_confirmed').count(),1)
        self.assertFalse(HardcoverSyncOutbox.objects.exists())

    def test_book_only_approval_leaves_uncertain_credit_pending(self):
        self.botm();s=self.submission(status='pending');sync_botm(s)
        self.assertEqual(BotmMatch.objects.get(submission=s).status,'pending_review')
        s.status='approved';s.approved_pages=158
        result=review_submission_with_matches(s,[],self.actor('floater'),timezone.now())
        self.assertEqual(result.final_scored_pages,158)
        self.assertFalse(BotmMatch.objects.get(submission=s).is_qualifying)

    def test_drusilla_strong_identity_automatic_and_duplicate_conflict(self):
        self.month.tbr_book_bonus_pages=0;self.month.save()
        s=self.submission(title=self.book.title,author=self.book.author,pages=384,catalog=self.book,status='pending')
        sync_tbr(s)
        s.status='approved';s.approved_pages=384
        result=review_submission_with_matches(s,[],self.actor('floater'),timezone.now())
        self.assertEqual(result.approved_pages,384)
        match=PersonalTBRMatch.objects.get(submission=s)
        self.assertTrue(match.is_qualifying);self.assertEqual(match.method,'catalog_work')
        before=BookSubmission.objects.filter(pk=s.pk).values().get()
        duplicate=self.submission(title=s.title,author=s.author,pages=384,catalog=self.book)
        sync_tbr(duplicate)
        suggestion=PersonalTBRMatch.objects.get(submission=duplicate)
        self.assertFalse(suggestion.is_qualifying);self.assertTrue(suggestion.conflict_reason)
        self.assertEqual(BookSubmission.objects.filter(pk=s.pk).values().get(),before)

    def test_reader_intent_both_domains_is_immutable_and_not_credit(self):
        target=self.botm();tbr=self.tbr.books.first()
        s=self.submission(save=False,status='pending',title=tbr.title_snapshot,author=tbr.author_snapshot)
        form=ReaderIntentForm({'selected_botm':target.pk,'selected_tbr':tbr.pk,
            'botm_token':selection_token(target,self.reader.user),'tbr_token':selection_token(tbr,self.reader.user)},
            submission=s,actor=self.reader.user)
        self.assertTrue(form.is_valid(),form.errors);s.full_clean();s.save()
        self.assertEqual((s.selected_botm_book_id,s.selected_tbr_book_id),(target.pk,tbr.pk))
        self.assertFalse(BotmMatch.objects.exists());self.assertFalse(PersonalTBRMatch.objects.exists())
        frozen=s.reader_intent.copy();s.catalog_book=self.book;s.save(update_fields=['catalog_book'])
        self.assertEqual(s.reader_intent,frozen)
        s.selected_tbr_book=None
        with self.assertRaises(ValidationError):s.save()

    def test_selected_entries_cannot_be_deleted_or_replaced(self):
        from .botm_configuration import delete_unused_botm_book
        from .personal_tbr import save_staff_challenge_tbr, staff_tbr_version
        target=self.botm();tbr=self.tbr.books.first()
        s=self.submission(save=False,status='pending')
        capture_intent(s,actor=self.reader.user,botm_id=target.pk,tbr_id=tbr.pk,
            tokens={'botm':selection_token(target,self.reader.user),'tbr':selection_token(tbr,self.reader.user)})
        s.save();actor=self.actor('platform')
        with self.assertRaisesMessage(ValidationError,'selection history'):
            delete_unused_botm_book(month=self.month,book=target,actor=actor)
        with self.assertRaisesMessage(ValidationError,'selection history'):
            save_staff_challenge_tbr(enrollment=self.tbr.enrollment,actor=actor,books=[],
                version=staff_tbr_version(self.tbr))
        self.assertTrue(BotmBook.objects.filter(pk=target.pk).exists())
        self.assertTrue(PersonalTBRBook.objects.filter(pk=tbr.pk).exists())

    def test_intent_tokens_scope_staleness_and_lifecycle(self):
        target=self.botm();s=self.submission(save=False)
        token=selection_token(target,self.reader.user)
        BotmBook.objects.filter(pk=target.pk).update(title_snapshot='Changed')
        with self.assertRaises(ValidationError):capture_intent(s,actor=self.reader.user,botm_id=target.pk,tokens={'botm':token})
        target.refresh_from_db();token=selection_token(target,self.reader.user)
        BotmBook.objects.filter(pk=target.pk).update(is_retired=True)
        with self.assertRaises(ValidationError):capture_intent(s,actor=self.reader.user,botm_id=target.pk,tokens={'botm':token})
        with self.assertRaises(PermissionDenied):capture_intent(s,actor=self.actor('host'))
        self.month.status='finalizing'
        with self.assertRaises(ValidationError):capture_intent(s,actor=self.reader.user)
        self.month.status='active'
        foreign=ChallengeMonth.objects.create(group=self.month.group,name='Other',status='active',starts_on=self.month.starts_on,ends_on=self.month.ends_on,botm_enabled=True)
        foreign_book=BotmBook.objects.create(month=foreign,position=1,title_snapshot='Other',author_snapshot='Writer',page_count_snapshot=158)
        with self.assertRaises(ValidationError):capture_intent(s,actor=self.reader.user,botm_id=foreign_book.pk,tokens={'botm':selection_token(foreign_book,self.reader.user)})
        tbr=self.tbr.books.first()
        with self.assertRaises(ValidationError):capture_intent(s,actor=self.reader.user,tbr_id=tbr.pk,tokens={'tbr':'tampered'})
        MonthEnrollment.objects.filter(pk=self.tbr.enrollment_id).update(is_active=False)
        with self.assertRaises(ValidationError):capture_intent(s,actor=self.reader.user)

    def test_selected_conflicting_volume_work_and_adaptation_cannot_be_confirmed(self):
        floater=self.actor('floater')
        for selected_title,submitted_title in [
                ('The Apothecary Diaries Light Novel Volume 14','The Apothecary Diaries Light Novel Volume 15'),
                ('The Apothecary Diaries Light Novel Volume 14','The Apothecary Diaries Manga Volume 14')]:
            target=self.botm(title=selected_title,author='Natsu Hyuuga')
            s=self.submission(title=submitted_title,author='Natsu Hyuuga',save=False)
            capture_intent(s,actor=self.reader.user,botm_id=target.pk,tokens={'botm':selection_token(target,self.reader.user)})
            s.save();sync_botm(s);match=BotmMatch.objects.get(submission=s)
            self.assertTrue(match.conflict_reason)
            with self.assertRaises(ValidationError):self.confirm(s,floater,'botm',match)
            self.assertFalse(BotmMatch.objects.get(pk=match.pk).is_qualifying)
            BotmBook.objects.filter(pk=target.pk).update(is_retired=True)

    def test_both_domains_award_independently_and_idempotently(self):
        self.botm(title=self.book.title,author=self.book.author,catalog_book=self.book,catalog_edition=self.edition)
        s=self.submission(title=self.book.title,author=self.book.author,pages=384,catalog=self.book)
        sync_botm(s);sync_tbr(s);sync_botm(s);sync_tbr(s)
        s.refresh_from_db();self.assertEqual(s.final_scored_pages,384+250+20)
        self.assertEqual(ModifierProvenance.objects.filter(submission=s,source_type='botm_book').count(),1)
        self.assertEqual(ModifierProvenance.objects.filter(submission=s,source_type='tbr_book').count(),1)

    def test_reviewed_credit_survives_departure_but_revalidates_identity(self):
        self.botm();s=self.submission();sync_botm(s);match=BotmMatch.objects.get(submission=s)
        self.confirm(s,self.actor('floater'),'botm',match)
        match.refresh_from_db();evidence=match.decision_evidence
        MonthEnrollment.objects.filter(pk=self.tbr.enrollment_id).update(is_active=False);sync_botm(s)
        match.refresh_from_db();self.assertTrue(match.is_qualifying)
        self.assertEqual(match.decision_evidence,evidence)
        MonthEnrollment.objects.filter(pk=self.tbr.enrollment_id).update(is_active=True);sync_botm(s)
        match.refresh_from_db();self.assertTrue(match.is_qualifying)
        BookSubmission.objects.filter(pk=s.pk).update(title='An unrelated corrected identity');sync_botm(s)
        match.refresh_from_db();self.assertFalse(match.is_qualifying);self.assertTrue(match.conflict_reason)
        self.assertEqual(match.decision_evidence,evidence)

    def test_prior_rejection_never_reactivates(self):
        self.botm();s=self.submission();sync_botm(s);match=BotmMatch.objects.get(submission=s)
        confirm_suggestion(match=match,actor=self.actor('floater'),decision='rejected')
        for _ in range(2):sync_botm(s)
        match.refresh_from_db();self.assertEqual((match.status,match.is_qualifying),('rejected',False))
        self.assertFalse(ModifierProvenance.objects.filter(submission=s,is_active=True).exists())

    def test_scope_stale_and_atomic_failure(self):
        self.botm();s=self.submission(status='pending');sync_botm(s);match=BotmMatch.objects.get(submission=s)
        s.status='approved';s.approved_pages=158
        actor=self.actor('team_leader')
        TeamAssignment.objects.filter(team=self.team,participant=self.reader).update(ended_at=timezone.now())
        with self.assertRaises(PermissionDenied):self.confirm(s,actor,'botm',match)
        floater=self.actor('floater')
        before=BookSubmission.objects.filter(pk=s.pk).values().get()
        with patch('core.matching_resolution.confirm_suggestion',side_effect=ValidationError('Failure after approval')):
            with self.assertRaises(ValidationError):self.confirm(s,floater,'botm',match)
        self.assertEqual(BookSubmission.objects.filter(pk=s.pk).values().get(),before)
        BookSubmission.objects.filter(pk=s.pk).update(author='Changed since suggestion')
        with self.assertRaises(ValidationError):self.confirm(s,floater,'botm',match)
        self.assertEqual(BookSubmission.objects.get(pk=s.pk).status,'pending')

    def test_tbr_cross_reader_selection_and_matching_rejection_are_preserved(self):
        user=get_user_model().objects.create_user('other-intent-reader')
        member=Membership.objects.create(group=self.month.group,user=user,display_name='Other')
        s=BookSubmission(month=self.month,participant=member,title=self.book.title,author=self.book.author,
            book_format='ebook',completed_on=date(2026,11,3),submitted_pages=384)
        MonthEnrollment.objects.create(month=self.month,participant=member)
        target=self.tbr.books.first()
        with self.assertRaises(ValidationError):
            capture_intent(s,actor=user,tbr_id=target.pk,tokens={'tbr':selection_token(target,user)})
        manual=self.submission(title=self.book.title,author=self.book.author,status='pending')
        sync_tbr(manual);match=PersonalTBRMatch.objects.get(submission=manual)
        # Rejecting a suggestion leaves a durable decision, never an automatic award.
        confirm_suggestion(match=match,actor=self.actor('floater'),decision='rejected')
        BookSubmission.objects.filter(pk=manual.pk).update(status='approved',approved_pages=158)
        sync_tbr(manual);match.refresh_from_db()
        self.assertEqual((match.status,match.is_qualifying),('rejected',False))

    def test_team_leader_can_confirm_suggestion_inside_scope_only(self):
        self.botm();s=self.submission(status='pending');sync_botm(s)
        match=BotmMatch.objects.get(submission=s)
        leader=self.actor('team_leader');s.status='approved';s.approved_pages=158
        result=self.confirm(s,leader,'botm',match)
        self.assertEqual(result.final_scored_pages,408)
        other=self.submission(status='pending');sync_botm(other)
        suggestion=BotmMatch.objects.get(submission=other)
        # Arbitrary entry IDs are never accepted in place of an existing suggestion.
        other.status='approved';other.approved_pages=158
        with self.assertRaises(ValidationError):
            review_submission_with_matches(other,[],leader,timezone.now(),suggestions={
                'botm':{'match_id':999999,'fingerprint':suggestion.suggestion_fingerprint}})
        self.assertEqual(BookSubmission.objects.get(pk=other.pk).status,'pending')


    def test_two_suggestions_confirm_independently_in_one_atomic_approval(self):
        target=self.botm()
        tbr=self.tbr.books.first()
        from .models import normalize_book_identity
        PersonalTBRBook.objects.filter(pk=tbr.pk).update(catalog_book=None,catalog_edition=None,
            title_snapshot=target.title_snapshot,author_snapshot=target.author_snapshot,
            normalized_title=normalize_book_identity(target.title_snapshot),
            normalized_author=normalize_book_identity(target.author_snapshot))
        s=self.submission(status='pending');sync_botm(s);sync_tbr(s)
        bm=BotmMatch.objects.get(submission=s);tm=PersonalTBRMatch.objects.get(submission=s)
        s.status='approved';s.approved_pages=158
        result=review_submission_with_matches(s,[],self.actor('floater'),timezone.now(),suggestions={
            'botm':{'match_id':bm.pk,'fingerprint':bm.suggestion_fingerprint},
            'tbr':{'match_id':tm.pk,'fingerprint':tm.suggestion_fingerprint}})
        self.assertEqual(result.final_scored_pages,428)
        tm.refresh_from_db();bm.refresh_from_db()
        self.assertTrue(tm.is_qualifying and bm.is_qualifying)

    def test_no_intent_backfill_and_stale_lifecycle(self):
        s=self.submission();target=self.botm()
        s.selected_botm_book=target
        with self.assertRaises(ValidationError):s.save()
        fresh=self.submission(save=False)
        token=selection_token(target,self.reader.user)
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status='completed')
        with self.assertRaises(ValidationError):
            capture_intent(fresh,actor=self.reader.user,botm_id=target.pk,tokens={'botm':token})

    def test_existing_approved_confirmation_cannot_enqueue_historical_writeback(self):
        from .models import ReaderHardcoverSyncPreference
        self.botm();s=self.submission();sync_botm(s);match=BotmMatch.objects.get(submission=s)
        ReaderHardcoverSyncPreference.objects.create(user=self.reader.user,sync_completed_books=True)
        with self.captureOnCommitCallbacks(execute=True), patch(
                'core.hardcover_sync.enqueue_eligible_approved_submission_safely') as enqueue:
            self.confirm(s,self.actor('floater'),'botm',match)
        enqueue.assert_not_called()
        self.assertFalse(HardcoverSyncOutbox.objects.exists())


from django.test import TransactionTestCase
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


class MatchingEvidenceMigrationTests(TransactionTestCase):
    def test_additive_migration_preserves_historical_decisions_and_awards(self):
        old_target=[('core','0064_registration_agreements')]
        new_target=[('core','0065_reader_intent_and_matching_evidence')]
        executor=MigrationExecutor(connection)
        executor.migrate(old_target)
        try:
            apps=executor.loader.project_state(old_target).apps
            def model(name):return apps.get_model('core',name)
            user=apps.get_model('auth','User').objects.create(username='historical-match')
            group=model('ReadingGroup').objects.create(name='History',slug='history')
            member=model('Membership').objects.create(group=group,user=user,display_name='Reader')
            month=model('ChallengeMonth').objects.create(group=group,name='History',status='active',
                starts_on=date(2026,11,1),ends_on=date(2026,11,30))
            submission=model('BookSubmission').objects.create(month=month,participant=member,
                title='Historical',author='Writer',book_format='ebook',completed_on=date(2026,11,3),
                submitted_pages=158,approved_pages=158,final_scored_pages=408,bonus_pages=250,status='approved')
            book=model('BotmBook').objects.create(month=month,position=1,title_snapshot='Historical',author_snapshot='Writer',page_count_snapshot=158,bonus_pages=250)
            match=model('BotmMatch').objects.create(month=month,participant=member,submission=submission,botm_book=book,
                method='manual_review',status='confirmed',is_qualifying=True,reviewer=user,
                botm_title_snapshot='Historical',botm_author_snapshot='Writer',
                submission_title_snapshot='Historical',submission_author_snapshot='Writer')
            model('ModifierProvenance').objects.create(month=month,participant=member,submission=submission,
                source_type='botm_book',source_reference='historical',source_label='History',amount=250,effective_date=date(2026,11,3))
            model('BotmCompletionAward').objects.create(month=month,participant=member,
                completion_set_fingerprint='frozen',configured_book_count=1,bonus_amount_snapshot=250,
                qualified_at=timezone.now(),last_qualified_at=timezone.now(),effective_date=date(2026,11,3))
            names=['BookSubmission','BotmMatch','ModifierProvenance','BotmCompletionAward']
            before={name:list(model(name).objects.values()) for name in names}
            executor=MigrationExecutor(connection);executor.migrate(new_target)
            apps=executor.loader.project_state(new_target).apps
            for name in names:
                after=list(apps.get_model('core',name).objects.values())
                self.assertEqual([{k:row[k] for k in before[name][0]} for row in after],before[name])
            new_submission=apps.get_model('core','BookSubmission').objects.get(pk=submission.pk)
            self.assertIsNone(new_submission.selected_botm_book_id)
            self.assertIsNone(new_submission.selected_tbr_book_id)
            self.assertEqual(new_submission.reader_intent,{})
            new_match=apps.get_model('core','BotmMatch').objects.get(pk=match.pk)
            self.assertEqual(new_match.decision_evidence,{})
            self.assertFalse(apps.get_model('core','HardcoverSyncOutbox').objects.exists())
        finally:
            executor=MigrationExecutor(connection);executor.migrate(executor.loader.graph.leaf_nodes())
