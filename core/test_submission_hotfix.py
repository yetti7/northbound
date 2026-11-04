import io
import json
import os
import subprocess
import sys
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import (
    AuditEvent, BookSubmission, CatalogBook, CatalogEdition, ChallengeMonth,
    Membership, ModifierProvenance, MonthEnrollment, PersonalTBR, PersonalTBRBook,
    PersonalTBRCompletionAward, PersonalTBRMatch, ReadingGroup,
)
from .personal_tbr_matching import synchronize_submission
from .personal_tbr_presentation import build_personal_tbr_reader_presentation


class SubmissionHotfixTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user('hotfix-reader')
        group = ReadingGroup.objects.create(name='Hotfix', slug='hotfix')
        self.reader = Membership.objects.create(group=group, user=user, display_name='Reader')
        self.month = ChallengeMonth.objects.create(
            group=group, name='November', status='active', starts_on=date(2026, 11, 1),
            ends_on=date(2026, 11, 30), tbr_enabled=True,
            tbr_book_bonus_pages=20, tbr_completion_bonus_pages=100,
        )
        enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.reader)
        self.tbr = PersonalTBR.objects.create(enrollment=enrollment)
        self.book = CatalogBook.objects.create(provider='hardcover', provider_book_id='hotfix', title='Not in Love', author='Ali Hazelwood')
        self.edition = CatalogEdition.objects.create(provider='hardcover', provider_edition_id='hotfix-edition', book=self.book, page_count=384)
        for position in range(1, 10):
            PersonalTBRBook.objects.create(
                personal_tbr=self.tbr, position=position,
                title_snapshot=self.book.title if position == 1 else f'Other {position}',
                author_snapshot=self.book.author,
                catalog_book=self.book if position == 1 else None,
                catalog_edition=self.edition if position == 1 else None,
            )
        self.tbr.confirmed_at = timezone.now()
        self.tbr.save(update_fields=['confirmed_at'])
        self.url = reverse('submission-create', args=['hotfix', self.month.pk])
        self.client.force_login(user)

    def payload(self, **overrides):
        values = dict(title=self.book.title, author=self.book.author, book_format='ebook',
                      completed_on='2026-11-03', submitted_pages=384)
        values.update(overrides)
        return values

    def token(self):
        return signing.dumps(dict(selected=self.edition.pk, scoring=self.edition.pk, method='hardcover'), salt='northbound.catalog-selection')

    def test_out_of_range_dates_return_form_errors_without_writes(self):
        for completed in ('2026-10-31', '2026-12-01'):
            with self.subTest(completed=completed):
                response = self.client.post(self.url, self.payload(completed_on=completed))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'Completion date must fall inside the challenge month.')
        self.assertFalse(BookSubmission.objects.exists())
        self.assertFalse(AuditEvent.objects.filter(action='submission.created').exists())

    def test_hardcover_date_errors_do_not_autoapprove_or_write(self):
        for completed in ('2026-10-31', ''):
            response = self.client.post(self.url, self.payload(
                completed_on=completed, catalog_selection=self.token(),
            ))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['form'].errors)
        self.assertFalse(BookSubmission.objects.exists())
        self.assertFalse(PersonalTBRMatch.objects.exists())

    def test_missing_and_invalid_completion_dates_are_field_errors(self):
        for completed in ('', 'not-a-date'):
            response = self.client.post(self.url, self.payload(completed_on=completed))
            self.assertEqual(response.status_code, 200)
            self.assertIn('completed_on', response.context['form'].errors)
        self.assertFalse(BookSubmission.objects.exists())

    def test_valid_boundary_dates_preserve_pending_approval_behavior(self):
        for completed in ('2026-11-01', '2026-11-30'):
            response = self.client.post(self.url, self.payload(completed_on=completed))
            self.assertEqual(response.status_code, 302)
        self.assertEqual(BookSubmission.objects.filter(status='pending', approved_pages=None).count(), 2)

    def test_missing_challenge_schedule_is_useful_form_error(self):
        ChallengeMonth.objects.filter(pk=self.month.pk).update(starts_on=None)
        response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'The Challenge schedule must be configured')
        self.assertFalse(BookSubmission.objects.exists())

    def test_final_validation_error_is_rendered_without_writes(self):
        from .models import BookSubmission as Model
        original = Model.full_clean
        def validate(instance, *args, **kwargs):
            if instance.participant_id:
                raise ValidationError('Final submission validation failed.')
            return original(instance, *args, **kwargs)
        with patch.object(Model, 'full_clean', validate):
            response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Final submission validation failed.')
        self.assertFalse(BookSubmission.objects.exists())

    def test_hardcover_autoapproval_completes_one_of_nine_and_is_idempotent(self):
        response = self.client.post(self.url, self.payload(catalog_selection=self.token()))
        self.assertEqual(response.status_code, 302)
        submission = BookSubmission.objects.get()
        self.assertEqual((submission.status, submission.approved_pages), ('approved', 384))
        self.assertEqual(build_personal_tbr_reader_presentation(personal_tbr=self.tbr)['completed_count'], 1)
        match = PersonalTBRMatch.objects.get()
        self.assertTrue(match.is_qualifying)
        provenance = ModifierProvenance.objects.get(source_type=ModifierProvenance.SourceType.TBR_BOOK)
        frozen = (provenance.pk, provenance.amount, provenance.effective_date)
        synchronize_submission(submission)
        synchronize_submission(submission)
        provenance.refresh_from_db()
        self.assertEqual((provenance.pk, provenance.amount, provenance.effective_date), frozen)
        self.assertEqual(PersonalTBRMatch.objects.count(), 1)
        self.assertEqual(ModifierProvenance.objects.count(), 1)
        self.assertFalse(PersonalTBRCompletionAward.objects.exists())
        submission.refresh_from_db()
        self.assertEqual((submission.approved_pages, submission.final_scored_pages), (384, 404))

    def test_pending_submission_grants_no_tbr_credit(self):
        self.assertEqual(self.client.post(self.url, self.payload()).status_code, 302)
        synchronize_submission(BookSubmission.objects.get())
        self.assertFalse(PersonalTBRMatch.objects.exists())
        self.assertFalse(ModifierProvenance.objects.exists())
        self.assertEqual(build_personal_tbr_reader_presentation(personal_tbr=self.tbr)['completed_count'], 0)

    def test_synchronization_failure_rolls_back_entire_submission(self):
        with patch('core.views.synchronize_personal_tbr_submission', side_effect=RuntimeError('test failure')):
            with self.assertRaises(RuntimeError):
                self.client.post(self.url, self.payload(catalog_selection=self.token()))
        self.assertFalse(BookSubmission.objects.exists())
        self.assertFalse(PersonalTBRMatch.objects.exists())
        self.assertFalse(ModifierProvenance.objects.exists())
        self.assertFalse(AuditEvent.objects.filter(action='submission.created').exists())

    def test_report_is_read_only_and_projects_duplicate_and_completion_impacts(self):
        # Model imports bypass the corrected endpoint to represent historical data.
        for book in self.tbr.books.order_by('position'):
            BookSubmission.objects.create(month=self.month, participant=self.reader,
                title=book.title_snapshot, author=book.author_snapshot, book_format='ebook',
                completed_on=date(2026, 11, 3), submitted_pages=384, approved_pages=384, status='approved')
        BookSubmission.objects.create(month=self.month, participant=self.reader,
            title=self.book.title, author=self.book.author, book_format='ebook',
            completed_on=date(2026, 11, 3), submitted_pages=384, approved_pages=384, status='approved')
        BookSubmission.objects.create(month=self.month, participant=self.reader,
            title=self.book.title, author=self.book.author, book_format='ebook',
            completed_on=date(2026, 11, 3), submitted_pages=384, status='pending')
        output = io.StringIO()
        with CaptureQueriesContext(connection) as queries:
            call_command('report_missing_personal_tbr_matches', group='hotfix', month=self.month.pk, stdout=output)
        self.assertTrue(all(query['sql'].lstrip().upper().startswith('SELECT') for query in queries))
        report = json.loads(output.getvalue())
        self.assertTrue(report['read_only'])
        self.assertEqual(len(report['submissions']), 10)
        self.assertEqual(sum(row['potential_new_book_bonus_pages'] for row in report['submissions']), 180)
        reader = report['readers'][0]
        self.assertEqual((reader['current_completed'], reader['projected_completed']), (0, 9))
        self.assertEqual(reader['potential_completion_bonus_pages'], 100)
        self.assertFalse(PersonalTBRMatch.objects.exists())
        self.assertFalse(ModifierProvenance.objects.exists())
        self.assertFalse(PersonalTBRCompletionAward.objects.exists())


class ProductionExceptionLoggingTests(SimpleTestCase):
    def test_debug_false_logs_safe_exception_stack_to_stderr(self):
        script = '''
import logging, os
os.environ['DJANGO_SETTINGS_MODULE'] = 'reading_challenge.settings'
import django
django.setup()
from django.conf import settings
assert settings.DEBUG is False
assert settings.LOGGING['loggers']['django.request']['propagate'] is False
try:
    raise ValueError('SECRET_EXCEPTION_VALUE')
except ValueError:
    logging.getLogger('django.request').error('SECRET_MESSAGE %s', 'SECRET_ARGUMENT', exc_info=True,
        extra={'request': 'SECRET_REQUEST'}, stack_info=True)
'''
        environment = dict(os.environ, DJANGO_DEBUG='0', DJANGO_SECRET_KEY='hotfix-test-secret', NORTHBOUND_TOKEN_ENCRYPTION_KEY='')
        result = subprocess.run([sys.executable, '-c', script], env=environment, capture_output=True, text=True, check=True)
        self.assertIn('Exception type: ValueError', result.stderr)
        self.assertIn('in <module>', result.stderr)
        self.assertNotIn('SECRET_', result.stderr)
        self.assertEqual(result.stdout, '')
