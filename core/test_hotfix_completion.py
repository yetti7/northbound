import io
import json
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import connection
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .forms import BookSubmissionForm
from .models import (
    AuditEvent, BookSubmission, ChallengeStaffAssignment, Membership, ModifierProvenance,
    PersonalTBRMatch, PersonalTBRCompletionAward, ProgressCheckpoint, ProgressCheckpointResult,
)
from . import test_submission_hotfix as hotfix_fixture
from .tbr_reconciliation import apply, preview


class DateGuidanceTests(TestCase):
    setUp = hotfix_fixture.SubmissionHotfixTests.setUp
    payload = hotfix_fixture.SubmissionHotfixTests.payload

    def test_actual_range_and_inclusive_limits(self):
        form = BookSubmissionForm(month=self.month)
        self.assertEqual(form.fields['completed_on'].widget.attrs['min'], '2026-11-01')
        self.assertEqual(form.fields['completed_on'].widget.attrs['max'], '2026-11-30')
        self.assertIn('November 01, 2026', form.fields['completed_on'].help_text)
        self.assertIn('inclusive', form.fields['completed_on'].help_text)
        for date in ('2026-11-01', '2026-11-30'):
            self.assertTrue(BookSubmissionForm(self.payload(completed_on=date), month=self.month).is_valid())
        response = self.client.post(self.url, self.payload(completed_on='2026-12-01'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('completed_on', response.context['form'].errors)
        self.assertContains(response, 'min="2026-11-01"')
        self.assertContains(response, 'max="2026-11-30"')

    def test_missing_schedule_does_not_invent_dates(self):
        self.month.starts_on = self.month.ends_on = None
        form = BookSubmissionForm(month=self.month)
        self.assertNotIn('min', form.fields['completed_on'].widget.attrs)
        self.assertNotIn('max', form.fields['completed_on'].widget.attrs)
        self.assertIn('schedule must be configured', form.fields['completed_on'].help_text)


class BookCoverTests(SimpleTestCase):
    def render(self, url, state=''):
        return render_to_string('core/includes/book_cover.html', dict(image_url=url, title='Book', state=state))

    def test_missing_and_malformed_urls_use_placeholder(self):
        for url in ('', None, 'bad', 'javascript:alert(1)', 'data:image/png;base64,x'):
            html = self.render(url)
            self.assertIn('data-book-cover-image hidden', html)
            self.assertIn('data-book-cover-placeholder aria-hidden', html)
            self.assertNotIn('src="bad"', html)
            self.assertNotIn('javascript:', html)

    def test_valid_cover_preserves_completed_and_pending_overlays(self):
        completed = self.render('https://example.com/cover.jpg', 'completed')
        self.assertIn('src="https://example.com/cover.jpg"', completed)
        self.assertIn('data-book-cover-placeholder hidden', completed)
        self.assertIn('book-completion-overlay', completed)
        self.assertIn('completed-stamp.png', completed)
        pending = self.render('', 'pending')
        self.assertIn('is-pending', pending)
        self.assertIn('book-pending-label', pending)


class TbrReconciliationTests(TransactionTestCase):
    setUp = hotfix_fixture.SubmissionHotfixTests.setUp

    def admin(self, role='owner'):
        user = get_user_model().objects.create_user('repair-admin')
        member = Membership.objects.create(group=self.month.group, user=user, display_name='Admin', role=role if role == 'owner' else 'member')
        if role in ('host', 'floater', 'team_leader'):
            kwargs = dict(month=self.month, membership=member, role=role)
            if role == 'team_leader':
                from .models import Team
                kwargs['team'] = Team.objects.create(month=self.month, name='Team')
            ChallengeStaffAssignment.objects.create(**kwargs)
        return user

    def historical(self, all_books=False):
        books = list(self.tbr.books.order_by('position')) if all_books else [self.tbr.books.order_by('position').first()]
        for book in books:
            BookSubmission.objects.create(month=self.month, participant=self.reader,
                title=book.title_snapshot, author=book.author_snapshot, book_format='ebook',
                completed_on=self.month.starts_on, submitted_pages=384, approved_pages=384, status='approved')

    def confirm(self):
        return f'RECONCILE {self.month.group.slug} {self.month.pk}'

    def test_exact_private_copy_preview_apply_and_replay_protection(self):
        actor = self.admin()
        self.historical(all_books=True)
        # A second occurrence must not award the first TBR book twice.
        first = BookSubmission.objects.first()
        first.pk = None
        first.save()
        checkpoint = ProgressCheckpoint.objects.create(month=self.month, scheduled_at=timezone.now(),
            fixed_target_pages=100, target_basis='fixed')
        result = ProgressCheckpointResult.objects.create(checkpoint=checkpoint, participant=self.reader,
            evaluated_at=timezone.now(), threshold_percentage=25, progress_basis='base',
            target_basis='fixed', target_pages=100, required_pages=25, progress_pages=1, outcome='below')
        with CaptureQueriesContext(connection) as queries:
            document = preview(self.month, actor)
        self.assertTrue(all(query['sql'].lstrip().upper().startswith('SELECT') for query in queries))
        self.assertTrue(document['exact_scoring_preview'])
        self.assertFalse(PersonalTBRMatch.objects.exists())
        self.assertFalse(ModifierProvenance.objects.exists())
        new_sources = document['plan']['effects']['ModifierProvenance']
        self.assertEqual(sum(item['after']['amount'] for item in new_sources.values()), 280)
        effects = apply(self.month, actor, document, self.confirm())
        self.assertEqual(len(effects['PersonalTBRMatch']), 10)
        self.assertEqual(PersonalTBRMatch.objects.filter(is_qualifying=True).count(), 9)
        self.assertEqual(ModifierProvenance.objects.count(), 10)
        self.assertEqual(PersonalTBRCompletionAward.objects.count(), 1)
        for source in ModifierProvenance.objects.filter(submission__isnull=False):
            self.assertEqual(source.effective_date, self.month.starts_on)
        self.assertEqual(PersonalTBRCompletionAward.objects.get().effective_date, timezone.localdate())
        result.refresh_from_db()
        self.assertEqual(result.progress_pages, 1)
        self.assertEqual(AuditEvent.objects.filter(action='personal_tbr.reconciled').count(), 1)
        with self.assertRaisesMessage(ValidationError, 'stale or already applied'):
            apply(self.month, actor, document, self.confirm())
        self.assertEqual(ModifierProvenance.objects.count(), 10)

    def test_concurrent_attempts_grant_credit_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import OperationalError, close_old_connections
        actor = self.admin('host')
        self.historical()
        document = preview(self.month, actor)
        barrier = Barrier(2)
        def attempt():
            close_old_connections()
            barrier.wait(timeout=10)
            try:
                apply(self.month, actor, document, self.confirm())
                return 'applied'
            except (ValidationError, OperationalError):
                return 'rejected'
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: attempt(), range(2)))
        self.assertEqual(sorted(outcomes), ['applied', 'rejected'])
        self.assertEqual(PersonalTBRMatch.objects.filter(is_qualifying=True).count(), 1)
        self.assertEqual(ModifierProvenance.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action='personal_tbr.reconciled').count(), 1)

    def test_new_preview_after_repair_preserves_existing_awards(self):
        actor = self.admin()
        self.historical(all_books=True)
        apply(self.month, actor, preview(self.month, actor), self.confirm())
        awards = list(PersonalTBRCompletionAward.objects.values())
        provenance = list(ModifierProvenance.objects.order_by('pk').values())
        document = preview(self.month, actor)
        self.assertEqual(document['plan']['effects'], {})
        apply(self.month, actor, document, self.confirm())
        self.assertEqual(list(PersonalTBRCompletionAward.objects.values()), awards)
        self.assertEqual(list(ModifierProvenance.objects.order_by('pk').values()), provenance)

    def test_command_creates_private_preview_and_requires_explicit_confirmation(self):
        import tempfile
        from django.core.management import CommandError
        actor = self.admin()
        self.historical()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'preview.json')
            call_command('reconcile_personal_tbr', group='hotfix', month=self.month.pk,
                actor=actor.username, output=path, stdout=io.StringIO())
            self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)
            self.assertFalse(PersonalTBRMatch.objects.exists())
            with self.assertRaises(CommandError):
                call_command('reconcile_personal_tbr', group='hotfix', month=self.month.pk,
                    actor=actor.username, apply=True, preview_file=path, confirm='yes')
            call_command('reconcile_personal_tbr', group='hotfix', month=self.month.pk,
                actor=actor.username, apply=True, preview_file=path, confirm=self.confirm(), stdout=io.StringIO())
            self.assertEqual(PersonalTBRMatch.objects.filter(is_qualifying=True).count(), 1)

    def test_authority_is_narrow_and_immediate_web_apply_is_disabled(self):
        self.historical()
        for actor in (self.reader.user, self.admin('floater')):
            with self.assertRaises(PermissionDenied):
                preview(self.month, actor)
            self.client.force_login(actor)
            response = self.client.post(reverse('personal-tbr-match-synchronize', args=['hotfix', self.month.pk]))
            self.assertEqual(response.status_code, 403)
        owner = get_user_model().objects.create_user('owner2')
        Membership.objects.create(group=self.month.group, user=owner, role='owner', display_name='Owner')
        self.client.force_login(owner)
        self.assertEqual(self.client.post(reverse('personal-tbr-match-synchronize', args=['hotfix', self.month.pk])).status_code, 409)
        self.assertFalse(PersonalTBRMatch.objects.exists())

    def test_confirmation_stale_plan_and_failure_roll_back(self):
        actor = self.admin('host')
        self.historical()
        document = preview(self.month, actor)
        with self.assertRaises(ValidationError):
            apply(self.month, actor, document, 'yes')
        from .personal_tbr_matching import synchronize_submission as real_sync
        def fail_after_write(submission):
            real_sync(submission)
            raise RuntimeError('Injected failure')
        with patch('core.tbr_reconciliation.synchronize_submission', side_effect=fail_after_write):
            with self.assertRaises(RuntimeError):
                apply(self.month, actor, document, self.confirm())
        self.assertFalse(PersonalTBRMatch.objects.exists())
        self.assertFalse(ModifierProvenance.objects.exists())
        BookSubmission.objects.update(notes='Edited since preview')
        with self.assertRaisesMessage(ValidationError, 'stale'):
            apply(self.month, actor, document, self.confirm())

    def test_plan_tampering_and_changed_effects_cannot_apply(self):
        actor = self.admin()
        self.historical()
        document = preview(self.month, actor)
        with self.assertRaises(ValidationError):
            apply(self.month, actor, {'signed_plan': document['signed_plan'] + 'bad'}, self.confirm())
        # Even an internally signed plan cannot authorize effects differing from
        # the actual services; the transaction must roll back.
        plan = dict(document['plan'], effects={})
        altered = {'plan': plan, 'signed_plan': signing.dumps(plan, salt='northbound.tbr-reconciliation.v1')}
        with self.assertRaisesMessage(ValidationError, 'Effects differ'):
            apply(self.month, actor, altered, self.confirm())
        self.assertFalse(PersonalTBRMatch.objects.exists())
        self.assertFalse(ModifierProvenance.objects.exists())
