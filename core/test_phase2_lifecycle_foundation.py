from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from .models import BookSubmission, ChallengeMonth, ChallengeStaffAssignment, Membership, MonthEnrollment, ReadingGroup, Team


class ChallengeLifecycleMigrationTests(TransactionTestCase):
    migrate_from = [("core", "0032_retire_review_submissions_capability")]
    migrate_to = [("core", "0034_challenge_schedule_foundation_correction")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        Group = old_apps.get_model("core", "ReadingGroup")
        Month = old_apps.get_model("core", "ChallengeMonth")
        group = Group.objects.create(name="Migration Group", slug="migration-group")
        self.month_ids = {
            status: Month.objects.create(
                group=group,
                name=f"Legacy {status}",
                starts_on=date(2026, 1, 1),
                ends_on=date(2026, 1, 31),
                late_entry_deadline=date(2026, 1, 5),
                status=status,
            ).pk
            for status in ("draft", "open", "closed", "finalized", "archived")
        }
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        migrated_apps = executor.loader.project_state(self.migrate_to).apps
        self.MigratedChallengeMonth = migrated_apps.get_model("core", "ChallengeMonth")

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_released_statuses_map_without_fabricating_registration_windows(self):
        expected = {
            "draft": ChallengeMonth.Status.DRAFT,
            "open": ChallengeMonth.Status.ACTIVE,
            "closed": ChallengeMonth.Status.FINALIZING,
            "finalized": ChallengeMonth.Status.COMPLETED,
            "archived": ChallengeMonth.Status.ARCHIVED,
        }
        for old_status, expected_status in expected.items():
            with self.subTest(old_status=old_status):
                month = self.MigratedChallengeMonth.objects.get(pk=self.month_ids[old_status])
                self.assertEqual(month.status, expected_status)
                self.assertIsNone(month.registration_opens_at)
                self.assertIsNone(month.registration_closes_at)
                self.assertIsNone(month.starts_at)
                self.assertIsNone(month.ends_at)
                self.assertIsNone(month.final_announcement_at)
                self.assertEqual(month.late_entry_deadline, date(2026, 1, 5))
                self.assertFalse(month.registration_is_open)

    def test_automation_defaults_are_compatible_without_fabricated_schedule(self):
        month = self.MigratedChallengeMonth.objects.get(pk=self.month_ids["draft"])
        self.assertTrue(month.auto_open_registration)
        self.assertTrue(month.auto_close_registration)
        self.assertTrue(month.auto_start_challenge)
        self.assertTrue(month.auto_end_challenge)
        self.assertFalse(month.auto_complete_challenge)


class ChallengeLifecycleDomainTests(TestCase):
    def setUp(self):
        self.group = ReadingGroup.objects.create(name="Lifecycle Group", slug="phase-two-lifecycle", timezone="America/New_York")
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="Lifecycle Challenge",
            starts_on=date(2026, 9, 1),
            ends_on=date(2026, 9, 30),
        )

    def test_forward_transitions_are_adjacent_and_arbitrary_jumps_are_rejected(self):
        self.month.transition_to(ChallengeMonth.Status.UPCOMING)
        self.assertEqual(self.month.status, ChallengeMonth.Status.UPCOMING)
        with self.assertRaisesMessage(ValidationError, "one adjacent lifecycle stage"):
            self.month.transition_to(ChallengeMonth.Status.COMPLETED)

    def test_direct_persisted_status_assignment_is_rejected(self):
        self.month.status = ChallengeMonth.Status.ACTIVE
        with self.assertRaisesMessage(ValidationError, "authoritative Challenge lifecycle transition mechanism"):
            self.month.save(update_fields=["status"])

    def test_backward_transition_requires_confirmation_and_preserves_identity(self):
        self.month.transition_to(ChallengeMonth.Status.UPCOMING)
        with self.assertRaisesMessage(ValidationError, "requires explicit confirmation"):
            self.month.transition_to(ChallengeMonth.Status.DRAFT)
        original_pk = self.month.pk
        self.month.transition_to(ChallengeMonth.Status.DRAFT, confirm_reversal=True)
        self.assertEqual(self.month.pk, original_pk)
        self.assertEqual(self.month.status, ChallengeMonth.Status.DRAFT)

    def test_completed_recovery_requires_strong_confirmation(self):
        self.month.status = ChallengeMonth.Status.COMPLETED
        self.month._allow_lifecycle_transition = True
        self.month.save(update_fields=["status"])
        with self.assertRaisesMessage(ValidationError, "explicit recovery confirmation"):
            self.month.transition_to(ChallengeMonth.Status.FINALIZING, confirm_reversal=True)
        self.month.transition_to(
            ChallengeMonth.Status.FINALIZING,
            confirm_reversal=True,
            confirm_completed_recovery=True,
        )
        self.assertEqual(self.month.status, ChallengeMonth.Status.FINALIZING)

    def test_archived_is_a_normal_one_way_boundary(self):
        self.month.status = ChallengeMonth.Status.ARCHIVED
        self.month._allow_lifecycle_transition = True
        self.month.save(update_fields=["status"])
        with self.assertRaisesMessage(ValidationError, "Archived Challenges cannot move backward"):
            self.month.transition_to(ChallengeMonth.Status.COMPLETED, confirm_reversal=True)

    def test_registration_chronology_validation(self):
        self.month.registration_opens_at = datetime(2026, 8, 10, 9, 0, tzinfo=ZoneInfo("America/New_York"))
        self.month.registration_closes_at = datetime(2026, 8, 9, 9, 0, tzinfo=ZoneInfo("America/New_York"))
        with self.assertRaisesMessage(ValidationError, "cannot precede"):
            self.month.full_clean()

    def test_registration_open_is_timezone_aware_and_advances_only_to_upcoming(self):
        self.month.registration_opens_at = datetime(2026, 8, 10, 9, 0, tzinfo=ZoneInfo("America/New_York"))
        self.month.save(update_fields=["registration_opens_at"])
        before = datetime(2026, 8, 10, 8, 59, tzinfo=ZoneInfo("America/New_York"))
        due = datetime(2026, 8, 10, 9, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertEqual(self.month.apply_scheduled_actions(now=before), [])
        self.assertEqual(
            self.month.apply_scheduled_actions(now=due),
            ["registration_opened", "lifecycle_upcoming"],
        )
        self.month.refresh_from_db()
        self.assertTrue(self.month.registration_is_open)
        self.assertEqual(self.month.status, ChallengeMonth.Status.UPCOMING)
        self.assertEqual(self.month.apply_scheduled_actions(now=due), [])

    def test_overdue_registration_open_catches_up_in_upcoming_and_active_but_not_later(self):
        due = datetime(2026, 8, 10, 9, 0, tzinfo=ZoneInfo("America/New_York"))
        for status in (ChallengeMonth.Status.UPCOMING, ChallengeMonth.Status.ACTIVE):
            with self.subTest(status=status):
                month = ChallengeMonth.objects.create(
                    group=self.group,
                    name=f"Open catch-up {status}",
                    starts_on=date(2026, 8, 1),
                    ends_on=date(2026, 8, 31),
                    status=status,
                    registration_is_open=False,
                    registration_opens_at=due,
                )
                self.assertEqual(month.apply_scheduled_actions(now=due), ["registration_opened"])
                month.refresh_from_db()
                self.assertTrue(month.registration_is_open)
                self.assertEqual(month.status, status)

        for status in (
            ChallengeMonth.Status.FINALIZING,
            ChallengeMonth.Status.COMPLETED,
            ChallengeMonth.Status.ARCHIVED,
        ):
            with self.subTest(status=status):
                month = ChallengeMonth.objects.create(
                    group=self.group,
                    name=f"No reopen {status}",
                    starts_on=date(2026, 8, 1),
                    ends_on=date(2026, 8, 31),
                    status=status,
                    registration_is_open=False,
                    registration_opens_at=due,
                )
                self.assertEqual(month.apply_scheduled_actions(now=due), [])
                month.refresh_from_db()
                self.assertFalse(month.registration_is_open)
                self.assertEqual(month.status, status)

    def test_registration_close_is_independent_from_upcoming_and_active(self):
        close_at = datetime(2026, 8, 10, 9, 0, tzinfo=ZoneInfo("America/New_York"))
        for status in (ChallengeMonth.Status.UPCOMING, ChallengeMonth.Status.ACTIVE):
            with self.subTest(status=status):
                month = ChallengeMonth.objects.create(
                    group=self.group,
                    name=f"Close {status}",
                    starts_on=date(2026, 9, 1),
                    ends_on=date(2026, 9, 30),
                    status=status,
                    registration_is_open=True,
                    registration_closes_at=close_at,
                )
                self.assertEqual(month.apply_scheduled_actions(now=close_at), ["registration_closed"])
                month.refresh_from_db()
                self.assertFalse(month.registration_is_open)
                self.assertEqual(month.status, status)

    def test_start_does_not_close_registration_and_end_advances_to_finalizing(self):
        start_at = datetime(2026, 9, 1, 8, 0, tzinfo=ZoneInfo("America/New_York"))
        month = ChallengeMonth.objects.create(
            group=self.group,
            name="Open Through Start",
            starts_on=date(2026, 9, 1),
            ends_on=date(2026, 9, 30),
            starts_at=start_at,
            status=ChallengeMonth.Status.UPCOMING,
            registration_is_open=True,
        )
        self.assertEqual(month.apply_scheduled_actions(now=start_at), ["lifecycle_active"])
        month.refresh_from_db()
        self.assertEqual(month.status, ChallengeMonth.Status.ACTIVE)
        self.assertTrue(month.registration_is_open)

        end_at = datetime(2026, 9, 30, 20, 0, tzinfo=ZoneInfo("America/New_York"))
        month.ends_at = end_at
        month.save(update_fields=["ends_at"])
        self.assertEqual(month.apply_scheduled_actions(now=end_at), ["lifecycle_finalizing"])
        self.assertEqual(month.status, ChallengeMonth.Status.FINALIZING)

    def test_overdue_schedules_do_not_skip_lifecycle_stages(self):
        due = datetime(2026, 9, 30, 20, 0, tzinfo=ZoneInfo("America/New_York"))
        self.month.registration_opens_at = due
        self.month.starts_at = due
        self.month.ends_at = due
        self.month.final_announcement_at = due
        self.month.auto_complete_challenge = True
        self.month.save()
        self.month.apply_scheduled_actions(now=due)
        self.assertEqual(self.month.status, ChallengeMonth.Status.UPCOMING)

    def test_final_announcement_defaults_off_and_cannot_skip_to_completed(self):
        due = datetime(2026, 10, 1, 9, 0, tzinfo=ZoneInfo("America/New_York"))
        finalizing = ChallengeMonth.objects.create(
            group=self.group,
            name="Final Announcement",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status=ChallengeMonth.Status.FINALIZING,
            final_announcement_at=due,
        )
        self.assertFalse(finalizing.auto_complete_challenge)
        self.assertEqual(finalizing.apply_scheduled_actions(now=due), [])
        finalizing.auto_complete_challenge = True
        finalizing.save(update_fields=["auto_complete_challenge"])
        self.assertEqual(finalizing.apply_scheduled_actions(now=due), ["lifecycle_completed"])

        active = ChallengeMonth.objects.create(
            group=self.group,
            name="No Completion Skip",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status=ChallengeMonth.Status.ACTIVE,
            final_announcement_at=due,
            auto_complete_challenge=True,
        )
        self.assertEqual(active.apply_scheduled_actions(now=due), [])
        self.assertEqual(active.status, ChallengeMonth.Status.ACTIVE)


class DraftChallengeVisibilityTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner_user = User.objects.create_user("phase2-owner", password="test-password")
        self.moderator_user = User.objects.create_user("phase2-moderator", password="test-password")
        self.host_user = User.objects.create_user("phase2-host", password="test-password")
        self.member_user = User.objects.create_user("phase2-member", password="test-password")
        self.platform_owner = User.objects.create_superuser("phase2-platform", "platform@example.com", "test-password")
        self.group = ReadingGroup.objects.create(name="Visibility Group", slug="phase-two-visibility")
        self.owner = Membership.objects.create(group=self.group, user=self.owner_user, role=Membership.Role.OWNER, display_name="Owner")
        self.moderator = Membership.objects.create(
            group=self.group,
            user=self.moderator_user,
            role=Membership.Role.MODERATOR,
            display_name="Delegated Moderator",
            permission_overrides={"manage_months": True},
        )
        self.host = Membership.objects.create(group=self.group, user=self.host_user, role=Membership.Role.MEMBER, display_name="Host")
        self.member = Membership.objects.create(group=self.group, user=self.member_user, role=Membership.Role.MEMBER, display_name="Member")
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="Private Draft Challenge",
            starts_on=date(2026, 10, 1),
            ends_on=date(2026, 10, 31),
        )
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.host, role=ChallengeStaffAssignment.Role.HOST)

    def assert_can_see_draft(self, user):
        self.client.force_login(user)
        self.assertContains(self.client.get(reverse("group-detail", kwargs={"group_slug": self.group.slug})), self.month.name)
        self.assertEqual(self.client.get(self.month.get_absolute_url()).status_code, 200)

    def test_owner_delegated_moderator_and_assigned_host_can_see_draft(self):
        for user in (self.owner_user, self.moderator_user, self.host_user):
            with self.subTest(user=user.username):
                self.assert_can_see_draft(user)

    def test_ordinary_group_member_cannot_discover_or_open_draft(self):
        self.client.force_login(self.member_user)
        self.assertNotContains(self.client.get(reverse("group-detail", kwargs={"group_slug": self.group.slug})), self.month.name)
        self.assertNotContains(self.client.get(reverse("month-list", kwargs={"group_slug": self.group.slug})), self.month.name)
        self.assertEqual(self.client.get(self.month.get_absolute_url()).status_code, 404)

    def test_platform_owner_override_has_no_membership_or_staffing_side_effect(self):
        self.assert_can_see_draft(self.platform_owner)
        self.assertFalse(Membership.objects.filter(user=self.platform_owner).exists())
        self.assertFalse(ChallengeStaffAssignment.objects.filter(membership__user=self.platform_owner).exists())

    def test_completed_recovery_ui_requires_warning_and_deliberate_post(self):
        completed = ChallengeMonth.objects.create(
            group=self.group,
            name="Completed Challenge",
            starts_on=date(2026, 7, 1),
            ends_on=date(2026, 7, 31),
            status=ChallengeMonth.Status.COMPLETED,
        )
        self.client.force_login(self.owner_user)
        url = reverse("challenge-lifecycle-transition", kwargs={
            "group_slug": self.group.slug,
            "pk": completed.pk,
            "target_status": ChallengeMonth.Status.FINALIZING,
        })
        confirmation = self.client.get(url)
        self.assertContains(confirmation, "results were declared final")
        completed.refresh_from_db()
        self.assertEqual(completed.status, ChallengeMonth.Status.COMPLETED)
        self.client.post(url)
        completed.refresh_from_db()
        self.assertEqual(completed.status, ChallengeMonth.Status.FINALIZING)

    def test_detail_consolidates_configuration_and_settings_keeps_adjacent_stage_controls(self):
        active = ChallengeMonth.objects.create(
            group=self.group,
            name="Active Challenge",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status=ChallengeMonth.Status.ACTIVE,
        )
        self.group.announcement_enabled = True
        self.group.announcement = "Group announcement"
        self.group.save(update_fields=["announcement_enabled", "announcement"])
        self.client.force_login(self.owner_user)
        response = self.client.get(active.get_absolute_url())
        self.assertContains(response, "Challenge Settings")
        self.assertNotContains(response, "Change Stage")
        self.assertNotContains(response, "Signup Questions")
        self.assertNotContains(response, "Progress Checkpoints")
        settings = self.client.get(reverse("challenge-settings", kwargs={"group_slug": self.group.slug, "month_pk": active.pk}))
        self.assertContains(settings, "Move Back to Upcoming")
        self.assertContains(settings, "Move Forward to Finalizing")
        self.assertNotContains(response, "<h2>Challenge Lifecycle</h2>", html=True)
        self.assertContains(response, "Group Announcement")
        self.assertNotContains(response, "Month Announcement")
        self.assertNotContains(response, "Non-competing support for this Challenge.")

    def test_schedule_is_visible_in_group_timezone(self):
        self.month.registration_opens_at = datetime(2026, 9, 1, 13, 0, tzinfo=ZoneInfo("UTC"))
        self.month.registration_closes_at = datetime(2026, 9, 10, 21, 0, tzinfo=ZoneInfo("UTC"))
        self.month.starts_at = datetime(2026, 10, 1, 12, 0, tzinfo=ZoneInfo("UTC"))
        self.month.ends_at = datetime(2026, 11, 1, 0, 0, tzinfo=ZoneInfo("UTC"))
        self.month.save()
        self.client.force_login(self.member_user)
        response = self.client.get(self.month.get_absolute_url())
        self.assertEqual(response.status_code, 404)
        self.month.transition_to(ChallengeMonth.Status.UPCOMING)
        response = self.client.get(self.month.get_absolute_url())
        self.assertContains(response, "Challenge Schedule")
        self.assertContains(response, "Sep 1, 2026, 9:00 AM EDT")
        self.assertContains(response, "Oct 31, 2026, 8:00 PM EDT")

    def test_header_uses_dynamic_status_and_registration_without_duplicate_dates(self):
        self.month.registration_is_open = True
        self.month.save(update_fields=["registration_is_open"])
        self.month.transition_to(ChallengeMonth.Status.UPCOMING)
        self.client.force_login(self.member_user)
        response = self.client.get(self.month.get_absolute_url())
        self.assertContains(response, '<p class="lede">Upcoming · Registration Open</p>', html=True)
        self.assertNotContains(
            response,
            f'<p class="lede">{self.month.starts_on} – {self.month.ends_on} · Upcoming</p>',
            html=True,
        )
        self.assertContains(response, "Challenge Schedule")
        self.assertContains(response, "Time not recorded")

        self.month.registration_is_open = False
        self.month.save(update_fields=["registration_is_open"])
        response = self.client.get(self.month.get_absolute_url())
        self.assertContains(response, '<p class="lede">Upcoming · Registration Closed</p>', html=True)

    def test_challenge_detail_omits_scored_pages_card_in_all_lifecycle_states(self):
        Team.objects.create(month=self.month, name="Preserved Score Surface")
        BookSubmission.objects.create(
            month=self.month,
            participant=self.member,
            title="Protected Aggregate",
            author="Author",
            book_format=BookSubmission.Format.EBOOK,
            completed_on=date(2026, 10, 10),
            submitted_pages=450,
            approved_pages=400,
            status=BookSubmission.Status.APPROVED,
        )
        self.client.force_login(self.platform_owner)
        for status in (
            ChallengeMonth.Status.UPCOMING,
            ChallengeMonth.Status.ACTIVE,
            ChallengeMonth.Status.FINALIZING,
            ChallengeMonth.Status.COMPLETED,
            ChallengeMonth.Status.ARCHIVED,
        ):
            with self.subTest(status=status):
                ChallengeMonth.objects.filter(pk=self.month.pk).update(status=status)
                response = self.client.get(self.month.get_absolute_url())
                self.assertNotContains(response, "Scored Pages")
                self.assertNotIn("challenge_approved_pages", response.context)
                self.assertContains(response, "Challenge Participants")
                self.assertContains(response, "Teams")
                self.assertContains(response, "Team Comparison")
        self.assertFalse(Membership.objects.filter(user=self.platform_owner).exists())
        self.assertFalse(ChallengeStaffAssignment.objects.filter(membership__user=self.platform_owner).exists())
        self.assertFalse(MonthEnrollment.objects.filter(participant__user=self.platform_owner).exists())

    def test_challenge_announcement_and_final_announcement_schedule_remain_separate(self):
        final_time = datetime(2026, 11, 2, 15, 0, tzinfo=ZoneInfo("UTC"))
        self.month.announcement_mode = ChallengeMonth.AnnouncementMode.CUSTOM
        self.month.announcement = "Reader reminder text"
        self.month.final_announcement_at = final_time
        self.month.save()
        self.month.transition_to(ChallengeMonth.Status.UPCOMING)
        self.client.force_login(self.member_user)
        response = self.client.get(self.month.get_absolute_url())
        self.assertContains(response, "Challenge Announcement")
        self.assertContains(response, "Reader reminder text")
        self.assertContains(response, "Final Announcement")
        self.assertContains(response, "Nov 2, 2026, 10:00 AM EST")
        self.assertFalse(self.month.auto_complete_challenge)

    def test_platform_owner_reads_private_participant_history_and_all_submissions_without_identity(self):
        MonthEnrollment.objects.create(month=self.month, participant=self.member)
        BookSubmission.objects.create(
            month=self.month,
            participant=self.member,
            title="Administrative Read Test",
            author="Author",
            book_format=BookSubmission.Format.EBOOK,
            completed_on=date(2026, 10, 10),
            submitted_pages=100,
        )
        self.client.force_login(self.platform_owner)
        detail = self.client.get(reverse("participant-detail", kwargs={"group_slug": self.group.slug, "pk": self.member.pk}))
        self.assertContains(detail, "Private Draft Challenge")
        challenge = self.client.get(self.month.get_absolute_url())
        self.assertContains(challenge, "All Submissions · Platform Administration")
        self.assertContains(challenge, "Administrative Read Test")
        self.assertFalse(Membership.objects.filter(user=self.platform_owner).exists())
        self.assertFalse(ChallengeStaffAssignment.objects.filter(membership__user=self.platform_owner).exists())
        self.assertFalse(MonthEnrollment.objects.filter(participant__user=self.platform_owner).exists())


