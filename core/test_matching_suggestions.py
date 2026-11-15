"""Synthetic pre-submission suggestions; no transport or provider requests."""
import io
import json
from datetime import date
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from . import test_submission_hotfix as fixture
from .matching_intent import selection_token
from .matching_resolution import assess_entries
from .reader_book_selection import preview_selection, suggestion_reason
from .models import (BookSubmission, PersonalTBRBook, PersonalTBRMatch, ModifierProvenance,
                     PersonalTBRCompletionAward, CatalogBook, CatalogEdition)


class MatchingSuggestionTests(TestCase):
    setUp = fixture.SubmissionHotfixTests.setUp
    payload = fixture.SubmissionHotfixTests.payload

    def target(self, title='Death Valley', author='Karina Halle', position=1):
        type(self.tbr).objects.filter(pk=self.tbr.pk).update(confirmed_at=None)
        book = PersonalTBRBook.objects.get(personal_tbr_id=self.tbr.pk, position=position)
        book.catalog_book = book.catalog_edition = None
        book.title_snapshot, book.author_snapshot = title, author
        book.save()
        type(self.tbr).objects.filter(pk=self.tbr.pk).update(confirmed_at=self.tbr.confirmed_at)
        return book

    def draft(self, title='Death Valley', author='Karin Hale', **kwargs):
        return BookSubmission(month=self.month, participant=self.reader, title=title, author=author, **kwargs)

    def preview(self, **data):
        return preview_selection(data={'title':'Death Valley', 'author':'Karin Hale', **data},
            month=self.month, participant=self.reader, actor=self.reader.user)

    def test_author_typo_suggests_but_live_evaluator_never_auto_matches(self):
        book = self.target()
        self.assertEqual(assess_entries(self.draft(), [book], 'tbr'), [])
        feedback = self.preview()[0]
        self.assertEqual(feedback['state'], 'suggested')
        self.assertIn('author information differs', feedback['suggestions'][0]['message'])
        self.assertFalse(PersonalTBRMatch.objects.exists())
        self.assertFalse(ModifierProvenance.objects.exists())

    def test_optional_no_selection_does_not_create_fuzzy_credit(self):
        self.target()
        self.assertEqual(self.client.post(self.url, self.payload(title='Death Valley', author='Karin Hale')).status_code,302)
        submission = BookSubmission.objects.get()
        self.assertIsNone(submission.selected_tbr_book_id)
        self.assertFalse(PersonalTBRMatch.objects.exists())

    def test_native_request_change_and_remove_preserve_signed_evidence(self):
        book = self.target()
        token = selection_token(book, self.reader.user)
        check = self.client.post(self.url+'?qualification-preview=1', self.payload(
            title='Death Valley', author='Karin Hale', tbr_choice=token))
        self.assertEqual(check.status_code,200)
        self.assertFalse(BookSubmission.objects.exists())
        self.assertContains(check,'Qualification request awaiting review')
        self.assertContains(check,'selected')
        response = self.client.post(self.url, self.payload(title='Death Valley', author='Karin Hale', tbr_choice=token))
        self.assertEqual(response.status_code,302)
        submission = BookSubmission.objects.get()
        self.assertEqual(submission.reader_intent['tbr']['id'], book.pk)
        match = PersonalTBRMatch.objects.get()
        self.assertEqual(match.status,'pending_review')
        self.assertFalse(match.is_qualifying)
        self.assertFalse(ModifierProvenance.objects.exists())
        self.assertFalse(PersonalTBRCompletionAward.objects.exists())
        self.assertEqual(self.client.post(self.url,self.payload(tbr_choice='',selected_tbr=book.pk,tbr_token=token)).status_code,302)
        self.assertIsNone(BookSubmission.objects.latest('pk').selected_tbr_book_id)

    def test_native_selection_tampering_rejected(self):
        response = self.client.post(self.url,self.payload(tbr_choice='tampered'))
        self.assertTrue(response.context['intent_form'].errors)
        self.assertFalse(BookSubmission.objects.exists())

    def test_no_javascript_picker_and_check_are_functional(self):
        self.target()
        response = self.client.get(self.url)
        self.assertContains(response,'name="tbr_choice" form="submission-form"')
        self.assertContains(response,'data-selection-options open')
        check=self.client.post(self.url,self.payload(title='Death Valley',author='Karin Hale',action='qualification-preview'))
        self.assertContains(check,'Request Review')
        self.assertContains(check,'Dismiss / show possible')
        self.assertFalse(BookSubmission.objects.exists())

    def test_subtitle_variations_and_punctuation(self):
        book=self.target('Death Valley: A Journey','Karina Halle')
        self.assertIn('subtitle',suggestion_reason(self.draft(author='Karina Halle'),book))
        book=self.target('Death—Valley','Karina Halle')
        self.assertEqual(assess_entries(self.draft(author='Karina Halle'),[book],'tbr')[0][1],'exact')

    def test_unrelated_author_or_title_never_suggested(self):
        book=self.target()
        self.assertEqual(suggestion_reason(self.draft(author='Other Writer'),book),'')
        self.assertEqual(suggestion_reason(self.draft(title='Death Sea'),book),'')

    def test_conflicting_work_volume_adaptation_excluded(self):
        book=self.target()
        left=CatalogBook.objects.create(provider='hardcover',provider_book_id='left',title='Death Valley',author='Karin Hale')
        right=CatalogBook.objects.create(provider='hardcover',provider_book_id='right',title='Death Valley',author='Karina Halle')
        book.catalog_book=right
        self.assertEqual(suggestion_reason(self.draft(catalog_book=left),book),'')
        for title,other in [('Death Valley Volume 1','Death Valley Volume 2'),('Death Valley','Death Valley Manga'),('Death Valley','Death Valley Abridged')]:
            book=self.target(other)
            self.assertEqual(suggestion_reason(self.draft(title=title),book),'')

    def test_multiple_candidates_bounded_and_deterministic(self):
        self.target()
        for position in (2,3):
            self.target(author='Karina Halle' + 'e' * (position-1), position=position)
        self.assertEqual(len(self.preview()[0]['suggestions']),3)
        self.target(author='Karina Hall', position=4)
        self.assertEqual(self.preview()[0]['suggestions'],[])

    def test_qualified_pending_and_rejected_entries_not_suggested(self):
        book=self.target()
        submission=self.draft(completed_on=date(2026,11,3),submitted_pages=100,status='approved',approved_pages=100)
        submission.save()
        for status,qualified in [('confirmed',True),('pending_review',False),('rejected',False)]:
            PersonalTBRMatch.objects.all().delete()
            PersonalTBRMatch.objects.create(personal_tbr_book=book,month=self.month,participant=self.reader,submission=submission,status=status,is_qualifying=qualified, method='suggested', tbr_title_snapshot=book.title_snapshot, tbr_author_snapshot=book.author_snapshot, submission_title_snapshot=submission.title, submission_author_snapshot=submission.author)
            self.assertEqual(self.preview()[0]['suggestions'],[])

    def test_unconfirmed_and_disabled_have_no_suggestions(self):
        self.target()
        self.month.tbr_enabled=False;self.month.save()
        self.assertEqual(self.preview(),[])
        self.month.tbr_enabled=True;self.month.save()
        type(self.tbr).objects.filter(pk=self.tbr.pk).update(confirmed_at=None)
        self.assertEqual(self.preview(),[])

    def test_diagnostic_matches_live_evidence_and_is_select_only(self):
        book=self.target('Death Valley Journey: A Memoir','Karina Halle')
        submission=self.draft(title='Death Valley Journey',author='Karina Halle',completed_on=date(2026,11,3),submitted_pages=100,status='approved',approved_pages=100)
        submission.save()
        expected=assess_entries(submission,[book],'tbr')
        output=io.StringIO()
        with CaptureQueriesContext(connection) as queries:
            call_command('report_missing_personal_tbr_matches',group='hotfix',month=self.month.pk,stdout=output)
        self.assertTrue(all(query['sql'].lstrip().upper().startswith('SELECT') for query in queries))
        row=json.loads(output.getvalue())['submissions'][0]
        from .tbr_reconciliation import _candidate_ids
        self.assertEqual(_candidate_ids(self.month), [])  # Historical repair candidates remain unchanged.
        self.assertEqual(row['method'],expected[0][1])
        self.assertEqual(row['assessment'],'ambiguous_requires_review')
        self.assertEqual(row['potential_new_book_bonus_pages'],0)
        self.assertFalse(PersonalTBRMatch.objects.exists())
        type(self.month).objects.filter(pk=self.month.pk).update(status='completed')
        output=io.StringIO();call_command('report_missing_personal_tbr_matches',group='hotfix',month=self.month.pk,stdout=output)
        self.assertEqual(json.loads(output.getvalue())['submissions'][0]['assessment'],'ineligible')

    def test_requested_typo_requires_explicit_authorized_qualification_review(self):
        from .test_submission_editing import SubmissionEditingTests
        from .matching_resolution import review_submission_with_matches, confirm_suggestion
        from django.core.exceptions import ValidationError
        from django.utils import timezone
        book=self.target()
        self.client.post(self.url,self.payload(title='Death Valley',author='Karin Hale',tbr_choice=selection_token(book,self.reader.user)))
        submission=BookSubmission.objects.get()
        match=PersonalTBRMatch.objects.get()
        with self.assertRaises(ValidationError):
            confirm_suggestion(match=match,actor=self.reader.user,decision='confirmed')
        actor=SubmissionEditingTests.actor(self,'floater')
        submission.status='approved';submission.approved_pages=384
        review_submission_with_matches(submission,[],actor,timezone.now())
        match.refresh_from_db()
        self.assertFalse(match.is_qualifying)
        confirm_suggestion(match=match,actor=actor,decision='confirmed')
        match.refresh_from_db();self.assertTrue(match.is_qualifying)
        from .personal_tbr_matching import synchronize_submission
        synchronize_submission(submission);synchronize_submission(submission)
        self.assertEqual(PersonalTBRMatch.objects.filter(is_qualifying=True).count(),1)
        self.assertEqual(ModifierProvenance.objects.filter(source_type='tbr_book').count(),1)

    def test_other_reader_and_group_are_never_suggestion_sources(self):
        from django.contrib.auth import get_user_model
        from .models import Membership, ReadingGroup
        self.target()
        other=get_user_model().objects.create_user('foreign-reader')
        member=Membership.objects.create(user=other,group=self.month.group,display_name='Foreign')
        self.assertEqual(preview_selection(data={'title':'Death Valley','author':'Karin Hale'},month=self.month,participant=member,actor=other),[])
        foreign_group=ReadingGroup.objects.create(name='Foreign',slug='foreign')
        member.group=foreign_group
        self.assertEqual(preview_selection(data={'title':'Death Valley','author':'Karin Hale'},month=self.month,participant=member,actor=other),[])
