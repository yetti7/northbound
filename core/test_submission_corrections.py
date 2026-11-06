import json
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from . import test_submission_hotfix as fixture
from .models import (
    BookSubmission, Membership, ChallengeStaffAssignment, Team, TeamAssignment,
    MonthEnrollment, MonthTheme, ThemeClaim, ModifierProvenance, PersonalTBRBook,
    PersonalTBRMatch, BotmBook, BotmMatch, PersonalTBRCompletionAward, BotmCompletionAward,
    AuditEvent, CatalogBook, CatalogEdition, ProgressCheckpoint, ProgressCheckpointResult,
)
from .permissions import can_edit_submission, can_review_submission
from .submission_corrections import correction_version, correct_submission
from .personal_tbr_matching import synchronize_submission as sync_tbr
from .botm_matching import synchronize_submission as sync_botm
from .score_aggregation import challenge_score_totals
from .progress_checkpoints import process_due_progress_checkpoints


class SubmissionCorrectionTests(TransactionTestCase):
    setUp = fixture.SubmissionHotfixTests.setUp

    def admin(self, role='host'):
        actor=get_user_model().objects.create_user('correction-'+role,is_superuser=role=='platform')
        if role!='platform':
            member=Membership.objects.create(group=self.month.group,user=actor,display_name=role,
                role=role if role in ('owner','moderator') else 'member')
            if role in ('host','floater','team_leader'):
                kwargs=dict(month=self.month,membership=member,role=role)
                if role=='team_leader':
                    self.team=Team.objects.create(month=self.month,name='Correction team')
                    MonthEnrollment.objects.create(month=self.month,participant=member)
                    TeamAssignment.objects.create(month=self.month,team=self.team,participant=member)
                    TeamAssignment.objects.create(month=self.month,team=self.team,participant=self.reader)
                    kwargs['team']=self.team
                ChallengeStaffAssignment.objects.create(**kwargs)
        return actor

    def submission(self, status='approved', title=None, pages=350, catalog=False):
        item=BookSubmission.objects.create(month=self.month,participant=self.reader,
            title=title or self.book.title,author=self.book.author,book_format='ebook',completed_on=date(2026,11,3),
            submitted_pages=pages,approved_pages=pages if status=='approved' else None,status=status,
            catalog_book=self.book if catalog else None,catalog_edition=self.edition if catalog else None)
        sync_tbr(item);sync_botm(item)
        return item

    def correct(self, item, changes, actor=None, version=None, **kwargs):
        actor=actor or getattr(self,'actor',None)
        if actor is None:self.actor=actor=self.admin()
        return correct_submission(submission=item,actor=actor,changes=changes,
            version=version or correction_version(item,actor),**kwargs)

    def totals(self):
        return challenge_score_totals(month=self.month,participant_ids=[self.reader.pk])[self.reader.pk]

    def test_permission_scopes_and_lifecycle(self):
        item=self.submission()
        self.assertFalse(can_edit_submission(self.reader.user,item))
        self.assertFalse(can_edit_submission(self.admin('moderator'),item))
        owner=self.admin('owner')
        self.assertTrue(can_edit_submission(owner,item))
        self.assertFalse(can_review_submission(owner,item))
        for role in ('host','floater','platform','team_leader'):
            self.assertTrue(can_edit_submission(self.admin(role),item))
        leader=get_user_model().objects.get(username='correction-team_leader')
        TeamAssignment.objects.filter(team=self.team,participant=self.reader).update(ended_at=timezone.now())
        self.assertFalse(can_edit_submission(leader,item))
        for state in ('upcoming','completed','archived','draft'):
            self.month.__class__.objects.filter(pk=self.month.pk).update(status=state)
            item.refresh_from_db();item.month.refresh_from_db()
            self.assertFalse(can_edit_submission(owner,item))
        self.month.__class__.objects.filter(pk=self.month.pk).update(status='finalizing')
        item.month.refresh_from_db()
        self.assertTrue(can_edit_submission(owner,item))
        self.correct(item,{'page_count':400},actor=owner)

    def test_status_transitions_and_page_replacement(self):
        for status in ('pending','approved','rejected'):
            item=self.submission(status=status,title='Unmatched '+status)
            pk=item.pk;before=self.totals()['base_pages']
            corrected=self.correct(item,{'title':'Corrected '+status,'page_count':400})
            self.assertEqual(corrected.pk,pk)
            self.assertEqual(corrected.status,'approved' if status=='approved' else 'pending')
            self.assertEqual(corrected.approved_pages,400 if status=='approved' else None)
            self.assertEqual(self.totals()['base_pages']-before,50 if status=='approved' else 0)
        self.assertEqual(BookSubmission.objects.count(),3)

    def test_hardcover_override_keeps_identity_and_provider_evidence(self):
        item=self.submission(catalog=True)
        BookSubmission.objects.filter(pk=item.pk).update(verification_method='hardcover',metadata_pages=350,
            scoring_catalog_edition=self.edition)
        item.refresh_from_db()
        catalog_before=list(CatalogEdition.objects.values())
        corrected=self.correct(item,{'page_count':400})
        self.assertEqual(corrected.verification_method,'manual')
        self.assertEqual((corrected.catalog_edition_id,corrected.scoring_catalog_edition_id),(self.edition.pk,self.edition.pk))
        self.assertEqual((corrected.metadata_pages,corrected.approved_pages),(350,400))
        self.assertEqual(list(CatalogEdition.objects.values()),catalog_before)
        payload=json.loads(AuditEvent.objects.filter(action='submission.corrected').get().summary)
        self.assertTrue(payload['manual_page_override'])
        self.assertEqual(payload['before']['submission']['approved_pages'],350)
        self.assertEqual(payload['after']['submission']['approved_pages'],400)

    def test_edition_change_fetches_outside_transaction_without_catalog_writes(self):
        item=self.submission(catalog=True)
        work=CatalogBook.objects.create(provider='hardcover',provider_book_id='101',title='Different',author='Writer')
        edition=CatalogEdition.objects.create(provider='hardcover',provider_edition_id='202',book=work,page_count=100,format_name='Ebook')
        before=list(CatalogBook.objects.values()),list(CatalogEdition.objects.values())
        def fetch(*args,**kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(kwargs,{'edition_id':'202','book_id':'101'})
            return {'title':'Fresh edition','author':'Writer','page_count':420}
        with patch('core.submission_corrections.fetch_edition_metadata',side_effect=fetch):
            corrected=self.correct(item,{'catalog_edition_id':edition.pk},hardcover_token='credential')
        self.assertEqual((corrected.catalog_book_id,corrected.catalog_edition_id,corrected.approved_pages),(work.pk,edition.pk,420))
        self.assertEqual(corrected.verification_method,'hardcover')
        self.assertEqual((list(CatalogBook.objects.values()),list(CatalogEdition.objects.values())),before)
        self.assertFalse(PersonalTBRMatch.objects.filter(submission=item,is_qualifying=True).exists())

    def test_themes_frozen_rates_zero_and_historical_reselection(self):
        item=self.submission(title='Theme book')
        percentage=MonthTheme.objects.create(month=self.month,name='25%',starts_on=self.month.starts_on,
            ends_on=self.month.ends_on,bonus_percentage=25)
        zero=MonthTheme.objects.create(month=self.month,name='0%',starts_on=self.month.starts_on,
            ends_on=self.month.ends_on,bonus_percentage=0)
        corrected=self.correct(item,{'theme_ids':[percentage.pk,zero.pk],'page_count':400})
        claim=ThemeClaim.objects.get(submission=item,theme=percentage);claim_id=claim.pk
        source=ModifierProvenance.objects.get(source_type='theme_bonus',source_reference=f'theme_claim:{claim.pk}')
        self.assertEqual((claim.award_percentage,claim.award_basis_pages,claim.award_bonus_pages),(25,400,100))
        self.assertEqual(ThemeClaim.objects.get(submission=item,theme=zero).award_bonus_pages,0)
        self.assertEqual(item.modifier_provenance.filter(source_type='theme_bonus').count(),1)
        # Frozen claim rates survive even a deliberately inconsistent fixture config.
        MonthTheme.objects.filter(pk=percentage.pk).update(bonus_percentage=50)
        corrected=self.correct(corrected,{'page_count':402})
        claim.refresh_from_db();self.assertEqual(claim.award_bonus_pages,101)
        corrected=self.correct(corrected,{'theme_ids':[]})
        source.refresh_from_db();self.assertFalse(source.is_active)
        self.assertEqual(ThemeClaim.objects.get(pk=claim_id).status,'rejected')
        self.correct(corrected,{'theme_ids':[percentage.pk,zero.pk]})
        source.refresh_from_db();self.assertTrue(source.is_active)
        self.assertEqual(source.amount,101)
        self.assertEqual(ModifierProvenance.objects.filter(source_reference=f'theme_claim:{claim_id}').count(),1)

    def test_rematching_restores_alternative_and_respects_rejection(self):
        item=self.submission();other=self.submission()
        oldmatch=PersonalTBRMatch.objects.get(submission=item)
        self.assertTrue(oldmatch.is_qualifying)
        selection_before=list(PersonalTBRBook.objects.values())
        self.correct(item,{'title':'Other work'})
        oldmatch.refresh_from_db();self.assertFalse(oldmatch.is_qualifying)
        self.assertTrue(PersonalTBRMatch.objects.get(submission=other).is_qualifying)
        self.assertEqual(list(PersonalTBRBook.objects.values()),selection_before)
        PersonalTBRMatch.objects.filter(submission=item).update(status='rejected')
        self.correct(item,{'title':self.book.title})
        oldmatch.refresh_from_db();self.assertEqual(oldmatch.status,'rejected')

    def test_reconciliation_promotes_released_credit_without_matching_unrelated_books(self):
        self.month.botm_enabled=True;self.month.save()
        item=self.submission();alternative=self.submission()
        BotmBook.objects.create(month=self.month,position=1,title_snapshot=item.title,
            author_snapshot=item.author,page_count_snapshot=350,bonus_pages=30)
        BotmBook.objects.create(month=self.month,position=2,title_snapshot='Other 2',
            author_snapshot=item.author,page_count_snapshot=350,bonus_pages=30)
        sync_botm(item);sync_botm(alternative)
        unrelated=BookSubmission.objects.create(month=self.month,participant=self.reader,
            title='Other 2',author=item.author,book_format='ebook',completed_on=item.completed_on,
            submitted_pages=350,approved_pages=350,status='approved')
        unrelated_before=BookSubmission.objects.filter(pk=unrelated.pk).values().get()
        # Page changes do not release matching credit or reconcile unrelated history.
        with patch('core.submission_corrections.synchronize_tbr') as tbr,patch(
                'core.submission_corrections.synchronize_botm') as botm:
            item=self.correct(item,{'page_count':400})
            tbr.assert_not_called();botm.assert_not_called()
        self.correct(item,{'title':'No longer a configured book'})
        self.assertTrue(PersonalTBRMatch.objects.get(submission=alternative).is_qualifying)
        self.assertTrue(BotmMatch.objects.get(submission=alternative).is_qualifying)
        self.assertFalse(PersonalTBRMatch.objects.filter(submission=item,is_qualifying=True).exists())
        self.assertFalse(BotmMatch.objects.filter(submission=item,is_qualifying=True).exists())
        self.assertFalse(PersonalTBRMatch.objects.filter(submission=unrelated).exists())
        self.assertFalse(BotmMatch.objects.filter(submission=unrelated).exists())
        self.assertFalse(ModifierProvenance.objects.filter(submission=unrelated).exists())
        self.assertEqual(BookSubmission.objects.filter(pk=unrelated.pk).values().get(),unrelated_before)

    def test_completion_date_updates_per_book_dates_and_theme_eligibility(self):
        item=self.submission()
        theme=MonthTheme.objects.create(month=self.month,name='Early',starts_on=date(2026,11,1),
            ends_on=date(2026,11,5),bonus_percentage=25)
        self.month.botm_enabled=True;self.month.save()
        botm=BotmBook.objects.create(month=self.month,position=1,title_snapshot=item.title,
            author_snapshot=item.author,page_count_snapshot=350,bonus_pages=30)
        sync_botm(item)
        corrected=self.correct(item,{'theme_ids':[theme.pk]})
        ProgressCheckpoint.objects.create(month=self.month,scheduled_at=timezone.now(),
            target_basis='fixed',fixed_target_pages=100,progress_basis='total')
        process_due_progress_checkpoints(now=timezone.now())
        checkpoints=list(ProgressCheckpointResult.objects.values())
        corrected=self.correct(corrected,{'completed_on':'2026-11-10'})
        self.assertEqual(ThemeClaim.objects.get(submission=item).status,'rejected')
        for source in ModifierProvenance.objects.filter(submission=item):
            self.assertEqual(source.effective_date,date(2026,11,10))
        self.assertEqual(list(ProgressCheckpointResult.objects.values()),checkpoints)
        self.assertEqual(corrected.completed_on,date(2026,11,10))
        old_range=challenge_score_totals(month=self.month,participant_ids=[self.reader.pk],effective_to=date(2026,11,5))
        self.assertEqual(old_range[self.reader.pk]['total_pages'],0)

    def test_stale_and_atomic_audit_rollback(self):
        item=self.submission();self.actor=self.admin()
        version=correction_version(item,self.actor)
        before=list(BookSubmission.objects.values()),list(ModifierProvenance.objects.values()),list(PersonalTBRMatch.objects.values())
        with patch('core.submission_corrections.AuditEvent.objects.create',side_effect=RuntimeError('Audit failed')):
            with self.assertRaises(RuntimeError):self.correct(item,{'page_count':400},version=version)
        self.assertEqual((list(BookSubmission.objects.values()),list(ModifierProvenance.objects.values()),list(PersonalTBRMatch.objects.values())),before)
        self.correct(item,{'page_count':400},version=version)
        with self.assertRaises(ValidationError):self.correct(item,{'page_count':450},version=version)

    def test_completion_awards_deactivate_reactivate_without_rewriting_snapshots(self):
        self.month.botm_enabled=True;self.month.botm_completion_bonus_pages=80;self.month.save()
        items=[]
        for book in self.tbr.books.order_by('position'):
            items.append(self.submission(title=book.title_snapshot))
        botm=BotmBook.objects.create(month=self.month,position=1,title_snapshot=items[0].title,
            author_snapshot=items[0].author,page_count_snapshot=350,bonus_pages=30)
        sync_botm(items[0])
        old_awards={model:list(model.objects.values('id','bonus_amount_snapshot','effective_date','completion_set_fingerprint'))
                    for model in (PersonalTBRCompletionAward,BotmCompletionAward)}
        self.assertEqual(len(old_awards[PersonalTBRCompletionAward]),1)
        self.assertEqual(len(old_awards[BotmCompletionAward]),1)
        display_before=list(PersonalTBRBook.objects.values()),list(BotmBook.objects.values())
        corrected=self.correct(items[0],{'title':'Unmatched work'})
        self.assertFalse(PersonalTBRCompletionAward.objects.get().is_qualifying)
        self.assertFalse(BotmCompletionAward.objects.get().is_qualifying)
        self.correct(corrected,{'title':self.book.title})
        self.assertTrue(PersonalTBRCompletionAward.objects.get().is_qualifying)
        self.assertTrue(BotmCompletionAward.objects.get().is_qualifying)
        for model,rows in old_awards.items():
            self.assertEqual(list(model.objects.values('id','bonus_amount_snapshot','effective_date','completion_set_fingerprint')),rows)
        self.assertEqual((list(PersonalTBRBook.objects.values()),list(BotmBook.objects.values())),display_before)

    def test_botm_replacement_and_rejected_decisions_remain_historical(self):
        self.month.botm_enabled=True;self.month.save()
        item=self.submission();other=self.submission()
        botm=BotmBook.objects.create(month=self.month,position=1,title_snapshot=item.title,
            author_snapshot=item.author,page_count_snapshot=350,bonus_pages=30)
        sync_botm(item);sync_botm(other)
        match=BotmMatch.objects.get(submission=item)
        self.correct(item,{'title':'No longer BOTM'})
        match.refresh_from_db();self.assertFalse(match.is_qualifying)
        self.assertTrue(BotmMatch.objects.get(submission=other).is_qualifying)
        BotmMatch.objects.filter(pk=match.pk).update(status='rejected')
        self.correct(item,{'title':self.book.title})
        match.refresh_from_db();self.assertEqual(match.status,'rejected')

    def test_verified_title_correction_preserves_verification_and_pending_catalog_correction_stays_pending(self):
        item=self.submission(catalog=True)
        BookSubmission.objects.filter(pk=item.pk).update(verification_method='hardcover',metadata_pages=350,
            scoring_catalog_edition=self.edition)
        corrected=self.correct(item,{'title':'Corrected title'})
        self.assertEqual(corrected.verification_method,'hardcover')
        self.assertEqual(corrected.approved_pages,350)
        pending=self.submission(status='pending',catalog=True)
        corrected=self.correct(pending,{'page_count':400})
        self.assertEqual((corrected.status,corrected.approved_pages,corrected.verification_method),('pending',None,'manual'))
        self.assertEqual(corrected.catalog_edition_id,self.edition.pk)
        self.assertFalse(corrected.personal_tbr_matches.filter(is_qualifying=True).exists())

    def test_invalid_fields_and_themes_roll_back_and_do_not_create_ui_permissions(self):
        item=self.submission()
        invalids=({'page_count':0},{'page_count':True},{'completed_on':'bad'},
                  {'completed_on':'2026-12-01'},{'theme_ids':[999999]},{'status':'approved'})
        before=list(BookSubmission.objects.values())
        for changes in invalids:
            with self.assertRaises(ValidationError):self.correct(item,changes)
            self.assertEqual(list(BookSubmission.objects.values()),before)
        self.assertFalse(can_edit_submission(self.reader.user,item))
        self.assertFalse(AuditEvent.objects.filter(action='submission.corrected').exists())

    def test_permissions_and_lifecycle_rechecked_after_provider_fetch(self):
        item=self.submission();self.actor=self.admin('host')
        work=CatalogBook.objects.create(provider='hardcover',provider_book_id='101',title='New',author='Author')
        edition=CatalogEdition.objects.create(provider='hardcover',provider_edition_id='202',book=work,page_count=300)
        def revoke(*args,**kwargs):
            self.assertFalse(connection.in_atomic_block)
            ChallengeStaffAssignment.objects.filter(month=self.month).update(ended_at=timezone.now())
            return {'title':'New','author':'Author','page_count':300}
        with patch('core.submission_corrections.fetch_edition_metadata',side_effect=revoke):
            with self.assertRaises(PermissionDenied):self.correct(item,{'catalog_edition_id':edition.pk},hardcover_token='credential')
        self.assertFalse(AuditEvent.objects.filter(action='submission.corrected').exists())
        self.assertEqual(BookSubmission.objects.get(pk=item.pk).catalog_edition_id,None)

    def test_display_refresh_does_not_invalidate_correction_or_become_matching_evidence(self):
        item=self.submission();self.actor=self.admin()
        version=correction_version(item,self.actor)
        PersonalTBRBook.objects.update(display_title='Other display name',display_page_count=9999,display_refreshed_at=timezone.now())
        display=list(PersonalTBRBook.objects.values())
        self.correct(item,{'page_count':400},version=version)
        self.assertEqual(list(PersonalTBRBook.objects.values()),display)
        self.assertTrue(PersonalTBRMatch.objects.get(submission=item).is_qualifying)

    def test_concurrent_edits_have_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections, OperationalError
        item=self.submission();actor=self.admin()
        version=correction_version(item,actor);barrier=Barrier(2)
        def prepare(*args):
            self.assertFalse(connection.in_atomic_block)
            barrier.wait(timeout=10)
            return None
        def attempt(pages):
            close_old_connections()
            try:
                correct_submission(submission=item,actor=actor,changes={'page_count':pages},version=version)
                return 'saved'
            except (ValidationError,OperationalError):return 'rejected'
            finally:close_old_connections()
        with patch('core.submission_corrections._prepare_edition',side_effect=prepare):
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes=list(pool.map(attempt,(400,450)))
        self.assertEqual(sorted(outcomes),['rejected','saved'])
        self.assertEqual(AuditEvent.objects.filter(action='submission.corrected').count(),1)
        self.assertIn(BookSubmission.objects.get(pk=item.pk).approved_pages,(400,450))
        self.assertEqual(BookSubmission.objects.count(),1)

    def test_edition_without_pages_requires_manual_override(self):
        item=self.submission(catalog=True)
        work=CatalogBook.objects.create(provider='hardcover',provider_book_id='101',title='Audio',author='Writer')
        edition=CatalogEdition.objects.create(provider='hardcover',provider_edition_id='202',book=work,format_name='Audiobook')
        with patch('core.submission_corrections.resolve_scoring_edition',return_value=(None,None)), patch('core.submission_corrections.fetch_edition_metadata',return_value={'title':'Audio','author':'Writer','page_count':None}):
            with self.assertRaises(ValidationError):
                self.correct(item,{'catalog_edition_id':edition.pk},hardcover_token='credential')
            corrected=self.correct(item,{'catalog_edition_id':edition.pk,'page_count':400},hardcover_token='credential')
        self.assertEqual(corrected.verification_method,'manual')
        self.assertEqual(corrected.catalog_edition_id,edition.pk)
        self.assertIsNone(corrected.scoring_catalog_edition_id)
        self.assertIsNone(corrected.metadata_pages)
        self.assertEqual((corrected.approved_pages,corrected.book_format),(400,'audio'))

    def test_pending_themes_continue_through_existing_review_workflow(self):
        from .scoring import apply_submission_review
        item=self.submission(status='rejected')
        theme=MonthTheme.objects.create(month=self.month,name='25%',starts_on=self.month.starts_on,
            ends_on=self.month.ends_on,bonus_percentage=25)
        self.actor=self.admin()
        corrected=self.correct(item,{'page_count':400,'theme_ids':[theme.pk]})
        claim=ThemeClaim.objects.get(submission=item)
        self.assertEqual((corrected.status,claim.status),('pending','pending'))
        self.assertFalse(ModifierProvenance.objects.filter(submission=item,is_active=True).exists())
        corrected.status='approved';corrected.approved_pages=400;claim.status='approved'
        reviewed=apply_submission_review(corrected,[claim],self.actor,timezone.now())
        claim.refresh_from_db()
        self.assertEqual(claim.approved_bonus_pages,100)
        self.assertEqual(reviewed.status,'approved')

    def test_out_of_window_rejected_claim_does_not_block_normal_review(self):
        from .forms import ThemeClaimReviewForm
        from .scoring import apply_submission_review
        item=self.submission(status='pending')
        theme=MonthTheme.objects.create(month=self.month,name='Early pending',starts_on=date(2026,11,1),
            ends_on=date(2026,11,5),bonus_percentage=25)
        self.actor=self.admin()
        corrected=self.correct(item,{'theme_ids':[theme.pk]})
        corrected=self.correct(corrected,{'completed_on':'2026-11-10'})
        claim=ThemeClaim.objects.get(submission=item)
        self.assertEqual(claim.status,'rejected')
        form=ThemeClaimReviewForm({'status':'rejected'},instance=claim)
        self.assertTrue(form.is_valid(),form.errors)
        corrected.status='approved';corrected.approved_pages=350
        apply_submission_review(corrected,[form.save(commit=False)],self.actor,timezone.now())
        claim.refresh_from_db()
        self.assertEqual((claim.status,claim.approved_bonus_pages),('rejected',0))
        claim.status='approved'
        with self.assertRaises(ValidationError):claim.full_clean()
