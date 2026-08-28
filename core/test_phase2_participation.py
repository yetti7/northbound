from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from .models import AuditEvent, BookSubmission, ChallengeMonth, ChallengeStaffAssignment, Membership, MonthEnrollment, ReadingGroup, Team, TeamAssignment


class DurableParticipationMigrationTests(TransactionTestCase):
    migrate_from = [("core", "0034_challenge_schedule_foundation_correction")]
    migrate_to = [("core", "0035_durable_participation_history")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        User = old_apps.get_model("auth", "User")
        ReadingGroupOld = old_apps.get_model("core", "ReadingGroup")
        MembershipOld = old_apps.get_model("core", "Membership")
        ChallengeMonthOld = old_apps.get_model("core", "ChallengeMonth")
        MonthEnrollmentOld = old_apps.get_model("core", "MonthEnrollment")
        TeamOld = old_apps.get_model("core", "Team")
        TeamAssignmentOld = old_apps.get_model("core", "TeamAssignment")

        user = User.objects.create(username="migration-reader")
        group = ReadingGroupOld.objects.create(name="Migration Group", slug="migration-group")
        member = MembershipOld.objects.create(group=group, user=user, role="member", display_name="Reader")
        month = ChallengeMonthOld.objects.create(
            group=group,
            name="Migration Challenge",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status="active",
        )
        team = TeamOld.objects.create(month=month, name="Legacy Team")
        self.enrollment_pk = MonthEnrollmentOld.objects.create(month=month, participant=member).pk
        self.assignment_pk = TeamAssignmentOld.objects.create(month=month, participant=member, team=team).pk

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_rows_become_active_current_history_without_fabricated_provenance(self):
        enrollment = MonthEnrollment.objects.get(pk=self.enrollment_pk)
        assignment = TeamAssignment.objects.get(pk=self.assignment_pk)
        self.assertTrue(enrollment.is_active)
        self.assertEqual(enrollment.origin, MonthEnrollment.Origin.LEGACY)
        self.assertEqual(enrollment.inactive_reason, "")
        self.assertIsNone(enrollment.inactivated_at)
        self.assertIsNone(assignment.assigned_at)
        self.assertIsNone(assignment.ended_at)


class SelfRegistrationAndParticipationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner_user = User.objects.create_user("participation-owner", password="test-password")
        self.moderator_user = User.objects.create_user("participation-moderator", password="test-password")
        self.host_user = User.objects.create_user("participation-host", password="test-password")
        self.reader_user = User.objects.create_user("participation-reader", password="test-password")
        self.other_user = User.objects.create_user("participation-other", password="test-password")
        self.floater_user = User.objects.create_user("participation-floater", password="test-password")
        self.platform_owner = User.objects.create_superuser("participation-platform", password="test-password")
        self.group = ReadingGroup.objects.create(name="Participation Group", slug="participation-group")
        self.owner = Membership.objects.create(group=self.group, user=self.owner_user, role=Membership.Role.OWNER, display_name="Owner")
        self.moderator = Membership.objects.create(group=self.group, user=self.moderator_user, role=Membership.Role.MODERATOR, display_name="Moderator")
        self.host = Membership.objects.create(group=self.group, user=self.host_user, role=Membership.Role.MEMBER, display_name="Host")
        self.reader = Membership.objects.create(group=self.group, user=self.reader_user, role=Membership.Role.MEMBER, display_name="Reader")
        self.other = Membership.objects.create(group=self.group, user=self.other_user, role=Membership.Role.MEMBER, display_name="Other Reader")
        self.floater = Membership.objects.create(group=self.group, user=self.floater_user, role=Membership.Role.MEMBER, display_name="Floater")
        self.upcoming = self.make_month("Upcoming Registration", ChallengeMonth.Status.UPCOMING, True)
        self.upcoming_closed = self.make_month("Upcoming Closed Registration", ChallengeMonth.Status.UPCOMING, False)
        self.active = self.make_month("Active Registration", ChallengeMonth.Status.ACTIVE, True)
        self.closed = self.make_month("Closed Registration", ChallengeMonth.Status.ACTIVE, False)
        self.finalizing = self.make_month("Finalizing Challenge", ChallengeMonth.Status.FINALIZING, False)
        self.completed = self.make_month("Completed Challenge", ChallengeMonth.Status.COMPLETED, False)
        self.archived = self.make_month("Archived Challenge", ChallengeMonth.Status.ARCHIVED, False)
        for month in (
            self.upcoming,
            self.upcoming_closed,
            self.active,
            self.closed,
            self.finalizing,
            self.completed,
            self.archived,
        ):
            ChallengeStaffAssignment.objects.create(
                month=month,
                membership=self.host,
                role=ChallengeStaffAssignment.Role.HOST,
                assigned_by=self.owner_user,
            )

    def make_month(self, name, status, registration_is_open):
        return ChallengeMonth.objects.create(
            group=self.group,
            name=name,
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status=status,
            registration_is_open=registration_is_open,
        )

    def register_url(self, month):
        return reverse("challenge-register", kwargs={"group_slug": self.group.slug, "month_pk": month.pk})

    def test_upcoming_and_active_open_registration_are_self_service_and_idempotent(self):
        for month in (self.upcoming, self.active):
            with self.subTest(month=month.name):
                self.client.force_login(self.reader_user)
                self.assertContains(self.client.get(month.get_absolute_url()), ">Register</a>", html=False)
                self.assertRedirects(self.client.post(self.register_url(month)), month.get_absolute_url())
                enrollment = MonthEnrollment.objects.get(month=month, participant=self.reader)
                enrollment_pk = enrollment.pk
                self.assertTrue(enrollment.is_active)
                self.assertEqual(enrollment.origin, MonthEnrollment.Origin.SELF)
                self.client.post(self.register_url(month), {"participant": self.other.pk})
                self.assertEqual(MonthEnrollment.objects.filter(month=month, participant=self.reader).count(), 1)
                self.assertEqual(MonthEnrollment.objects.get(month=month, participant=self.reader).pk, enrollment_pk)
                self.assertFalse(MonthEnrollment.objects.filter(month=month, participant=self.other).exists())
                self.assertContains(self.client.get(month.get_absolute_url()), ">Withdraw</a>", html=False)

    def test_closed_registration_blocks_self_registration(self):
        self.client.force_login(self.reader_user)
        for month in (self.upcoming_closed, self.closed):
            with self.subTest(month=month.name):
                self.assertNotContains(self.client.get(month.get_absolute_url()), ">Register</a>", html=False)
                self.client.post(self.register_url(month))
                self.assertFalse(MonthEnrollment.objects.filter(month=month, participant=self.reader).exists())

    def test_withdrawal_depends_on_lifecycle_not_registration_availability(self):
        self.client.force_login(self.reader_user)
        for month in (self.upcoming, self.upcoming_closed, self.active, self.closed):
            with self.subTest(month=month.name):
                enrollment = MonthEnrollment.objects.create(
                    month=month,
                    participant=self.reader,
                    enrolled_by=self.reader_user,
                    origin=MonthEnrollment.Origin.SELF,
                )
                withdraw_url = reverse(
                    "challenge-withdraw",
                    kwargs={"group_slug": self.group.slug, "month_pk": month.pk},
                )
                self.assertContains(self.client.get(month.get_absolute_url()), ">Withdraw</a>", html=False)
                self.assertRedirects(self.client.post(withdraw_url), month.get_absolute_url())
                enrollment.refresh_from_db()
                self.assertFalse(enrollment.is_active)
                self.assertEqual(enrollment.inactive_reason, MonthEnrollment.InactiveReason.WITHDRAWN)

        for month in (self.finalizing, self.completed, self.archived):
            with self.subTest(month=month.name):
                enrollment = MonthEnrollment.objects.create(
                    month=month,
                    participant=self.reader,
                    enrolled_by=self.reader_user,
                    origin=MonthEnrollment.Origin.SELF,
                )
                withdraw_url = reverse(
                    "challenge-withdraw",
                    kwargs={"group_slug": self.group.slug, "month_pk": month.pk},
                )
                self.assertNotContains(self.client.get(month.get_absolute_url()), ">Withdraw</a>", html=False)
                self.assertRedirects(self.client.post(withdraw_url), month.get_absolute_url())
                enrollment.refresh_from_db()
                self.assertTrue(enrollment.is_active)
                self.assertEqual(enrollment.inactive_reason, "")

    def test_withdrawal_preserves_participation_submission_assignment_and_ends_leadership(self):
        enrollment = MonthEnrollment.objects.create(
            month=self.active,
            participant=self.reader,
            enrolled_by=self.reader_user,
            origin=MonthEnrollment.Origin.SELF,
        )
        team = Team.objects.create(month=self.active, name="First Team")
        assignment = TeamAssignment.objects.create(
            month=self.active,
            participant=self.reader,
            team=team,
            assigned_by=self.host_user,
        )
        leadership = ChallengeStaffAssignment.objects.create(
            month=self.active,
            membership=self.reader,
            team=team,
            role=ChallengeStaffAssignment.Role.TEAM_LEADER,
            assigned_by=self.host_user,
        )
        submission = BookSubmission.objects.create(
            month=self.active,
            participant=self.reader,
            title="Preserved Book",
            author="Author",
            book_format=BookSubmission.Format.EBOOK,
            completed_on=date(2026, 8, 10),
            submitted_pages=200,
            approved_pages=200,
            status=BookSubmission.Status.APPROVED,
        )
        self.client.force_login(self.reader_user)
        withdraw_url = reverse("challenge-withdraw", kwargs={"group_slug": self.group.slug, "month_pk": self.active.pk})
        self.assertRedirects(self.client.post(withdraw_url), self.active.get_absolute_url())

        enrollment.refresh_from_db()
        assignment.refresh_from_db()
        leadership.refresh_from_db()
        self.assertEqual(enrollment.pk, MonthEnrollment.objects.get(month=self.active, participant=self.reader).pk)
        self.assertFalse(enrollment.is_active)
        self.assertEqual(enrollment.inactive_reason, MonthEnrollment.InactiveReason.WITHDRAWN)
        self.assertIsNotNone(assignment.ended_at)
        self.assertIsNotNone(leadership.ended_at)
        self.assertTrue(BookSubmission.objects.filter(pk=submission.pk, approved_pages=200).exists())
        self.assertContains(self.client.get(self.active.get_absolute_url()), "Withdrawn")

        self.client.force_login(self.host_user)
        roster = self.client.get(reverse("month-participant-list", kwargs={"group_slug": self.group.slug, "month_pk": self.active.pk}))
        self.assertContains(roster, "Withdrawn")
        self.assertContains(roster, "Reactivate")
        self.client.force_login(self.reader_user)

        response = self.client.post(
            reverse("submission-create", kwargs={"group_slug": self.group.slug, "month_pk": self.active.pk}),
            {"title": "Blocked", "author": "Author", "book_format": "ebook", "completed_on": "2026-08-11", "submitted_pages": 100},
        )
        self.assertRedirects(response, self.active.get_absolute_url())
        self.assertFalse(BookSubmission.objects.filter(title="Blocked").exists())

        self.client.post(self.register_url(self.active))
        enrollment.refresh_from_db()
        self.assertTrue(enrollment.is_active)
        self.assertFalse(TeamAssignment.objects.filter(month=self.active, participant=self.reader, ended_at__isnull=True).exists())
        self.assertFalse(ChallengeStaffAssignment.objects.filter(pk=leadership.pk, ended_at__isnull=True).exists())

    def test_staff_post_close_add_reactivate_and_authority_boundaries(self):
        add_url = reverse("month-participant-add", kwargs={"group_slug": self.group.slug, "month_pk": self.closed.pk})
        self.client.force_login(self.owner_user)
        self.assertEqual(self.client.post(add_url, {"participant": self.reader.pk}).status_code, 403)
        self.client.force_login(self.moderator_user)
        self.assertEqual(self.client.post(add_url, {"participant": self.reader.pk}).status_code, 403)

        self.client.force_login(self.host_user)
        self.assertRedirects(
            self.client.post(add_url, {"participant": self.reader.pk}),
            reverse("month-participant-list", kwargs={"group_slug": self.group.slug, "month_pk": self.closed.pk}),
        )
        enrollment = MonthEnrollment.objects.get(month=self.closed, participant=self.reader)
        self.assertEqual(enrollment.origin, MonthEnrollment.Origin.STAFF)
        remove_url = reverse("month-participant-remove", kwargs={"group_slug": self.group.slug, "month_pk": self.closed.pk, "pk": enrollment.pk})
        self.client.post(remove_url)
        enrollment.refresh_from_db()
        self.assertFalse(enrollment.is_active)
        reactivate_url = reverse("month-participant-reactivate", kwargs={"group_slug": self.group.slug, "month_pk": self.closed.pk, "pk": enrollment.pk})
        self.client.post(reactivate_url)
        enrollment.refresh_from_db()
        self.assertTrue(enrollment.is_active)

        MonthEnrollment.objects.filter(pk=enrollment.pk).update(is_active=False, inactive_reason=MonthEnrollment.InactiveReason.REMOVED)
        self.client.force_login(self.platform_owner)
        self.client.post(reactivate_url)
        enrollment.refresh_from_db()
        self.assertTrue(enrollment.is_active)
        self.assertFalse(Membership.objects.filter(user=self.platform_owner).exists())
        self.assertFalse(MonthEnrollment.objects.filter(participant__user=self.platform_owner).exists())

    def test_team_move_preserves_old_assignment_and_only_one_current_assignment(self):
        MonthEnrollment.objects.create(month=self.active, participant=self.reader, origin=MonthEnrollment.Origin.STAFF)
        first = Team.objects.create(month=self.active, name="First Team")
        second = Team.objects.create(month=self.active, name="Second Team")
        initial = TeamAssignment.objects.create(month=self.active, participant=self.reader, team=first, assigned_by=self.host_user)
        edit_url = reverse("month-participant-edit", kwargs={"group_slug": self.group.slug, "month_pk": self.active.pk, "pk": self.active.enrollments.get(participant=self.reader).pk})
        self.client.force_login(self.host_user)
        self.client.post(edit_url, {"team": second.pk})
        initial.refresh_from_db()
        self.assertIsNotNone(initial.ended_at)
        current = TeamAssignment.objects.get(month=self.active, participant=self.reader, ended_at__isnull=True)
        self.assertEqual(current.team, second)
        self.assertEqual(TeamAssignment.objects.filter(month=self.active, participant=self.reader).count(), 2)
        with self.assertRaises(ValidationError):
            TeamAssignment.objects.create(month=self.active, participant=self.reader, team=first)

    def test_floater_cannot_self_register_and_inactive_reader_cannot_be_team_leader(self):
        ChallengeStaffAssignment.objects.create(
            month=self.upcoming,
            membership=self.floater,
            role=ChallengeStaffAssignment.Role.FLOATER,
            assigned_by=self.host_user,
        )
        self.client.force_login(self.floater_user)
        self.client.post(self.register_url(self.upcoming))
        self.assertFalse(MonthEnrollment.objects.filter(month=self.upcoming, participant=self.floater).exists())

        enrollment = MonthEnrollment.objects.create(month=self.active, participant=self.reader, is_active=False)
        team = Team.objects.create(month=self.active, name="Inactive Team")
        with self.assertRaises(ValidationError):
            TeamAssignment.objects.create(month=self.active, participant=self.reader, team=team)
        self.assertFalse(enrollment.is_active)

    def test_expected_audit_events_are_recorded(self):
        self.client.force_login(self.reader_user)
        self.client.post(self.register_url(self.active))
        self.client.post(reverse("challenge-withdraw", kwargs={"group_slug": self.group.slug, "month_pk": self.active.pk}))
        self.assertTrue(AuditEvent.objects.filter(action="participation.self_registered", actor=self.reader_user).exists())
        self.assertTrue(AuditEvent.objects.filter(action="participation.self_withdrew", actor=self.reader_user).exists())
