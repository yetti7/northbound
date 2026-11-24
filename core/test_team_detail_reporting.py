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

    def test_floater_reports_all_teams_without_enrollment_and_preserves_scores(self):
        from .permissions import can_edit_submission, can_manage_months
        user = get_user_model().objects.create_user("report-wide-floater")
        member = self.member(user, "Floater")
        staff = ChallengeStaffAssignment.objects.create(
            month=self.month, membership=member, role=ChallengeStaffAssignment.Role.FLOATER,
        )
        self.add_progress()
        before_books = list(BookSubmission.objects.values_list("pk", "approved_pages", "status"))
        before_credits = list(ModifierProvenance.objects.values_list("pk", "amount", "is_active"))
        edit_access = can_edit_submission(user, self.alpha_submission)
        for team in self.month.teams.all():
            url = reverse("team-detail", args=[self.group.slug, self.month.pk, team.pk])
            self.client.force_login(user)
            response = self.client.get(url, {"bonuses": "0", "sort": "base", "direction": "desc", "show_average": "1"})
            self.assertEqual(response.status_code, 200)
            for flag in ("can_view_reader_scores", "can_view_team_standings", "can_view_planning_data", "can_filter_progress"):
                self.assertTrue(response.context[flag])
            self.assertContains(response, 'type="date" name="from"')
            self.assertContains(response, '<th>Base</th>', html=True)
            self.assertNotContains(response, '<th>Modifier</th>', html=True)
            self.assertEqual(response.context["sort_key"], "base")
            for row in response.context["roster"]:
                self.assertEqual(row.total_pages, row.base_pages)
        response = self.get_as(user, {"from": "2026-08-05", "to": "2026-08-10", "bonuses": "1", "sort": "modifier", "direction": "desc"})
        self.assertEqual(response.context["team_total"], 440)
        self.assertEqual([(row.base_pages, row.modifier_pages, row.total_pages) for row in response.context["roster"]], [(300, 40, 340), (100, 0, 100), (0, 0, 0)])
        response = self.get_as(user, {"themes": "complete", "tbr": "complete", "botm": "complete"})
        self.assertEqual([row.participant_id for row in response.context["roster"]], [self.alpha.pk])
        self.assertFalse(MonthEnrollment.objects.filter(month=self.month, participant=member).exists())
        self.assertFalse(TeamAssignment.objects.filter(month=self.month, participant=member).exists())
        self.assertFalse(can_manage_months(user, self.group))
        self.assertEqual(can_edit_submission(user, self.alpha_submission), edit_access)
        self.assertEqual(self.client.get(reverse("team-create", args=[self.group.slug, self.month.pk])).status_code, 403)
        self.assertEqual(list(BookSubmission.objects.values_list("pk", "approved_pages", "status")), before_books)
        self.assertEqual(list(ModifierProvenance.objects.values_list("pk", "amount", "is_active")), before_credits)
        # Same Group membership is insufficient outside the assigned Challenge.
        other = ChallengeMonth.objects.create(group=self.group, name="Other", status="active", starts_on=self.month.starts_on, ends_on=self.month.ends_on)
        other_team = Team.objects.create(month=other, name="Other Team", color="#123456")
        url = reverse("team-detail", args=[self.group.slug, other.pk, other_team.pk])
        self.assertFalse(self.client.get(url).context["can_view_reader_scores"])
        staff.ended_at = timezone.now(); staff.save(update_fields=["ended_at"])
        self.assertFalse(self.get_as(user).context["can_view_reader_scores"])

    def test_role_controls_follow_existing_data_authority(self):
        from .permissions import can_view_reader_scores, can_view_team_standings
        users = [self.host_user, self.leader_user, self.ordinary_user]
        for role in (Membership.Role.OWNER, Membership.Role.MODERATOR):
            user = get_user_model().objects.create_user("report-" + role)
            membership = self.member(user, role)
            membership.role = role; membership.save(update_fields=["role"])
            users.append(user)
        users.append(get_user_model().objects.create_superuser("report-admin"))
        for visibility in ChallengeMonth.CompetitionVisibility.values:
            self.month.reader_scores_visibility = visibility
            self.month.team_standings_visibility = visibility
            self.month.save(update_fields=["reader_scores_visibility", "team_standings_visibility"])
            for user in users:
                with self.subTest(visibility=visibility, user=user.username):
                    response = self.get_as(user, {"bonuses": "0", "show_average": "1", "show_last": "1", "show_completed": "1", "themes": "complete", "sort": "base"})
                    score = can_view_reader_scores(user, self.month, team=self.team)
                    self.assertEqual(response.context["can_view_reader_scores"], score)
                    self.assertEqual('type="date" name="from"' in response.content.decode(), score)
                    self.assertEqual('type="checkbox" name="show_average"' in response.content.decode(), response.context["can_view_planning_data"])
                    self.assertEqual('<select name="themes"' in response.content.decode(), response.context["can_filter_progress"])
                    self.assertEqual(response.context["sort_key"], "base" if score else "reader")
                    if not can_view_team_standings(user, self.month, team=self.team):
                        self.assertIsNone(response.context["team_total"])

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
        self.assertContains(response, "<small>Base + Bonus</small>", html=True)
        self.assertNotContains(response, "Filtered roster")
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
        for heading in ("Average Pages", "Last Challenge", "Challenges Completed"):
            self.assertNotContains(default, f"<th>{heading}</th>", html=True)
        response = self.get_as(self.host_user, {
            "show_discord": "1", "show_average": "1", "show_last": "1", "show_completed": "1",
        })
        for heading in ("Average Pages", "Last Challenge", "Challenges Completed"):
            self.assertContains(response, f"<th>{heading}</th>", html=True)
            self.assertContains(response, f"<dt>{heading}</dt>", html=True)
        self.assertContains(response, "AlphaDiscord")
        self.assertNotContains(response, 'name="show_discord" value="1" checked')
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
        self.assertContains(response, "<small>Base Pages</small>", html=True)
        self.assertNotContains(response, "Filtered roster")
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
        self.assertFalse(response.context['can_filter_discord'])
        self.assertNotContains(response, 'type="checkbox" name="show_discord"')
        self.assertContains(response, "AlphaDiscord")
        self.assertNotContains(response, 'data-discord="AlphaDiscord"')
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

    def test_scoped_staff_planning_and_discord_do_not_unlock_competition_or_registration(self):
        from .models import ChallengeSignupAnswer, ChallengeSignupQuestion

        floater_user = get_user_model().objects.create_user("report-floater")
        floater = self.member(floater_user, "Floater")
        ChallengeStaffAssignment.objects.create(
            month=self.month, membership=floater, role=ChallengeStaffAssignment.Role.FLOATER,
        )
        other_team = self.month.teams.exclude(pk=self.team.pk).get()
        profile = self.ordinary_user.northbound_profile
        profile.discord_username = "OutsideTeamDiscord"
        profile.save(update_fields=["discord_username"])
        previous = ChallengeMonth.objects.create(
            group=self.group, name="Past", starts_on=date(2026, 7, 1),
            ends_on=date(2026, 7, 31), status=ChallengeMonth.Status.COMPLETED,
        )
        for member, pages in ((self.alpha, 731), (self.ordinary, 947)):
            MonthEnrollment.objects.create(month=previous, participant=member)
            BookSubmission.objects.create(
                month=previous, participant=member, title="Past Reading", author="Writer",
                book_format=BookSubmission.Format.PAPERBACK, completed_on=date(2026, 7, 10),
                submitted_pages=pages, approved_pages=pages, status=BookSubmission.Status.APPROVED,
            )
        enrollment = self.month.enrollments.get(participant=self.alpha)
        question = ChallengeSignupQuestion.objects.create(month=self.month, wording="Private registration question", question_type="short_text")
        ChallengeSignupAnswer.objects.create(enrollment=enrollment, question=question, value="Private registration answer")
        month_args = [self.group.slug, self.month.pk]
        other_url = reverse("team-detail", args=[*month_args, other_team.pk])
        list_url = reverse("month-participant-list", args=month_args)
        detail_url = reverse("challenge-participant-detail", args=[*month_args, self.alpha.pk])
        registration_url = reverse("challenge-registration-detail", args=[*month_args, enrollment.pk])
        query = {"show_discord": "1", "show_average": "1", "from": "2026-08-05", "bonuses": "1"}

        for user, whole_challenge in ((floater_user, True), (self.leader_user, False)):
            with self.subTest(user=user.username):
                response = self.get_as(user, query)
                self.assertContains(response, "AlphaDiscord")
                self.assertContains(response, "731")
                # Sorting preserves the viewer's query in hidden inputs; it must
                # not expose actionable score controls or authoritative scores.
                self.assertEqual(response.context["can_view_reader_scores"], whole_challenge)
                if whole_challenge:
                    self.assertContains(response, 'type="date" name="from"')
                    self.assertContains(response, 'type="checkbox" name="bonuses"')
                    self.assertContains(response, '<th>Base</th>', html=True)
                    self.assertEqual(response.context["team_total"], 660)
                else:
                    self.assertNotContains(response, 'type="date" name="from"')
                    self.assertNotContains(response, '<th>Base</th>', html=True)
                    self.assertIsNone(response.context["team_total"])
                    self.assertFalse(hasattr(response.context["roster"][0], "base_pages"))
                listing = self.client.get(list_url, {"sort": "average"})
                self.assertContains(listing, "AlphaDiscord")
                self.assertContains(listing, "731")
                other = self.client.get(other_url, query)
                if whole_challenge:
                    self.assertContains(listing, "OutsideTeamDiscord")
                    self.assertContains(other, "947")
                else:
                    self.assertContains(listing, "OutsideTeamDiscord")
                    self.assertNotContains(listing, 'data-discord="OutsideTeamDiscord"')
                    self.assertNotContains(listing, "947")
                    self.assertNotContains(other, "Show Columns")
                    self.assertContains(other, "OutsideTeamDiscord")
                    self.assertNotContains(other, 'data-discord="OutsideTeamDiscord"')
                    self.assertEqual(self.client.get(reverse(
                        "challenge-participant-detail", args=[*month_args, self.ordinary.pk],
                    )).status_code, 403)
                detail = self.client.get(detail_url)
                self.assertContains(detail, "AlphaDiscord")
                self.assertContains(detail, "731")
                self.assertNotContains(detail, "Private registration answer")
                self.assertNotContains(listing, reverse("challenge-registration-detail", args=[*month_args, enrollment.pk]))
                self.assertEqual(self.client.get(registration_url).status_code, 403)
                self.assertEqual(self.client.get(reverse("team-create", args=month_args)).status_code, 403)

        response = self.get_as(self.ordinary_user, query)
        self.assertNotContains(response, "Show Columns")
        self.assertContains(response, "AlphaDiscord")
        self.assertNotContains(response, 'data-discord="AlphaDiscord"')
        self.assertNotContains(response, "731")
