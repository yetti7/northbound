from unittest.mock import patch

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.template.loader import render_to_string
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from . import test_personal_tbr_completion as completion_fixture
from . import test_staff_challenge_tbr as staff_fixture
from .botm_configuration import add_botm_book, update_botm_book
from .botm_matching import synchronize_submission as synchronize_botm
from .models import (
    PersonalTBRBook, BotmBook, MonthTheme, ThemeClaim, ProgressCheckpoint,
    Team, TeamAssignment,
)
from .personal_tbr import replace_draft_personal_tbr
from .personal_tbr_matching import synchronize_submission, select_candidates
from .progress_checkpoints import process_due_progress_checkpoints
from .score_aggregation import challenge_score_totals
from .scoring import refresh_submission_score

DISPLAY = {'display_title', 'display_author', 'display_cover_url', 'display_page_count', 'display_refreshed_at'}


def evidence_state(model_apps=apps):
    return {model._meta.label: list(model.objects.order_by('pk').values(
        *[field.attname for field in model._meta.local_fields if field.name not in DISPLAY]
    )) for model in model_apps.get_app_config('core').get_models()}


class DisplayPropertiesTests(TestCase):
    def test_fallback_is_none_only_for_both_models(self):
        for model in (PersonalTBRBook, BotmBook):
            book = model(title_snapshot='Original', author_snapshot='Writer',
                         cover_url_snapshot='https://example.com/original.jpg', page_count_snapshot=384)
            for suffix, expected in [('title', 'Original'), ('author', 'Writer'),
                                     ('cover_url', 'https://example.com/original.jpg'), ('page_count', 384)]:
                self.assertEqual(getattr(book, 'effective_display_' + suffix), expected)
                setattr(book, 'display_' + suffix, 0 if suffix == 'page_count' else '')
                self.assertEqual(getattr(book, 'effective_display_' + suffix), 0 if suffix == 'page_count' else '')
            # Zero demonstrates accessor semantics, not a valid persisted page value.
            with self.assertRaises(ValidationError):
                book._meta.get_field('display_page_count').clean(0, book)


class SelectionDisplayTests(TestCase):
    setUp = staff_fixture.StaffChallengeTbrTests.setUp
    books = staff_fixture.StaffChallengeTbrTests.books
    save = staff_fixture.StaffChallengeTbrTests.save
    payload = staff_fixture.StaffChallengeTbrTests.payload

    def test_new_selections_initialize_and_staff_save_keeps_evidence_identity(self):
        values = self.books(1)
        values[0].update(page_count_snapshot=384, cover_url_snapshot='https://example.com/original.jpg')
        tbr = self.save(values)
        book = tbr.books.get()
        self.assertEqual((book.display_title, book.display_author, book.display_page_count), ('Book 1', 'Author', 384))
        self.assertEqual(book.display_cover_url, values[0]['cover_url_snapshot'])
        self.assertIsNone(book.display_refreshed_at)
        PersonalTBRBook.objects.filter(pk=book.pk).update(display_title='Display title', display_cover_url='https://example.com/display.jpg')
        # Model evidence guards remain in force; there is no refresh write API.
        book.refresh_from_db()
        response = self.client.get(self.url)
        self.assertContains(response, 'src="https://example.com/display.jpg"')
        self.assertEqual(response.context['tbr_formset'].initial[0]['title_snapshot'], 'Book 1')
        self.save(values)
        self.assertEqual(tbr.books.get().pk, book.pk)
        self.assertEqual(tbr.books.get().display_title, 'Display title')

    def test_unchanged_draft_save_retains_display_and_primary_key(self):
        values = self.books(1)
        tbr = replace_draft_personal_tbr(enrollment=self.enrollment, books=values)
        book = tbr.books.get()
        PersonalTBRBook.objects.filter(pk=book.pk).update(display_title='Display only')
        replace_draft_personal_tbr(enrollment=self.enrollment, books=values)
        retained = tbr.books.get()
        self.assertEqual((retained.pk, retained.display_title), (book.pk, 'Display only'))

    def test_botm_configuration_preserves_display_on_unrelated_save(self):
        from .models import ChallengeStaffAssignment
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.staff, role='host')
        values = dict(position=1, title_snapshot='BOTM', author_snapshot='Author', page_count_snapshot=200)
        book = add_botm_book(month=self.month, actor=self.staff_user, values=values)
        self.assertEqual((book.display_title, book.display_page_count), ('BOTM', 200))
        BotmBook.objects.filter(pk=book.pk).update(display_title='Display BOTM')
        updated = update_botm_book(month=self.month, book=book, actor=self.staff_user, values=values)
        self.assertEqual((updated.pk, updated.display_title), (book.pk, 'Display BOTM'))
        updated = update_botm_book(month=self.month, book=book, actor=self.staff_user,
                                   values=dict(values, page_count_snapshot=250))
        self.assertEqual(updated.display_page_count, 250)
        self.assertEqual(updated.display_title, 'Display BOTM')

    def test_reader_cards_use_display_without_changing_evidence_views(self):
        tbr = self.save(self.books(1), complete=False)
        tbr.confirmed_at = timezone.now(); tbr.save(update_fields=['confirmed_at'])
        book = tbr.books.get()
        PersonalTBRBook.objects.filter(pk=book.pk).update(display_title='Visible title', display_author='Visible author',
            display_cover_url='malformed', display_page_count=123)
        self.client.force_login(self.reader_user)
        from django.urls import reverse
        response = self.client.get(reverse('personal-tbr-detail', args=[self.group.slug, self.month.pk]))
        self.assertContains(response, 'Visible title')
        self.assertContains(response, 'Visible author')
        self.assertContains(response, '123 pages')
        self.assertContains(response, 'data-book-cover-image hidden')
        self.assertNotContains(response, 'src="malformed"')
        book.refresh_from_db()
        self.assertEqual(book.title_snapshot, 'Book 1')


