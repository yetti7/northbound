from datetime import date, timedelta
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.utils import timezone

from .models import (
    BookSubmission,
    ChallengeMonth,
    Game,
    GameRewardApplication,
    GameRewardRecipient,
    Membership,
    ModifierProvenance,
    ReadingGroup,
    Team,
)


class GamesSchemaTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user("games-host")
        self.reader_user = get_user_model().objects.create_user("games-reader")
        self.group = ReadingGroup.objects.create(name="Games Group", slug="games-group")
        self.reader = Membership.objects.create(
            group=self.group,
            user=self.reader_user,
            role=Membership.Role.MEMBER,
            display_name="Games Reader",
        )
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="Games Challenge",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
        )
        self.team = Team.objects.create(month=self.month, name="Blue Team")
        self.starts_at = timezone.now()
        self.game = Game.objects.create(
            month=self.month,
            name="Reading Sprint",
            starts_at=self.starts_at,
            ends_at=self.starts_at + timedelta(days=2),
            advertised_bonus_pages=50,
        )

    def application(self, **overrides):
        values = {
            "game": self.game,
            "amount": 50,
            "target_type": GameRewardApplication.TargetType.READER,
            "target_participant": self.reader,
            "target_label": self.reader.display_name,
            "game_name_snapshot": self.game.name,
            "advertised_bonus_pages_snapshot": self.game.advertised_bonus_pages,
            "reason": "Completed the sprint.",
            "applied_by": self.actor,
        }
        values.update(overrides)
        return GameRewardApplication(**values)

    def test_games_default_disabled_and_game_is_challenge_bound(self):
        self.assertFalse(self.month.games_enabled)
        self.assertEqual(self.game.month, self.month)
        self.assertEqual(list(self.month.games.all()), [self.game])

    def test_game_validation_requires_name_positive_amount_and_ordered_times(self):
        invalid_values = (
            {"name": "   "},
            {"advertised_bonus_pages": 0},
            {"ends_at": self.starts_at},
        )
        for overrides in invalid_values:
            values = {
                "month": self.month,
                "name": "Valid Game",
                "starts_at": self.starts_at,
                "ends_at": self.starts_at + timedelta(days=1),
                "advertised_bonus_pages": 10,
            }
            values.update(overrides)
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                Game(**values).full_clean()

    def test_game_can_be_retired_without_losing_identity(self):
        game_id = self.game.pk
        self.game.is_active = False
        self.game.save(update_fields=["is_active"])
        self.assertEqual(Game.objects.get(pk=game_id).name, "Reading Sprint")
        self.assertFalse(Game.objects.get(pk=game_id).is_active)

    def test_application_supports_reader_team_and_challenge_targets(self):
        reader = self.application()
        reader.full_clean()
        team = self.application(
            target_type=GameRewardApplication.TargetType.TEAM,
            target_participant=None,
            target_team=self.team,
            target_label=self.team.name,
        )
        team.full_clean()
        challenge = self.application(
            target_type=GameRewardApplication.TargetType.CHALLENGE,
            target_participant=None,
            target_label=self.month.name,
        )
        challenge.full_clean()

    def test_application_rejects_nonpositive_or_excess_amount_and_blank_reason(self):
        for values, field in (
            ({"amount": 0}, "amount"),
            ({"amount": 51}, "amount"),
            ({"reason": "   "}, "reason"),
        ):
            with self.subTest(values=values):
                with self.assertRaises(ValidationError) as raised:
                    self.application(**values).full_clean()
                self.assertIn(field, raised.exception.message_dict)

    def test_application_idempotency_key_is_unique(self):
        token = uuid.uuid4()
        first = self.application(idempotency_key=token)
        first.full_clean()
        first.save()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.application(idempotency_key=token).save()

    def test_void_state_requires_actor_timestamp_and_reason(self):
        invalid = self.application(is_voided=True, voided_at=timezone.now(), void_reason="Correction")
        with self.assertRaises(ValidationError):
            invalid.full_clean()
        valid = self.application(
            is_voided=True,
            voided_by=self.actor,
            voided_at=timezone.now(),
            void_reason="Applied to the wrong Reader.",
        )
        valid.full_clean()

    def test_recipient_is_unique_per_application_but_reusable_across_applications(self):
        first = self.application()
        first.full_clean()
        first.save()
        second = self.application(idempotency_key=uuid.uuid4())
        second.full_clean()
        second.save()
        GameRewardRecipient.objects.create(application=first, participant=self.reader)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                GameRewardRecipient.objects.create(application=first, participant=self.reader)
        GameRewardRecipient.objects.create(application=second, participant=self.reader)
        self.assertEqual(self.reader.game_reward_recipients.count(), 2)

    def test_recipient_can_have_one_matching_submission_free_game_provenance(self):
        application = self.application()
        application.full_clean()
        application.save()
        recipient = GameRewardRecipient.objects.create(application=application, participant=self.reader)
        provenance = ModifierProvenance.objects.create(
            month=self.month,
            participant=self.reader,
            submission=None,
            source_type=ModifierProvenance.SourceType.GAME_REWARD,
            source_reference=f"game_reward_recipient:{recipient.pk}",
            source_label=self.game.name,
            source_context=application.reason,
            amount=application.amount,
            effective_date=timezone.localdate(application.applied_at),
            applied_by=self.actor,
            applied_at=application.applied_at,
        )
        recipient.provenance = provenance
        recipient.full_clean()
        recipient.save(update_fields=["provenance"])
        self.assertIsNone(provenance.submission)
        self.assertEqual(recipient.provenance.source_type, ModifierProvenance.SourceType.GAME_REWARD)

    def test_historical_application_protects_game_application_recipient_and_reader(self):
        application = self.application()
        application.full_clean()
        application.save()
        GameRewardRecipient.objects.create(application=application, participant=self.reader)
        for instance in (self.game, application, self.reader):
            with self.subTest(instance=type(instance).__name__):
                with self.assertRaises(ProtectedError):
                    instance.delete()

    def test_game_schema_does_not_change_submission_scores(self):
        submission = BookSubmission.objects.create(
            month=self.month,
            participant=self.reader,
            title="Existing Book",
            author="Author",
            book_format=BookSubmission.Format.EBOOK,
            completed_on=date(2026, 8, 10),
            submitted_pages=200,
            approved_pages=200,
            bonus_pages=25,
            final_scored_pages=225,
            status=BookSubmission.Status.APPROVED,
        )
        self.application().full_clean()
        submission.refresh_from_db()
        self.assertEqual((submission.approved_pages, submission.bonus_pages, submission.final_scored_pages), (200, 25, 225))