class ChallengeCreationHandoffTests(TestCase):
    def test_create_collects_only_title_and_hosts_without_fabricating_schedule(self):
        User = get_user_model()
        owner_user = User.objects.create_user("timezone-owner", password="test-password")
        group = ReadingGroup.objects.create(name="Pacific Group", slug="pacific-group", timezone="America/Los_Angeles")
        owner = Membership.objects.create(group=group, user=owner_user, role=Membership.Role.OWNER, display_name="Owner")
        host_user = User.objects.create_user("timezone-host", password="test-password")
        host = Membership.objects.create(group=group, user=host_user, display_name="Host")
        self.client.force_login(owner_user)
        create_url = reverse("month-create", kwargs={"group_slug": group.slug})
        page = self.client.get(create_url)
        self.assertEqual(set(page.context["form"].fields), {"name", "hosts"})
        response = self.client.post(reverse("month-create", kwargs={"group_slug": group.slug}), {
            "name": "Pacific Challenge",
            "hosts": [host.pk],
        })
        month = ChallengeMonth.objects.get(name="Pacific Challenge")
        self.assertRedirects(response, reverse("challenge-settings", kwargs={"group_slug": group.slug, "month_pk": month.pk}))
        self.assertEqual(month.status, ChallengeMonth.Status.DRAFT)
        self.assertFalse(month.registration_is_open)
        self.assertEqual(month.description, "")
        for field in ("registration_opens_at", "registration_closes_at", "starts_at", "ends_at", "final_announcement_at", "starts_on", "ends_on"):
            self.assertIsNone(getattr(month, field))
        self.assertTrue(ChallengeStaffAssignment.objects.filter(month=month, membership=host, role=ChallengeStaffAssignment.Role.HOST).exists())
        self.assertFalse(ChallengeStaffAssignment.objects.filter(month=month, membership=owner).exists())
        self.assertFalse(month.enrollments.exists())
        self.assertFalse(month.teams.exists())
