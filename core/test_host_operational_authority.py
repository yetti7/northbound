from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import AuditEvent, BookSubmission, ChallengeMonth, ChallengeStaffAssignment, Membership, MonthEnrollment, MonthTheme, ReadingGroup, Team, TeamAssignment


class HostOperationalAuthorityTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner_user = User.objects.create_user("ops-owner", password="test-password")
        self.moderator_user = User.objects.create_user("ops-moderator", password="test-password")
        self.host_user = User.objects.create_user("ops-host", password="test-password")
        self.leader_user = User.objects.create_user("ops-leader", password="test-password")
        self.floater_user = User.objects.create_user("ops-floater", password="test-password")
        self.reader_user = User.objects.create_user("ops-reader", password="test-password")
        self.other_host_user = User.objects.create_user("ops-other-host", password="test-password")
        self.platform_owner = User.objects.create_superuser("ops-platform", password="test-password")
        self.group = ReadingGroup.objects.create(name="Operations Group", slug="operations-group", announcement_enabled=True, announcement="Group news")

        def member(user, name, role=Membership.Role.MEMBER, overrides=None):
            return Membership.objects.create(group=self.group, user=user, display_name=name, role=role, permission_overrides=overrides or {})

        self.owner = member(self.owner_user, "Owner", Membership.Role.OWNER)
        self.moderator = member(self.moderator_user, "Moderator", Membership.Role.MODERATOR, {
            "manage_participants": True,
            "manage_months": True,
            "manage_announcements": True,
            "manage_teams": True,
            "remove_content": True,
        })
        self.host = member(self.host_user, "Host")
        self.leader = member(self.leader_user, "Leader")
        self.floater = member(self.floater_user, "Floater")
        self.reader = member(self.reader_user, "Reader")
        self.other_host = member(self.other_host_user, "Other Host")
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="Operations Month",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status=ChallengeMonth.Status.DRAFT,
            announcement_mode=ChallengeMonth.AnnouncementMode.CUSTOM,
            announcement="Challenge news",
        )
        self.other_month = ChallengeMonth.objects.create(group=self.group, name="Other Operations Month", starts_on=date(2026, 9, 1), ends_on=date(2026, 9, 30), status=ChallengeMonth.Status.DRAFT)
        self.team_one = Team.objects.create(month=self.month, name="Team One", color="#112233")
        self.team_two = Team.objects.create(month=self.month, name="Team Two", color="#445566")
        MonthEnrollment.objects.create(month=self.month, participant=self.leader, enrolled_by=self.owner_user)
        TeamAssignment.objects.create(month=self.month, participant=self.leader, team=self.team_one)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.host, role=ChallengeStaffAssignment.Role.HOST, assigned_by=self.owner_user)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.leader, team=self.team_one, role=ChallengeStaffAssignment.Role.TEAM_LEADER, assigned_by=self.host_user)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.floater, role=ChallengeStaffAssignment.Role.FLOATER, assigned_by=self.host_user)
        ChallengeStaffAssignment.objects.create(month=self.other_month, membership=self.other_host, role=ChallengeStaffAssignment.Role.HOST, assigned_by=self.owner_user)
        self.hidden_theme = MonthTheme.objects.create(month=self.month, name="Hidden Theme", starts_on=self.month.starts_on, ends_on=self.month.ends_on, is_visible=False)
        self.submission = BookSubmission.objects.create(month=self.month, participant=self.leader, title="Removal Book", author="Author", book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 8, 10), submitted_pages=200)

    def test_host_can_create_edit_archive_restore_and_delete_unused_teams(self):
        self.client.force_login(self.host_user)
        create_url = reverse("team-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.assertRedirects(
            self.client.post(create_url, {"name": "Created Team", "color": "#778899"}),
            reverse("team-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}),
        )
        created = Team.objects.get(month=self.month, name="Created Team")
        edit_url = reverse("team-edit", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": created.pk})
        self.assertRedirects(self.client.post(edit_url, {"name": "Edited Team", "color": "#abcdef"}), reverse("team-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        toggle_url = reverse("team-archive-toggle", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": created.pk})
        self.client.post(toggle_url)
        created.refresh_from_db()
        self.assertTrue(created.is_archived)
        self.client.post(toggle_url)
        created.refresh_from_db()
        self.assertFalse(created.is_archived)
        delete_url = reverse("team-delete", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": created.pk})
        self.client.post(delete_url)
        self.assertFalse(Team.objects.filter(pk=created.pk).exists())

    def test_host_can_enroll_assign_move_unassign_and_remove_reader(self):
        self.client.force_login(self.host_user)
        add_url = reverse("month-participant-add", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.client.post(add_url, {"participant": self.reader.pk, "team": self.team_one.pk})
        enrollment = MonthEnrollment.objects.get(month=self.month, participant=self.reader)
        assignment = TeamAssignment.objects.get(month=self.month, participant=self.reader)
        self.assertEqual(assignment.team, self.team_one)
        edit_url = reverse("month-participant-edit", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": enrollment.pk})
        self.client.post(edit_url, {"team": self.team_two.pk})
        assignment.refresh_from_db()
        self.assertIsNotNone(assignment.ended_at)
        assignment = TeamAssignment.objects.get(month=self.month, participant=self.reader, ended_at__isnull=True)
        self.assertEqual(assignment.team, self.team_two)
        remove_team_url = reverse("team-assignment-remove", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": assignment.pk})
        self.client.post(remove_team_url, {"reason": "Roster change"})
        assignment.refresh_from_db()
        self.assertIsNotNone(assignment.ended_at)
        self.assertTrue(MonthEnrollment.objects.filter(pk=enrollment.pk).exists())
        remove_enrollment_url = reverse("month-participant-remove", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": enrollment.pk})
        self.client.post(remove_enrollment_url)
        enrollment.refresh_from_db()
        self.assertFalse(enrollment.is_active)

    def test_host_can_manage_hidden_themes_and_challenge_announcement(self):
        self.client.force_login(self.host_user)
        theme_page = self.client.get(reverse("theme-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}))
        self.assertContains(theme_page, self.hidden_theme.name)
        create_url = reverse("theme-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.client.post(create_url, {"name": "Host Theme", "description": "", "starts_on": "2026-08-01", "ends_on": "2026-08-31", "bonus_pages": 35, "allow_stacking": "on", "prompt": "", "is_active": "on", "is_visible": "on"})
        theme = MonthTheme.objects.get(month=self.month, name="Host Theme")
        edit_url = reverse("theme-edit", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": theme.pk})
        self.client.post(edit_url, {"name": "Edited Host Theme", "description": "Edited", "starts_on": "2026-08-01", "ends_on": "2026-08-31", "bonus_pages": 45, "allow_stacking": "on", "prompt": "", "is_active": "on", "is_visible": "on"})
        theme.refresh_from_db()
        self.assertEqual(theme.name, "Edited Host Theme")
        announcement_url = reverse("month-announcement-update", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.client.post(announcement_url, {"announcement": "Host challenge announcement"})
        self.month.refresh_from_db()
        self.assertEqual(self.month.announcement, "Host challenge announcement")

    def test_host_soft_removes_submission_with_attribution_and_audit(self):
        self.client.force_login(self.host_user)
        url = reverse("submission-remove", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.submission.pk})
        self.client.post(url, {"reason": "Duplicate entry"})
        self.submission.refresh_from_db()
        self.assertTrue(self.submission.is_removed)
        self.assertEqual(self.submission.removed_by, self.host_user)
        self.assertIsNotNone(self.submission.removed_at)
        self.assertEqual(self.submission.removal_reason, "Duplicate entry")
        self.assertTrue(AuditEvent.objects.filter(action="submission.removed", actor=self.host_user, object_id=str(self.submission.pk)).exists())

    def test_group_and_non_host_staffing_do_not_grant_challenge_operations(self):
        urls = (
            reverse("team-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}),
            reverse("team-edit", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.team_two.pk}),
            reverse("month-participant-add", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}),
            reverse("theme-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}),
            reverse("submission-remove", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": self.submission.pk}),
        )
        for user in (self.owner_user, self.moderator_user, self.leader_user, self.floater_user, self.reader_user, self.other_host_user):
            self.client.force_login(user)
            for url in urls:
                with self.subTest(user=user.username, url=url):
                    self.assertEqual(self.client.get(url).status_code, 403)
            announcement_url = reverse("month-announcement-update", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
            expected = 302 if user in {self.owner_user, self.moderator_user} else 403
            self.assertEqual(self.client.post(announcement_url, {"announcement": "Authority check"}).status_code, expected)

    def test_ended_host_loses_operational_authority_immediately(self):
        assignment = ChallengeStaffAssignment.objects.get(month=self.month, membership=self.host, role=ChallengeStaffAssignment.Role.HOST)
        assignment.ended_at = timezone.now()
        assignment.ended_by = self.owner_user
        assignment.save()
        self.client.force_login(self.host_user)
        self.assertEqual(self.client.get(reverse("team-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})).status_code, 403)
        self.assertEqual(self.client.get(reverse("theme-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})).status_code, 403)

    def test_confirmed_group_authority_and_visibility_boundaries_remain(self):
        self.client.force_login(self.owner_user)
        self.assertEqual(self.client.get(reverse("member-create", kwargs={"group_slug": self.group.slug})).status_code, 200)
        self.assertEqual(self.client.get(reverse("month-create", kwargs={"group_slug": self.group.slug})).status_code, 200)
        self.assertEqual(self.client.get(reverse("month-edit", kwargs={"group_slug": self.group.slug, "pk": self.month.pk})).status_code, 200)
        self.assertEqual(self.client.get(reverse("month-delete", kwargs={"group_slug": self.group.slug, "pk": self.month.pk})).status_code, 200)
        self.assertEqual(self.client.get(reverse("challenge-host-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})).status_code, 200)
        self.assertEqual(self.client.post(reverse("group-announcement-update", kwargs={"group_slug": self.group.slug}), {"announcement": "Updated group announcement"}).status_code, 302)
        self.assertEqual(self.client.get(reverse("challenge-visibility-settings", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})).status_code, 200)
        self.client.force_login(self.host_user)
        self.assertEqual(self.client.get(reverse("member-create", kwargs={"group_slug": self.group.slug})).status_code, 403)
        self.assertEqual(self.client.get(reverse("month-create", kwargs={"group_slug": self.group.slug})).status_code, 403)
        self.assertEqual(self.client.get(reverse("month-edit", kwargs={"group_slug": self.group.slug, "pk": self.month.pk})).status_code, 403)
        self.assertEqual(self.client.get(reverse("month-delete", kwargs={"group_slug": self.group.slug, "pk": self.month.pk})).status_code, 403)
        self.assertEqual(self.client.get(reverse("challenge-visibility-settings", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})).status_code, 200)
