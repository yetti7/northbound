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
        self.assertContains(host, "Reader — Stories Under Stars")
        self.assertContains(host, "North")
        self.assertContains(host, "secret response")
        self.assertContains(host, "private.reader")
        self.assertContains(host, "<dt>Base</dt>", html=True)
        self.assertContains(host, "<dd>100</dd>", html=True)
        self.assertContains(host, "<dd>25</dd>", html=True)
        floater = self.get_as("floater")
        self.assertNotContains(floater, "secret response")
        self.assertNotContains(floater, "Private registration answer")
        self.assertNotContains(floater, "private.reader")
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
