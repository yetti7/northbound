from datetime import date
from copy import copy
from unittest.mock import patch
from django.core import signing
from django.core.exceptions import ValidationError,PermissionDenied
from django.db import OperationalError,IntegrityError
from django.test import TransactionTestCase,override_settings
from django.urls import reverse
from django.utils import timezone
from . import test_prompt_foundations as foundation
from .models import (BookSubmission,PromptCategory,ChallengePrompt,PromptClaim,PromptCompletion,ModifierProvenance,AuditEvent,
    PersonalTBR,PersonalTBRBook,PersonalTBRMatch,MonthTheme,BotmBook,MonthEnrollment,CatalogBook,CatalogEdition,ReadingGroup,ChallengeMonth,Membership)
from .prompt_reservations import (save_new_submission,replace_selections,available_prompts,current_selections,reservations_ready)
from .submission_corrections import correction_version,correct_submission,preview_correction,apply_previewed_correction
from .personal_tbr_matching import synchronize_submission as match_tbr


@override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=True,NORTHBOUND_HARDCOVER_WRITEBACK_ENABLED=False)
class PromptReservationTests(TransactionTestCase):
    setUp_fixture=foundation.PromptFoundationTests.setUp
    token=foundation.PromptFoundationTests.token
    prompt=foundation.PromptFoundationTests.prompt
    settings=foundation.PromptFoundationTests.settings

    def setUp(self):
        self.setUp_fixture()
        self.bingo=[self.prompt(number=i) for i in range(1,4)]
        self.scavenger=[self.prompt(number=i,kind='scavenger') for i in range(1,4)]
        self.settings(enabled=True);self.settings(kind='scavenger',enabled=True)
        self.reader=self.members['reader'];self.client.force_login(self.reader.user)
        self.url=reverse('submission-create',args=[self.group.slug,self.month.pk])

    def payload(self,**updates):
        return {'title':'Target','author':'Author','book_format':'ebook','completed_on':'2027-01-03','submitted_pages':368,'themes':[],**updates}
    def pending(self,**values):
        return BookSubmission.objects.create(month=self.month,participant=self.reader,title='Target',author='Author',book_format='ebook',completed_on=date(2027,1,3),submitted_pages=368,**values)
    def reserve(self,book=None,**selections):
        return replace_selections(submission=book or self.book,actor=self.actor,selections=selections)
    def edit(self,book,**changes):
        return correct_submission(submission=book,actor=self.actor,version=correction_version(book,self.actor),changes=changes)
    def hardcover(self):
        work=CatalogBook.objects.create(provider='hardcover',provider_book_id='prompt-work',title='Target',author='Author')
        edition=CatalogEdition.objects.create(book=work,provider='hardcover',provider_edition_id='prompt-edition',page_count=368,format_name='Ebook')
        return signing.dumps({'selected':edition.pk,'scoring':edition.pk,'method':'hardcover'},salt='northbound.catalog-selection')
    def tbr(self):
        ChallengeMonth.objects.filter(pk=self.month.pk).update(tbr_enabled=True,tbr_book_bonus_pages=25)
        self.month.refresh_from_db()
        tbr=PersonalTBR.objects.create(enrollment=MonthEnrollment.objects.get(month=self.month,participant=self.reader))
        books=[PersonalTBRBook.objects.create(personal_tbr=tbr,position=i,title_snapshot='Target' if i==1 else 'Other '+str(i),author_snapshot='Author') for i in range(1,10)]
        tbr.confirmed_at=timezone.now();tbr.save()
        return books[0]

    @override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=False)
    def test_gate_hides_selectors_and_rejects_forged_posts_models_and_corrections(self):
        self.assertFalse(reservations_ready())
        response=self.client.get(self.url);self.assertNotContains(response,'name="bingo_prompt"');self.assertNotContains(response,'name="scavenger_prompt"')
        before=BookSubmission.objects.count()
        response=self.client.post(self.url,self.payload(bingo_prompt=self.bingo[0].pk));self.assertEqual(response.status_code,200)
        self.assertContains(response,'not activated');self.assertEqual(BookSubmission.objects.count(),before)
        with self.assertRaises(ValidationError):self.reserve(bingo=self.bingo[0].pk)
        with self.assertRaises(ValidationError):self.edit(self.book,bingo_prompt_id=self.bingo[0].pk)
        with self.assertRaises(ValidationError):PromptClaim.objects.create(category=self.bingo[0].category,prompt=self.bingo[0],submission=self.book,participant=self.reader)
        self.assertFalse(PromptClaim.objects.exists())
        self.assertEqual(self.client.post(self.url,self.payload()).status_code,302)

    def test_manual_both_selectors_reserve_without_credit_and_full_descriptions_render(self):
        response=self.client.get(self.url);self.assertContains(response,'name="bingo_prompt"');self.assertContains(response,'1. Full prompt wording')
        self.assertEqual(self.client.post(self.url,self.payload(bingo_prompt=self.bingo[0].pk,scavenger_prompt=self.scavenger[0].pk)).status_code,302)
        book=BookSubmission.objects.latest('pk');claims=list(book.prompt_claims.order_by('category__kind'))
        self.assertEqual(len(claims),2);self.assertEqual(book.status,'pending')
        self.assertEqual({claim.state for claim in claims},{'reserved'})
        for claim in claims:
            self.assertEqual(claim.selection_evidence['points'],25);self.assertTrue(claim.selection_evidence['selected_at']);self.assertEqual(claim.participant_id,self.reader.pk)
        self.assertFalse(ModifierProvenance.objects.exists());self.assertFalse(PromptCompletion.objects.exists())

    def test_hardcover_autoapproval_preserves_unconfirmed_reservation_and_normal_pages(self):
        response=self.client.post(self.url,self.payload(catalog_selection=self.hardcover(),bingo_prompt=self.bingo[0].pk))
        self.assertEqual(response.status_code,302);book=BookSubmission.objects.latest('pk')
        self.assertEqual((book.status,book.verification_method,book.approved_pages,book.final_scored_pages),('approved','hardcover',368,368))
        self.assertEqual(book.prompt_claims.get().state,'reserved');self.assertEqual(book.bonus_pages,0)
        self.assertFalse(ModifierProvenance.objects.exists());self.assertFalse(PromptCompletion.objects.exists())

    def test_no_selection_and_validation_errors_preserve_ordinary_flow(self):
        self.assertEqual(self.client.post(self.url,self.payload()).status_code,302)
        self.assertFalse(PromptClaim.objects.exists())
        response=self.client.post(self.url,self.payload(title='',bingo_prompt=self.bingo[0].pk))
        self.assertEqual(response.status_code,200);self.assertEqual(str(response.context['form']['bingo_prompt'].value()),str(self.bingo[0].pk));self.assertFalse(PromptClaim.objects.exists())

    def test_disabled_and_retired_choices_cannot_be_posted(self):
        PromptCategory.objects.filter(month=self.month,kind='bingo').update(enabled=False)
        self.assertNotContains(self.client.get(self.url),'name="bingo_prompt"')
        self.assertEqual(self.client.post(self.url,self.payload(bingo_prompt=self.bingo[0].pk)).status_code,200)
        with self.assertRaises(ValidationError):self.reserve(bingo=self.bingo[0].pk)
        ChallengePrompt.objects.filter(pk=self.scavenger[0].pk).update(is_active=False)
        with self.assertRaises(ValidationError):self.reserve(scavenger=self.scavenger[0].pk)
        self.assertFalse(PromptClaim.objects.exists())

    def test_reservations_and_qualifications_hide_choices_but_current_submission_keeps_its_own(self):
        self.reserve(bingo=self.bingo[0].pk)
        other=self.pending();self.assertNotIn(self.bingo[0],list(available_prompts(other,'bingo')))
        self.assertIn(self.bingo[0],list(available_prompts(self.book,'bingo')))
        claim=self.book.prompt_claims.get();claim.state='qualified';claim.save()
        self.assertNotIn(self.bingo[0],list(available_prompts(other,'bingo')))
        with self.assertRaises(ValidationError):self.reserve(other,bingo=self.bingo[0].pk)
        self.assertEqual(PromptClaim.objects.count(),1)

    def test_replacement_removal_preserve_superseded_history_and_freeze(self):
        self.reserve(bingo=self.bingo[0].pk);old=self.book.prompt_claims.get();evidence=old.selection_evidence.copy()
        self.edit(self.book,bingo_prompt_id=self.bingo[1].pk)
        old.refresh_from_db();self.assertFalse(old.is_current);self.assertEqual(old.state,'released');self.assertEqual(old.selection_evidence,evidence)
        self.assertEqual(current_selections(self.book)['bingo'].prompt_id,self.bingo[1].pk)
        self.edit(self.book,bingo_prompt_id=None)
        self.assertFalse(self.book.prompt_claims.filter(is_current=True).exists());self.assertEqual(self.book.prompt_claims.count(),2)
        category=PromptCategory.objects.get(month=self.month,kind='bingo');self.assertIsNotNone(category.structure_frozen_at)
        self.assertFalse(ModifierProvenance.objects.exists())

    def test_metadata_and_link_only_preserve_all_reservation_evidence(self):
        self.reserve(bingo=self.bingo[0].pk);before=list(self.book.prompt_claims.values())
        self.edit(self.book,title='Metadata correction')
        self.assertEqual(list(self.book.prompt_claims.values()),before)
        work=CatalogBook.objects.create(provider='hardcover',provider_book_id='link-work',title='Linked work',author='Author')
        edition=CatalogEdition.objects.create(provider='hardcover',provider_edition_id='link-edition',book=work,page_count=400)
        with patch('core.submission_corrections.fetch_edition_metadata',return_value={'title':'Linked work','author':'Author','page_count':400}):
            correct_submission(submission=self.book,actor=self.actor,version=correction_version(self.book,self.actor),changes={'association_mode':'only','catalog_edition_id':edition.pk},hardcover_token='mock-credential')
        self.assertEqual(list(self.book.prompt_claims.values()),before)
        self.book.refresh_from_db();self.assertEqual((self.book.approved_pages,self.book.final_scored_pages),(368,368))

    def test_rejection_and_void_release_without_qualifying_or_awarding(self):
        self.reserve(bingo=self.bingo[0].pk,scavenger=self.scavenger[0].pk)
        self.book.status='rejected';self.book.save()
        self.assertEqual(set(self.book.prompt_claims.values_list('state',flat=True)),{'rejected'})
        self.book.status='pending';self.book.save()
        self.assertEqual(set(self.book.prompt_claims.values_list('state',flat=True)),{'reserved'})
        self.client.force_login(self.actor)
        response=self.client.post(reverse('submission-remove',args=[self.group.slug,self.month.pk,self.book.pk]))
        self.assertEqual(response.status_code,302);self.assertEqual(set(self.book.prompt_claims.values_list('state',flat=True)),{'released'})
        self.assertFalse(ModifierProvenance.objects.exists());self.assertFalse(PromptCompletion.objects.exists())

    def test_restore_reacquires_original_reservation_and_conflicts_roll_back(self):
        from .recovery_scoring import restore_submission_as_staff,staff_restore_confirmation
        book=self.pending();self.reserve(book,bingo=self.bingo[0].pk)
        book.is_removed=True;book.removed_at=timezone.now();book.removed_by=self.actor;book.save()
        claim=book.prompt_claims.get();self.assertEqual(claim.state,'released')
        other=self.pending();self.reserve(other,bingo=self.bingo[0].pk)
        with self.assertRaises(ValidationError):restore_submission_as_staff(submission=book,actor=self.actor,confirmation=staff_restore_confirmation(book,self.actor))
        book.refresh_from_db();self.assertTrue(book.is_removed);claim.refresh_from_db();self.assertEqual(claim.state,'released')
        self.reserve(other,bingo=None)
        restore_submission_as_staff(submission=book,actor=self.actor,confirmation=staff_restore_confirmation(book,self.actor))
        claim.refresh_from_db();self.assertEqual(claim.state,'reserved');self.assertEqual(book.prompt_claims.count(),1)
        self.assertFalse(ModifierProvenance.objects.exists())

    def test_duplicate_and_busy_requests_fail_cleanly_without_partial_writes(self):
        self.reserve(bingo=self.bingo[0].pk);other=self.pending();before=list(PromptClaim.objects.values())
        with self.assertRaises(ValidationError):self.reserve(other,bingo=self.bingo[0].pk)
        with patch('core.prompt_reservations._replace',side_effect=OperationalError('database is locked')):
            with self.assertRaisesMessage(ValidationError,'Nothing was saved'):self.reserve(other,bingo=self.bingo[1].pk)
        self.assertEqual(list(PromptClaim.objects.values()),before)
        count=AuditEvent.objects.count();self.reserve(bingo=self.bingo[0].pk);self.assertEqual(AuditEvent.objects.count(),count)

    def test_two_category_failure_rolls_back_first_reservation_and_structure_freeze(self):
        with self.assertRaises(ValidationError):self.reserve(bingo=self.bingo[0].pk,scavenger=999999)
        self.assertFalse(PromptClaim.objects.exists());self.assertFalse(PromptCategory.objects.filter(structure_frozen_at__isnull=False).exists())

    def test_cross_challenge_reader_and_group_tampering(self):
        group=ReadingGroup.objects.create(name='Other',slug='foreign-prompt')
        month=ChallengeMonth.objects.create(group=group,name='Other',status='active',starts_on=self.month.starts_on,ends_on=self.month.ends_on)
        category=PromptCategory.objects.create(month=month,kind='bingo')
        prompt=ChallengePrompt.objects.create(category=category,number=1,description='Other')
        with self.assertRaises(ValidationError):self.reserve(bingo=prompt.pk)
        with self.assertRaises(PermissionDenied):replace_selections(submission=self.book,actor=self.users['outsider'],selections={'bingo':self.bingo[0].pk})
        self.reserve(bingo=self.bingo[0].pk);claim=self.book.prompt_claims.get();claim.participant=self.members['owner']
        with self.assertRaises(ValidationError):claim.save()
        fresh=BookSubmission(month=self.month,participant=self.reader,title='Target',author='Author',book_format='ebook',completed_on=date(2027,1,3),submitted_pages=368)
        with self.assertRaises(PermissionDenied):save_new_submission(submission=fresh,actor=self.users['outsider'],selections={'bingo':self.bingo[1].pk})

    def test_bingo_suppresses_unawarded_automatic_tbr_but_scavenger_does_not(self):
        entry=self.tbr()
        self.assertEqual(self.client.post(self.url,self.payload(catalog_selection=self.hardcover(),bingo_prompt=self.bingo[0].pk)).status_code,302)
        book=BookSubmission.objects.latest('pk');self.assertFalse(book.personal_tbr_matches.filter(is_qualifying=True).exists());self.assertFalse(ModifierProvenance.objects.exists())
        for _ in range(2):match_tbr(book)
        self.assertFalse(book.personal_tbr_matches.exists())
        # Separate approved book gets the existing TBR behavior alongside Scavenger.
        self.assertEqual(self.client.post(self.url,self.payload(catalog_selection=self.hardcover_existing(),scavenger_prompt=self.scavenger[0].pk)).status_code,302)
        other=BookSubmission.objects.latest('pk');self.assertTrue(other.personal_tbr_matches.filter(personal_tbr_book=entry,is_qualifying=True).exists())
        other.refresh_from_db();self.assertEqual(other.bonus_pages,25);self.assertEqual(other.prompt_claims.get().state,'reserved');self.assertFalse(PromptCompletion.objects.exists())

    def hardcover_existing(self):
        edition=CatalogEdition.objects.get(provider_edition_id='prompt-edition')
        return signing.dumps({'selected':edition.pk,'scoring':edition.pk,'method':'hardcover'},salt='northbound.catalog-selection')

    def test_explicit_tbr_and_bingo_new_submission_is_rejected(self):
        from .matching_intent import selection_token
        entry=self.tbr();before=BookSubmission.objects.count()
        response=self.client.post(self.url,self.payload(bingo_prompt=self.bingo[0].pk,selected_tbr=entry.pk,tbr_token=selection_token(entry,self.reader.user)))
        self.assertEqual(response.status_code,200);self.assertEqual(BookSubmission.objects.count(),before);self.assertFalse(PromptClaim.objects.exists())

    def test_existing_tbr_credit_requires_audited_reversal_and_matching_cannot_bypass(self):
        from .administrative_matching import preview_credit,apply_credit
        entry=self.tbr();book=self.pending(status='approved',approved_pages=368)
        match_tbr(book);match=book.personal_tbr_matches.get(is_qualifying=True)
        before=list(ModifierProvenance.objects.values())
        with self.assertRaises(ValidationError):self.reserve(book,bingo=self.bingo[0].pk)
        self.assertEqual(list(ModifierProvenance.objects.values()),before);self.assertFalse(PromptClaim.objects.exists())
        operation={'domain':'tbr','entry_id':entry.pk,'operation':'reverse','reason':'Explicit test correction','acknowledge':False}
        _,token=preview_credit(submission=book,actor=self.actor,**operation);apply_credit(submission=book,actor=self.actor,token=token,**operation)
        self.reserve(book,bingo=self.bingo[0].pk)
        match.refresh_from_db();self.assertFalse(match.is_qualifying)
        match.status='confirmed';match.is_qualifying=True
        with self.assertRaises(ValidationError):match.save()
        self.assertTrue(AuditEvent.objects.filter(action='submission.credit_reverse').exists())

    def test_november_guard_blocks_selectors_service_creation_and_correction(self):
        ReadingGroup.objects.filter(pk=self.group.pk).update(slug='guildwars');self.group.refresh_from_db()
        ChallengeMonth.objects.filter(pk=self.month.pk).update(starts_on=date(2026,11,1),ends_on=date(2026,11,30));self.month.refresh_from_db()
        BookSubmission.objects.filter(pk=self.book.pk).update(completed_on=date(2026,11,3));self.book.refresh_from_db()
        self.url=reverse('submission-create',args=[self.group.slug,self.month.pk])
        self.assertNotContains(self.client.get(self.url),'name="bingo_prompt"')
        with self.assertRaises(ValidationError):self.reserve(bingo=self.bingo[0].pk)
        with self.assertRaises(ValidationError):self.edit(self.book,bingo_prompt_id=self.bingo[0].pk)
        self.assertFalse(PromptClaim.objects.exists());self.assertFalse(ModifierProvenance.objects.exists())

    def test_signed_prompt_preview_is_nonmutating_and_detects_new_reservations(self):
        version=correction_version(self.book,self.actor)
        preview=preview_correction(submission=self.book,actor=self.actor,changes={'bingo_prompt_id':self.bingo[0].pk},version=version)
        self.assertFalse(PromptClaim.objects.exists())
        other=self.pending();self.reserve(other,bingo=self.bingo[0].pk)
        with self.assertRaises(ValidationError):apply_previewed_correction(submission=self.book,actor=self.actor,changes={'bingo_prompt_id':self.bingo[0].pk},version=version,preview_token=preview['token'],confirm_approved=True)
        self.assertFalse(self.book.prompt_claims.exists())

    def test_signed_prompt_preview_applies_through_existing_correction_engine(self):
        version=correction_version(self.book,self.actor);changes={'bingo_prompt_id':self.bingo[0].pk}
        preview=preview_correction(submission=self.book,actor=self.actor,changes=changes,version=version)
        apply_previewed_correction(submission=self.book,actor=self.actor,changes=changes,version=version,preview_token=preview['token'],confirm_approved=True)
        self.assertEqual(self.book.prompt_claims.get().state,'reserved');self.book.refresh_from_db();self.assertEqual(self.book.final_scored_pages,368)

    def test_ordinary_approval_keeps_reservations_unconfirmed(self):
        from .submission_review_matching import review_token,apply_unified_review
        book=self.pending();self.reserve(book,bingo=self.bingo[0].pk)
        version=review_token(book,self.actor);book.status='approved';book.approved_pages=368
        apply_unified_review(book,[],self.actor,timezone.now(),version=version,confirmations=set())
        self.assertEqual(book.prompt_claims.get().state,'reserved');self.assertFalse(PromptCompletion.objects.exists());self.assertFalse(ModifierProvenance.objects.exists())

    def test_prompt_selections_remain_compatible_with_themes_and_botm(self):
        ChallengeMonth.objects.filter(pk=self.month.pk).update(botm_enabled=True)
        theme=MonthTheme.objects.create(month=self.month,name='Compatible Theme',starts_on=self.month.starts_on,ends_on=self.month.ends_on)
        botm=BotmBook.objects.create(month=self.month,position=1,title_snapshot='Target',author_snapshot='Author',page_count_snapshot=368,bonus_pages=0)
        response=self.client.post(self.url,self.payload(catalog_selection=self.hardcover(),themes=[theme.pk],bingo_prompt=self.bingo[0].pk,scavenger_prompt=self.scavenger[0].pk))
        self.assertEqual(response.status_code,302)
        book=BookSubmission.objects.latest('pk')
        self.assertEqual(book.prompt_claims.filter(state='reserved').count(),2)
        self.assertTrue(book.theme_claims.filter(theme=theme).exists());self.assertTrue(book.botm_matches.filter(botm_book=botm,is_qualifying=True).exists())
        self.assertFalse(PromptCompletion.objects.exists())

    def test_closed_gate_metadata_preserves_claims_but_lifecycle_post_fails_safely(self):
        self.reserve(bingo=self.bingo[0].pk)
        before=list(self.book.prompt_claims.values())
        with override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=False):
            self.edit(self.book,title='Ordinary metadata remains available')
            self.assertEqual(list(self.book.prompt_claims.values()),before)
            self.client.force_login(self.actor)
            response=self.client.post(reverse('submission-remove',args=[self.group.slug,self.month.pk,self.book.pk]),follow=True)
            self.assertEqual(response.status_code,200);self.assertContains(response,'Nothing was saved')
            self.book.refresh_from_db();self.assertFalse(self.book.is_removed)
            self.assertEqual(list(self.book.prompt_claims.values()),before)

    def test_rejected_edit_can_choose_a_new_prompt_without_reclaiming_old_hold(self):
        book=self.pending();self.reserve(book,bingo=self.bingo[0].pk)
        book.status='rejected';book.save()
        other=self.pending();self.reserve(other,bingo=self.bingo[0].pk)
        self.edit(book,bingo_prompt_id=self.bingo[1].pk,title='Corrected rejected submission')
        book.refresh_from_db();self.assertEqual(book.status,'pending')
        self.assertEqual(current_selections(book)['bingo'].prompt_id,self.bingo[1].pk)
        self.assertEqual(current_selections(book)['bingo'].state,'reserved')
        self.assertEqual(current_selections(other)['bingo'].prompt_id,self.bingo[0].pk)

    def test_partial_metadata_save_does_not_release_from_unpersisted_status(self):
        self.reserve(bingo=self.bingo[0].pk)
        before=list(self.book.prompt_claims.values())
        stale=BookSubmission.objects.get(pk=self.book.pk)
        stale.status='rejected'
        stale.title='Only the metadata field is saved'
        stale.save(update_fields=['title'])
        stale.refresh_from_db()
        self.assertEqual(stale.status,'approved')
        self.assertEqual(list(self.book.prompt_claims.values()),before)
