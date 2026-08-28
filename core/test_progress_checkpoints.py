from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    BookSubmission,
    ChallengeMonth,
    ChallengeStaffAssignment,
    Membership,
    MonthEnrollment,
    ProgressCheckpoint,
    ProgressCheckpointResult,
    ReadingGroup,
    Team,
    TeamAssignment,
    UserProfile,
)
from .progress_checkpoints import process_due_progress_checkpoints
from .review_attention import needs_attention_summary
from .scheduling import process_due_challenge_schedules


class ProgressCheckpointTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.root = User.objects.create_superuser("checkpoint-root", "root@example.com", "password")
        self.owner_user = User.objects.create_user("checkpoint-owner")
        self.host_user = User.objects.create_user("checkpoint-host")
        self.reader_user = User.objects.create_user("checkpoint-reader")
        self.new_user = User.objects.create_user("checkpoint-new")
        self.inactive_user = User.objects.create_user("checkpoint-inactive")
        self.ordinary_user = User.objects.create_user("checkpoint-ordinary")
        self.floater_user = User.objects.create_user("checkpoint-floater")
        self.group = ReadingGroup.objects.create(name="Checkpoint Group", slug="checkpoint-group")
        self.owner = self.member(self.owner_user, "Owner", Membership.Role.OWNER)
        self.host = self.member(self.host_user, "Host")
        self.reader = self.member(self.reader_user, "History Reader")
        self.new_reader = self.member(self.new_user, "New Reader")
        self.inactive_reader = self.member(self.inactive_user, "Inactive Reader")
        self.ordinary = self.member(self.ordinary_user, "Ordinary Reader")
        self.floater = self.member(self.floater_user, "Floater")
        self.month = self.challenge("Current", date(2026, 8, 1), ChallengeMonth.Status.ACTIVE)
        ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=self.host,
            role=ChallengeStaffAssignment.Role.HOST,
            host_assignment_notice_seen_at=timezone.now(),
        )
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.floater, role=ChallengeStaffAssignment.Role.FLOATER)
        self.reader_enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.reader)
        self.new_enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.new_reader)
        self.inactive_enrollment = MonthEnrollment.objects.create(
            month=self.month,
            participant=self.inactive_reader,
            is_active=False,
            inactive_reason=MonthEnrollment.InactiveReason.WITHDRAWN,
        )
        self.team = Team.objects.create(month=self.month, name="Checkpoint Team")
        self.assignment = TeamAssignment.objects.create(month=self.month, participant=self.reader, team=self.team)
        history = self.challenge("History", date(2026, 6, 1), ChallengeMonth.Status.COMPLETED)
        MonthEnrollment.objects.create(month=history, participant=self.reader)
        self.book(history, self.reader, 400, bonus=100)
        self.book(self.month, self.reader, 50, bonus=50)
        for user in (self.reader_user, self.new_user, self.inactive_user):
            UserProfile.objects.create(user=user)
        self.now = timezone.now()

    def member(self, user, display_name, role=Membership.Role.MEMBER):
        return Membership.objects.create(group=self.group, user=user, display_name=display_name, role=role)

    def challenge(self, name, starts_on, status):
        return ChallengeMonth.objects.create(
            group=self.group,
            name=name,
            starts_on=starts_on,
            ends_on=date(starts_on.year, starts_on.month, 28),
            status=status,
        )

    def book(self, month, participant, pages, *, bonus=0):
        return BookSubmission.objects.create(
            month=month,
            participant=participant,
            title=f"{month.name} Book",
            author="Author",
            book_format=BookSubmission.Format.EBOOK,
            completed_on=month.starts_on,
            submitted_pages=pages,
            approved_pages=pages,
            bonus_pages=bonus,
            status=BookSubmission.Status.APPROVED,
        )

    def checkpoint(self, **overrides):
        values = {
            "month": self.month,
            "scheduled_at": self.now - timedelta(minutes=1),
            "threshold_percentage": 25,
            "progress_basis": ProgressCheckpoint.ProgressBasis.BASE,
            "target_basis": ProgressCheckpoint.TargetBasis.PREVIOUS_AVERAGE,
            "position": self.month.progress_checkpoints.count() + 1,
        }
        values.update(overrides)
        return ProgressCheckpoint.objects.create(**values)

    def test_configuration_allows_zero_to_five_and_rejects_sixth(self):
        self.assertFalse(self.month.progress_checkpoints.exists())
        for position in range(1, 6):
            self.checkpoint(position=position, scheduled_at=self.now + timedelta(days=position))
        with self.assertRaises(ValidationError):
            self.checkpoint(position=6, scheduled_at=self.now + timedelta(days=6))

    def test_fixed_target_is_required_and_previous_average_discards_it(self):
        with self.assertRaises(ValidationError):
            self.checkpoint(target_basis=ProgressCheckpoint.TargetBasis.FIXED)
        checkpoint = self.checkpoint(fixed_target_pages=900)
        self.assertIsNone(checkpoint.fixed_target_pages)

    def test_previous_average_evaluation_snapshots_active_readers_only(self):
        checkpoint = self.checkpoint()
        participation_state = self.reader_enrollment.is_active
        team_id = self.assignment.team_id
        lifecycle = self.month.status
        self.assertEqual(process_due_progress_checkpoints(now=self.now), [(self.month.pk, checkpoint.pk, "evaluated", 2)])
        below = checkpoint.results.get(participant=self.reader)
        self.assertEqual(below.target_pages, Decimal("400"))
        self.assertEqual(below.required_pages, Decimal("100"))
        self.assertEqual(below.progress_pages, 50)
        self.assertEqual(below.outcome, ProgressCheckpointResult.Outcome.BELOW)
        new_result = checkpoint.results.get(participant=self.new_reader)
        self.assertEqual(new_result.outcome, ProgressCheckpointResult.Outcome.NOT_EVALUATED)
        self.assertFalse(checkpoint.results.filter(participant=self.inactive_reader).exists())
        self.reader_enrollment.refresh_from_db()
        self.assignment.refresh_from_db()
        self.month.refresh_from_db()
        self.assertEqual(self.reader_enrollment.is_active, participation_state)
        self.assertEqual(self.assignment.team_id, team_id)
        self.assertEqual(self.month.status, lifecycle)

    def test_total_pages_uses_current_final_scored_pages(self):
        checkpoint = self.checkpoint(
            threshold_percentage=50,
            progress_basis=ProgressCheckpoint.ProgressBasis.TOTAL,
            target_basis=ProgressCheckpoint.TargetBasis.FIXED,
            fixed_target_pages=200,
        )
        process_due_progress_checkpoints(now=self.now)
        result = checkpoint.results.get(participant=self.reader)
        self.assertEqual(result.progress_pages, 100)
        self.assertEqual(result.outcome, ProgressCheckpointResult.Outcome.MET)

    def test_result_is_idempotent_locked_and_unchanged_after_later_reading(self):
        checkpoint = self.checkpoint()
        process_due_progress_checkpoints(now=self.now)
        original = checkpoint.results.get(participant=self.reader)
        self.book(self.month, self.reader, 500)
        self.assertEqual(process_due_progress_checkpoints(now=self.now + timedelta(hours=1)), [])
        original.refresh_from_db()
        self.assertEqual(original.progress_pages, 50)
        checkpoint.refresh_from_db()
        checkpoint.threshold_percentage = 90
        with self.assertRaises(ValidationError):
            checkpoint.save()
        with self.assertRaises(ValidationError):
            checkpoint.delete()

    def test_multiple_checkpoints_are_independent(self):
        first = self.checkpoint(position=1)
        second = self.checkpoint(
            position=2,
            threshold_percentage=10,
            target_basis=ProgressCheckpoint.TargetBasis.FIXED,
            fixed_target_pages=200,
        )
        process_due_progress_checkpoints(now=self.now)
        self.assertEqual(first.results.get(participant=self.reader).outcome, ProgressCheckpointResult.Outcome.BELOW)
        self.assertEqual(second.results.get(participant=self.reader).outcome, ProgressCheckpointResult.Outcome.MET)

    def test_existing_challenge_scheduler_loop_evaluates_due_checkpoint(self):
        checkpoint = self.checkpoint()
        processed = process_due_challenge_schedules(now=self.now)
        self.assertIn(
            (self.month.pk, [f"checkpoint_evaluated:{checkpoint.pk}:2"]),
            processed,
        )
        checkpoint.refresh_from_db()
        self.assertEqual(checkpoint.evaluation_state, ProgressCheckpoint.EvaluationState.EVALUATED)

    def test_finalizing_overdue_checkpoint_is_skipped_without_results(self):
        self.month.status = ChallengeMonth.Status.FINALIZING
        self.month._allow_lifecycle_transition = True
        self.month.save(update_fields=["status"])
        checkpoint = self.checkpoint()
        process_due_progress_checkpoints(now=self.now)
        checkpoint.refresh_from_db()
        self.assertEqual(checkpoint.evaluation_state, ProgressCheckpoint.EvaluationState.SKIPPED)
        self.assertFalse(checkpoint.results.exists())

    def test_configuration_view_is_purpose_built_and_authorized(self):
        url = reverse("challenge-progress-checkpoints", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.client.force_login(self.owner_user)
        response = self.client.get(url)
        self.assertContains(response, "Add Checkpoint")
        self.assertContains(response, "data-fixed-target")
        self.assertNotContains(response, ">Order<")
        self.assertNotContains(response, ">Delete<")
        self.client.force_login(self.ordinary_user)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_configuration_post_creates_independent_checkpoints_and_locks_after_evaluation(self):
        url = reverse("challenge-progress-checkpoints", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.client.force_login(self.owner_user)
        data = {
            "checkpoints-TOTAL_FORMS": "2",
            "checkpoints-INITIAL_FORMS": "0",
            "checkpoints-MIN_NUM_FORMS": "0",
            "checkpoints-MAX_NUM_FORMS": "5",
            "checkpoints-0-scheduled_at": (self.now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M"),
            "checkpoints-0-threshold_percentage": "25",
            "checkpoints-0-progress_basis": ProgressCheckpoint.ProgressBasis.BASE,
            "checkpoints-0-target_basis": ProgressCheckpoint.TargetBasis.PREVIOUS_AVERAGE,
            "checkpoints-0-fixed_target_pages": "999",
            "checkpoints-0-ORDER": "1",
            "checkpoints-1-scheduled_at": (self.now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M"),
            "checkpoints-1-threshold_percentage": "60",
            "checkpoints-1-progress_basis": ProgressCheckpoint.ProgressBasis.TOTAL,
            "checkpoints-1-target_basis": ProgressCheckpoint.TargetBasis.FIXED,
            "checkpoints-1-fixed_target_pages": "1200",
            "checkpoints-1-ORDER": "2",
        }
        response = self.client.post(url, data)
        self.assertRedirects(response, url)
        checkpoints = list(self.month.progress_checkpoints.all())
        self.assertEqual(len(checkpoints), 2)
        self.assertIsNone(checkpoints[0].fixed_target_pages)
        self.assertEqual(checkpoints[1].fixed_target_pages, 1200)
        process_due_progress_checkpoints(now=self.now + timedelta(days=3))
        response = self.client.get(url)
        self.assertContains(response, "Checkpoint configuration locked")
        self.assertNotContains(response, "Add Checkpoint")

    def test_below_warning_and_needs_attention_visibility_are_host_only(self):
        checkpoint = self.checkpoint()
        process_due_progress_checkpoints(now=self.now)
        roster_url = reverse("month-participant-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.client.force_login(self.host_user)
        response = self.client.get(roster_url)
        self.assertContains(response, "checkpoint-warning-dot")
        self.assertContains(response, "Below 25% checkpoint")
        self.assertContains(response, "participant-planning-mobile")
        host_attention = needs_attention_summary(self.host_user)
        self.assertEqual(host_attention["total"], 1)
        self.assertEqual(host_attention["challenges"][0]["checkpoint_results"][0].participant, self.reader)
        self.assertEqual(needs_attention_summary(self.root)["total"], 1)
        for user in (self.ordinary_user, self.floater_user):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(roster_url)
                self.assertNotContains(response, "Below 25% checkpoint")
                self.assertEqual(needs_attention_summary(user)["total"], 0)

    def test_passing_reader_has_no_roster_warning(self):
        checkpoint = self.checkpoint(
            threshold_percentage=10,
            target_basis=ProgressCheckpoint.TargetBasis.FIXED,
            fixed_target_pages=200,
        )
        process_due_progress_checkpoints(now=self.now)
        self.assertEqual(checkpoint.results.get(participant=self.reader).outcome, ProgressCheckpointResult.Outcome.MET)
        self.client.force_login(self.host_user)
        response = self.client.get(reverse("month-participant-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        reader_row = next(
            enrollment for enrollment in response.context["enrollments"]
            if enrollment.participant_id == self.reader.pk
        )
        self.assertEqual(reader_row.checkpoint_warnings, [])
