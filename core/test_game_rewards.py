from datetime import date, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from .game_rewards import apply_game_reward, void_game_reward
from .models import (
    AuditEvent,
    BookSubmission,
    ChallengeMonth,
    ChallengeStaffAssignment,
    Game,
    GameRewardApplication,
    GameRewardRecipient,
    Membership,
    ModifierProvenance,
    MonthEnrollment,
    ReadingGroup,
    Team,
    TeamAssignment,
)


class GameRewardServiceTests(TestCase):
    def setUp(self):
        self.group = ReadingGroup.objects.create(
            name="Reward Group",
            slug="reward-group",
            timezone="America/Los_Angeles",
        )
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="Reward Challenge",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status=ChallengeMonth.Status.ACTIVE,
            games_enabled=True,
        )
        self.team = Team.objects.create(month=self.month, name="Reward Team")
        now = timezone.now()
        self.game = Game.objects.create(
            month=self.month,
            name="Page Sprint",
            starts_at=now - timedelta(days=10),
            ends_at=now - timedelta(days=5),
            advertised_bonus_pages=50,
        )
        self.host = self.make_member("host", role=Membership.Role.MEMBER)
        self.host_assignment = ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=self.host,
            role=ChallengeStaffAssignment.Role.HOST,
        )
        self.reader = self.make_member("reader")
        self.enroll(self.reader)

    def make_member(self, name, *, role=Membership.Role.MEMBER, active=True):
        user = get_user_model().objects.create_user(f"reward-{name}")
        return Membership.objects.create(
            group=self.group,
            user=user,
            role=role,
            display_name=name.replace("-", " ").title(),
            is_active=active,
        )

    def enroll(self, participant, *, active=True):
        return MonthEnrollment.objects.create(month=self.month, participant=participant, is_active=active)

    def assign(self, participant, team=None):
        return TeamAssignment.objects.create(
            month=self.month,
            team=team or self.team,
            participant=participant,
        )

    def apply(self, **overrides):
        values = {
            "game": self.game,
            "actor": self.host.user,
            "target_type": GameRewardApplication.TargetType.READER,
            "target_participant": self.reader,
            "amount": 40,
            "reason": "Completed the Page Sprint.",
            "idempotency_key": uuid4(),
        }
        values.update(overrides)
        return apply_game_reward(**values)

    def test_authority_is_current_host_or_platform_owner_only(self):
        platform_owner = get_user_model().objects.create_superuser("reward-platform", password="test")
        owner = self.make_member("owner", role=Membership.Role.OWNER)
        moderator = self.make_member("moderator", role=Membership.Role.MODERATOR)
        floater = self.make_member("floater")
        ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=floater,
            role=ChallengeStaffAssignment.Role.FLOATER,
        )
        leader = self.make_member("leader")
        self.enroll(leader)
        self.assign(leader)
        ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=leader,
            team=self.team,
            role=ChallengeStaffAssignment.Role.TEAM_LEADER,
        )
        ordinary = self.make_member("ordinary")
        self.enroll(ordinary)
        ended_host = self.make_member("ended-host")
        ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=ended_host,
            role=ChallengeStaffAssignment.Role.HOST,
            ended_at=timezone.now(),
        )

        for label, actor, allowed in (
            ("current host", self.host.user, True),
            ("platform owner", platform_owner, True),
            ("group owner", owner.user, False),
            ("moderator", moderator.user, False),
            ("floater", floater.user, False),
            ("team leader", leader.user, False),
            ("reader", ordinary.user, False),
            ("ended host", ended_host.user, False),
        ):
            with self.subTest(label=label):
                if allowed:
                    application, created = self.apply(actor=actor)
                    self.assertTrue(created)
                    self.assertEqual(application.applied_by, actor)
                else:
                    with self.assertRaises(ValidationError):
                        self.apply(actor=actor)

    def test_apply_lifecycle_and_game_state_rules(self):
        for status, allowed in (
            (ChallengeMonth.Status.ACTIVE, True),
            (ChallengeMonth.Status.FINALIZING, True),
            (ChallengeMonth.Status.DRAFT, False),
            (ChallengeMonth.Status.UPCOMING, False),
            (ChallengeMonth.Status.COMPLETED, False),
            (ChallengeMonth.Status.ARCHIVED, False),
        ):
            with self.subTest(status=status):
                ChallengeMonth.objects.filter(pk=self.month.pk).update(status=status)
                self.month.refresh_from_db()
                if allowed:
                    self.assertTrue(self.apply()[1])
                else:
                    with self.assertRaises(ValidationError):
                        self.apply()

        ChallengeMonth.objects.filter(pk=self.month.pk).update(status=ChallengeMonth.Status.ACTIVE)
        self.month.refresh_from_db()
        self.game.is_active = False
        self.game.save(update_fields=["is_active"])
        with self.assertRaisesMessage(ValidationError, "retired Game"):
            self.apply()

    def test_individual_requires_active_enrollment_membership_and_ownership(self):
        application, created = self.apply()
        self.assertTrue(created)
        self.assertEqual(list(application.recipients.values_list("participant_id", flat=True)), [self.reader.pk])

        self.month.enrollments.filter(participant=self.reader).update(is_active=False)
        with self.assertRaisesMessage(ValidationError, "no eligible Readers"):
            self.apply()
        self.month.enrollments.filter(participant=self.reader).update(is_active=True)
        self.reader.is_active = False
        self.reader.save(update_fields=["is_active"])
        with self.assertRaisesMessage(ValidationError, "no eligible Readers"):
            self.apply()

        other_group = ReadingGroup.objects.create(name="Other Group", slug="other-reward-group")
        other_user = get_user_model().objects.create_user("other-reward-reader")
        other_reader = Membership.objects.create(group=other_group, user=other_user, display_name="Other Reader")
        with self.assertRaisesMessage(ValidationError, "does not belong"):
            self.apply(target_participant=other_reader)

    def test_team_resolution_freezes_only_current_eligible_readers(self):
        self.assign(self.reader)
        leader = self.make_member("qualifying-leader")
        self.enroll(leader)
        self.assign(leader)
        ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=leader,
            team=self.team,
            role=ChallengeStaffAssignment.Role.TEAM_LEADER,
        )
        inactive_enrollment = self.make_member("inactive-enrollment")
        self.enroll(inactive_enrollment, active=False)
        TeamAssignment.objects.bulk_create([TeamAssignment(month=self.month, team=self.team, participant=inactive_enrollment)])
        inactive_member = self.make_member("inactive-member", active=False)
        MonthEnrollment.objects.bulk_create([MonthEnrollment(month=self.month, participant=inactive_member, is_active=True)])
        TeamAssignment.objects.bulk_create([TeamAssignment(month=self.month, team=self.team, participant=inactive_member)])

        application, _ = self.apply(
            target_type=GameRewardApplication.TargetType.TEAM,
            target_participant=None,
            target_team=self.team,
        )
        frozen = set(application.recipients.values_list("participant_id", flat=True))
        self.assertEqual(frozen, {self.reader.pk, leader.pk})

        self.month.enrollments.filter(participant=self.reader).update(is_active=False)
        self.team.assignments.filter(participant=leader).update(ended_at=timezone.now())
        self.month.enrollments.filter(participant=inactive_enrollment).update(is_active=True)
        newcomer = self.make_member("newcomer")
        self.enroll(newcomer)
        self.assign(newcomer)
        self.assertEqual(set(application.recipients.values_list("participant_id", flat=True)), frozen)

    def test_challenge_wide_includes_teamless_reader_and_independently_enrolled_host_but_not_floater(self):
        teamless = self.make_member("teamless")
        self.enroll(teamless)
        self.enroll(self.host)
        floater = self.make_member("malformed-floater")
        self.enroll(floater)
        ChallengeStaffAssignment.objects.bulk_create([
            ChallengeStaffAssignment(
                month=self.month,
                membership=floater,
                role=ChallengeStaffAssignment.Role.FLOATER,
            )
        ])

        application, _ = self.apply(
            target_type=GameRewardApplication.TargetType.CHALLENGE,
            target_participant=None,
        )
        frozen = set(application.recipients.values_list("participant_id", flat=True))
        self.assertIn(teamless.pk, frozen)
        self.assertIn(self.host.pk, frozen)
        self.assertNotIn(floater.pk, frozen)

    def test_zero_recipient_team_and_challenge_targets_fail_atomically(self):
        empty_team = Team.objects.create(month=self.month, name="Empty Team")
        self.month.enrollments.all().update(is_active=False)
        for target_type, target_team in (
            (GameRewardApplication.TargetType.TEAM, empty_team),
            (GameRewardApplication.TargetType.CHALLENGE, None),
        ):
            with self.subTest(target_type=target_type):
                before = (GameRewardApplication.objects.count(), GameRewardRecipient.objects.count(), ModifierProvenance.objects.count(), AuditEvent.objects.count())
                with self.assertRaisesMessage(ValidationError, "no eligible Readers"):
                    self.apply(target_type=target_type, target_participant=None, target_team=target_team)
                self.assertEqual(
                    (GameRewardApplication.objects.count(), GameRewardRecipient.objects.count(), ModifierProvenance.objects.count(), AuditEvent.objects.count()),
                    before,
                )

    def test_wrong_challenge_team_is_rejected(self):
        other_month = ChallengeMonth.objects.create(group=self.group, name="Other Challenge")
        other_team = Team.objects.create(month=other_month, name="Other Team")
        with self.assertRaisesMessage(ValidationError, "does not belong"):
            self.apply(
                target_type=GameRewardApplication.TargetType.TEAM,
                target_participant=None,
                target_team=other_team,
            )

    def test_provenance_is_one_per_recipient_and_preserves_submission_caches_and_existing_sources(self):
        submission = BookSubmission.objects.create(
            month=self.month,
            participant=self.reader,
            title="Preserved Book",
            author="Author",
            book_format=BookSubmission.Format.EBOOK,
            completed_on=date(2026, 8, 10),
            submitted_pages=200,
            approved_pages=200,
            bonus_pages=25,
            final_scored_pages=225,
            status=BookSubmission.Status.APPROVED,
        )
        legacy = ModifierProvenance.objects.create(
            month=self.month,
            participant=self.reader,
            submission=submission,
            source_type=ModifierProvenance.SourceType.LEGACY_MODIFIER,
            source_reference=f"legacy_submission:{submission.pk}",
            source_label="Legacy Modifier",
            amount=25,
            effective_date=submission.completed_on,
        )
        applied_at = datetime(2026, 8, 20, 1, 30, tzinfo=ZoneInfo("UTC"))
        with patch("core.game_rewards.timezone.now", return_value=applied_at):
            application, _ = self.apply()
        recipient = application.recipients.get()
        provenance = recipient.provenance
        self.assertEqual(provenance.source_type, ModifierProvenance.SourceType.GAME_REWARD)
        self.assertIsNone(provenance.submission_id)
        self.assertEqual((provenance.month_id, provenance.participant_id, provenance.amount), (self.month.pk, self.reader.pk, 40))
        self.assertEqual(provenance.source_reference, f"game_reward_recipient:{recipient.pk}")
        self.assertEqual(provenance.effective_date, date(2026, 8, 19))
        self.assertEqual(ModifierProvenance.objects.filter(source_reference=provenance.source_reference).count(), 1)
        submission.refresh_from_db()
        legacy.refresh_from_db()
        self.assertEqual((submission.approved_pages, submission.bonus_pages, submission.final_scored_pages), (200, 25, 225))
        self.assertEqual((legacy.amount, legacy.is_active), (25, True))

    def test_idempotent_retry_reuses_application_effects_and_audit_but_conflict_fails(self):
        key = uuid4()
        first, created = self.apply(idempotency_key=key)
        counts = (GameRewardApplication.objects.count(), GameRewardRecipient.objects.count(), ModifierProvenance.objects.count(), AuditEvent.objects.filter(action="game.reward_applied").count())
        retried, retry_created = self.apply(idempotency_key=key)
        self.assertTrue(created)
        self.assertFalse(retry_created)
        self.assertEqual(retried.pk, first.pk)
        self.assertEqual(
            (GameRewardApplication.objects.count(), GameRewardRecipient.objects.count(), ModifierProvenance.objects.count(), AuditEvent.objects.filter(action="game.reward_applied").count()),
            counts,
        )
        for conflict in ({"amount": 39}, {"reason": "Different reason"}):
            with self.subTest(conflict=conflict), self.assertRaisesMessage(ValidationError, "different Game reward payload"):
                self.apply(idempotency_key=key, **conflict)

    def test_recipient_provenance_failure_rolls_back_everything(self):
        second = self.make_member("second-reader")
        self.enroll(second)
        original_create = ModifierProvenance.objects.create
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValidationError("Simulated provenance failure")
            return original_create(*args, **kwargs)

        with patch.object(ModifierProvenance.objects, "create", side_effect=fail_second):
            with self.assertRaisesMessage(ValidationError, "Simulated provenance failure"):
                self.apply(target_type=GameRewardApplication.TargetType.CHALLENGE, target_participant=None)
        self.assertFalse(GameRewardApplication.objects.exists())
        self.assertFalse(GameRewardRecipient.objects.exists())
        self.assertFalse(ModifierProvenance.objects.filter(source_type=ModifierProvenance.SourceType.GAME_REWARD).exists())
        self.assertFalse(AuditEvent.objects.filter(action="game.reward_applied").exists())

    def test_void_authority_history_idempotence_and_cache_preservation(self):
        second = self.make_member("void-second")
        self.enroll(second)
        submission = BookSubmission.objects.create(
            month=self.month,
            participant=self.reader,
            title="Void Preserved Book",
            author="Author",
            book_format=BookSubmission.Format.EBOOK,
            completed_on=date(2026, 8, 12),
            submitted_pages=100,
            approved_pages=100,
            bonus_pages=10,
            final_scored_pages=110,
            status=BookSubmission.Status.APPROVED,
        )
        application, _ = self.apply(target_type=GameRewardApplication.TargetType.CHALLENGE, target_participant=None)
        recipient_ids = set(application.recipients.values_list("pk", flat=True))
        provenance_ids = set(application.recipients.values_list("provenance_id", flat=True))
        unauthorized = self.make_member("void-unauthorized")
        with self.assertRaises(ValidationError):
            void_game_reward(application=application, actor=unauthorized.user, reason="No authority")
        with self.assertRaisesMessage(ValidationError, "Enter a reason"):
            void_game_reward(application=application, actor=self.host.user, reason="  ")

        voided = void_game_reward(application=application, actor=self.host.user, reason="Applied in error.")
        self.assertTrue(voided.is_voided)
        self.assertEqual(voided.voided_by, self.host.user)
        self.assertEqual(voided.void_reason, "Applied in error.")
        self.assertIsNotNone(voided.voided_at)
        self.assertEqual(set(GameRewardRecipient.objects.filter(application=application).values_list("pk", flat=True)), recipient_ids)
        self.assertEqual(set(ModifierProvenance.objects.filter(pk__in=provenance_ids).values_list("pk", flat=True)), provenance_ids)
        self.assertFalse(ModifierProvenance.objects.filter(pk__in=provenance_ids, is_active=True).exists())
        self.assertEqual(ModifierProvenance.objects.filter(pk__in=provenance_ids, void_reason="Applied in error.").count(), len(provenance_ids))
        self.assertEqual(ModifierProvenance.objects.filter(pk__in=provenance_ids, voided_by=self.host.user).count(), len(provenance_ids))
        self.assertEqual(ModifierProvenance.objects.filter(pk__in=provenance_ids, voided_at=voided.voided_at).count(), len(provenance_ids))
        self.assertEqual(AuditEvent.objects.filter(action="game.reward_voided", object_id=str(application.pk)).count(), 1)
        submission.refresh_from_db()
        self.assertEqual((submission.approved_pages, submission.bonus_pages, submission.final_scored_pages), (100, 10, 110))
        with self.assertRaisesMessage(ValidationError, "already been voided"):
            void_game_reward(application=application, actor=self.host.user, reason="Again")

    def test_platform_owner_may_void(self):
        platform_owner = get_user_model().objects.create_superuser("void-platform", password="test")
        application, _ = self.apply()
        voided = void_game_reward(application=application, actor=platform_owner, reason="Administrative void.")
        self.assertEqual(voided.voided_by, platform_owner)

    def test_successful_apply_creates_one_concise_audit_and_failure_creates_none(self):
        application, _ = self.apply()
        audit = AuditEvent.objects.get(action="game.reward_applied", object_id=str(application.pk))
        self.assertEqual(audit.actor, self.host.user)
        self.assertEqual(audit.group, self.group)
        before = AuditEvent.objects.filter(action="game.reward_applied").count()
        with self.assertRaises(ValidationError):
            self.apply(amount=0)
        self.assertEqual(AuditEvent.objects.filter(action="game.reward_applied").count(), before)
