from copy import copy
from datetime import date

from django.test import TestCase

from . import test_submission_hotfix as fixture
from .forms import BookSubmissionForm, SubmissionCorrectionForm
from .models import BookSubmission, MonthTheme, ThemeClaim, ModifierProvenance
from .scoring import refresh_submission_score


class ThemeEligibilityTests(TestCase):
    setUp = fixture.SubmissionHotfixTests.setUp
    payload = fixture.SubmissionHotfixTests.payload

    def theme(self):
        return MonthTheme.objects.create(month=self.month, name='First week',
            starts_on=date(2026, 11, 1), ends_on=date(2026, 11, 7), bonus_pages=25)

    def test_visible_windows_and_disabled_options_without_javascript(self):
        theme = self.theme()
        for completed, disabled in [('2026-11-01', False), ('2026-11-07', False), ('2026-11-08', True), ('', True)]:
            form = BookSubmissionForm(self.payload(completed_on=completed), month=self.month)
            html = str(form['themes'])
            self.assertIn('First week', html)
            self.assertIn('Nov 07, 2026', html)
            self.assertIn('data-theme-start="2026-11-01"', html)
            self.assertEqual('disabled' in html, disabled)

    def test_forged_ineligible_selection_is_rejected_server_side(self):
        theme = self.theme()
        form = BookSubmissionForm(self.payload(completed_on='2026-11-08', themes=[theme.pk]), month=self.month)
        self.assertFalse(form.is_valid())
        self.assertIn('themes', form.errors)
        valid = BookSubmissionForm(self.payload(completed_on='2026-11-07', themes=[theme.pk]), month=self.month)
        self.assertTrue(valid.is_valid(), valid.errors)

    def test_correction_date_change_requires_explicit_claim_removal(self):
        theme = self.theme()
        item = BookSubmission.objects.create(month=self.month,participant=self.reader,title='Original',author='Author',
            book_format='ebook',completed_on=date(2026,11,3),submitted_pages=100,approved_pages=100,status='approved')
        claim = ThemeClaim.objects.create(submission=item,theme=theme,status='approved',approved_bonus_pages=25)
        refresh_submission_score(item)
        before = list(ModifierProvenance.objects.values())
        data = dict(version='not-used-for-form-validation', title=item.title, author=item.author,
            completed_on='2026-11-08', submitted_pages=100, themes=[])
        form = SubmissionCorrectionForm(data, instance=copy(item), month=self.month)
        self.assertFalse(form.is_valid())
        self.assertIn('confirm_theme_removal', form.errors)
        claim.refresh_from_db()
        self.assertEqual(claim.status, 'approved')
        self.assertEqual(before, list(ModifierProvenance.objects.values()))
        data['confirm_theme_removal'] = 'on'
        form = SubmissionCorrectionForm(data, instance=copy(item), month=self.month)
        self.assertTrue(form.is_valid(), form.errors)
        data['themes'] = [theme.pk]
        form = SubmissionCorrectionForm(data, instance=copy(item), month=self.month)
        self.assertFalse(form.is_valid())
        self.assertIn('themes', form.errors)
