from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    BookSubmission, BotmBook, BotmMatch, ChallengeMonth, ChallengeStaffAssignment,
    Membership, ModifierProvenance, MonthEnrollment, MonthTheme, PersonalTBR,
    PersonalTBRBook, PersonalTBRMatch, ReadingGroup, Team, TeamAssignment,
    ThemeClaim, UserProfile,
)


class TeamDetailReportingTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.host_user = User.objects.create_user("report-host")
        self.leader_user = User.objects.create_user("report-leader")
        self.alpha_user = User.objects.create_user("report-alpha")
        self.beta_user = User.objects.create_user("report-beta")
        self.ordinary_user = User.objects.create_user("report-ordinary")
        self.group = ReadingGroup.objects.create(name="Reporting", slug="reporting")
        self.host = self.member(self.host_user, "Host")
        self.leader = self.member(self.leader_user, "Zoe Leader")
        self.alpha = self.member(self.alpha_user, "Alpha Reader", "AlphaDiscord")
        self.beta = self.member(self.beta_user, "Beta Reader", "BetaDiscord")
        self.ordinary = self.member(self.ordinary_user, "Ordinary")
        self.month = ChallengeMonth.objects.create(
            group=self.group, name="August Reporting", starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31), status=ChallengeMonth.Status.ACTIVE,
            team_standings_visibility=ChallengeMonth.CompetitionVisibility.HOSTS,
            reader_scores_visibility=ChallengeMonth.CompetitionVisibility.HOSTS,
        )
        self.team = Team.objects.create(month=self.month, name="North", color="#123456")
        other_team = Team.objects.create(month=self.month, name="South", color="#654321")
        for member in (self.leader, self.alpha, self.beta):
            enrollment = MonthEnrollment.objects.create(month=self.month, participant=member)
            TeamAssignment.objects.create(month=self.month, team=self.team, participant=member)
            setattr(self, f"{member.user.username.replace('-', '_')}_enrollment", enrollment)
        MonthEnrollment.objects.create(month=self.month, participant=self.ordinary)
        TeamAssignment.objects.create(month=self.month, team=other_team, participant=self.ordinary)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.host, role=ChallengeStaffAssignment.Role.HOST)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.leader, team=self.team, role=ChallengeStaffAssignment.Role.TEAM_LEADER)
        self.alpha_submission = self.submission(self.alpha, "Alpha Early", date(2026, 8, 5), 100)
        self.submission(self.alpha, "Alpha Late", date(2026, 8, 25), 200)
        self.submission(self.beta, "Beta Middle", date(2026, 8, 10), 300)
        ModifierProvenance.objects.create(
            month=self.month, participant=self.alpha, submission=self.alpha_submission,
            source_type=ModifierProvenance.SourceType.THEME_BONUS,
            source_reference="report-theme", source_label="Theme", amount=20,
            effective_date=date(2026, 8, 20),
        )
        ModifierProvenance.objects.create(
            month=self.month, participant=self.beta, source_type=ModifierProvenance.SourceType.GAME_REWARD,
            source_reference="report-game", source_label="Game", amount=40,
            effective_date=date(2026, 8, 10),
        )
        self.url = reverse("team-detail", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.team.pk})

    def member(self, user, name, discord=""):
        UserProfile.objects.create(user=user, discord_username=discord)
        return Membership.objects.create(group=self.group, user=user, display_name=name)

    def submission(self, participant, title, completed_on, pages):
        return BookSubmission.objects.create(
            month=self.month, participant=participant, title=title, author="Author",
            book_format=BookSubmission.Format.EBOOK, completed_on=completed_on,
            submitted_pages=pages, approved_pages=pages, status=BookSubmission.Status.APPROVED,
        )

    def get_as(self, user, params=None):
        self.client.force_login(user)
        return self.client.get(self.url, params or {})

    def add_progress(self):
        theme = MonthTheme.objects.create(bonus_rule="legacy_fixed", month=self.month, name="Theme", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31))
        ThemeClaim.objects.create(submission=self.alpha_submission, theme=theme, status=ThemeClaim.Status.APPROVED, approved_bonus_pages=20)
        enrollment = MonthEnrollment.objects.get(month=self.month, participant=self.alpha)
        tbr = PersonalTBR.objects.create(enrollment=enrollment)
        books = [PersonalTBRBook.objects.create(personal_tbr=tbr, position=i, title_snapshot=f"TBR {i}", author_snapshot="Author") for i in range(1, 7)]
        tbr.confirmed_at = timezone.now(); tbr.save(update_fields=["confirmed_at"])
        for book in books:
            matched_submission = self.submission(self.alpha, f"Matched {book.position}", date(2026, 8, 12 + book.position), 10)
            PersonalTBRMatch.objects.create(
                personal_tbr_book=book, month=self.month, participant=self.alpha,
                submission=matched_submission, method=PersonalTBRMatch.Method.NORMALIZED_TITLE_AUTHOR,
                status=PersonalTBRMatch.Status.CONFIRMED, is_qualifying=True,
                tbr_title_snapshot=book.title_snapshot, tbr_author_snapshot=book.author_snapshot,
                submission_title_snapshot=matched_submission.title, submission_author_snapshot=matched_submission.author,
            )
        botm = BotmBook.objects.create(month=self.month, position=1, title_snapshot="BOTM", author_snapshot="Author", page_count_snapshot=100)
        BotmMatch.objects.create(
            month=self.month, botm_book=botm, participant=self.alpha, submission=self.alpha_submission,
            method=BotmMatch.Method.NORMALIZED_TITLE_AUTHOR, status=BotmMatch.Status.CONFIRMED,
            is_qualifying=True, botm_title_snapshot="BOTM", botm_author_snapshot="Author",
            submission_title_snapshot=self.alpha_submission.title, submission_author_snapshot=self.alpha_submission.author,
        )

    def test_default_columns_range_bonus_and_leader_order(self):
        response = self.get_as(self.host_user)
        self.assertEqual(response.context["effective_from"], self.month.starts_on)
        self.assertEqual(response.context["effective_to"], self.month.ends_on)
        self.assertTrue(response.context["show_bonuses"])
        self.assertEqual([row.participant for row in response.context["roster"]], [self.leader, self.alpha, self.beta])
        for heading in ("Reader", "Role", "Base", "Modifier", "Total", "Themes", "TBR", "BOTM", "Actions"):
            self.assertContains(response, f"<th>{heading}</th>", html=True)
        for heading in ("Discord", "Avg Pages", "Last Challenge", "Completed"):
            self.assertNotContains(response, f"<th>{heading}</th>", html=True)
        self.assertContains(response, "Filtered roster · Base + Modifier")
        self.assertContains(response, 'class="team-detail-desktop roster-table-scroll"')
        self.assertNotContains(response, "Active reporting range")
        self.assertContains(response, ">Remove</a>", count=6)
        self.assertNotContains(response, "Remove from Team")
        self.assertEqual(response.context["team_total"], 660)
        self.assertNotContains(response, "average_min")
        self.assertNotContains(response, "average_max")

    def test_optional_columns_are_query_driven_on_desktop_and_mobile(self):
        historical = ChallengeMonth.objects.create(
            group=self.group, name="July", starts_on=date(2026, 7, 1), ends_on=date(2026, 7, 31),
            status=ChallengeMonth.Status.COMPLETED,
        )
        MonthEnrollment.objects.create(month=historical, participant=self.alpha)
        BookSubmission.objects.create(
            month=historical, participant=self.alpha, title="History", author="Author",
            book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 7, 10),
            submitted_pages=150, approved_pages=150, status=BookSubmission.Status.APPROVED,
        )
        default = self.get_as(self.host_user)
        for heading in ("Discord Username", "Average Pages", "Last Challenge", "Challenges Completed"):
            self.assertNotContains(default, f"<th>{heading}</th>", html=True)
        response = self.get_as(self.host_user, {
            "show_discord": "1", "show_average": "1", "show_last": "1", "show_completed": "1",
        })
        for heading in ("Discord Username", "Average Pages", "Last Challenge", "Challenges Completed"):
            self.assertContains(response, f"<th>{heading}</th>", html=True)
            self.assertContains(response, f"<dt>{heading}</dt>", html=True)
        self.assertContains(response, "AlphaDiscord")
        self.assertContains(response, 'name="show_discord" value="1" checked')
        self.assertContains(response, 'name="show_average" value="1" checked')
        self.assertContains(response, 'name="show_last" value="1" checked')
        self.assertContains(response, 'name="show_completed" value="1" checked')

    def test_date_range_uses_completed_on_for_base_and_effective_date_for_modifier_inclusively(self):
        response = self.get_as(self.host_user, {"from": "2026-08-05", "to": "2026-08-10", "bonuses": "1"})
        alpha = next(row for row in response.context["roster"] if row.participant == self.alpha)
        beta = next(row for row in response.context["roster"] if row.participant == self.beta)
        self.assertEqual((alpha.base_pages, alpha.modifier_pages, alpha.total_pages), (100, 0, 100))
        self.assertEqual((beta.base_pages, beta.modifier_pages, beta.total_pages), (300, 40, 340))
        self.assertEqual(response.context["team_total"], 440)
        self.assertTrue(response.context["date_range_active"])
        self.assertContains(response, "Aug 5–Aug 10")
        self.assertContains(response, "Active reporting range")

    def test_non_date_filter_marks_filters_active_without_date_summary(self):
        response = self.get_as(self.host_user, {"show_average": "1"})
        self.assertTrue(response.context["filters_active"])
        self.assertFalse(response.context["date_range_active"])
        self.assertContains(response, "Filters (Active)")
        self.assertNotContains(response, "Active reporting range")

    def test_show_bonuses_off_is_server_rendered_base_only_without_mutation(self):
        before = list(ModifierProvenance.objects.values_list("pk", "is_active", "amount"))
        response = self.get_as(self.host_user, {"bonuses": "0"})
        self.assertFalse(response.context["show_bonuses"])
        self.assertNotContains(response, "<th>Modifier</th>", html=True)
        self.assertEqual(response.context["team_total"], 600)
        self.assertContains(response, "Filtered roster · Base only")
        self.assertEqual(list(ModifierProvenance.objects.values_list("pk", "is_active", "amount")), before)

    def test_progress_values_and_filters_use_actual_tbr_denominator(self):
        self.add_progress()
        response = self.get_as(self.host_user, {"tbr": "complete"})
        self.assertEqual([row.participant for row in response.context["roster"]], [self.alpha])
        self.assertContains(response, "6 / 6")
        self.assertContains(response, "1 / 1")
        self.assertFalse(hasattr(PersonalTBR.objects.get(enrollment__participant=self.alpha), "completion_award"))
        self.assertEqual(self.get_as(self.host_user, {"tbr": "none"}).context["roster"][0].participant, self.leader)

    def test_theme_and_botm_complete_and_incomplete_filters(self):
        self.add_progress()
        self.assertEqual([row.participant for row in self.get_as(self.host_user, {"themes": "complete"}).context["roster"]], [self.alpha])
        self.assertEqual([row.participant for row in self.get_as(self.host_user, {"themes": "incomplete"}).context["roster"]], [self.leader, self.beta])
        self.assertEqual([row.participant for row in self.get_as(self.host_user, {"botm": "complete"}).context["roster"]], [self.alpha])
        self.assertEqual([row.participant for row in self.get_as(self.host_user, {"botm": "incomplete"}).context["roster"]], [self.leader, self.beta])

    def test_reader_filters_change_displayed_total_but_not_current_reader_count(self):
        self.add_progress()
        response = self.get_as(self.host_user, {"themes": "complete"})
        self.assertEqual([row.participant for row in response.context["roster"]], [self.alpha])
        self.assertEqual(response.context["team_total"], 380)
        self.assertEqual(response.context["full_roster_count"], 3)
        self.assertContains(response, "1 of 3 shown")
        self.assertContains(response, "Reset Filters")
        empty = self.get_as(self.host_user, {"themes": "complete", "tbr": "none"})
        self.assertEqual(empty.context["team_total"], 0)
        self.assertContains(empty, "No Readers match these filters.")

    def test_invalid_dates_fail_safely_with_defaults_and_errors(self):
        malformed = self.get_as(self.host_user, {"from": "not-a-date"})
        self.assertEqual(malformed.status_code, 200)
        self.assertContains(malformed, "Enter a valid From Date.")
        self.assertEqual(malformed.context["effective_from"], self.month.starts_on)
        reversed_range = self.get_as(self.host_user, {"from": "2026-08-20", "to": "2026-08-10"})
        self.assertContains(reversed_range, "From Date cannot be after To Date.")
        self.assertEqual(reversed_range.context["team_total"], 660)

    def test_hidden_score_and_discord_controls_do_not_leak(self):
        response = self.get_as(self.ordinary_user, {"show_discord": "1", "from": "2026-08-05", "bonuses": "1"})
        self.assertNotContains(response, "From Date")
        self.assertNotContains(response, "Discord Username")
        self.assertNotContains(response, "AlphaDiscord")
        self.assertNotContains(response, "<th>Base</th>", html=True)
        self.assertNotContains(response, "<th>Modifier</th>", html=True)
        self.assertNotContains(response, "<th>Total</th>", html=True)
        self.assertNotContains(response, "data-base")
        self.assertContains(response, "Team total hidden")

    def test_team_leader_scope_and_reader_link_remain_intact(self):
        self.month.reader_scores_visibility = ChallengeMonth.CompetitionVisibility.HOSTS_TEAM_LEADERS
        self.month.team_standings_visibility = ChallengeMonth.CompetitionVisibility.HOSTS_TEAM_LEADERS
        self.month.save(update_fields=["reader_scores_visibility", "team_standings_visibility"])
        response = self.get_as(self.leader_user)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("challenge-participant-detail", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.alpha.pk}))
        self.assertContains(response, ">Remove</a>" if response.context["can_remove_roster"] else "Current Roster")
