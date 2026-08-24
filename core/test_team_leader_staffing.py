from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .models import AuditEvent, BookSubmission, ChallengeMonth, ChallengeStaffAssignment, Membership, MonthEnrollment, ReadingGroup, Team, TeamAssignment
from .permissions import can_view_team_stats


class TeamLeaderStaffingTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("leader-owner", password="test-password")
        self.host_user = User.objects.create_user("leader-host", password="test-password")
        self.reader_one_user = User.objects.create_user("leader-reader-one", password="test-password")
        self.reader_two_user = User.objects.create_user("leader-reader-two", password="test-password")
        self.unenrolled_user = User.objects.create_user("leader-unenrolled", password="test-password")
        self.unassigned_user = User.objects.create_user("leader-unassigned", password="test-password")
        self.group = ReadingGroup.objects.create(name="Leader Group", slug="leader-group")
        self.owner_membership = Membership.objects.create(
            group=self.group, user=self.owner, role=Membership.Role.OWNER, display_name="Owner"
        )
        self.host_membership = Membership.objects.create(
            group=self.group, user=self.host_user, role=Membership.Role.MEMBER, display_name="Host Reader"
        )
        self.reader_one = Membership.objects.create(
            group=self.group, user=self.reader_one_user, role=Membership.Role.MEMBER, display_name="Reader One"
        )
        self.reader_two = Membership.objects.create(
            group=self.group, user=self.reader_two_user, role=Membership.Role.MEMBER, display_name="Reader Two"
        )
        self.unenrolled = Membership.objects.create(
            group=self.group, user=self.unenrolled_user, role=Membership.Role.MEMBER, display_name="Unenrolled"
        )
        self.unassigned = Membership.objects.create(
            group=self.group, user=self.unassigned_user, role=Membership.Role.MEMBER, display_name="Unassigned"
        )
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="Leader Month",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status=ChallengeMonth.Status.OPEN,
        )
        self.other_month = ChallengeMonth.objects.create(
            group=self.group,
            name="Other Leader Month",
            starts_on=date(2026, 9, 1),
            ends_on=date(2026, 9, 30),
            status=ChallengeMonth.Status.OPEN,
        )
        self.team_one = Team.objects.create(month=self.month, name="Team One")
        self.team_two = Team.objects.create(month=self.month, name="Team Two")
        self.reader_one_assignment = TeamAssignment.objects.create(
            month=self.month, team=self.team_one, participant=self.reader_one
        )
        self.reader_two_assignment = TeamAssignment.objects.create(
            month=self.month, team=self.team_one, participant=self.reader_two
        )
        self.host_team_assignment = TeamAssignment.objects.create(
            month=self.month, team=self.team_two, participant=self.host_membership
        )
        MonthEnrollment.objects.create(month=self.month, participant=self.unassigned, enrolled_by=self.owner)
        self.host_assignment = ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=self.host_membership,
            role=ChallengeStaffAssignment.Role.HOST,
            assigned_by=self.owner,
        )
        self.list_url = reverse(
            "team-leader-list",
            kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "team_pk": self.team_one.pk},
        )

    def create_leader(self, membership, team=None, assigned_by=None):
        return ChallengeStaffAssignment.objects.create(
            month=self.month,
            team=team or self.team_one,
            membership=membership,
            role=ChallengeStaffAssignment.Role.TEAM_LEADER,
            assigned_by=assigned_by or self.host_user,
        )

    def test_host_assigns_eligible_reader_without_changing_participation(self):
        enrollment_id = MonthEnrollment.objects.get(month=self.month, participant=self.reader_one).pk
        team_assignment_id = self.reader_one_assignment.pk
        self.client.force_login(self.host_user)
        response = self.client.post(self.list_url, {"membership": self.reader_one.pk})
        self.assertRedirects(response, self.list_url)

        leader = ChallengeStaffAssignment.objects.get(
            membership=self.reader_one,
            role=ChallengeStaffAssignment.Role.TEAM_LEADER,
        )
        self.assertEqual(leader.team, self.team_one)
        self.assertEqual(leader.assigned_by, self.host_user)
        self.assertEqual(MonthEnrollment.objects.get(month=self.month, participant=self.reader_one).pk, enrollment_id)
        self.assertEqual(TeamAssignment.objects.get(month=self.month, participant=self.reader_one).pk, team_assignment_id)
        event = AuditEvent.objects.get(action="challenge.team_leader_assigned", object_id=str(leader.pk))
        self.assertEqual(event.actor, self.host_user)
        self.assertIn(self.month.name, event.summary)
        self.assertIn(self.team_one.name, event.summary)
        self.assertIn(self.reader_one.display_name, event.summary)

    def test_multiple_leaders_different_teams_and_host_plus_team_leader_are_supported(self):
        first = self.create_leader(self.reader_one)
        second = self.create_leader(self.reader_two)
        host_leader = self.create_leader(self.host_membership, team=self.team_two)

        self.assertEqual(self.team_one.staff_assignments.filter(ended_at__isnull=True).count(), 2)
        self.assertEqual(host_leader.membership, self.host_assignment.membership)
        self.assertEqual(
            self.month.staff_assignments.filter(
                role=ChallengeStaffAssignment.Role.TEAM_LEADER,
                ended_at__isnull=True,
            ).count(),
            3,
        )
        self.assertNotEqual(first.pk, second.pk)

    def test_unenrolled_unassigned_and_wrong_team_readers_are_ineligible(self):
        with self.assertRaises(ValidationError):
            self.create_leader(self.unenrolled)
        with self.assertRaises(ValidationError):
            self.create_leader(self.unassigned)
        with self.assertRaises(ValidationError):
            self.create_leader(self.reader_one, team=self.team_two)

        inactive_user = get_user_model().objects.create_user("inactive-leader")
        inactive = Membership.objects.create(
            group=self.group,
            user=inactive_user,
            role=Membership.Role.MEMBER,
            display_name="Inactive Leader",
            is_active=False,
        )
        TeamAssignment.objects.create(month=self.month, team=self.team_one, participant=inactive)
        with self.assertRaises(ValidationError):
            self.create_leader(inactive)

        other_group = ReadingGroup.objects.create(name="Other Leader Group", slug="other-leader-group")
        other_membership = Membership.objects.create(
            group=other_group,
            user=get_user_model().objects.create_user("cross-group-leader"),
            role=Membership.Role.MEMBER,
            display_name="Cross Group",
        )
        with self.assertRaises(ValidationError):
            self.create_leader(other_membership)

    def test_duplicate_active_team_leader_assignment_is_prevented(self):
        self.create_leader(self.reader_one)
        with self.assertRaises(ValidationError):
            self.create_leader(self.reader_one)

    def test_team_leader_remains_reader_without_group_or_visibility_authority(self):
        self.create_leader(self.reader_one)
        self.assertFalse(can_view_team_stats(self.reader_one_user, self.month))

        self.client.force_login(self.reader_one_user)
        self.assertEqual(
            self.client.get(
                reverse("review-queue", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
            ).status_code,
            200,
        )
        response = self.client.post(
            reverse("submission-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}),
            {
                "title": "Leader Reader Book",
                "author": "Author",
                "book_format": BookSubmission.Format.EBOOK,
                "started_on": "2026-08-01",
                "completed_on": "2026-08-12",
                "submitted_pages": 250,
                "reference_url": "",
                "notes": "",
            },
        )
        self.assertRedirects(response, self.month.get_absolute_url())
        self.assertTrue(BookSubmission.objects.filter(participant=self.reader_one).exists())

    def test_host_removal_ends_staffing_but_preserves_reader_and_history(self):
        leader = self.create_leader(self.reader_one)
        enrollment_id = MonthEnrollment.objects.get(month=self.month, participant=self.reader_one).pk
        team_assignment_id = self.reader_one_assignment.pk
        end_url = reverse(
            "team-leader-end",
            kwargs={
                "group_slug": self.group.slug,
                "month_pk": self.month.pk,
                "team_pk": self.team_one.pk,
                "pk": leader.pk,
            },
        )
        self.client.force_login(self.host_user)
        response = self.client.post(end_url)
        self.assertRedirects(response, self.list_url)
        leader.refresh_from_db()

        self.assertIsNotNone(leader.ended_at)
        self.assertEqual(leader.ended_by, self.host_user)
        self.assertEqual(MonthEnrollment.objects.get(month=self.month, participant=self.reader_one).pk, enrollment_id)
        self.assertEqual(TeamAssignment.objects.get(month=self.month, participant=self.reader_one).pk, team_assignment_id)
        self.assertTrue(ChallengeStaffAssignment.objects.filter(pk=leader.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(action="challenge.team_leader_ended", object_id=str(leader.pk)).exists())
        history = self.client.get(self.list_url)
        self.assertContains(history, "Team Leader History")

    def test_team_change_automatically_ends_active_leadership_with_attribution(self):
        leader = self.create_leader(self.reader_one)
        enrollment = MonthEnrollment.objects.get(month=self.month, participant=self.reader_one)
        self.client.force_login(self.host_user)
        response = self.client.post(
            reverse(
                "month-participant-edit",
                kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": enrollment.pk},
            ),
            {"team": self.team_two.pk},
        )
        self.assertRedirects(
            response,
            reverse("month-participant-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}),
        )
        leader.refresh_from_db()
        self.assertIsNotNone(leader.ended_at)
        self.assertEqual(leader.ended_by, self.host_user)
        self.assertEqual(TeamAssignment.objects.get(pk=self.reader_one_assignment.pk).team, self.team_two)
        self.assertTrue(
            AuditEvent.objects.filter(
                action="challenge.team_leader_ended",
                object_id=str(leader.pk),
                actor=self.host_user,
                summary__contains="underlying team assignment changed",
            ).exists()
        )

    def test_team_assignment_removal_automatically_ends_active_leadership(self):
        leader = self.create_leader(self.reader_one)
        remove_url = reverse(
            "team-assignment-remove",
            kwargs={
                "group_slug": self.group.slug,
                "month_pk": self.month.pk,
                "pk": self.reader_one_assignment.pk,
            },
        )
        self.client.force_login(self.host_user)
        response = self.client.post(remove_url)
        self.assertRedirects(
            response,
            reverse("team-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}),
        )
        leader.refresh_from_db()
        self.assertIsNotNone(leader.ended_at)
        self.assertFalse(TeamAssignment.objects.filter(pk=self.reader_one_assignment.pk).exists())
        self.assertTrue(MonthEnrollment.objects.filter(month=self.month, participant=self.reader_one).exists())

    def test_only_current_host_for_this_challenge_can_manage_team_leaders(self):
        self.client.force_login(self.reader_one_user)
        self.assertEqual(self.client.post(self.list_url, {"membership": self.reader_two.pk}).status_code, 403)

        other_team = Team.objects.create(month=self.other_month, name="Other Team")
        TeamAssignment.objects.create(month=self.other_month, team=other_team, participant=self.reader_two)
        other_url = reverse(
            "team-leader-list",
            kwargs={"group_slug": self.group.slug, "month_pk": self.other_month.pk, "team_pk": other_team.pk},
        )
        self.client.force_login(self.host_user)
        self.assertEqual(self.client.post(other_url, {"membership": self.reader_two.pk}).status_code, 403)
        self.assertFalse(
            ChallengeStaffAssignment.objects.filter(
                month=self.other_month,
                role=ChallengeStaffAssignment.Role.TEAM_LEADER,
            ).exists()
        )

    def test_team_roster_marks_leaders_and_only_hosts_see_management_link(self):
        self.create_leader(self.reader_one)
        team_page = reverse("team-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})

        self.client.force_login(self.host_user)
        host_view = self.client.get(team_page)
        self.assertContains(host_view, "Reader One · Team Leader")
        self.assertContains(host_view, "Manage Team Leaders")

        self.client.force_login(self.reader_two_user)
        reader_view = self.client.get(team_page)
        self.assertContains(reader_view, "Reader One · Team Leader")
        self.assertNotContains(reader_view, "Manage Team Leaders")