class HistoricalDisplayTests(TestCase):
    setUp = completion_fixture.PersonalTbrCompletionRewardTests.setUp
    locked_tbr = completion_fixture.PersonalTbrCompletionRewardTests.locked_tbr
    submit_for = completion_fixture.PersonalTbrCompletionRewardTests.submit_for
    qualify = completion_fixture.PersonalTbrCompletionRewardTests.qualify

    def representative_history(self):
        tbr, books, submissions = self.qualify()
        self.month.botm_enabled = True
        self.month.botm_completion_bonus_pages = 100
        self.month.save()
        botm = BotmBook.objects.create(month=self.month, position=1, title_snapshot=books[0].title_snapshot,
            author_snapshot=books[0].author_snapshot, page_count_snapshot=100, bonus_pages=25)
        synchronize_botm(submissions[0])
        theme = MonthTheme.objects.create(month=self.month, name='Theme', starts_on=self.month.starts_on,
            ends_on=self.month.ends_on, bonus_rule='legacy_fixed', bonus_pages=20)
        ThemeClaim.objects.create(submission=submissions[0], theme=theme, status='approved', approved_bonus_pages=20)
        refresh_submission_score(submissions[0])
        team = Team.objects.create(month=self.month, name='Team')
        TeamAssignment.objects.create(month=self.month, team=team, participant=self.reader)
        checkpoint = ProgressCheckpoint.objects.create(month=self.month, scheduled_at=timezone.now(),
            target_basis='fixed', fixed_target_pages=100, progress_basis='total')
        process_due_progress_checkpoints(now=timezone.now())
        return tbr, books, submissions, botm, team

    def test_display_updates_preserve_all_evidence_scores_and_locks(self):
        tbr, books, submissions, botm, team = self.representative_history()
        before = evidence_state()
        totals = challenge_score_totals(month=self.month)
        team_total = team.approved_pages
        for model in (PersonalTBRBook, BotmBook):
            model.objects.update(display_title='Different work', display_author='Different author',
                display_page_count=9999, display_cover_url='https://example.com/display.jpg')
        self.assertEqual(evidence_state(), before)
        self.assertEqual(challenge_score_totals(month=self.month), totals)
        self.assertEqual(team.approved_pages, team_total)
        book = PersonalTBRBook.objects.get(pk=books[0].pk)
        method, candidates = select_candidates(submission_catalog_book_id=None,
            normalized_title=book.normalized_title, normalized_author=book.normalized_author, books=[book])
        self.assertEqual([candidate.pk for candidate in candidates], [book.pk])
        _, wrong = select_candidates(submission_catalog_book_id=None,
            normalized_title='different work', normalized_author='different author', books=[book])
        self.assertEqual(wrong, [])
        with self.assertRaises(ValidationError):
            book.title_snapshot = 'Forbidden evidence change'; book.save()
        botm.title_snapshot = 'Forbidden evidence change'
        with self.assertRaises(ValidationError):
            botm.save()
        for submission in submissions:
            synchronize_submission(submission)
            synchronize_botm(submission)
        self.assertEqual(challenge_score_totals(month=self.month), totals)
        self.assertEqual(team.approved_pages, team_total)


class DisplayBackfillMigrationTests(TransactionTestCase):
    locked_tbr = HistoricalDisplayTests.locked_tbr
    submit_for = HistoricalDisplayTests.submit_for
    qualify = HistoricalDisplayTests.qualify
    representative_history = HistoricalDisplayTests.representative_history

    def test_additive_sqlite_backfill_preserves_representative_history(self):
        completion_fixture.PersonalTbrCompletionRewardTests.setUp(self)
        tbr, books, submissions, botm, team = self.representative_history()
        executor = MigrationExecutor(connection)
        historical = executor.loader.project_state([('core', '0060_theme_percentage_awards')]).apps
        before = evidence_state(historical)
        try:
            executor.migrate([('core', '0060_theme_percentage_awards')])
            from django.core.management import call_command
            from io import StringIO
            output = StringIO()
            call_command('sqlmigrate', 'core', '0061', stdout=output)
            sql = output.getvalue().upper()
            self.assertEqual(sql.count(' ADD COLUMN '), 10)
            self.assertNotIn('DROP TABLE', sql)
            self.assertNotIn('CREATE TABLE', sql)
            def forbid_selection_save(sender, **kwargs):
                if sender._meta.label_lower in ('core.personaltbrbook', 'core.botmbook'):
                    raise AssertionError('Backfill must not save selections')
            from django.db.models.signals import pre_save
            pre_save.connect(forbid_selection_save, weak=False)
            try:
                with patch('core.integrations.hardcover.execute_graphql', side_effect=AssertionError('No API calls')):
                    executor = MigrationExecutor(connection)
                    executor.migrate([('core', '0062_backfill_selection_display_metadata')])
            finally:
                pre_save.disconnect(forbid_selection_save)
            migrated = executor.loader.project_state([('core', '0062_backfill_selection_display_metadata')]).apps
            self.assertEqual(evidence_state(migrated), before)
            for model in (PersonalTBRBook, BotmBook):
                for book in model.objects.all():
                    self.assertEqual(book.display_title, book.title_snapshot)
                    self.assertEqual(book.display_author, book.author_snapshot)
                    self.assertEqual(book.display_cover_url, book.cover_url_snapshot)
                    self.assertEqual(book.display_page_count, book.page_count_snapshot)
                    self.assertIsNone(book.display_refreshed_at)
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
