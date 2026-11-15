"""Reader-only edits and post-submission requests on synthetic Challenge records."""
from datetime import date
from unittest.mock import patch
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from . import test_submission_hotfix as fixture
from . import reader_submissions as service
from .matching_intent import selection_token
from .matching_resolution import confirm_suggestion, review_submission_with_matches
from .models import (BookSubmission, PersonalTBRBook, PersonalTBRMatch, BotmBook, BotmMatch,
    AuditEvent, MonthEnrollment, Membership, MonthTheme, ThemeClaim, ModifierProvenance,
    PersonalTBRCompletionAward, Team, TeamAssignment, ChallengeStaffAssignment, CatalogBook, CatalogEdition, DiscordEventDelivery)
from . import test_submission_editing as editing


@override_settings(NORTHBOUND_DISCORD_EVENTS_ENABLED=False, NORTHBOUND_DISCORD_SCHEDULER_ENABLED=False)
class ReaderSubmissionTests(TestCase):
    setUp=fixture.SubmissionHotfixTests.setUp
    actor=editing.SubmissionEditingTests.actor
    payload=fixture.SubmissionHotfixTests.payload
    token=fixture.SubmissionHotfixTests.token

    def submission(self, status='pending', **kwargs):
        return BookSubmission.objects.create(month=self.month,participant=self.reader,title='Death Valley',author='Karin Hale',
            book_format='ebook',completed_on=date(2026,11,3),submitted_pages=384,status=status,
            approved_pages=384 if status=='approved' else None,**kwargs)

    def target(self):
        type(self.tbr).objects.filter(pk=self.tbr.pk).update(confirmed_at=None)
        book=PersonalTBRBook.objects.get(personal_tbr=self.tbr,position=1)
        book.personal_tbr.confirmed_at=None
        book.catalog_book=book.catalog_edition=None
        book.title_snapshot='Death Valley';book.author_snapshot='Karina Halle';book.save()
        type(self.tbr).objects.filter(pk=self.tbr.pk).update(confirmed_at=self.tbr.confirmed_at)
        return book

    def edit_data(self,s,**changes):
        data=self.payload(title=s.title,author=s.author,version=service.version(s,self.reader.user),book_format=s.book_format)
        data.update(changes);return data

    def url_for(self,s,name='reader-submission'):
        return reverse(name,args=['hotfix',self.month.pk,s.pk])

    def request_match(self,s,book,domain='tbr',actor=None):
        actor=actor or self.reader.user
        return service.request_qualification(submission=s,actor=actor,domain=domain,
            token=selection_token(book,actor),state=service.version(s,actor))

    def test_own_pending_fields_preserve_status_and_audit(self):
        s=self.submission();data=self.edit_data(s,title='Corrected',author='Correct Author',book_format='audio',submitted_pages=200,completed_on='2026-11-04',status='approved',approved_pages=999,reviewed_by=99,participant=99,month=99)
        service.save_pending(submission=s,actor=self.reader.user,data=data)
        s.refresh_from_db()
        self.assertEqual((s.title,s.author,s.book_format,s.submitted_pages,s.completed_on),('Corrected','Correct Author','audio',200,date(2026,11,4)))
        self.assertEqual(s.status,'pending');self.assertIsNone(s.approved_pages);self.assertIsNone(s.reviewed_by_id)
        self.assertEqual(s.participant_id,self.reader.pk)
        self.assertTrue(AuditEvent.objects.filter(action='submission.reader_edited').exists())
        self.assertContains(self.client.get(self.url_for(s)),'Submission edited by Reader')

    def test_other_reader_and_staff_cannot_use_reader_editor(self):
        s=self.submission();staff=self.actor('host')
        with self.assertRaises(PermissionDenied):service.save_pending(submission=s,actor=staff,data=self.edit_data(s))
        self.client.force_login(staff)
        self.assertEqual(self.client.get(self.url_for(s,'reader-submission-edit')).status_code,403)
        self.assertEqual(self.client.post(self.url_for(s),{}).status_code,403)
        self.assertEqual(self.client.get(reverse('submission-editor',args=['hotfix',self.month.pk,s.pk])).status_code,200)

    def test_approved_rejected_voided_and_closed_not_editable(self):
        for status in ('approved','rejected'):
            s=self.submission(status=status)
            self.assertFalse(service.eligible(self.reader.user,s,editing=True))
            self.assertEqual(self.client.get(self.url_for(s,'reader-submission-edit')).status_code,403)
        s=self.submission();BookSubmission.objects.filter(pk=s.pk).update(is_removed=True)
        self.assertFalse(service.eligible(self.reader.user,service.load(s.pk),editing=True))
        for status in ('completed','archived','draft'):
            type(self.month).objects.filter(pk=self.month.pk).update(status=status)
            self.assertFalse(service.eligible(self.reader.user,service.load(s.pk),editing=True))

    def test_hardcover_edit_uses_signed_provider_evidence_but_stays_pending(self):
        s=self.submission()
        result=service.save_pending(submission=s,actor=self.reader.user,data=self.edit_data(s,catalog_selection=self.token(),title='Forged',submitted_pages=999))
        self.assertEqual(result.title,self.book.title);self.assertEqual(result.catalog_book_id,self.book.pk)
        self.assertEqual(result.submitted_pages,384);self.assertEqual(result.metadata_pages,384)
        self.assertEqual(result.status,'pending');self.assertEqual(result.verification_method,'manual')
        self.assertIsNone(result.approved_pages)
        result.refresh_from_db();self.assertEqual(result.status,'pending')
        data=self.edit_data(result,catalog_selection='tampered')
        with self.assertRaises(ValidationError):service.save_pending(submission=result,actor=self.reader.user,data=data)

    def test_catalog_parent_conflict_rejected(self):
        s=self.submission()
        other=CatalogBook.objects.create(provider='hardcover',provider_book_id='other-work',title='Other',author='Other')
        edition=CatalogEdition.objects.create(book=other,provider='hardcover',provider_edition_id='other-edition',page_count=100)
        token=signing.dumps({'selected':self.edition.pk,'scoring':edition.pk,'method':'hardcover'},salt='northbound.catalog-selection')
        with self.assertRaises(ValidationError):service.save_pending(submission=s,actor=self.reader.user,data=self.edit_data(s,catalog_selection=token))
        s.refresh_from_db();self.assertIsNone(s.catalog_book_id)

    def test_themes_added_and_removed_without_awards(self):
        theme=MonthTheme.objects.create(month=self.month,name='November',starts_on=self.month.starts_on,ends_on=self.month.ends_on)
        s=self.submission()
        result=service.save_pending(submission=s,actor=self.reader.user,data=self.edit_data(s,themes=[theme.pk]))
        claim=ThemeClaim.objects.get(submission=s);self.assertEqual(claim.status,'pending')
        self.assertEqual(claim.approved_bonus_pages,0)
        service.save_pending(submission=result,actor=self.reader.user,data=self.edit_data(result,themes=[]))
        claim.refresh_from_db();self.assertEqual(claim.status,'rejected');self.assertFalse(ModifierProvenance.objects.exists())

    def test_date_and_theme_validation_are_reused(self):
        s=self.submission()
        for changes in ({'completed_on':'2026-12-01'},{'submitted_pages':0},{'completed_on':'invalid-date'}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                service.save_pending(submission=s,actor=self.reader.user,data=self.edit_data(s,**changes))
        self.assertFalse(AuditEvent.objects.filter(action='submission.reader_edited').exists())

    def test_stale_approval_and_rejection_preserved(self):
        for status in ('approved','rejected'):
            s=self.submission();data=self.edit_data(s,title='Must not save')
            BookSubmission.objects.filter(pk=s.pk).update(status=status,approved_pages=321 if status=='approved' else None)
            with self.assertRaises(PermissionDenied):service.save_pending(submission=s,actor=self.reader.user,data=data)
            s.refresh_from_db();self.assertEqual(s.status,status);self.assertEqual(s.title,'Death Valley')
            if status=='approved':self.assertEqual(s.approved_pages,321)

    def test_concurrent_edit_and_duplicate_noop_form_are_stale(self):
        s=self.submission();data=self.edit_data(s)
        service.save_pending(submission=s,actor=self.reader.user,data=data)
        with self.assertRaises(ValidationError):service.save_pending(submission=s,actor=self.reader.user,data=data)
        self.assertEqual(AuditEvent.objects.filter(action='submission.reader_edited').count(),1)

    def test_departure_and_team_transfer_invalidate_form(self):
        s=self.submission();data=self.edit_data(s)
        team=Team.objects.create(month=self.month,name='New Team')
        TeamAssignment.objects.create(month=self.month,team=team,participant=self.reader)
        with self.assertRaises(ValidationError):service.save_pending(submission=s,actor=self.reader.user,data=data)
        MonthEnrollment.objects.filter(month=self.month,participant=self.reader).update(is_active=False)
        with self.assertRaises(PermissionDenied):service.save_pending(submission=s,actor=self.reader.user,data=data)

    def test_lifecycle_transition_invalidates_form(self):
        s=self.submission();data=self.edit_data(s)
        type(self.month).objects.filter(pk=self.month.pk).update(status='finalizing')
        with self.assertRaises(ValidationError):service.save_pending(submission=s,actor=self.reader.user,data=data)
        type(self.month).objects.filter(pk=self.month.pk).update(status='completed')
        self.assertEqual(self.client.post(self.url_for(s,'reader-submission-edit'),data).status_code,403)

    def test_ambiguous_pending_request_and_original_intent_preserved(self):
        book=self.target();s=self.submission()
        before=(s.selected_tbr_book_id,s.reader_intent.copy())
        match=self.request_match(s,book)
        self.assertEqual(match.status,'pending_review');self.assertFalse(match.is_qualifying)
        self.assertEqual(service.request_data(match)['actor_id'],self.reader.user_id)
        s.refresh_from_db();self.assertEqual((s.selected_tbr_book_id,s.reader_intent),before)
        self.assertContains(self.client.get(self.url_for(s)),'Reader post-submission request')
        self.assertTrue(AuditEvent.objects.filter(action='submission.qualification_requested').exists())
        self.assertFalse(ModifierProvenance.objects.exists())

    def test_approved_strong_request_never_awards_during_matching(self):
        book=self.target();s=self.submission(status='approved');s.author=book.author_snapshot;s.save()
        before=BookSubmission.objects.filter(pk=s.pk).values().get()
        match=self.request_match(s,book)
        from .personal_tbr_matching import synchronize_submission
        synchronize_submission(s);synchronize_submission(s)
        match.refresh_from_db();self.assertFalse(match.is_qualifying);self.assertEqual(match.status,'pending_review')
        self.assertEqual(BookSubmission.objects.filter(pk=s.pk).values().get(),before)
        self.assertFalse(ModifierProvenance.objects.exists())

    def test_request_duplicate_rejection_tampering_and_cross_scope(self):
        book=self.target();s=self.submission()
        self.request_match(s,book)
        with self.assertRaises(ValidationError):self.request_match(s,book)
        other=self.actor('host')
        with self.assertRaises(PermissionDenied):self.request_match(s,book,actor=other)
        s2=self.submission()
        with self.assertRaises(ValidationError):service.request_qualification(submission=s2,actor=self.reader.user,domain='tbr',token='tampered',state=service.version(s2,self.reader.user))
        self.assertEqual(PersonalTBRMatch.objects.filter(submission=s).count(),1)

    def test_approved_ambiguous_request_review_preserves_book_and_themes(self):
        book=self.target();s=self.submission(status='approved');actor=self.actor('floater')
        match=self.request_match(s,book)
        before=BookSubmission.objects.filter(pk=s.pk).values().get()
        with self.assertRaises(ValidationError):confirm_suggestion(match=match,actor=self.reader.user,decision='confirmed')
        service.review_request(submission=s,actor=actor,domain='tbr',match_id=match.pk,state=service.version(service.load(s.pk),actor),decision='confirmed')
        s.refresh_from_db();match.refresh_from_db()
        self.assertTrue(match.is_qualifying);self.assertEqual(s.approved_pages,384)
        after=BookSubmission.objects.filter(pk=s.pk).values().get()
        for key in ('bonus_pages','final_scored_pages'):before.pop(key);after.pop(key)
        self.assertEqual(before,after)
        self.assertTrue(service.request_data(match))
        self.assertContains(self.client.get(self.url_for(s)),'Qualification request approved')
        with self.assertRaises(ValidationError):self.request_match(s,book)

    def test_rejection_is_visible_and_cannot_be_re_requested(self):
        book=self.target();s=self.submission(status='approved');actor=self.actor('floater')
        match=self.request_match(s,book)
        service.review_request(submission=s,actor=actor,domain='tbr',match_id=match.pk,state=service.version(s,actor),decision='rejected')
        self.assertContains(self.client.get(self.url_for(s)),'Rejected')
        with self.assertRaises(ValidationError):self.request_match(s,book)
        self.assertFalse(ModifierProvenance.objects.exists())

    def test_book_approval_alone_keeps_request_pending_then_existing_review_confirms(self):
        book=self.target();s=self.submission();match=self.request_match(s,book);actor=self.actor('floater')
        s.status='approved';s.approved_pages=384
        review_submission_with_matches(s,[],actor,timezone.now())
        match.refresh_from_db();self.assertFalse(match.is_qualifying)
        confirm_suggestion(match=match,actor=actor,decision='confirmed')
        match.refresh_from_db();self.assertTrue(match.is_qualifying)

    def test_stale_request_review_and_reader_departure_fail_closed(self):
        book=self.target();s=self.submission(status='approved');match=self.request_match(s,book);actor=self.actor('floater')
        token=service.version(s,actor)
        BookSubmission.objects.filter(pk=s.pk).update(author='Changed Identity')
        with self.assertRaises(ValidationError):service.review_request(submission=s,actor=actor,domain='tbr',match_id=match.pk,state=token,decision='confirmed')
        BookSubmission.objects.filter(pk=s.pk).update(author='Karin Hale')
        MonthEnrollment.objects.filter(month=self.month,participant=self.reader).update(is_active=False)
        with self.assertRaises(ValidationError):confirm_suggestion(match=match,actor=actor,decision='confirmed')
        match.refresh_from_db();self.assertFalse(match.is_qualifying)

    def test_conflicting_work_and_unrelated_target_fail_closed(self):
        book=self.target();s=self.submission(status='approved')
        other=self.tbr.books.get(position=2)
        with self.assertRaises(ValidationError):self.request_match(s,other)
        left=CatalogBook.objects.create(provider='hardcover',provider_book_id='left',title='Death Valley',author='Karin Hale')
        right=CatalogBook.objects.create(provider='hardcover',provider_book_id='right',title='Death Valley',author='Karina Halle')
        BookSubmission.objects.filter(pk=s.pk).update(catalog_book=left)
        PersonalTBRBook.objects.filter(pk=book.pk).update(catalog_book=right)
        s=service.load(s.pk);book.refresh_from_db()
        with self.assertRaises(ValidationError):self.request_match(s,book)

    def test_botm_pending_edit_selection_is_audited_request_not_original_intent(self):
        self.month.botm_enabled=True;self.month.save()
        book=BotmBook.objects.create(month=self.month,position=1,title_snapshot='Death Valley',author_snapshot='Karina Halle',page_count_snapshot=384,bonus_pages=250)
        s=self.submission()
        service.save_pending(submission=s,actor=self.reader.user,data=self.edit_data(s,botm_choice=selection_token(book,self.reader.user)))
        s.refresh_from_db();self.assertIsNone(s.selected_botm_book_id);self.assertEqual(s.reader_intent,{})
        match=BotmMatch.objects.get(submission=s);self.assertEqual(match.status,'pending_review');self.assertTrue(service.request_data(match))
        self.assertFalse(match.is_qualifying)

    def test_request_and_pending_edit_audits_roll_back_with_failures(self):
        book=self.target();s=self.submission()
        with patch('core.reader_submissions.AuditEvent.objects.create',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.request_match(s,book)
        self.assertFalse(PersonalTBRMatch.objects.exists())
        original=s.title
        with patch('core.reader_submissions.AuditEvent.objects.create',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):service.save_pending(submission=s,actor=self.reader.user,data=self.edit_data(s,title='Must roll back'))
        s.refresh_from_db();self.assertEqual(s.title,original)
        self.assertFalse(DiscordEventDelivery.objects.exists())

    def test_detail_actions_and_validation_errors(self):
        self.target();s=self.submission()
        self.assertContains(self.client.get(self.url_for(s)),'Edit Submission')
        self.assertContains(self.client.get(self.url_for(s)),'Possible Match')
        response=self.client.post(self.url_for(s,'reader-submission-edit'),self.edit_data(s,completed_on='2026-12-01'))
        self.assertContains(response,'Completion date must fall inside the challenge month')
        self.assertContains(response,'Cancel')
        BookSubmission.objects.filter(pk=s.pk).update(status='approved',approved_pages=384)
        self.assertNotContains(self.client.get(self.url_for(s)),'Edit Submission')
        self.assertContains(self.client.get(self.url_for(s)),'Request Review')

    def test_finalizing_reader_edit_and_read_only_picker_preview(self):
        s=self.submission()
        type(self.month).objects.filter(pk=self.month.pk).update(status='finalizing')
        s=service.load(s.pk)
        self.assertEqual(self.client.get(self.url_for(s,'reader-submission-edit')).status_code,200)
        response=self.client.post(self.url_for(s,'reader-submission-edit'),self.edit_data(s,action='intent-preview'))
        self.assertTrue(response.json()['ok']);self.assertFalse(AuditEvent.objects.exists())
        service.save_pending(submission=s,actor=self.reader.user,data=self.edit_data(s,title='Finalizing correction'))
        s.refresh_from_db();self.assertEqual(s.status,'pending')

    def test_existing_catalog_link_is_kept_unless_explicitly_removed(self):
        s=self.submission(catalog_book=self.book,catalog_edition=self.edition)
        result=service.save_pending(submission=s,actor=self.reader.user,data=self.edit_data(s,title='Manual correction',submitted_pages=222))
        self.assertEqual(result.catalog_edition_id,self.edition.pk)
        self.assertEqual(result.title,'Manual correction');self.assertEqual(result.submitted_pages,222)
        result=service.save_pending(submission=result,actor=self.reader.user,data=self.edit_data(result,clear_catalog='on'))
        self.assertIsNone(result.catalog_book_id);self.assertIsNone(result.catalog_edition_id)

    def test_selection_removal_and_retarget_preserve_original_intent(self):
        book=self.target();s=self.submission()
        first=self.request_match(s,book)
        response=self.client.get(self.url_for(s,'reader-submission-edit'))
        self.assertContains(response,'selected')
        service.save_pending(submission=s,actor=self.reader.user,data=self.edit_data(s,tbr_choice=''))
        first.refresh_from_db();self.assertEqual(first.status,'rejected')
        self.assertIn('reader_withdrawal',first.decision_evidence)
        self.assertContains(self.client.get(self.url_for(s)),'Withdrawn by Reader')
        s.refresh_from_db();self.assertIsNone(s.selected_tbr_book_id);self.assertEqual(s.reader_intent,{})

    def test_original_selection_and_acceptance_evidence_survive_pending_edit(self):
        from .matching_intent import capture_intent
        from .personal_tbr_matching import synchronize_submission
        book=self.tbr.books.first()
        s=BookSubmission(month=self.month,participant=self.reader,title=book.title_snapshot,author=book.author_snapshot,
            book_format='ebook',completed_on=date(2026,11,3),submitted_pages=384)
        capture_intent(s,actor=self.reader.user,tbr_id=book.pk,tokens={'tbr':selection_token(book,self.reader.user)})
        s.save();synchronize_submission(s)
        original=s.reader_intent.copy()
        service.save_pending(submission=s,actor=self.reader.user,data=self.edit_data(s,title='Edited identity',tbr_choice=''))
        s.refresh_from_db();self.assertEqual(s.reader_intent,original);self.assertEqual(s.selected_tbr_book_id,book.pk)
        self.assertEqual(PersonalTBRMatch.objects.get(submission=s).status,'rejected')
        from .submission_review_matching import matching_cards
        self.assertEqual(matching_cards(service.load(s.pk))[0]['state'],'conflict')

    def test_frozen_completion_guard_rolls_back_request_decision(self):
        book=self.target();s=self.submission(status='approved');actor=self.actor('floater');match=self.request_match(s,book)
        now=timezone.now()
        award=PersonalTBRCompletionAward.objects.create(month=self.month,participant=self.reader,personal_tbr=self.tbr,
            completion_set_fingerprint='synthetic-frozen',bonus_amount_snapshot=123,qualified_at=now,last_qualified_at=now,effective_date=date(2026,11,2),is_qualifying=False)
        # A synthetic downstream fault must not rewrite frozen award evidence.
        def corrupt(*args,**kwargs):
            PersonalTBRCompletionAward.objects.filter(pk=award.pk).update(bonus_amount_snapshot=999)
        with patch('core.personal_tbr_matching._finalize_submission_matches',side_effect=corrupt):
            with self.assertRaisesMessage(ValidationError,'frozen completion award'):
                confirm_suggestion(match=match,actor=actor,decision='confirmed')
        award.refresh_from_db();match.refresh_from_db()
        self.assertEqual(award.bonus_amount_snapshot,123);self.assertFalse(match.is_qualifying)
        self.assertFalse(AuditEvent.objects.filter(action='submission.qualification_confirmed').exists())
        self.assertFalse(ModifierProvenance.objects.exists())

    def test_existing_theme_evidence_and_pages_unchanged_by_approved_request(self):
        book=self.target();s=self.submission(status='approved')
        theme=MonthTheme.objects.create(month=self.month,name='Existing Theme',starts_on=self.month.starts_on,ends_on=self.month.ends_on)
        ThemeClaim.objects.create(submission=s,theme=theme,status='approved',approved_bonus_pages=50)
        before=list(s.theme_claims.values());pages=s.approved_pages
        self.request_match(s,book)
        self.assertEqual(list(s.theme_claims.values()),before)
        s.refresh_from_db();self.assertEqual(s.approved_pages,pages)

    def test_scoped_reviewer_and_ordinary_self_approval_are_preserved(self):
        book=self.target();s=self.submission(status='approved');match=self.request_match(s,book)
        team=Team.objects.create(month=self.month,name='Reader Team')
        TeamAssignment.objects.create(month=self.month,team=team,participant=self.reader)
        ChallengeStaffAssignment.objects.create(month=self.month,membership=self.reader,role='team_leader',team=team)
        # Existing ordinary-book self-review remains valid for an eligible Leader.
        confirm_suggestion(match=match,actor=self.reader.user,decision='confirmed')
        match.refresh_from_db();self.assertTrue(match.is_qualifying)

    def test_request_closed_challenge_and_already_qualified_entry_denied(self):
        book=self.target();s=self.submission(status='approved');match=self.request_match(s,book);actor=self.actor('floater')
        confirm_suggestion(match=match,actor=actor,decision='confirmed')
        other=self.submission(status='approved')
        with self.assertRaises(ValidationError):self.request_match(other,book)
        type(self.month).objects.filter(pk=self.month.pk).update(status='completed')
        with self.assertRaises(PermissionDenied):self.request_match(other,book)

    def test_request_review_post_uses_existing_scoped_review_page(self):
        book=self.target();s=self.submission(status='approved');match=self.request_match(s,book);actor=self.actor('floater')
        self.client.force_login(actor)
        url=reverse('submission-review',args=['hotfix',self.month.pk,s.pk])
        response=self.client.get(url)
        self.assertContains(response,'Reader Qualification Requests')
        response=self.client.post(url,{'action':'reader-qualification-review','request_version':service.version(s,actor),
            'domain':'tbr','match_id':match.pk,'decision':'confirmed'})
        self.assertEqual(response.status_code,302)
        match.refresh_from_db();self.assertTrue(match.is_qualifying)

    def test_foreign_target_and_review_team_scope_are_rejected(self):
        from django.contrib.auth import get_user_model
        from .models import PersonalTBR, ReadingGroup, ChallengeMonth
        foreign_group=ReadingGroup.objects.create(name='Foreign',slug='foreign')
        foreign_month=ChallengeMonth.objects.create(group=foreign_group,name='Foreign',status='active',starts_on=self.month.starts_on,ends_on=self.month.ends_on,botm_enabled=True)
        book=BotmBook.objects.create(month=foreign_month,position=1,title_snapshot='Death Valley',author_snapshot='Karina Halle',page_count_snapshot=384)
        s=self.submission(status='approved')
        with self.assertRaises(ValidationError):self.request_match(s,book,'botm')
        target=self.target();match=self.request_match(s,target)
        actor=self.actor('team_leader')
        # The fixture assigns the Reader to this Leader's Team; transfer away.
        TeamAssignment.objects.filter(participant=self.reader,month=self.month).update(ended_at=timezone.now())
        with self.assertRaises(PermissionDenied):service.review_request(submission=s,actor=actor,domain='tbr',match_id=match.pk,state=service.version(s,actor),decision='confirmed')
        match.refresh_from_db();self.assertFalse(match.is_qualifying)

    def test_new_completion_award_is_allowed_without_rewriting_existing_evidence(self):
        from .personal_tbr_matching import synchronize_submission
        book=self.target()
        for other in self.tbr.books.exclude(pk=book.pk):
            s=BookSubmission.objects.create(month=self.month,participant=self.reader,title=other.title_snapshot,author=other.author_snapshot,
                book_format='ebook',completed_on=date(2026,11,3),submitted_pages=100,approved_pages=100,status='approved')
            synchronize_submission(s)
        before=list(BookSubmission.objects.order_by('pk').values())
        self.assertFalse(PersonalTBRCompletionAward.objects.exists())
        s=self.submission(status='approved');match=self.request_match(s,book)
        confirm_suggestion(match=match,actor=self.actor('floater'),decision='confirmed')
        self.assertTrue(PersonalTBRCompletionAward.objects.get().is_qualifying)
        self.assertEqual(PersonalTBRCompletionAward.objects.get().bonus_amount_snapshot,100)
        self.assertEqual(list(BookSubmission.objects.exclude(pk=s.pk).order_by('pk').values()),before)
        s.refresh_from_db();self.assertEqual(s.approved_pages,384)

    def test_subtitle_request_remains_ambiguous_and_optional(self):
        book=self.target()
        # Change synthetic locked evidence only through a query setup.
        from .models import normalize_book_identity
        title='Death Valley: A Journey'
        PersonalTBRBook.objects.filter(pk=book.pk).update(title_snapshot=title,normalized_title=normalize_book_identity(title))
        book.refresh_from_db();s=self.submission(status='approved');s.author=book.author_snapshot;s.save()
        self.assertContains(self.client.get(self.url_for(s)),'Possible Match')
        self.assertFalse(PersonalTBRMatch.objects.exists())
        match=self.request_match(s,book)
        self.assertFalse(match.is_qualifying)

    def test_pending_edit_botm_request_can_be_changed_before_save(self):
        self.month.botm_enabled=True;self.month.save()
        first=BotmBook.objects.create(month=self.month,position=1,title_snapshot='Death Valley',author_snapshot='Karina Halle',page_count_snapshot=384)
        second=BotmBook.objects.create(month=self.month,position=2,title_snapshot='Death Valley',author_snapshot='Karina Hall',page_count_snapshot=384)
        s=self.submission();old=self.request_match(s,first,'botm')
        service.save_pending(submission=s,actor=self.reader.user,data=self.edit_data(s,botm_choice=selection_token(second,self.reader.user)))
        old.refresh_from_db();self.assertEqual(old.status,'rejected')
        self.assertIn('reader_withdrawal',old.decision_evidence)
        self.assertEqual(BotmMatch.objects.filter(submission=s,status='pending_review').count(),1)
        self.assertEqual(BotmMatch.objects.get(submission=s,status='pending_review').botm_book_id,second.pk)

    def test_authentication_and_csrf_remain_required(self):
        from django.test import Client
        s=self.submission();client=Client(enforce_csrf_checks=True)
        self.assertEqual(client.get(self.url_for(s)).status_code,302)
        client.force_login(self.reader.user)
        self.assertEqual(client.post(self.url_for(s,'reader-submission-edit'),self.edit_data(s)).status_code,403)
        self.assertEqual(client.post(self.url_for(s),{}).status_code,403)

    def test_acceptance_snapshot_is_historical_when_reader_corrects_pending_book(self):
        self.client.post(self.url,self.payload(title='Original Title',author='Original Author'))
        s=BookSubmission.objects.get()
        before={key:getattr(s,key) for key in ('participation_period_id','accepted_at','accepted_completed_on','acceptance_claims','reader_intent')}
        service.save_pending(submission=s,actor=self.reader.user,data=self.edit_data(s,title='Corrected Title',completed_on='2026-11-04'))
        s.refresh_from_db()
        self.assertEqual({key:getattr(s,key) for key in before},before)
        self.assertEqual(s.acceptance_claims['book']['title'],'Original Title')

    def test_concurrent_request_and_qualification_decision_invalidate_pending_form(self):
        book=self.target();s=self.submission();data=self.edit_data(s)
        initial=service.version(s,self.reader.user)
        match=self.request_match(s,book)
        with self.assertRaises(ValidationError):service.save_pending(submission=s,actor=self.reader.user,data=data)
        with self.assertRaises(ValidationError):service.request_qualification(submission=s,actor=self.reader.user,domain='tbr',token=selection_token(book,self.reader.user),state=initial)
        data=self.edit_data(s)
        confirm_suggestion(match=match,actor=self.actor('floater'),decision='rejected')
        with self.assertRaises(ValidationError):service.save_pending(submission=s,actor=self.reader.user,data=data)

    def test_outbox_and_audit_rollback_after_pending_edit_observation_failure(self):
        from . import discord_events
        from .discord_notifications import configure_notifications
        from .discord_scheduling import configure_report_schedule
        administrator=self.actor('platform')
        configure_notifications(group=self.month.group,actor=administrator,destination='moderator',enabled=True,
            webhook_url='https://discord.com/api/webhooks/123456789012345678/synthetic_reader_edit_test_token')
        form,_=configure_report_schedule(group=self.month.group,actor=administrator,destination='moderator',
            data={'immediate_controls':'1','immediate_enabled':'on','immediate_categories':['review_required']})
        self.assertTrue(form.is_valid(),form.errors)
        s=self.submission();theme=MonthTheme.objects.create(month=self.month,name='New pending Theme',starts_on=self.month.starts_on,ends_on=self.month.ends_on)
        before={model.__name__:list(model.objects.order_by('pk').values()) for model in (BookSubmission,ThemeClaim,AuditEvent,DiscordEventDelivery)}
        original=discord_events.record_book_changes
        def fail(*args,**kwargs):
            original(*args,**kwargs)
            self.assertEqual(DiscordEventDelivery.objects.count(),1)
            self.assertTrue(AuditEvent.objects.filter(action='submission.reader_edited').exists())
            raise RuntimeError('Synthetic observer failure')
        with patch('core.discord_notifications.send_webhook') as transport,patch.object(discord_events,'record_book_changes',side_effect=fail):
            with self.assertRaises(RuntimeError):service.save_pending(submission=s,actor=self.reader.user,data=self.edit_data(s,themes=[theme.pk]))
            transport.assert_not_called()
        after={model.__name__:list(model.objects.order_by('pk').values()) for model in (BookSubmission,ThemeClaim,AuditEvent,DiscordEventDelivery)}
        self.assertEqual(before,after)

    def test_pending_catalog_actions_reuse_provider_service_in_finalizing(self):
        from django.http import JsonResponse
        s=self.submission();type(self.month).objects.filter(pk=self.month.pk).update(status='finalizing')
        before=BookSubmission.objects.filter(pk=s.pk).values().get()
        with patch('core.views._submission_catalog_action',return_value=JsonResponse({'ok':True,'results':[]})) as catalog:
            response=self.client.post(self.url_for(s,'reader-submission-edit'),{'action':'search','query':'Death Valley'})
            self.assertTrue(response.json()['ok']);catalog.assert_called_once()
        self.assertEqual(BookSubmission.objects.filter(pk=s.pk).values().get(),before)
        BookSubmission.objects.filter(pk=s.pk).update(status='approved',approved_pages=384)
        with patch('core.views._submission_catalog_action') as catalog:
            self.assertEqual(self.client.post(self.url_for(s,'reader-submission-edit'),{'action':'search'}).status_code,403)
            catalog.assert_not_called()

    def test_existing_pending_theme_is_not_lost_when_theme_is_hidden(self):
        s=self.submission()
        theme=MonthTheme.objects.create(month=self.month,name='Original Theme',starts_on=self.month.starts_on,ends_on=self.month.ends_on,is_visible=False)
        claim=ThemeClaim.objects.create(submission=s,theme=theme,status='pending')
        form=service.PendingReaderForm(instance=s,month=self.month)
        self.assertIn(theme.pk,list(form.fields['themes'].queryset.values_list('pk',flat=True)))
        service.save_pending(submission=s,actor=self.reader.user,data=self.edit_data(s,title='Corrected title',themes=[theme.pk]))
        claim.refresh_from_db();self.assertEqual(claim.status,'pending')

    def test_pending_editor_ignores_unpermitted_start_date_notes_and_reference(self):
        s=self.submission();s.notes='Original note';s.reference_url='https://example.com/original';s.save()
        service.save_pending(submission=s,actor=self.reader.user,data=self.edit_data(s,notes='Changed',started_on='2026-11-01',reference_url='https://example.com/changed'))
        s.refresh_from_db();self.assertIsNone(s.started_on);self.assertEqual(s.notes,'Original note')
        self.assertEqual(s.reference_url,'https://example.com/original')
