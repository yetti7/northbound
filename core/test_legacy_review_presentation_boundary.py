from datetime import date

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from .models import BookSubmission, ChallengeMonth, ChallengeStaffAssignment, Membership, MonthEnrollment, ReadingGroup, Team, TeamAssignment
from .permissions import CAPABILITIES, membership_has_capability


class LegacyReviewPresentationBoundaryTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner_user = User.objects.create_user("boundary-owner", password="test-password")
        self.moderator_user = User.objects.create_user("boundary-moderator", password="test-password")
        self.host_user = User.objects.create_user("boundary-host", password="test-password")
        self.leader_user = User.objects.create_user("boundary-leader", password="test-password")
        self.floater_user = User.objects.create_user("boundary-floater", password="test-password")
        self.reader_user = User.objects.create_user("boundary-reader", password="test-password")
        self.opponent_user = User.objects.create_user("boundary-opponent", password="test-password")
        self.platform_owner = User.objects.create_superuser("boundary-platform", password="test-password")
        self.group = ReadingGroup.objects.create(name="Boundary Group", slug="boundary-group")

        def member(user, name, role=Membership.Role.MEMBER, overrides=None):
            return Membership.objects.create(
                group=self.group,
                user=user,
                display_name=name,
                role=role,
                permission_overrides=overrides or {},
            )

        self.owner = member(self.owner_user, "Owner", Membership.Role.OWNER)
        self.moderator = member(self.moderator_user, "Moderator", Membership.Role.MODERATOR)
        self.host = member(self.host_user, "Host")
        self.leader = member(self.leader_user, "Leader")
        self.floater = member(self.floater_user, "Floater")
        self.reader = member(self.reader_user, "Reader")
        self.opponent = member(self.opponent_user, "Opponent")
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="Boundary Month",
            starts_on=date(2026, 10, 1),
            ends_on=date(2026, 10, 31),
            status=ChallengeMonth.Status.ACTIVE,
        )
        self.team_one = Team.objects.create(month=self.month, name="Team One")
        self.team_two = Team.objects.create(month=self.month, name="Team Two")
        for participant, team in (
            (self.host, self.team_one),
            (self.leader, self.team_one),
            (self.reader, self.team_one),
            (self.opponent, self.team_two),
        ):
            MonthEnrollment.objects.create(month=self.month, participant=participant)
            TeamAssignment.objects.create(month=self.month, participant=participant, team=team)
        ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=self.host,
            role=ChallengeStaffAssignment.Role.HOST,
            assigned_by=self.owner_user,
        )
        ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=self.leader,
            team=self.team_one,
            role=ChallengeStaffAssignment.Role.TEAM_LEADER,
            assigned_by=self.host_user,
        )
        ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=self.floater,
            role=ChallengeStaffAssignment.Role.FLOATER,
            assigned_by=self.host_user,
        )
        self.host_submission = self.submission(self.host, "Host Own Book")
        self.leader_submission = self.submission(self.leader, "Leader Own Book")
        self.reader_submission = self.submission(self.reader, "Reader Private Book")
        self.opponent_submission = self.submission(self.opponent, "Opponent Private Book")
        self.profile_url = reverse(
            "participant-detail",
            kwargs={"group_slug": self.group.slug, "pk": self.reader.pk},
        )

    def submission(self, participant, title):
        return BookSubmission.objects.create(
            month=self.month,
            participant=participant,
            title=title,
            author="Author",
            book_format=BookSubmission.Format.EBOOK,
            completed_on=date(2026, 10, 10),
            submitted_pages=200,
            status=BookSubmission.Status.PENDING,
        )

    def test_reader_keeps_self_detailed_history(self):
        self.client.force_login(self.reader_user)
        response = self.client.get(self.profile_url)
        self.assertContains(response, "Monthly History")
        self.assertContains(response, self.month.name)
        self.assertContains(response, self.team_one.name)

    def test_non_platform_authority_layers_receive_summary_only(self):
        for user in (
            self.owner_user,
            self.moderator_user,
            self.host_user,
            self.leader_user,
            self.floater_user,
            self.opponent_user,
        ):
            self.client.force_login(user)
            response = self.client.get(self.profile_url)
            with self.subTest(user=user.username):
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Verified Pages")
                self.assertNotContains(response, "Monthly History")
                self.assertNotContains(response, self.month.name)
                self.assertNotContains(response, self.team_one.name)

    def test_platform_owner_has_administrative_history_visibility(self):
        self.client.force_login(self.platform_owner)
        response = self.client.get(self.profile_url)
        self.assertContains(response, "Monthly History")
        self.assertContains(response, self.month.name)
        self.assertContains(response, self.team_one.name)

    def test_month_detail_is_self_only_for_reader_and_staffing_roles(self):
        expectations = (
            (self.reader_user, "Reader Private Book"),
            (self.host_user, "Host Own Book"),
            (self.leader_user, "Leader Own Book"),
            (self.owner_user, None),
            (self.moderator_user, None),
            (self.floater_user, None),
        )
        all_titles = {
            "Host Own Book", "Leader Own Book", "Reader Private Book", "Opponent Private Book"
        }
        for user, own_title in expectations:
            self.client.force_login(user)
            response = self.client.get(self.month.get_absolute_url())
            with self.subTest(user=user.username):
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "My Submissions")
                self.assertNotContains(response, "Recent Submissions")
                for title in all_titles:
                    if title == own_title:
                        self.assertContains(response, title)
                    else:
                        self.assertNotContains(response, title)

        self.client.force_login(self.platform_owner)
        response = self.client.get(self.month.get_absolute_url())
        self.assertContains(response, "All Submissions · Platform Administration")
        for title in all_titles:
            self.assertContains(response, title)

    def test_staffing_review_queue_scope_remains_operational(self):
        review_url = reverse(
            "review-queue",
            kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk},
        )
        for user in (self.host_user, self.floater_user):
            self.client.force_login(user)
            response = self.client.get(review_url)
            with self.subTest(user=user.username):
                self.assertContains(response, "Reader Private Book")
                self.assertContains(response, "Opponent Private Book")
        self.client.force_login(self.leader_user)
        response = self.client.get(review_url)
        self.assertContains(response, "Reader Private Book")
        self.assertNotContains(response, "Opponent Private Book")

    def test_review_submissions_is_retired_from_registry_defaults_and_ui(self):
        self.assertNotIn("review_submissions", CAPABILITIES)
        self.assertFalse(membership_has_capability(self.moderator, "review_submissions"))
        self.client.force_login(self.owner_user)
        response = self.client.get(reverse(
            "participant-permissions-edit",
            kwargs={"group_slug": self.group.slug, "pk": self.moderator.pk},
        ))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("review_submissions", response.context["form"].fields)
        self.assertNotContains(response, "Legacy detailed Reader and Month submission access")


class RetireReviewSubmissionsMigrationTests(TransactionTestCase):
    migrate_from = [("core", "0031_retire_manage_teams_capability")]
    migrate_to = [("core", "0032_retire_review_submissions_capability")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        User = old_apps.get_model("auth", "User")
        ReadingGroup = old_apps.get_model("core", "ReadingGroup")
        Membership = old_apps.get_model("core", "Membership")
        user = User.objects.create(username="review-retirement-user")
        group = ReadingGroup.objects.create(name="Review Retirement", slug="review-retirement")
        self.membership_id = Membership.objects.create(
            group=group,
            user=user,
            display_name="Review Retirement User",
            role="moderator",
            permission_overrides={
                "review_submissions": False,
                "remove_content": True,
                "view_hidden_stats": False,
                "unknown_restored_key": {"preserve": True},
            },
        ).pk
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_migration_removes_only_review_submissions_override(self):
        from .models import Membership

        membership = Membership.objects.get(pk=self.membership_id)
        self.assertEqual(membership.permission_overrides, {
            "remove_content": True,
            "view_hidden_stats": False,
            "unknown_restored_key": {"preserve": True},
        })
