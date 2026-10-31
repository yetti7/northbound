from datetime import date, timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    BookSubmission, ChallengeMonth, ChallengeStaffAssignment, Membership,
    MonthEnrollment, ProgressCheckpoint, ProgressCheckpointResult, ReadingGroup,
    RecoveryOperation, Team, TeamAssignment,
)
from .progress_checkpoints import evaluate_progress_checkpoint, process_due_progress_checkpoints
from .recovery import RecoveryRequest
from .recovery_checkpoints import (
    checkpoint_recovery_label, release_checkpoint_evaluation,
    reset_checkpoint_evaluation,
)
from .review_attention import needs_attention_summary


class CheckpointRecoveryTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_superuser(
            "checkpoint-platform-owner", password="owner-password-963!",
        )
        self.group_owner_user = User.objects.create_user("checkpoint-group-owner", password="test-password")
        self.moderator_user = User.objects.create_user("checkpoint-moderator", password="test-password")
        self.host_user = User.objects.create_user("checkpoint-host", password="test-password")
        self.floater_user = User.objects.create_user("checkpoint-floater", password="test-password")
        self.leader_user = User.objects.create_user("checkpoint-leader", password="test-password")
        self.ordinary_user = User.objects.create_user("checkpoint-ordinary", password="test-password")
        self.group = ReadingGroup.objects.create(name="Checkpoint Recovery Group", slug="checkpoint-recovery")

        def member(user, display_name, role=Membership.Role.MEMBER):
            return Membership.objects.create(
                group=self.group, user=user, display_name=display_name, role=role,
            )

        self.group_owner = member(self.group_owner_user, "Group Owner", Membership.Role.OWNER)
        self.moderator = member(self.moderator_user, "Moderator", Membership.Role.MODERATOR)
        self.host = member(self.host_user, "Host")
        self.floater = member(self.floater_user, "Floater")
        self.leader = member(self.leader_user, "Leader")
        now = timezone.now()
        self.month = ChallengeMonth.objects.create(
            group=self.group, name="Checkpoint Recovery Challenge",
            starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31),
            status=ChallengeMonth.Status.ACTIVE,
        )
        ChallengeStaffAssignment.objects.create(
            month=self.month, membership=self.host,
            role=ChallengeStaffAssignment.Role.HOST,
        )
        ChallengeStaffAssignment.objects.create(
            month=self.month, membership=self.floater,
            role=ChallengeStaffAssignment.Role.FLOATER,
        )
        team = Team.objects.create(month=self.month, name="Checkpoint Team")
        MonthEnrollment.objects.create(month=self.month, participant=self.leader)
        TeamAssignment.objects.create(month=self.month, participant=self.leader, team=team)
        ChallengeStaffAssignment.objects.create(
            month=self.month, membership=self.leader, team=team,
            role=ChallengeStaffAssignment.Role.TEAM_LEADER,
        )
        self.readers = [self.leader]
        BookSubmission.objects.create(
            month=self.month, participant=self.leader, title="Checkpoint Book 1",
            author="Writer", book_format=BookSubmission.Format.EBOOK,
            completed_on=date(2026, 8, 10), submitted_pages=100,
            approved_pages=100, status=BookSubmission.Status.APPROVED,
        )
        for index in range(2, 8):
            user = User.objects.create_user(f"checkpoint-reader-{index}", password="test-password")
            reader = member(user, f"Reader {index}")
            MonthEnrollment.objects.create(month=self.month, participant=reader)
            TeamAssignment.objects.create(month=self.month, participant=reader, team=team)
            self.readers.append(reader)
            if index <= 3:
                BookSubmission.objects.create(
                    month=self.month, participant=reader, title=f"Checkpoint Book {index}",
                    author="Writer", book_format=BookSubmission.Format.EBOOK,
                    completed_on=date(2026, 8, 10), submitted_pages=100,
                    approved_pages=100, status=BookSubmission.Status.APPROVED,
                )
        self.checkpoint = ProgressCheckpoint.objects.create(
            month=self.month, scheduled_at=now - timedelta(days=1), position=1,
            threshold_percentage=50,
            progress_basis=ProgressCheckpoint.ProgressBasis.BASE,
            target_basis=ProgressCheckpoint.TargetBasis.FIXED,
            fixed_target_pages=100,
        )
        evaluate_progress_checkpoint(self.checkpoint, now=now - timedelta(hours=12))
        self.checkpoint.refresh_from_db()
        self.pending = ProgressCheckpoint.objects.create(
            month=self.month, scheduled_at=now + timedelta(days=2), position=2,
            threshold_percentage=25,
            progress_basis=ProgressCheckpoint.ProgressBasis.TOTAL,
            target_basis=ProgressCheckpoint.TargetBasis.FIXED,
            fixed_target_pages=200,
        )

    def request(self, *, action, tier, password="", checkpoint=None):
        target = checkpoint or self.checkpoint
        label = checkpoint_recovery_label(target)
        return RecoveryRequest(
            actor=self.owner, tier=tier, action=action,
            target_type="ProgressCheckpoint", target_id=str(target.pk),
            target_label=label, reason="Invalidate a malformed checkpoint evaluation",
            required_confirmation=label, supplied_confirmation=label,
            current_password=password,
            confirmation_method="exact target label" + (" plus current password" if tier == 3 else ""),
            group=self.group, challenge=self.month,
        )

    def reset(self):
        return reset_checkpoint_evaluation(
            checkpoint=self.checkpoint,
            recovery_request=self.request(
                action="CHECKPOINT_EVALUATION_RESET", tier=3,
                password="owner-password-963!",
            ),
        )

    def test_platform_owner_preview_is_accurate_and_pending_has_no_reset_action(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("recovery-checkpoint-reset", kwargs={"pk": self.checkpoint.pk}))
        self.assertContains(response, "Results removed</dt><dd>7", html=False)
        self.assertContains(response, "Met</dt><dd>3", html=False)
        self.assertContains(response, "Below</dt><dd>4", html=False)
        self.assertContains(response, "Backup Advisory")
        pending_detail = self.client.get(reverse("recovery-checkpoint-detail", kwargs={"pk": self.pending.pk}))
        self.assertNotContains(pending_detail, "Reset Evaluation")
        reset_pending = self.client.get(reverse("recovery-checkpoint-reset", kwargs={"pk": self.pending.pk}))
        self.assertRedirects(reset_pending, reverse("recovery-checkpoint-detail", kwargs={"pk": self.pending.pk}))

    def test_ordinary_checkpoint_settings_show_recovery_hold_without_recovery_actions(self):
        self.reset()
        self.client.force_login(self.host_user)
        response = self.client.get(reverse("challenge-progress-checkpoints", kwargs={
            "group_slug": self.group.slug,
            "month_pk": self.month.pk,
        }))
        self.assertContains(response, "Recovery Hold")
        self.assertContains(response, "status-recovery-hold")
        self.assertNotContains(response, "Release Recovery Hold")
        self.assertNotContains(response, "Reset Evaluation")

    def test_reset_requires_reason_exact_confirmation_and_current_password(self):
        self.client.force_login(self.owner)
        url = reverse("recovery-checkpoint-reset", kwargs={"pk": self.checkpoint.pk})
        label = checkpoint_recovery_label(self.checkpoint)
        for payload in (
            {"reason": "", "confirmation": label, "current_password": "owner-password-963!"},
            {"reason": "Repair", "confirmation": "wrong", "current_password": "owner-password-963!"},
            {"reason": "Repair", "confirmation": label, "current_password": "wrong"},
        ):
            response = self.client.post(url, payload)
            self.assertEqual(response.status_code, 200)
            self.checkpoint.refresh_from_db()
            self.assertEqual(self.checkpoint.evaluation_state, ProgressCheckpoint.EvaluationState.EVALUATED)
            self.assertEqual(self.checkpoint.results.count(), 7)

    def test_reset_is_atomic_pending_held_and_preserves_configuration_and_evidence(self):
        configuration = (
            self.checkpoint.scheduled_at, self.checkpoint.threshold_percentage,
            self.checkpoint.progress_basis, self.checkpoint.target_basis,
            self.checkpoint.fixed_target_pages, self.checkpoint.position,
        )
        operation = self.reset()
        self.checkpoint.refresh_from_db()
        self.assertEqual(self.checkpoint.evaluation_state, ProgressCheckpoint.EvaluationState.PENDING)
        self.assertIsNone(self.checkpoint.evaluated_at)
        self.assertTrue(self.checkpoint.recovery_hold)
        self.assertEqual(self.checkpoint.results.count(), 0)
        self.assertEqual(configuration, (
            self.checkpoint.scheduled_at, self.checkpoint.threshold_percentage,
            self.checkpoint.progress_basis, self.checkpoint.target_basis,
            self.checkpoint.fixed_target_pages, self.checkpoint.position,
        ))
        self.assertEqual(operation.action, "CHECKPOINT_EVALUATION_RESET")
        previous = operation.after_state["previous_evaluation"]
        self.assertEqual(previous["result_count"], 7)
        self.assertEqual(previous["met_count"], 3)
        self.assertEqual(previous["below_count"], 4)
        self.assertEqual(operation.after_state["remaining_result_count"], 0)
        self.assertNotIn("display_name", str(operation.after_state))

    def test_reset_clears_checkpoint_needs_attention_and_participant_warning(self):
        self.assertEqual(needs_attention_summary(self.owner)["total"], 4)
        self.client.force_login(self.host_user)
        before = self.client.get(reverse(
            "month-participant-list",
            kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk},
        ))
        self.assertContains(before, "Below 50% checkpoint")
        self.reset()
        self.assertEqual(needs_attention_summary(self.owner)["total"], 0)
        after = self.client.get(reverse(
            "month-participant-list",
            kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk},
        ))
        self.assertNotContains(after, "Below 50% checkpoint")

    def test_held_checkpoints_are_suppressed_without_affecting_unrelated_pending(self):
        self.reset()
        self.pending.scheduled_at = timezone.now() - timedelta(minutes=1)
        self.pending.save(update_fields=["scheduled_at"])
        processed = process_due_progress_checkpoints(now=timezone.now())
        self.checkpoint.refresh_from_db()
        self.pending.refresh_from_db()
        self.assertEqual(self.checkpoint.evaluation_state, ProgressCheckpoint.EvaluationState.PENDING)
        self.assertTrue(self.checkpoint.recovery_hold)
        self.assertEqual(self.checkpoint.results.count(), 0)
        self.assertEqual(self.pending.evaluation_state, ProgressCheckpoint.EvaluationState.EVALUATED)
        self.assertTrue(any(item[1] == self.pending.pk for item in processed))

    def test_held_checkpoint_is_not_skipped_in_later_lifecycle_states(self):
        self.reset()
        for status in (
            ChallengeMonth.Status.FINALIZING,
            ChallengeMonth.Status.COMPLETED,
            ChallengeMonth.Status.ARCHIVED,
        ):
            ChallengeMonth.objects.filter(pk=self.month.pk).update(status=status)
            process_due_progress_checkpoints(now=timezone.now())
            self.checkpoint.refresh_from_db()
            self.assertEqual(self.checkpoint.evaluation_state, ProgressCheckpoint.EvaluationState.PENDING)
            self.assertTrue(self.checkpoint.recovery_hold)

    def test_configuration_can_change_while_held_and_save_does_not_release(self):
        self.reset()
        self.client.force_login(self.host_user)
        url = reverse("challenge-progress-checkpoints", kwargs={
            "group_slug": self.group.slug, "month_pk": self.month.pk,
        })
        payload = {
            "checkpoints-TOTAL_FORMS": "2", "checkpoints-INITIAL_FORMS": "2",
            "checkpoints-MIN_NUM_FORMS": "0", "checkpoints-MAX_NUM_FORMS": "5",
        }
        for index, checkpoint in enumerate((self.checkpoint, self.pending)):
            payload.update({
                f"checkpoints-{index}-checkpoint_id": str(checkpoint.pk),
                f"checkpoints-{index}-scheduled_at": timezone.localtime(checkpoint.scheduled_at).strftime("%Y-%m-%dT%H:%M"),
                f"checkpoints-{index}-threshold_percentage": "60" if index == 0 else str(checkpoint.threshold_percentage),
                f"checkpoints-{index}-progress_basis": checkpoint.progress_basis,
                f"checkpoints-{index}-target_basis": checkpoint.target_basis,
                f"checkpoints-{index}-fixed_target_pages": str(checkpoint.fixed_target_pages),
                f"checkpoints-{index}-ORDER": str(index + 1),
                f"checkpoints-{index}-DELETE": "",
            })
        response = self.client.post(url, payload)
        self.assertRedirects(response, url)
        self.checkpoint.refresh_from_db()
        self.assertEqual(self.checkpoint.threshold_percentage, 60)
        self.assertTrue(self.checkpoint.recovery_hold)
        self.assertEqual(self.checkpoint.evaluation_state, ProgressCheckpoint.EvaluationState.PENDING)

    def test_release_requires_reason_clears_only_hold_and_scheduler_then_resumes(self):
        self.reset()
        self.client.force_login(self.owner)
        url = reverse("recovery-checkpoint-release", kwargs={"pk": self.checkpoint.pk})
        label = checkpoint_recovery_label(self.checkpoint)
        blocked = self.client.post(url, {"reason": "", "confirmation": label})
        self.assertEqual(blocked.status_code, 200)
        self.checkpoint.refresh_from_db()
        self.assertTrue(self.checkpoint.recovery_hold)
        response = self.client.post(url, {
            "reason": "Release after correction", "confirmation": label,
        })
        self.assertRedirects(response, reverse("recovery-checkpoint-detail", kwargs={"pk": self.checkpoint.pk}))
        self.checkpoint.refresh_from_db()
        self.assertFalse(self.checkpoint.recovery_hold)
        self.assertEqual(self.checkpoint.evaluation_state, ProgressCheckpoint.EvaluationState.PENDING)
        self.assertEqual(self.checkpoint.results.count(), 0)
        operations = RecoveryOperation.objects.filter(target_id=str(self.checkpoint.pk)).order_by("created_at")
        self.assertEqual(
            list(operations.values_list("action", flat=True)),
            ["CHECKPOINT_EVALUATION_RESET", "CHECKPOINT_EVALUATION_RELEASED"],
        )
        process_due_progress_checkpoints(now=timezone.now())
        self.checkpoint.refresh_from_db()
        self.assertEqual(self.checkpoint.evaluation_state, ProgressCheckpoint.EvaluationState.EVALUATED)
        self.assertEqual(self.checkpoint.results.count(), 7)

    def test_released_later_lifecycle_checkpoint_follows_existing_skip_rule(self):
        self.reset()
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status=ChallengeMonth.Status.COMPLETED)
        release_checkpoint_evaluation(
            checkpoint=self.checkpoint,
            recovery_request=self.request(action="CHECKPOINT_EVALUATION_RELEASED", tier=2),
        )
        self.checkpoint.refresh_from_db()
        self.assertEqual(self.checkpoint.evaluation_state, ProgressCheckpoint.EvaluationState.PENDING)
        process_due_progress_checkpoints(now=timezone.now())
        self.checkpoint.refresh_from_db()
        self.assertEqual(self.checkpoint.evaluation_state, ProgressCheckpoint.EvaluationState.SKIPPED)
        self.assertEqual(self.checkpoint.results.count(), 0)

    def test_reset_and_release_injected_failures_roll_back_without_false_success(self):
        reset_request = self.request(
            action="CHECKPOINT_EVALUATION_RESET", tier=3,
            password="owner-password-963!",
        )
        with self.assertRaises(RuntimeError):
            reset_checkpoint_evaluation(
                checkpoint=self.checkpoint, recovery_request=reset_request,
                fail_after_step="results",
            )
        self.checkpoint.refresh_from_db()
        self.assertEqual(self.checkpoint.evaluation_state, ProgressCheckpoint.EvaluationState.EVALUATED)
        self.assertFalse(self.checkpoint.recovery_hold)
        self.assertEqual(self.checkpoint.results.count(), 7)
        self.assertEqual(
            RecoveryOperation.objects.get(operation_id=reset_request.operation_id).result,
            RecoveryOperation.Result.FAILED,
        )
        self.assertFalse(RecoveryOperation.objects.filter(
            operation_id=reset_request.operation_id,
            result=RecoveryOperation.Result.SUCCEEDED,
        ).exists())

        self.reset()
        release_request = self.request(action="CHECKPOINT_EVALUATION_RELEASED", tier=2)
        with self.assertRaises(RuntimeError):
            release_checkpoint_evaluation(
                checkpoint=self.checkpoint, recovery_request=release_request,
                fail_after_step="state",
            )
        self.checkpoint.refresh_from_db()
        self.assertTrue(self.checkpoint.recovery_hold)
        self.assertEqual(
            RecoveryOperation.objects.get(operation_id=release_request.operation_id).result,
            RecoveryOperation.Result.FAILED,
        )

    def test_all_normal_roles_are_denied_and_no_identity_is_created(self):
        routes = (
            reverse("recovery-checkpoint-list"),
            reverse("recovery-checkpoint-detail", kwargs={"pk": self.checkpoint.pk}),
            reverse("recovery-checkpoint-reset", kwargs={"pk": self.checkpoint.pk}),
            reverse("recovery-checkpoint-release", kwargs={"pk": self.checkpoint.pk}),
        )
        baseline = (
            Membership.objects.count(), ChallengeStaffAssignment.objects.count(),
            MonthEnrollment.objects.count(), TeamAssignment.objects.count(),
            BookSubmission.objects.count(),
        )
        users = (
            self.group_owner_user, self.moderator_user, self.host_user,
            self.floater_user, self.leader_user, self.readers[0].user,
            self.ordinary_user,
        )
        for user in users:
            self.client.force_login(user)
            for route in routes:
                with self.subTest(user=user.username, route=route):
                    self.assertEqual(self.client.get(route).status_code, 403)
        self.assertEqual(baseline, (
            Membership.objects.count(), ChallengeStaffAssignment.objects.count(),
            MonthEnrollment.objects.count(), TeamAssignment.objects.count(),
            BookSubmission.objects.count(),
        ))
        self.assertFalse(Membership.objects.filter(user=self.owner).exists())

    def test_individual_result_has_no_recovery_edit_or_delete_route(self):
        result = self.checkpoint.results.first()
        route_names = {pattern.name for pattern in __import__("core.urls", fromlist=["urlpatterns"]).urlpatterns}
        self.assertNotIn("recovery-checkpoint-result-edit", route_names)
        self.assertNotIn("recovery-checkpoint-result-delete", route_names)
        self.assertIsNotNone(result)
        result.progress_pages += 1
        with self.assertRaises(ValidationError):
            result.save()
        with self.assertRaises(ValidationError):
            result.delete()
