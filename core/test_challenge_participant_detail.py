from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import (
    BookSubmission, BotmBook, BotmMatch, CatalogBook, CatalogEdition, ChallengeMonth, ChallengeSignupAnswer,
    ChallengeSignupQuestion, ChallengeStaffAssignment, Membership, ModifierProvenance,
    MonthEnrollment, MonthTheme, PersonalTBR, PersonalTBRBook, PersonalTBRMatch,
    ReadingGroup, Team, TeamAssignment, ThemeClaim, UserProfile,
)


class ChallengeParticipantDetailTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.users = {name: (User.objects.create_superuser(f"cpd-{name}") if name == "platform" else User.objects.create_user(f"cpd-{name}")) for name in ("host", "floater", "leader", "reader", "other", "outsider", "platform")}
        self.group = ReadingGroup.objects.create(name="Participant Detail", slug="participant-detail-test")
        self.members = {name: Membership.objects.create(group=self.group, user=self.users[name], display_name=name.title()) for name in ("host", "floater", "leader", "reader", "other", "outsider")}
        for name in self.members:
            UserProfile.objects.create(user=self.users[name])
        self.month = ChallengeMonth.objects.create(
            group=self.group, name="Stories Under Stars", starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31), status=ChallengeMonth.Status.ACTIVE,
            reader_scores_visibility=ChallengeMonth.CompetitionVisibility.HOSTS,
        )
        self.north = Team.objects.create(month=self.month, name="North", color="#112233")
        self.south = Team.objects.create(month=self.month, name="South", color="#334455")
        for name, team in (("leader", self.north), ("reader", self.north), ("other", self.south)):
            enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.members[name])
            TeamAssignment.objects.create(month=self.month, participant=self.members[name], team=team)
            setattr(self, f"{name}_enrollment", enrollment)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.members["host"], role=ChallengeStaffAssignment.Role.HOST)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.members["floater"], role=ChallengeStaffAssignment.Role.FLOATER)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.members["leader"], team=self.north, role=ChallengeStaffAssignment.Role.TEAM_LEADER)
        self.question = ChallengeSignupQuestion.objects.create(month=self.month, wording="Private registration answer", question_type=ChallengeSignupQuestion.QuestionType.SHORT_TEXT, position=1)
        ChallengeSignupAnswer.objects.create(enrollment=self.reader_enrollment, question=self.question, value="secret response")
        self.catalog_book = CatalogBook.objects.create(
            provider="hardcover", provider_book_id="participant-work",
            title="Reader Book", author="Author",
            source_url="https://hardcover.app/books/reader-book?token=private-catalog-material",
        )
        self.catalog_edition = CatalogEdition.objects.create(
            book=self.catalog_book, provider="hardcover", provider_edition_id="participant-edition",
            source_url="https://hardcover.app/books/reader-book/editions/participant-edition?token=private-catalog-material",
        )
        self.submission = BookSubmission.objects.create(
            month=self.month, participant=self.members["reader"], title="Reader Book", author="Author",
            catalog_book=self.catalog_book, catalog_edition=self.catalog_edition,
            book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 8, 10),
            submitted_pages=100, approved_pages=100, status=BookSubmission.Status.APPROVED,
        )
        ModifierProvenance.objects.create(
            month=self.month, participant=self.members["reader"], submission=self.submission,
            source_type=ModifierProvenance.SourceType.GAME_REWARD,
            source_reference="cpd-game", source_label="Game reward", amount=25,
            effective_date=date(2026, 8, 10), is_active=True,
        )
        ModifierProvenance.objects.create(
            month=self.month, participant=self.members["reader"],
            source_type=ModifierProvenance.SourceType.LEGACY_MODIFIER,
            source_reference="cpd-void", source_label="Voided reward", amount=999,
            effective_date=date(2026, 8, 10), is_active=False,
        )
        self.url = self.detail_url(self.members["reader"])

    def detail_url(self, member):
        return reverse("challenge-participant-detail", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": member.pk})

    def get_as(self, name, url=None):
        self.client.force_login(self.users[name])
        return self.client.get(url or self.url)

    def test_participant_and_team_links_share_challenge_destination(self):
        expected = self.detail_url(self.members["reader"])
        participant_list = self.get_as("host", reverse("month-participant-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        team = self.get_as("host", reverse("team-detail", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.north.pk}))
        self.assertContains(participant_list, expected)
        self.assertContains(team, expected)
        self.assertNotContains(participant_list, reverse("participant-detail", kwargs={"group_slug": self.group.slug, "pk": self.members["reader"].pk}))

    def test_participant_list_keeps_compact_registration_state_and_planning_actions(self):
        response = self.get_as("host", reverse("month-participant-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        self.assertContains(response, "<th>Registration</th>", html=True)
        self.assertContains(response, '<span class="pill status-badge status-success">Active</span>', html=True)
        self.assertContains(response, 'aria-label="Challenge participant planning table"')
        self.assertContains(response, "Edit Team")
        self.assertContains(response, "Remove")
        self.assertNotContains(response, "View Registration")
        self.assertNotContains(response, "Open registration detail")
        self.assertNotContains(response, "Private registration answer")
        self.assertNotContains(response, "secret response")

    def test_host_floater_team_leader_and_platform_authority(self):
        self.assertEqual(self.get_as("host").status_code, 200)
        self.assertEqual(self.get_as("floater").status_code, 200)
        self.assertEqual(self.get_as("leader").status_code, 200)
        self.assertEqual(self.get_as("platform").status_code, 200)
        self.assertEqual(self.get_as("leader", self.detail_url(self.members["other"])).status_code, 403)
        self.assertEqual(self.get_as("outsider").status_code, 403)

    def test_identity_registration_discord_and_score_privacy_are_independent(self):
        profile = self.members["reader"].user.northbound_profile
        profile.discord_username = "private.reader"; profile.save(update_fields=["discord_username"])
        host = self.get_as("host")
        self.assertContains(host, "cpd-reader — Stories Under Stars")
        self.assertContains(host, "North")
        self.assertContains(host, "secret response")
        self.assertContains(host, "private.reader")
        self.assertContains(host, "<dt>Base</dt>", html=True)
        self.assertContains(host, "<dd>100</dd>", html=True)
        self.assertContains(host, "<dd>25</dd>", html=True)
        floater = self.get_as("floater")
        self.assertNotContains(floater, "secret response")
        self.assertNotContains(floater, "Private registration answer")
        self.assertContains(floater, "private.reader")
        self.assertNotContains(floater, "<dt>Base</dt>", html=True)
        self.assertNotContains(floater, ">100<")
        self.assertNotContains(floater, ">25<")
        self.assertNotContains(floater, "Modifiers / Rewards")

    def test_submissions_and_active_provenance_are_reader_and_challenge_scoped(self):
        other_submission = BookSubmission.objects.create(
            month=self.month, participant=self.members["other"], title="Other Book", author="Author",
            book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 8, 11),
            submitted_pages=200, approved_pages=200, status=BookSubmission.Status.APPROVED,
        )
        response = self.get_as("host")
        self.assertContains(response, "Reader Book")
        self.assertContains(response, reverse("submission-review", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.submission.pk}))
        self.assertNotContains(response, other_submission.title)
        self.assertContains(response, "Game reward")
        self.assertNotContains(response, "Voided reward")

    def test_submission_keeps_internal_detail_and_adds_safe_hardcover_action(self):
        response = self.get_as("host")
        internal_url = reverse("submission-review", kwargs={
            "group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.submission.pk,
        })
        hardcover_url = "https://hardcover.app/books/reader-book/editions/participant-edition"
        self.assertContains(response, internal_url)
        self.assertContains(response, hardcover_url)
        self.assertContains(response, "View on Hardcover")
        self.assertNotContains(response, "private-catalog-material")
        challenge = self.get_as("reader", self.month.get_absolute_url())
        self.assertContains(challenge, hardcover_url)
        review = self.get_as("host", internal_url)
        self.assertContains(review, hardcover_url)
        self.assertContains(review, "View on Hardcover")
        pending = BookSubmission.objects.create(
            month=self.month, participant=self.members["reader"], title="Pending Catalog Book", author="Author",
            catalog_book=self.catalog_book, catalog_edition=self.catalog_edition,
            book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 8, 12),
            submitted_pages=120, status=BookSubmission.Status.PENDING,
        )
        queue = self.get_as("host", reverse("review-queue", kwargs={
            "group_slug": self.group.slug, "month_pk": self.month.pk,
        }))
        self.assertContains(queue, reverse("submission-review", kwargs={
            "group_slug": self.group.slug, "month_pk": self.month.pk, "pk": pending.pk,
        }))
        self.assertContains(queue, hardcover_url)

    def test_progress_uses_zero_actual_denominators_without_creating_data(self):
        response = self.get_as("host")
        self.assertContains(response, "<dt>Themes</dt><dd>0 / 0</dd>", html=True)
        self.assertContains(response, "<dt>Personal TBR</dt><dd>Not registered</dd>", html=True)
        self.assertContains(response, "<dt>BOTM</dt><dd>0 / 0</dd>", html=True)
        self.assertEqual(response.context["participant"], self.members["reader"])
        self.assertEqual(response.context["month"], self.month)

    def test_tbr_botm_and_theme_progress_use_durable_qualifying_truth(self):
        tbr = PersonalTBR.objects.create(enrollment=self.reader_enrollment)
        tbr_books = [PersonalTBRBook.objects.create(
            personal_tbr=tbr, position=position, title_snapshot=f"TBR {position}",
            author_snapshot="TBR Author", page_count_snapshot=100 + position,
        ) for position in (1, 2)]
        from django.utils import timezone
        tbr.confirmed_at = timezone.now(); tbr.save(update_fields=["confirmed_at"])
        tbr_pending_submission = BookSubmission.objects.create(
            month=self.month, participant=self.members["reader"], title="TBR 2", author="TBR Author",
            book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 8, 12),
            submitted_pages=102, status=BookSubmission.Status.PENDING,
        )
        PersonalTBRMatch.objects.create(
            personal_tbr_book=tbr_books[0], month=self.month, participant=self.members["reader"],
            submission=self.submission, method=PersonalTBRMatch.Method.NORMALIZED_TITLE_AUTHOR,
            status=PersonalTBRMatch.Status.CONFIRMED, is_qualifying=True,
            tbr_title_snapshot="TBR 1", tbr_author_snapshot="TBR Author",
            submission_title_snapshot=self.submission.title, submission_author_snapshot=self.submission.author,
        )
        PersonalTBRMatch.objects.create(
            personal_tbr_book=tbr_books[1], month=self.month, participant=self.members["reader"],
            submission=tbr_pending_submission, method=PersonalTBRMatch.Method.NORMALIZED_TITLE_AUTHOR,
            status=PersonalTBRMatch.Status.PENDING_REVIEW, is_qualifying=False,
            tbr_title_snapshot="TBR 2", tbr_author_snapshot="TBR Author",
            submission_title_snapshot=tbr_pending_submission.title, submission_author_snapshot=tbr_pending_submission.author,
        )
        botm = BotmBook.objects.create(
            month=self.month, position=1, title_snapshot="BOTM One", author_snapshot="BOTM Author",
            page_count_snapshot=300,
        )
        BotmMatch.objects.create(
            month=self.month, botm_book=botm, participant=self.members["reader"], submission=self.submission,
            method=BotmMatch.Method.NORMALIZED_TITLE_AUTHOR, status=BotmMatch.Status.CONFIRMED,
            is_qualifying=True, botm_title_snapshot="BOTM One", botm_author_snapshot="BOTM Author",
            submission_title_snapshot=self.submission.title, submission_author_snapshot=self.submission.author,
        )
        theme = MonthTheme.objects.create(bonus_rule="legacy_fixed",
            month=self.month, name="Night Sky", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31),
        )
        ThemeClaim.objects.create(
            submission=self.submission, theme=theme, status=ThemeClaim.Status.APPROVED,
            approved_bonus_pages=50,
        )
        response = self.get_as("host")
        self.assertContains(response, "<dt>Themes</dt><dd>1 / 1</dd>", html=True)
        self.assertContains(response, "<dt>Personal TBR</dt><dd>1 / 2</dd>", html=True)
        self.assertContains(response, "<dt>BOTM</dt><dd>1 / 1</dd>", html=True)
        self.assertContains(response, "TBR 1")
        self.assertContains(response, "Pending")
        self.assertContains(response, "BOTM One")
        self.assertContains(response, "Night Sky")
        self.assertEqual(response.content.decode().count("book-completion-overlay"), 2)

    def test_permanent_group_member_profile_remains_separate(self):
        response = self.get_as("reader", reverse("participant-detail", kwargs={"group_slug": self.group.slug, "pk": self.members["reader"].pk}))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/participant_detail.html")
        self.assertNotContains(response, "Challenge Progress")

    def test_participant_search_indexes_visible_names_without_leaking_private_discord(self):
        UserProfile.objects.filter(user=self.users["reader"]).update(discord_username="NorthVisible")
        UserProfile.objects.filter(user=self.users["other"]).update(discord_username="SouthPrivate")
        self.client.force_login(self.users["leader"])
        url = reverse("month-participant-list", args=[self.group.slug, self.month.pk])
        response = self.client.get(url)
        self.assertContains(response, "Search User")
        self.assertContains(response, 'placeholder="Username or Discord name"')
        self.assertContains(response, 'data-username="cpd-reader"', count=2)
        self.assertContains(response, 'data-discord="NorthVisible"', count=2)
        self.assertNotContains(response, "SouthPrivate")
        self.assertContains(response, "participant_search.js")

    def test_team_books_are_independent_of_scores_and_follow_current_assignment(self):
        from .participation import assign_participant_to_team

        self.submission.review_notes = "PRIVATE REVIEW DATA"
        self.submission.save(update_fields=["review_notes"])
        north_url = reverse("team-detail", args=[self.group.slug, self.month.pk, self.north.pk])
        north_book = reverse("team-book-detail", args=[self.group.slug, self.month.pk, self.north.pk, self.submission.pk])
        south_book = reverse("team-book-detail", args=[self.group.slug, self.month.pk, self.south.pk, self.submission.pk])
        BookSubmission.objects.create(
            month=self.month, participant=self.members["reader"], title="Pending teammate book", author="Author",
            book_format="ebook", completed_on=date(2026, 8, 10), submitted_pages=30,
        )
        BookSubmission.objects.create(
            month=self.month, participant=self.members["reader"], title="Rejected teammate book", author="Author",
            book_format="ebook", completed_on=date(2026, 8, 10), submitted_pages=30, status="rejected",
        )
        removed = BookSubmission.objects.create(
            month=self.month, participant=self.members["reader"], title="Removed teammate book", author="Author",
            book_format="ebook", completed_on=date(2026, 8, 10), submitted_pages=30,
        )
        BookSubmission.objects.filter(pk=removed.pk).update(is_removed=True)
        self.client.force_login(self.users["leader"])
        response = self.client.get(north_url, {"sort": "total", "from": "2026-08-03"})
        self.assertContains(response, "Team Books")
        self.assertContains(response, "Pending teammate book")
        self.assertNotContains(response, "Rejected teammate book")
        self.assertNotContains(response, "Removed teammate book")
        self.assertFalse(response.context["can_view_reader_scores"])
        response = self.client.get(north_book)
        self.assertContains(response, self.submission.title)
        self.assertNotContains(response, "PRIVATE REVIEW DATA")
        self.assertNotContains(response, "scored pages")
        self.assertEqual(self.client.get(south_book).status_code, 403)
        self.client.force_login(self.users["other"])
        self.assertEqual(self.client.get(north_book).status_code, 403)
        assign_participant_to_team(month=self.month, participant=self.members["reader"],
                                   team=self.south, actor=self.users["floater"])
        self.assertEqual(self.client.get(south_book).status_code, 200)
        self.client.force_login(self.users["leader"])
        self.assertEqual(self.client.get(north_book).status_code, 404)

    def test_participating_host_and_floater_can_manage_only_existing_enrollments(self):
        from django.core.exceptions import PermissionDenied, ValidationError
        from .participation import assign_participant_to_team

        host = self.members["host"]
        MonthEnrollment.objects.create(month=self.month, participant=host)
        TeamAssignment.objects.create(month=self.month, participant=host, team=self.north)
        self.client.force_login(self.users["host"])
        edit = reverse("month-participant-edit", args=[self.group.slug, self.month.pk, self.other_enrollment.pk])
        self.assertEqual(self.client.post(edit, {"team": self.north.pk}).status_code, 302)
        with self.assertRaises(PermissionDenied):
            assign_participant_to_team(month=self.month, participant=host, team=self.south, actor=self.users["host"])
        with self.assertRaises(ValidationError):
            assign_participant_to_team(month=self.month, participant=self.members["outsider"],
                                       team=self.south, actor=self.users["floater"])
        self.client.force_login(self.users["floater"])
        self.assertEqual(self.client.post(edit, {"team": ""}).status_code, 302)
        self.assertFalse(TeamAssignment.objects.filter(
            month=self.month, participant=self.members["other"], ended_at__isnull=True,
        ).exists())
        self.other_enrollment.refresh_from_db()
        self.assertTrue(self.other_enrollment.is_active)
        self.client.force_login(self.users["leader"])
        self.assertEqual(self.client.post(edit, {"team": self.south.pk}).status_code, 403)

    def test_team_roster_search_and_reporting_respect_leader_scope(self):
        self.month.reader_scores_visibility = ChallengeMonth.CompetitionVisibility.HOSTS_TEAM_LEADERS
        self.month.save(update_fields=["reader_scores_visibility"])
        UserProfile.objects.filter(user=self.users["reader"]).update(discord_username="NorthVisible")
        UserProfile.objects.filter(user=self.users["other"]).update(discord_username="SouthPrivate")
        self.client.force_login(self.users["leader"])
        north = reverse("team-detail", args=[self.group.slug, self.month.pk, self.north.pk])
        south = reverse("team-detail", args=[self.group.slug, self.month.pk, self.south.pk])
        query = {"sort": "total", "from": "2026-08-03"}
        response = self.client.get(north, query)
        self.assertContains(response, "Search User")
        self.assertContains(response, 'data-discord="NorthVisible"', count=2)
        self.assertContains(response, 'name="from" value="2026-08-03"')
        self.assertEqual(response.context["sort_key"], "total")
        response = self.client.get(south, query)
        self.assertNotContains(response, "SouthPrivate")
        self.assertFalse(response.context["can_view_reader_scores"])
        self.assertEqual(response.context["sort_key"], "reader")
        self.assertNotContains(response, "<legend>Reporting</legend>", html=True)

    def search_as(self, role, query, **context):
        response = self.get_search_response(role, query, **context)
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        return {group["category"]: group["items"] for group in response.json()["results"]}

    def get_search_response(self, role, query, **context):
        self.client.force_login(self.users[role])
        return self.client.get(reverse("global-search"), {"q": query, **context})

    def test_global_search_requires_authentication_and_scopes_discovery(self):
        self.assertEqual(self.client.get(reverse("global-search"), {"q": "stories"}).status_code, 403)
        self.assertEqual(self.search_as("reader", ""), {})
        self.assertEqual(self.search_as("reader", "stories")["Challenges"][0]["label"], self.month.name)
        self.assertIn("Navigation", self.search_as("reader", "my challenges"))
        self.assertContains(self.get_as("reader", reverse("dashboard")), "Search Northbound")
        hidden = ReadingGroup.objects.create(name="Private Search Group", slug="private-search")
        challenge = ChallengeMonth.objects.create(
            group=hidden, name="Private Search Challenge", starts_on=self.month.starts_on,
            ends_on=self.month.ends_on, status=ChallengeMonth.Status.ACTIVE,
        )
        self.assertEqual(self.search_as("reader", "private search", group=hidden.slug, month=challenge.pk), {})
        self.assertIn("Challenges", self.search_as("platform", "private search"))

    def test_global_search_lists_each_eligible_submission_action(self):
        second = ChallengeMonth.objects.create(
            group=self.group, name="Another Challenge", starts_on=date(2026, 9, 1),
            ends_on=date(2026, 9, 30), status=ChallengeMonth.Status.ACTIVE,
        )
        MonthEnrollment.objects.create(month=second, participant=self.members["reader"])
        result = self.search_as("reader", "submit", month=second.pk)["Actions"]
        self.assertEqual([item["url"] for item in result], [
            reverse("submission-create", args=[self.group.slug, month.pk])
            for month in (second, self.month)
        ])
        self.assertNotIn("Actions", self.search_as("floater", "submit"))
        self.assertNotIn("Actions", self.search_as("outsider", "submit"))

    def test_global_search_keeps_team_books_reader_and_staff_scopes_separate(self):
        profile = self.users["reader"].northbound_profile
        profile.discord_username = "private-roster-handle"
        profile.save(update_fields=["discord_username"])
        self.submission.review_notes = "PRIVATE REVIEW DATA"
        self.submission.save(update_fields=["review_notes"])
        self.assertEqual(self.search_as("other", "reader book"), {})
        self.assertEqual(self.search_as("other", "private-roster-handle"), {})
        self.assertIn("Readers", self.search_as("leader", "private-roster-handle"))
        own = self.search_as("reader", "reader book")
        self.assertEqual(own["Submissions"][0]["url"], reverse("submission-history", args=[
            self.group.slug, self.month.pk, self.submission.pk,
        ]))
        result = self.search_as("leader", "reader book")
        self.assertIn("Books", result)
        self.assertIn("Submissions", result)
        self.assertNotIn("PRIVATE REVIEW DATA", str(result))
        self.assertNotIn("token=", str(result))
        MonthEnrollment.objects.create(month=self.month, participant=self.members["host"])
        TeamAssignment.objects.create(month=self.month, participant=self.members["host"], team=self.south)
        self.assertEqual(self.search_as("host", "reader book"), {})
        self.assertEqual(self.search_as("host", "private-roster-handle"), {})
        self.assertEqual(self.client.get(result["Books"][0]["url"]).status_code, 403)

    def test_current_username_replaces_legacy_reader_labels_without_changing_membership(self):
        user = self.users["reader"]
        member = self.members["reader"]
        member.display_name = "Obsolete Membership Alias"
        member.save(update_fields=["display_name"])
        user.username = "renamed-current-reader"
        user.first_name = "Private First"
        user.last_name = "Private Last"
        user.save(update_fields=["username", "first_name", "last_name"])
        UserProfile.objects.filter(user=user).update(discord_username="SeparateDiscordHandle")
        group_url = reverse("participant-list", args=[self.group.slug])
        args = [self.group.slug, self.month.pk]
        for route in (
            group_url,
            reverse("month-participant-list", args=args),
            reverse("team-detail", args=[*args, self.north.pk]),
            reverse("team-book-detail", args=[*args, self.north.pk, self.submission.pk]),
            reverse("month-detail", args=args),
            self.url,
        ):
            with self.subTest(route=route):
                response = self.get_as("platform", route)
                self.assertContains(response, "renamed-current-reader")
                self.assertNotContains(response, "Obsolete Membership Alias")
                self.assertNotContains(response, "Private First")
        self.assertEqual(self.search_as("platform", user.username)["Readers"][0]["label"], user.username)
        self.assertEqual(self.search_as("platform", member.display_name), {})
        self.assertEqual(self.search_as("other", "SeparateDiscordHandle"), {})
        self.assertEqual(self.search_as("leader", "SeparateDiscordHandle")["Readers"][0]["label"], user.username)
        self.assertNotContains(self.get_as("other", reverse("month-participant-list", args=args)), "SeparateDiscordHandle")
        self.assertContains(self.get_as("leader", self.url), "SeparateDiscordHandle")
        member.refresh_from_db()
        self.submission.refresh_from_db()
        self.assertEqual(member.display_name, "Obsolete Membership Alias")
        self.assertEqual(member.user_id, user.pk)
        self.assertEqual(self.submission.participant_id, member.pk)

    def test_group_creation_flows_store_username_compatibility_value(self):
        reader = self.users["outsider"]
        reader.first_name = "Private"
        reader.last_name = "Name"
        reader.save(update_fields=["first_name", "last_name"])
        group = ReadingGroup.objects.create(name="Join Identity", slug="join-identity")
        group.regenerate_access_code()
        group.save()
        self.client.force_login(reader)
        response = self.client.post(reverse("group-join"), {"access_code": group.join_code})
        self.assertRedirects(response, reverse("group-detail", args=[group.slug]))
        self.assertEqual(Membership.objects.get(group=group, user=reader).display_name, reader.username)

        self.client.force_login(self.users["platform"])
        url = reverse("member-create", args=[group.slug])
        self.assertNotContains(self.client.get(url), 'name="display_name"')
        response = self.client.post(url, {
            "username": "staff-created-reader", "email": "", "role": Membership.Role.MEMBER,
            "temporary_password": "test-only-password-42",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Membership.objects.get(group=group, user__username="staff-created-reader").display_name,
                         "staff-created-reader")

    def test_group_directory_uses_only_authorized_discord_profile_values(self):
        user = self.users["reader"]
        user.username = "directory-current-reader"
        user.save(update_fields=["username"])
        profile = user.northbound_profile
        profile.discord_username = "PrivateDirectoryDiscord"
        profile.save(update_fields=["discord_username"])
        url = reverse("participant-list", args=[self.group.slug])
        self.assertContains(self.get_as("platform", url), "Discord: PrivateDirectoryDiscord")
        self.assertContains(self.get_as("reader", url), "Discord: PrivateDirectoryDiscord")
        ordinary = self.get_as("other", url)
        self.assertContains(ordinary, "directory-current-reader")
        self.assertNotContains(ordinary, "PrivateDirectoryDiscord")
        self.assertNotContains(ordinary, "@directory-current-reader")
        profile.discord_username_is_public = True
        profile.save(update_fields=["discord_username_is_public"])
        self.assertContains(self.get_as("other", url), "Discord: PrivateDirectoryDiscord")
        profile.discord_username = ""
        profile.save(update_fields=["discord_username"])
        self.assertNotContains(self.get_as("platform", url), "Discord:")
        nonmember = get_user_model().objects.create_user("directory-nonmember")
        self.client.force_login(nonmember)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_submission_reader_profile_links_follow_exact_enrollment(self):
        member = self.members["reader"]
        user = member.user
        user.username = "submission-current-reader"
        user.save(update_fields=["username"])
        month = ChallengeMonth.objects.create(
            group=self.group, name="Other Challenge", starts_on=date(2026, 9, 1),
            ends_on=date(2026, 9, 30), status=ChallengeMonth.Status.ACTIVE,
        )
        MonthEnrollment.objects.create(month=month, participant=member)
        expected = self.detail_url(member)
        other = reverse("challenge-participant-detail", args=[self.group.slug, month.pk, member.pk])
        fragment = self.month.get_absolute_url() + "?submissions_fragment=1"
        response = self.get_as("platform", fragment)
        self.assertContains(response, f'href="{expected}" class="submission-reader-name"')
        self.assertContains(response, ">submission-current-reader</a>")
        self.assertNotContains(response, other)
        self.assertEqual(self.get_as("platform", expected).status_code, 200)
        self.assertEqual(self.get_as("other", expected).status_code, 403)
        self.assertEqual(self.get_as("other", fragment).status_code, 403)

        self.reader_enrollment.is_active = False
        self.reader_enrollment.save(update_fields=["is_active"])
        self.assertContains(self.get_as("platform", fragment), f'href="{expected}" class="submission-reader-name"')
        self.assertEqual(self.get_as("platform", expected).status_code, 200)

        self.reader_enrollment.delete()
        response = self.get_as("platform", fragment)
        self.assertContains(response, ">submission-current-reader</span>")
        self.assertNotContains(response, expected)
        self.assertNotContains(response, other)
        self.assertEqual(self.get_as("platform", expected).status_code, 404)

    def test_own_voided_history_shows_reason_without_staff_details(self):
        from django.utils import timezone
        from .models import AuditEvent
        from .score_aggregation import challenge_score_totals

        reason = "Duplicate entry <confirmed>"
        BookSubmission.objects.filter(pk=self.submission.pk).update(
            is_removed=True, removed_at=timezone.now(), removed_by=self.users["host"],
            removal_reason=reason, review_notes="INTERNAL REVIEW NOTE", final_scored_pages=517,
        )
        AuditEvent.objects.create(
            actor=self.users["host"], group=self.group, action="submission.removed",
            object_type="BookSubmission", object_id=str(self.submission.pk), summary="RAW ADMIN SUMMARY",
        )
        theme = MonthTheme.objects.create(
            month=self.month, name="Voided Claim", starts_on=self.month.starts_on,
            ends_on=self.month.ends_on, bonus_pages=30,
        )
        ThemeClaim.objects.create(submission=self.submission, theme=theme, status="approved")
        args = [self.group.slug, self.month.pk, self.submission.pk]
        history = reverse("submission-history", args=args)
        profile = self.get_as("reader")
        self.assertContains(profile, history)
        self.assertContains(profile, '<s class="voided-submission-title">Reader Book</s>', html=True)
        self.assertContains(profile, "Excluded from totals")
        self.assertEqual(profile.context["theme_completed_count"], 0)
        reader_id = self.members["reader"].pk
        self.assertEqual(challenge_score_totals(month=self.month, participant_ids=[reader_id])[reader_id]["total_pages"], 0)
        response = self.get_as("reader", history)
        self.assertContains(response, "Duplicate entry &lt;confirmed&gt;")
        self.assertNotContains(response, "INTERNAL REVIEW NOTE")
        self.assertNotContains(response, "RAW ADMIN SUMMARY")
        self.assertNotContains(response, "By cpd-host")
        self.assertContains(self.get_as("host", history), "By cpd-host")
        self.assertEqual(self.get_as("other", history).status_code, 403)
        outside = get_user_model().objects.create_user("void-history-nonmember")
        self.client.force_login(outside)
        self.assertEqual(self.client.get(history).status_code, 403)
        review = reverse("submission-review", args=args)
        self.assertContains(self.get_as("host", review), "This page is read-only.")
        self.assertEqual(self.client.post(review, {"approved_pages": 900}).status_code, 403)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.final_scored_pages, 517)
