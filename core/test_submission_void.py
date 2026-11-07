"""Submission-specific void authority and participant action regressions."""
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import test_submission_editing as editing_fixture
from . import test_submission_hotfix as fixture
from .models import (
    AuditEvent, BookSubmission, ChallengeMonth, ChallengeStaffAssignment,
    Membership, MonthEnrollment, ReadingGroup, Team, TeamAssignment,
    BotmBook, ModifierProvenance, MonthTheme, ThemeClaim, ProgressCheckpoint, ProgressCheckpointResult,
)
from .permissions import can_operate_challenge, can_void_submission
from .score_aggregation import challenge_score_totals


class SubmissionVoidTests(TestCase):
    setUp = fixture.SubmissionHotfixTests.setUp
    actor = editing_fixture.SubmissionEditingTests.actor
    submission = editing_fixture.SubmissionEditingTests.submission

    def void_url(self, item):
        return reverse('submission-remove', args=[item.month.group.slug, item.month_id, item.pk])

    def detail_url(self):
        return reverse('challenge-participant-detail', args=['hotfix', self.month.pk, self.reader.pk])

    def test_all_authorized_roles_can_void_with_history_preserved(self):
        for role in ('platform', 'owner', 'moderator', 'host', 'floater', 'team_leader'):
            with self.subTest(role=role):
                actor = self.actor(role)
                item = self.submission(catalog=True)
                self.assertTrue(can_void_submission(actor, item))
                if role not in ('platform', 'host'):
                    self.assertFalse(can_operate_challenge(actor, self.month))
                self.client.force_login(actor)
                response = self.client.post(self.void_url(item), {'reason': 'Accidental box set'})
                self.assertEqual(response.status_code, 302)
                item.refresh_from_db()
                self.assertTrue(item.is_removed)
                self.assertEqual((item.status, item.approved_pages), ('approved', 350))
                self.assertEqual(item.removed_by, actor)
                self.assertEqual(item.removal_reason, 'Accidental box set')
                self.assertTrue(AuditEvent.objects.filter(
                    action='submission.removed', object_id=str(item.pk), actor=actor).exists())

    def test_reader_inactive_and_anonymous_cannot_void(self):
        item = self.submission()
        self.assertFalse(can_void_submission(self.reader.user, item))
        self.assertEqual(self.client.post(self.void_url(item)).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post(self.void_url(item)).status_code, 302)
        actor = self.actor('owner')
        actor.is_active = False
        self.assertFalse(can_void_submission(actor, item))
        actor.save(update_fields=['is_active'])
        self.client.force_login(actor)
        self.assertNotEqual(self.client.post(self.void_url(item)).status_code, 200)
        item.refresh_from_db()
        self.assertFalse(item.is_removed)

    def test_team_leader_current_team_and_enrollment_scope(self):
        actor = self.actor('team_leader')
        item = self.submission()
        self.client.force_login(actor)
        self.assertTrue(can_void_submission(actor, item))
        assignment = TeamAssignment.objects.get(month=self.month, participant=self.reader)
        assignment.ended_at = timezone.now()
        assignment.save(update_fields=['ended_at'])
        other_team = Team.objects.create(month=self.month, name='Other team')
        TeamAssignment.objects.create(month=self.month, participant=self.reader, team=other_team)
        self.assertFalse(can_void_submission(actor, item))
        self.assertEqual(self.client.post(self.void_url(item)).status_code, 403)
        TeamAssignment.objects.filter(month=self.month, participant=self.reader).delete()
        TeamAssignment.objects.create(month=self.month, participant=self.reader, team=self.team)
        MonthEnrollment.objects.filter(month=self.month, participant=self.reader).update(is_active=False)
        self.assertFalse(can_void_submission(actor, item))
        MonthEnrollment.objects.filter(month=self.month, participant=self.reader).update(is_active=True)
        ChallengeStaffAssignment.objects.filter(month=self.month, membership__user=actor).update(ended_at=timezone.now())
        self.assertFalse(can_void_submission(actor, item))
        self.assertEqual(self.client.post(self.void_url(item)).status_code, 403)

    def test_group_and_challenge_boundaries(self):
        item = self.submission()
        other_group = ReadingGroup.objects.create(name='Other', slug='other')
        other_month = ChallengeMonth.objects.create(group=self.month.group, name='Other',
            status='active', starts_on=self.month.starts_on, ends_on=self.month.ends_on)
        for role in ('host', 'floater', 'team_leader', 'owner', 'moderator'):
            actor = self.actor(role)
            member = Membership.objects.get(user=actor)
            if role in ('owner', 'moderator'):
                member.group = other_group
                member.save(update_fields=['group'])
            else:
                ChallengeStaffAssignment.objects.filter(membership=member).update(month=other_month)
            self.assertFalse(can_void_submission(actor, item))
            self.client.force_login(actor)
            self.assertEqual(self.client.post(self.void_url(item)).status_code, 403)
        self.client.force_login(self.actor('platform'))
        wrong_path = reverse('submission-remove', args=['other', self.month.pk, item.pk])
        self.assertEqual(self.client.post(wrong_path).status_code, 404)
        wrong_month = reverse('submission-remove', args=['hotfix', other_month.pk, item.pk])
        self.assertEqual(self.client.post(wrong_month).status_code, 404)

    def test_owner_and_moderator_scope_covers_other_group_challenges(self):
        item = self.submission()
        other_month = ChallengeMonth.objects.create(group=self.month.group, name='Other',
            status='active', starts_on=self.month.starts_on, ends_on=self.month.ends_on)
        item.month = other_month
        item.save(update_fields=['month'])
        for role in ('owner', 'moderator'):
            actor = self.actor(role)
            self.assertTrue(can_void_submission(actor, item))

    def test_lifecycle_restrictions_for_all_authorized_roles(self):
        item = self.submission()
        for role in ('platform', 'owner', 'moderator', 'host', 'floater', 'team_leader'):
            actor = self.actor(role)
            self.client.force_login(actor)
            for status in ('draft', 'upcoming', 'active', 'finalizing', 'completed', 'archived'):
                self.month.status = status
                ChallengeMonth.objects.filter(pk=self.month.pk).update(status=self.month.status)
                item.month = self.month
                allowed = status in ('draft', 'upcoming', 'active')
                with self.subTest(role=role, status=status):
                    self.assertTrue(can_void_submission(actor, item))
                    response = self.client.get(self.void_url(item))
                    self.assertEqual(response.status_code, 200 if allowed else 302)
                    if not allowed:
                        self.assertEqual(self.client.post(self.void_url(item)).status_code, 302)
        item.refresh_from_db()
        self.assertFalse(item.is_removed)

    def test_void_reconciles_tbr_and_totals_without_touching_other_submission(self):
        item = self.submission(catalog=True)
        other = BookSubmission.objects.create(month=self.month, participant=self.reader,
            title='Unrelated', author='Other', book_format='ebook', completed_on=item.completed_on,
            submitted_pages=100, approved_pages=100, status='approved')
        self.assertTrue(item.personal_tbr_matches.filter(is_qualifying=True).exists())
        self.client.force_login(self.actor('floater'))
        self.assertEqual(self.client.post(self.void_url(item)).status_code, 302)
        item.refresh_from_db()
        self.assertFalse(item.personal_tbr_matches.filter(is_qualifying=True).exists())
        self.assertEqual((item.bonus_pages, item.final_scored_pages), (0, None))
        self.assertEqual(challenge_score_totals(month=self.month)[self.reader.pk]['total_pages'], 100)
        other.refresh_from_db()
        self.assertFalse(other.is_removed)
        self.assertEqual(other.approved_pages, 100)
        self.assertEqual(self.client.post(self.void_url(item)).status_code, 404)

    def test_void_scoring_failure_rolls_back(self):
        item = self.submission(catalog=True)
        self.client.force_login(self.actor('moderator'))
        with patch('core.scoring.refresh_submission_score', side_effect=ValidationError('Synthetic failure')):
            with self.assertRaises(ValidationError):
                self.client.post(self.void_url(item))
        item.refresh_from_db()
        self.assertFalse(item.is_removed)
        self.assertTrue(item.personal_tbr_matches.filter(is_qualifying=True).exists())
        self.assertFalse(AuditEvent.objects.filter(action='submission.removed', object_id=str(item.pk)).exists())

    def test_participant_actions_and_compact_scoring(self):
        item = self.submission(catalog=True)
        self.book.source_url = 'https://hardcover.app/books/not-in-love'
        self.book.save(update_fields=['source_url'])
        for role in ('host', 'floater', 'team_leader', 'owner', 'moderator', 'platform'):
            actor = self.actor(role)
            self.client.force_login(actor)
            response = self.client.get(self.detail_url())
            with self.subTest(role=role):
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'Void Submission')
                self.assertContains(response, self.void_url(item))
                self.assertContains(response, '<th>Actions</th>', html=True)
                self.assertContains(response, 'aria-label="View Not in Love on Hardcover (opens in a new tab)"')
                self.assertContains(response, '<span aria-hidden="true">↗</span>', html=True)
                self.assertNotContains(response, 'Approved / Base Pages')
                self.assertNotContains(response, 'Score context')
                if role == 'moderator':
                    self.assertNotContains(response, '>Manage Submission</a>')
                else:
                    self.assertContains(response, '>Manage Submission</a>')
                purge_url = reverse('recovery-submission-purge', args=[item.pk])
                if role == 'platform':
                    self.assertContains(response, purge_url)
                    self.assertContains(response, 'Permanently Delete')
                else:
                    self.assertNotContains(response, purge_url)
                    self.assertNotContains(response, 'Permanently Delete')
                self.assertEqual(self.client.get(purge_url).status_code, 200 if role == 'platform' else 403)
        self.client.force_login(self.reader.user)
        response = self.client.get(self.detail_url())
        self.assertNotContains(response, 'Void Submission')
        self.assertNotContains(response, '>Manage Submission</a>')
        self.assertNotContains(response, 'Permanently Delete')

    def test_actions_hidden_when_out_of_scope_or_lifecycle_locked(self):
        item = self.submission()
        actor = self.actor('team_leader')
        TeamAssignment.objects.filter(participant=self.reader).update(ended_at=timezone.now())
        self.client.force_login(actor)
        self.assertEqual(self.client.get(self.detail_url()).status_code, 403)
        self.client.force_login(self.actor('host'))
        self.month.status = 'completed'
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status=self.month.status)
        self.assertNotContains(self.client.get(self.detail_url()), 'Void Submission')

    def test_theme_botm_and_checkpoint_history_after_void(self):
        from .botm_matching import synchronize_submission as sync_botm
        from .scoring import refresh_submission_score
        item = self.submission(catalog=True)
        actor = self.actor('floater')
        theme = MonthTheme.objects.create(month=self.month, name='Theme',
            starts_on=self.month.starts_on, ends_on=self.month.ends_on, bonus_pages=30)
        claim = ThemeClaim.objects.create(submission=item, theme=theme, status='approved',
            approved_bonus_pages=30)
        BotmBook.objects.create(month=self.month, position=1, catalog_book=self.book,
            title_snapshot=self.book.title, author_snapshot=self.book.author, page_count_snapshot=350, bonus_pages=25)
        self.month.botm_enabled = True
        self.month.save(update_fields=['botm_enabled'])
        sync_botm(item)
        refresh_submission_score(item)
        self.assertGreater(challenge_score_totals(month=self.month)[self.reader.pk]['modifier_pages'], 0)
        checkpoint = ProgressCheckpoint.objects.create(month=self.month, scheduled_at=timezone.now(),
            fixed_target_pages=100, target_basis='fixed')
        ProgressCheckpointResult.objects.create(checkpoint=checkpoint, participant=self.reader,
            evaluated_at=timezone.now(), threshold_percentage=25, progress_basis='base',
            target_basis='fixed', target_pages=100, required_pages=25, progress_pages=350, outcome='met')
        history = list(ProgressCheckpointResult.objects.values())
        self.client.force_login(actor)
        self.assertEqual(self.client.post(self.void_url(item)).status_code, 302)
        self.assertEqual(challenge_score_totals(month=self.month, participant_ids=[self.reader.pk])[self.reader.pk]['total_pages'], 0)
        self.assertFalse(item.botm_matches.filter(is_qualifying=True).exists())
        claim.refresh_from_db()
        self.assertEqual(claim.status, 'approved')
        self.assertEqual(claim.approved_bonus_pages, 30)
        self.assertEqual(list(ProgressCheckpointResult.objects.values()), history)
        self.assertTrue(ModifierProvenance.objects.filter(submission=item).exists())
