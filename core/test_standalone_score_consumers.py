from datetime import date, timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .game_rewards import apply_game_reward
from .models import (
    BookSubmission,
    ChallengeMonth,
    ChallengeStaffAssignment,
    Game,
    Membership,
    ModifierProvenance,
    MonthEnrollment,
    ReadingGroup,
    Team,
    TeamAssignment,
    UserProfile,
)
from .reader_planning import historical_reader_planning_data


class StandaloneScoreConsumerTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.host_user = User.objects.create_user("aggregate-host")
        self.reader_user = User.objects.create_user("aggregate-reader")
        self.ordinary_user = User.objects.create_user("aggregate-ordinary")
        self.group = ReadingGroup.objects.create(name="Aggregate Group", slug="aggregate-group")
        self.host = Membership.objects.create(group=self.group, user=self.host_user, display_name="Host")
        self.reader = Membership.objects.create(group=self.group, user=self.reader_user, display_name="Reader")
        self.ordinary = Membership.objects.create(group=self.group, user=self.ordinary_user, display_name="Ordinary")
        for user in (self.host_user, self.reader_user, self.ordinary_user):
            UserProfile.objects.create(user=user)
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="Aggregate Challenge",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status=ChallengeMonth.Status.ACTIVE,
            team_standings_visibility=ChallengeMonth.CompetitionVisibility.HOSTS,
            reader_scores_visibility=ChallengeMonth.CompetitionVisibility.HOSTS,
        )
        ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=self.host,
            role=ChallengeStaffAssignment.Role.HOST,
        )
        MonthEnrollment.objects.create(month=self.month, participant=self.reader)
        self.team = Team.objects.create(month=self.month, name="Aggregate Team")
        TeamAssignment.objects.create(month=self.month, participant=self.reader, team=self.team)
        self.submission = BookSubmission.objects.create(
            month=self.month,
            participant=self.reader,
            title="Submission-specific Book",
            author="Author",
            book_format=BookSubmission.Format.EBOOK,
            completed_on=date(2026, 8, 10),
            submitted_pages=100,
            approved_pages=100,
            bonus_pages=20,
            final_scored_pages=120,
            status=BookSubmission.Status.APPROVED,
        )
        ModifierProvenance.objects.create(
            month=self.month,
            participant=self.reader,
            submission=self.submission,
            source_type=ModifierProvenance.SourceType.THEME_BONUS,
            source_reference="theme_claim:aggregate-consumer",
            source_label="Theme",
            amount=20,
            effective_date=self.submission.completed_on,
        )
        self.game_effect = ModifierProvenance.objects.create(
            month=self.month,
            participant=self.reader,
            source_type=ModifierProvenance.SourceType.GAME_REWARD,
            source_reference="game_reward_recipient:aggregate-consumer",
            source_label="Game",
            amount=30,
            effective_date=date(2026, 8, 15),
        )

    def test_authorized_aggregate_surfaces_include_game_once(self):
        self.client.force_login(self.reader_user)
        stats = self.client.get(reverse("my-stats"))
        self.assertEqual(stats.context["approved_pages"], 150)
        self.assertEqual(stats.context["months"][0].reader_pages, 150)
        self.assertEqual(stats.context["groups"][0].reader_pages, 150)

        month_list = self.client.get(reverse("month-list", kwargs={"group_slug": self.group.slug}))
        card = month_list.context["months"][0]
        self.assertFalse(card.can_view_card_statistics)
        self.assertFalse(hasattr(card, "approved_pages"))
        self.assertFalse(hasattr(card, "approved_books"))

        administrator = get_user_model().objects.create_superuser("aggregate-administrator")
        self.client.force_login(administrator)
        month_list = self.client.get(reverse("month-list", kwargs={"group_slug": self.group.slug}))
        self.assertEqual(month_list.context["months"][0].approved_pages, 150)
        self.client.force_login(self.reader_user)

        participant_list = self.client.get(reverse("participant-list", kwargs={"group_slug": self.group.slug}))
        listed_reader = next(
            participant
            for participant in participant_list.context["participants"]
            if participant.pk == self.reader.pk
        )
        # Accepted UI.2 directory metrics are historical, base-only planning.
        # This fixture's active Challenge is not completed history. Aggregate
        # scores remain asserted on My Stats, Member Detail and Teams below.
        self.assertEqual(listed_reader.planning.completed_challenges, 0)
        self.assertIsNone(listed_reader.planning.average_pages)
        self.assertContains(participant_list, "Challenges Completed")
        self.assertContains(participant_list, "Average Pages")
        self.assertNotContains(participant_list, "Verified Pages")
        participant_detail = self.client.get(
            reverse("participant-detail", kwargs={"group_slug": self.group.slug, "pk": self.reader.pk})
        )
        self.assertEqual(participant_detail.context["approved_pages"], 150)
        current_month = next(
            month for month in participant_detail.context["months"] if month.pk == self.month.pk
        )
        self.assertEqual(current_month.participant_pages, 150)

        self.client.force_login(self.host_user)
        teams = self.client.get(reverse("team-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        self.assertEqual(teams.context["teams"][0].score_total, 150)
        detail = self.client.get(reverse("team-detail", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.team.pk}))
        assignment = detail.context["roster"][0]
        self.assertEqual((assignment.base_pages, assignment.modifier_pages, assignment.total_pages), (100, 50, 150))
        self.assertEqual(detail.context["team_total"], 150)
        challenge = self.client.get(self.month.get_absolute_url())
        self.assertEqual(challenge.context["teams"][0].visible_approved_pages, 150)

    def test_visibility_still_suppresses_team_and_reader_scores(self):
        self.client.force_login(self.ordinary_user)
        team_detail = self.client.get(reverse("team-detail", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.team.pk}))
        self.assertIsNone(team_detail.context["team_total"])
        self.assertFalse(hasattr(team_detail.context["roster"][0], "total_pages"))
        self.assertNotContains(team_detail, 'data-base-pages="100"')
        challenge = self.client.get(self.month.get_absolute_url())
        self.assertNotContains(challenge, ">150<")

    def test_submission_and_review_displays_remain_submission_specific(self):
        self.client.force_login(self.reader_user)
        challenge = self.client.get(self.month.get_absolute_url())
        self.assertContains(challenge, "Submission-specific Book")
        self.assertContains(challenge, ">20<")
        self.assertContains(challenge, ">120<")
        self.assertNotContains(challenge, ">50<")

        self.client.force_login(self.host_user)
        review = self.client.get(reverse("submission-review", kwargs={
            "group_slug": self.group.slug,
            "month_pk": self.month.pk,
            "pk": self.submission.pk,
        }))
        # UI.3 names all awarded score components modifiers, with a bold total.
        self.assertContains(review, "<dd>100 base + 20 modifier = <strong>120 total</strong></dd>", html=True)

    def test_historical_planning_remains_base_only(self):
        historical = ChallengeMonth.objects.create(
            group=self.group,
            name="Historical Aggregate",
            starts_on=date(2026, 7, 1),
            ends_on=date(2026, 7, 31),
            status=ChallengeMonth.Status.COMPLETED,
        )
        MonthEnrollment.objects.create(month=historical, participant=self.reader)
        book = BookSubmission.objects.create(
            month=historical,
            participant=self.reader,
            title="Historical Book",
            author="Author",
            book_format=BookSubmission.Format.EBOOK,
            completed_on=date(2026, 7, 10),
            submitted_pages=200,
            approved_pages=200,
            bonus_pages=25,
            final_scored_pages=225,
            status=BookSubmission.Status.APPROVED,
        )
        ModifierProvenance.objects.create(
            month=historical,
            participant=self.reader,
            submission=book,
            source_type=ModifierProvenance.SourceType.LEGACY_MODIFIER,
            source_reference="legacy_submission:historical-aggregate",
            source_label="Legacy",
            amount=25,
            effective_date=book.completed_on,
        )
        ModifierProvenance.objects.create(
            month=historical,
            participant=self.reader,
            source_type=ModifierProvenance.SourceType.GAME_REWARD,
            source_reference="game_reward_recipient:historical-aggregate",
            source_label="Game",
            amount=50,
            effective_date=date(2026, 7, 15),
        )
        planning = historical_reader_planning_data(month=self.month, participant_ids=[self.reader.pk])[self.reader.pk]
        self.assertEqual(planning.average_pages, 200)
        self.assertEqual(planning.last_challenge_pages, 200)

    def test_current_team_attribution_moves_score_without_changing_reward_recipient(self):
        self.month.games_enabled = True
        self.month.save(update_fields=["games_enabled"])
        now = timezone.now()
        game = Game.objects.create(
            month=self.month,
            name="Current Team Reward",
            starts_at=now - timedelta(days=1),
            ends_at=now + timedelta(days=1),
            advertised_bonus_pages=10,
        )
        application, _ = apply_game_reward(
            game=game,
            actor=self.host_user,
            target_type="reader",
            target_participant=self.reader,
            amount=10,
            reason="Current team attribution test.",
            idempotency_key=uuid4(),
        )
        frozen_ids = list(application.recipients.values_list("participant_id", flat=True))
        old_assignment = self.team.assignments.get(participant=self.reader, ended_at__isnull=True)
        old_assignment.ended_at = now
        old_assignment.save(update_fields=["ended_at"])
        new_team = Team.objects.create(month=self.month, name="New Aggregate Team")
        TeamAssignment.objects.create(month=self.month, participant=self.reader, team=new_team)

        self.client.force_login(self.host_user)
        response = self.client.get(reverse("team-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        totals = {team.name: team.score_total for team in response.context["teams"]}
        self.assertEqual(totals[self.team.name], 0)
        self.assertEqual(totals[new_team.name], 160)
        self.assertEqual(list(application.recipients.values_list("participant_id", flat=True)), frozen_ids)

    def test_full_score_follows_assignment_and_survives_departure_and_reactivation(self):
        from .participation import activate_participation, assign_participant_to_team, deactivate_participation
        from .score_aggregation import challenge_score_totals, challenge_team_totals

        BookSubmission.objects.filter(pk=self.submission.pk).update(approved_pages=450)
        month = self.month
        enrollment = MonthEnrollment.objects.get(month=month, participant=self.reader)
        team_b = Team.objects.create(month=month, name="Team B")
        team_c = Team.objects.create(month=month, name="Team C")
        ids = [self.team.pk, team_b.pk, team_c.pk]

        def team_scores():
            return challenge_team_totals(month=month, team_ids=ids)

        self.assertEqual(team_scores(), dict(zip(ids, [500, 0, 0])))
        assign_participant_to_team(month=month, participant=self.reader, team=team_b, actor=self.host_user)
        self.assertEqual(team_scores(), dict(zip(ids, [0, 500, 0])))
        BookSubmission.objects.create(
            month=month, participant=self.reader, title="Second book", author="Writer",
            book_format="ebook", completed_on=self.submission.completed_on,
            submitted_pages=300, approved_pages=300, status="approved",
        )
        self.assertEqual(team_scores(), dict(zip(ids, [0, 800, 0])))
        deactivate_participation(enrollment=enrollment, actor=self.reader.user, reason="withdrawn")
        self.assertEqual(team_scores(), dict(zip(ids, [0, 800, 0])))
        activate_participation(month=month, participant=self.reader, actor=self.host_user, origin="staff")
        enrollment.refresh_from_db()
        self.assertTrue(enrollment.awaiting_scoring_team)
        self.assertEqual(enrollment.credited_team_id, team_b.pk)
        self.assertEqual(team_scores(), dict(zip(ids, [0, 800, 0])))
        self.client.force_login(self.reader.user)
        submit = reverse("submission-create", args=[self.group.slug, month.pk])
        self.assertRedirects(self.client.get(submit), month.get_absolute_url(), fetch_redirect_response=False)
        assign_participant_to_team(month=month, participant=self.reader, team=team_c, actor=self.host_user)
        enrollment.refresh_from_db()
        self.assertFalse(enrollment.awaiting_scoring_team)
        self.assertEqual(team_scores(), dict(zip(ids, [0, 0, 800])))
        self.assertEqual(challenge_score_totals(month=month)[self.reader.pk]["total_pages"], 800)
        self.assertEqual(team_c.approved_pages, 800)

    def test_legacy_finalized_totals_do_not_infer_ended_assignments(self):
        from .score_aggregation import challenge_team_totals

        for status in ("completed", "archived"):
            with self.subTest(status=status):
                ChallengeMonth.objects.filter(pk=self.month.pk).update(status=status)
                self.month.refresh_from_db()
                self.assertEqual(challenge_team_totals(month=self.month, team_ids=[self.team.pk]), {self.team.pk: 150})
        TeamAssignment.objects.filter(month=self.month, participant=self.reader).update(ended_at=timezone.now())
        MonthEnrollment.objects.filter(month=self.month, participant=self.reader).update(is_active=False)
        self.assertEqual(challenge_team_totals(month=self.month, team_ids=[self.team.pk]), {self.team.pk: 0})

    def test_credited_team_requires_same_challenge_and_prevents_deletion(self):
        from django.core.exceptions import ValidationError
        from django.db.models.deletion import RestrictedError

        other = ChallengeMonth.objects.create(
            group=self.group, name="Other", starts_on=self.month.starts_on, ends_on=self.month.ends_on,
        )
        foreign_team = Team.objects.create(month=other, name="Foreign")
        enrollment = MonthEnrollment.objects.get(month=self.month, participant=self.reader)
        enrollment.credited_team = foreign_team
        with self.assertRaises(ValidationError):
            enrollment.save(update_fields=["credited_team"])
        team = Team.objects.create(month=self.month, name="Historical credit")
        enrollment.credited_team = team
        enrollment.save(update_fields=["credited_team"])
        with self.assertRaises(RestrictedError):
            team.delete()
