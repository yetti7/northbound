from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import (
    BookSubmission,
    ChallengeMonth,
    ChallengeSignupAnswer,
    ChallengeSignupQuestion,
    ChallengeStaffAssignment,
    Membership,
    MonthEnrollment,
    ReadingGroup,
    Team,
    TeamAssignment,
    UserProfile,
)
from .reader_planning import historical_reader_planning_data


class HistoricalReaderPlanningDataTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("planning-reader")
        self.new_user = User.objects.create_user("planning-new")
        self.group = ReadingGroup.objects.create(name="Planning Group", slug="planning-group")
        self.other_group = ReadingGroup.objects.create(name="Other Group", slug="other-planning-group")
        self.reader = Membership.objects.create(group=self.group, user=self.user, display_name="History Reader")
        self.new_reader = Membership.objects.create(group=self.group, user=self.new_user, display_name="New Reader")
        self.other_membership = Membership.objects.create(group=self.other_group, user=self.user, display_name="Other History Reader")
        self.current = self.challenge(self.group, "Current", date(2026, 8, 1), ChallengeMonth.Status.UPCOMING)

    def challenge(self, group, name, starts_on, status):
        return ChallengeMonth.objects.create(
            group=group,
            name=name,
            starts_on=starts_on,
            ends_on=date(starts_on.year, starts_on.month, 28),
            status=status,
        )

    def submission(self, month, participant, pages, *, status=BookSubmission.Status.APPROVED, bonus=0, removed=False):
        return BookSubmission.objects.create(
            month=month,
            participant=participant,
            title=f"Book {month.name} {pages}",
            author="Author",
            book_format=BookSubmission.Format.EBOOK,
            completed_on=month.starts_on,
            submitted_pages=pages,
            approved_pages=pages if status == BookSubmission.Status.APPROVED else None,
            bonus_pages=bonus,
            final_scored_pages=pages + bonus if status == BookSubmission.Status.APPROVED else None,
            status=status,
            is_removed=removed,
        )

    def test_metrics_are_group_scoped_base_page_only_and_preserve_inactive_history(self):
        older = self.challenge(self.group, "Older", date(2026, 5, 1), ChallengeMonth.Status.COMPLETED)
        latest = self.challenge(self.group, "Latest", date(2026, 6, 1), ChallengeMonth.Status.COMPLETED)
        active = self.challenge(self.group, "Not Completed", date(2026, 7, 1), ChallengeMonth.Status.ACTIVE)
        other = self.challenge(self.other_group, "Other Group History", date(2026, 7, 1), ChallengeMonth.Status.COMPLETED)
        MonthEnrollment.objects.create(month=older, participant=self.reader)
        MonthEnrollment.objects.create(
            month=latest,
            participant=self.reader,
            is_active=False,
            inactive_reason=MonthEnrollment.InactiveReason.REMOVED,
        )
        MonthEnrollment.objects.create(month=active, participant=self.reader)
        MonthEnrollment.objects.create(month=other, participant=self.other_membership)
        self.submission(older, self.reader, 100, bonus=50)
        self.submission(older, self.reader, 800, removed=True)
        self.submission(latest, self.reader, 300, bonus=75)
        self.submission(active, self.reader, 700)
        self.submission(other, self.other_membership, 900)

        with self.assertNumQueries(2):
            data = historical_reader_planning_data(
                month=self.current,
                participant_ids=[self.reader.pk, self.new_reader.pk],
            )

        self.assertEqual(data[self.reader.pk].completed_challenges, 2)
        self.assertEqual(data[self.reader.pk].average_pages, 200)
        self.assertEqual(data[self.reader.pk].last_challenge_pages, 300)
        self.assertEqual(data[self.new_reader.pk].completed_challenges, 0)
        self.assertIsNone(data[self.new_reader.pk].average_pages)
        self.assertIsNone(data[self.new_reader.pk].last_challenge_pages)

    def test_current_challenge_is_excluded_even_if_completed(self):
        self.current.status = ChallengeMonth.Status.COMPLETED
        self.current._allow_lifecycle_transition = True
        self.current.save(update_fields=["status"])
        MonthEnrollment.objects.create(month=self.current, participant=self.reader)
        self.submission(self.current, self.reader, 500)
        data = historical_reader_planning_data(month=self.current, participant_ids=[self.reader.pk])
        self.assertIsNone(data[self.reader.pk].average_pages)

    def test_completed_participation_without_approved_pages_is_a_real_zero(self):
        history = self.challenge(self.group, "Zero Page History", date(2026, 4, 1), ChallengeMonth.Status.COMPLETED)
        MonthEnrollment.objects.create(month=history, participant=self.reader)
        data = historical_reader_planning_data(month=self.current, participant_ids=[self.reader.pk])
        self.assertEqual(data[self.reader.pk].average_pages, 0)
        self.assertEqual(data[self.reader.pk].last_challenge_pages, 0)


class ChallengeParticipantsPlanningViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("planning-reader")
        self.new_user = User.objects.create_user("planning-new")
        self.group = ReadingGroup.objects.create(name="Planning Group", slug="planning-group")
        self.reader = Membership.objects.create(group=self.group, user=self.user, display_name="History Reader")
        self.new_reader = Membership.objects.create(group=self.group, user=self.new_user, display_name="New Reader")
        self.current = self.challenge(self.group, "Current", date(2026, 8, 1), ChallengeMonth.Status.UPCOMING)
        self.owner_user = User.objects.create_user("planning-owner")
        self.host_user = User.objects.create_user("planning-host")
        self.moderator_user = User.objects.create_user("planning-moderator")
        self.ordinary_user = User.objects.create_user("planning-ordinary")
        self.floater_user = User.objects.create_user("planning-floater")
        self.leader_user = User.objects.create_user("planning-leader")
        self.owner = Membership.objects.create(group=self.group, user=self.owner_user, role=Membership.Role.OWNER, display_name="Owner")
        self.host = Membership.objects.create(group=self.group, user=self.host_user, display_name="Host")
        self.moderator = Membership.objects.create(
            group=self.group,
            user=self.moderator_user,
            role=Membership.Role.MODERATOR,
            display_name="Moderator",
            permission_overrides={"manage_months": True},
        )
        self.ordinary = Membership.objects.create(group=self.group, user=self.ordinary_user, display_name="Ordinary")
        self.floater = Membership.objects.create(group=self.group, user=self.floater_user, display_name="Floater")
        self.leader = Membership.objects.create(group=self.group, user=self.leader_user, display_name="Team Leader")
        ChallengeStaffAssignment.objects.create(month=self.current, membership=self.host, role=ChallengeStaffAssignment.Role.HOST)
        ChallengeStaffAssignment.objects.create(month=self.current, membership=self.floater, role=ChallengeStaffAssignment.Role.FLOATER)
        self.reader_enrollment = MonthEnrollment.objects.create(month=self.current, participant=self.reader)
        self.new_enrollment = MonthEnrollment.objects.create(month=self.current, participant=self.new_reader)
        self.team = Team.objects.create(month=self.current, name="Violet Team")
        TeamAssignment.objects.create(month=self.current, participant=self.reader, team=self.team)
        MonthEnrollment.objects.create(month=self.current, participant=self.leader)
        TeamAssignment.objects.create(month=self.current, participant=self.leader, team=self.team)
        ChallengeStaffAssignment.objects.create(
            month=self.current,
            membership=self.leader,
            team=self.team,
            role=ChallengeStaffAssignment.Role.TEAM_LEADER,
        )
        UserProfile.objects.create(user=self.user, discord_username="history-reader")
        UserProfile.objects.create(user=self.new_user)
        UserProfile.objects.create(user=self.leader_user)
        self.question = ChallengeSignupQuestion.objects.create(
            month=self.current,
            wording="Favorite snack?",
            question_type=ChallengeSignupQuestion.QuestionType.SHORT_TEXT,
            position=1,
        )
        ChallengeSignupAnswer.objects.create(
            enrollment=self.reader_enrollment,
            question=self.question,
            value="Popcorn",
        )
        history = self.challenge(self.group, "History", date(2026, 6, 1), ChallengeMonth.Status.COMPLETED)
        MonthEnrollment.objects.create(month=history, participant=self.reader)
        self.submission(history, self.reader, 250, bonus=100)

    def challenge(self, group, name, starts_on, status):
        return ChallengeMonth.objects.create(
            group=group,
            name=name,
            starts_on=starts_on,
            ends_on=date(starts_on.year, starts_on.month, 28),
            status=status,
        )

    def submission(self, month, participant, pages, *, bonus=0):
        return BookSubmission.objects.create(
            month=month,
            participant=participant,
            title=f"Book {month.name} {pages}",
            author="Author",
            book_format=BookSubmission.Format.EBOOK,
            completed_on=month.starts_on,
            submitted_pages=pages,
            approved_pages=pages,
            bonus_pages=bonus,
            final_scored_pages=pages + bonus,
            status=BookSubmission.Status.APPROVED,
        )

    @property
    def url(self):
        return reverse("month-participant-list", kwargs={"group_slug": self.group.slug, "month_pk": self.current.pk})

    def test_authorized_owner_sees_planning_data_inline_registration_and_profile_link(self):
        self.client.force_login(self.owner_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Avg Pages")
        self.assertContains(response, "history-reader")
        self.assertContains(response, "250")
        self.assertContains(response, "Favorite snack?")
        self.assertContains(response, "Popcorn")
        self.assertContains(response, reverse("participant-detail", kwargs={"group_slug": self.group.slug, "pk": self.reader.pk}))
        self.assertContains(response, "participant-planning-mobile")
        self.assertNotContains(response, "Edit Team")
        self.assertNotContains(response, ">Remove<")

    def test_host_keeps_roster_controls(self):
        self.client.force_login(self.host_user)
        response = self.client.get(self.url)
        self.assertContains(response, "Edit Team")
        self.assertContains(response, "Remove")
        self.assertContains(response, "View Registration")

    def test_delegated_moderator_has_planning_visibility_without_host_controls(self):
        self.client.force_login(self.moderator_user)
        response = self.client.get(self.url)
        self.assertContains(response, "Avg Pages")
        self.assertContains(response, "View Registration")
        self.assertNotContains(response, "Edit Team")
        self.assertNotContains(response, ">Remove<")

    def test_ordinary_reader_and_floater_cannot_see_private_planning_data(self):
        for user in (self.ordinary_user, self.floater_user, self.leader_user):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(self.url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "Avg Pages")
                self.assertNotContains(response, "history-reader")
                self.assertNotContains(response, "Favorite snack?")
                self.assertNotContains(response, "View Registration")
                self.assertNotContains(response, "Edit Team")

    def test_sorting_supports_planning_fields_and_keeps_na_last(self):
        self.client.force_login(self.owner_user)
        response = self.client.get(self.url, {"sort": "average", "direction": "desc"})
        enrollments = response.context["enrollments"]
        self.assertEqual(enrollments[0].participant_id, self.reader.pk)
        self.assertEqual(
            {enrollment.participant_id for enrollment in enrollments[1:]},
            {self.new_reader.pk, self.leader.pk},
        )
        response = self.client.get(self.url, {"sort": "team", "direction": "asc"})
        self.assertEqual(response.context["enrollments"][0].participant_id, self.new_reader.pk)

    def test_existing_registration_detail_authority_is_unchanged(self):
        detail_url = reverse(
            "challenge-registration-detail",
            kwargs={"group_slug": self.group.slug, "month_pk": self.current.pk, "enrollment_pk": self.reader_enrollment.pk},
        )
        self.client.force_login(self.owner_user)
        self.assertEqual(self.client.get(detail_url).status_code, 200)
        self.client.force_login(self.ordinary_user)
        self.assertEqual(self.client.get(detail_url).status_code, 403)
