from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from .models import (
    BookSubmission,
    ChallengeMonth,
    Membership,
    ModifierProvenance,
    ReadingGroup,
)
from .score_aggregation import challenge_score_totals


class ChallengeScoreAggregationTests(TestCase):
    def setUp(self):
        self.group = ReadingGroup.objects.create(name="Score Group", slug="score-group")
        self.reader = self.member("score-reader", "Reader")
        self.other_reader = self.member("score-other", "Other Reader")
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="Score Challenge",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status=ChallengeMonth.Status.ACTIVE,
        )
        self.other_month = ChallengeMonth.objects.create(
            group=self.group,
            name="Other Score Challenge",
            starts_on=date(2026, 9, 1),
            ends_on=date(2026, 9, 30),
            status=ChallengeMonth.Status.ACTIVE,
        )

    def member(self, username, display_name):
        user = get_user_model().objects.create_user(username)
        return Membership.objects.create(group=self.group, user=user, display_name=display_name)

    def submission(self, participant, *, pages=100, status=BookSubmission.Status.APPROVED, removed=False, completed_on=date(2026, 8, 10), month=None):
        approved = pages if status == BookSubmission.Status.APPROVED else None
        return BookSubmission.objects.create(
            month=month or self.month,
            participant=participant,
            title=f"Book {participant.pk} {completed_on}",
            author="Author",
            book_format=BookSubmission.Format.EBOOK,
            completed_on=completed_on,
            submitted_pages=pages,
            approved_pages=approved,
            status=status,
            is_removed=removed,
        )

    def linked_modifier(self, submission, amount, source_type, reference):
        return ModifierProvenance.objects.create(
            month=submission.month,
            participant=submission.participant,
            submission=submission,
            source_type=source_type,
            source_reference=reference,
            source_label="Linked Modifier",
            amount=amount,
            effective_date=submission.completed_on,
        )

    def standalone(self, participant, amount, reference, *, active=True, effective_date=date(2026, 8, 20), month=None):
        return ModifierProvenance.objects.create(
            month=month or self.month,
            participant=participant,
            submission=None,
            source_type=ModifierProvenance.SourceType.GAME_REWARD,
            source_reference=reference,
            source_label="Game Reward",
            amount=amount,
            effective_date=effective_date,
            is_active=active,
        )

    def score(self, participant=None, **kwargs):
        participant = participant or self.reader
        return challenge_score_totals(
            month=self.month,
            participant_ids=[participant.pk],
            **kwargs,
        )[participant.pk]

    def test_base_only_reader(self):
        self.submission(self.reader, pages=125)
        self.assertEqual(self.score(), {"base_pages": 125, "modifier_pages": 0, "total_pages": 125})

    def test_theme_legacy_and_game_stack_without_cache_double_counting(self):
        submission = self.submission(self.reader, pages=200)
        submission.bonus_pages = 35
        submission.final_scored_pages = 235
        submission.save(update_fields=["bonus_pages", "final_scored_pages"])
        self.linked_modifier(submission, 20, ModifierProvenance.SourceType.THEME_BONUS, "theme_claim:score-test")
        self.linked_modifier(submission, 15, ModifierProvenance.SourceType.LEGACY_MODIFIER, "legacy_submission:score-test")
        self.standalone(self.reader, 40, "game_reward_recipient:score-one")
        self.assertEqual(self.score(), {"base_pages": 200, "modifier_pages": 75, "total_pages": 275})

    def test_multiple_games_sum_and_voided_game_is_excluded(self):
        self.standalone(self.reader, 10, "game_reward_recipient:score-two")
        self.standalone(self.reader, 25, "game_reward_recipient:score-three")
        self.standalone(self.reader, 99, "game_reward_recipient:score-void", active=False)
        self.assertEqual(self.score(), {"base_pages": 0, "modifier_pages": 35, "total_pages": 35})

    def test_pending_and_removed_submissions_exclude_base_and_linked_modifiers_but_not_game(self):
        pending = self.submission(self.reader, pages=80, status=BookSubmission.Status.PENDING)
        removed = self.submission(self.reader, pages=120, removed=True, completed_on=date(2026, 8, 11))
        self.linked_modifier(pending, 10, ModifierProvenance.SourceType.THEME_BONUS, "theme_claim:pending-score")
        self.linked_modifier(removed, 20, ModifierProvenance.SourceType.LEGACY_MODIFIER, "legacy_submission:removed-score")
        self.standalone(self.reader, 30, "game_reward_recipient:score-four")
        self.assertEqual(self.score(), {"base_pages": 0, "modifier_pages": 30, "total_pages": 30})

    def test_reader_challenge_and_participant_filter_isolation(self):
        self.submission(self.reader, pages=100)
        self.submission(self.other_reader, pages=300, completed_on=date(2026, 8, 12))
        self.standalone(self.other_reader, 50, "game_reward_recipient:score-five")
        other_submission = self.submission(self.reader, pages=900, month=self.other_month, completed_on=date(2026, 9, 10))
        self.linked_modifier(other_submission, 90, ModifierProvenance.SourceType.LEGACY_MODIFIER, "legacy_submission:other-month")
        filtered = challenge_score_totals(month=self.month, participant_ids=[self.reader.pk])
        self.assertEqual(set(filtered), {self.reader.pk})
        self.assertEqual(filtered[self.reader.pk]["total_pages"], 100)
        all_scores = challenge_score_totals(month=self.month)
        self.assertEqual(all_scores[self.other_reader.pk]["total_pages"], 350)

    def test_date_bounds_use_submission_completion_for_base_and_effective_date_for_all_modifiers(self):
        early = self.submission(self.reader, pages=100, completed_on=date(2026, 8, 5))
        late = self.submission(self.reader, pages=200, completed_on=date(2026, 8, 25))
        self.linked_modifier(early, 10, ModifierProvenance.SourceType.THEME_BONUS, "theme_claim:early")
        self.linked_modifier(late, 20, ModifierProvenance.SourceType.THEME_BONUS, "theme_claim:late")
        ModifierProvenance.objects.filter(source_reference="theme_claim:early").update(effective_date=date(2026, 8, 22))
        ModifierProvenance.objects.filter(source_reference="theme_claim:late").update(effective_date=date(2026, 8, 8))
        self.standalone(self.reader, 30, "game_reward_recipient:early-game", effective_date=date(2026, 8, 7))
        self.standalone(self.reader, 40, "game_reward_recipient:late-game", effective_date=date(2026, 8, 27))
        self.assertEqual(
            self.score(effective_from=date(2026, 8, 1), effective_to=date(2026, 8, 10)),
            {"base_pages": 100, "modifier_pages": 50, "total_pages": 150},
        )
        with self.assertRaises(ValidationError):
            self.score(effective_from=date(2026, 8, 20), effective_to=date(2026, 8, 10))

    def test_bulk_participant_aggregation_uses_two_queries(self):
        self.submission(self.reader, pages=100)
        self.submission(self.other_reader, pages=200, completed_on=date(2026, 8, 12))
        self.standalone(self.reader, 10, "game_reward_recipient:score-six")
        self.standalone(self.other_reader, 20, "game_reward_recipient:score-seven")
        with self.assertNumQueries(2):
            scores = challenge_score_totals(
                month=self.month,
                participant_ids=[self.reader.pk, self.other_reader.pk],
            )
        self.assertEqual(scores[self.reader.pk]["total_pages"], 110)
        self.assertEqual(scores[self.other_reader.pk]["total_pages"], 220)
