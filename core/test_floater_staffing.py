from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .models import AuditEvent, BookSubmission, ChallengeMonth, ChallengeStaffAssignment, Membership, MonthEnrollment, ReadingGroup, Team, TeamAssignment
from .permissions import can_view_team_stats


class FloaterStaffingTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("floater-owner", password="test-password")
        self.moderator = User.objects.create_user("floater-moderator", password="test-password")
        self.host_user = User.objects.create_user("floater-host", password="test-password")
        self.other_host_user = User.objects.create_user("floater-other-host", password="test-password")
        self.member_user = User.objects.create_user("floater-member", password="test-password")
        self.second_member_user = User.objects.create_user("floater-second", password="test-password")
        self.reader_user = User.objects.create_user("floater-reader", password="test-password")
        self.group = ReadingGroup.objects.create(name="Floater Group", slug="floater-group")
        self.owner_membership = Membership.objects.create(group=self.group, user=self.owner, role=Membership.Role.OWNER, display_name="Owner")
        self.moderator_membership = Membership.objects.create(group=self.group, user=self.moderator, role=Membership.Role.MODERATOR, display_name="Moderator")
        self.host_membership = Membership.objects.create(group=self.group, user=self.host_user, role=Membership.Role.MEMBER, display_name="Host")
        self.other_host_membership = Membership.objects.create(group=self.group, user=self.other_host_user, role=Membership.Role.MEMBER, display_name="Other Host")
        self.member = Membership.objects.create(group=self.group, user=self.member_user, role=Membership.Role.MEMBER, display_name="Floater One")
        self.second_member = Membership.objects.create(group=self.group, user=self.second_member_user, role=Membership.Role.MEMBER, display_name="Floater Two")
        self.reader = Membership.objects.create(group=self.group, user=self.reader_user, role=Membership.Role.MEMBER, display_name="Reader")
        self.month = ChallengeMonth.objects.create(group=self.group, name="Floater Month", starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31), status=ChallengeMonth.Status.OPEN)
        self.other_month = ChallengeMonth.objects.create(group=self.group, name="Other Month", starts_on=date(2026, 9, 1), ends_on=date(2026, 9, 30), status=ChallengeMonth.Status.OPEN)
        self.team = Team.objects.create(month=self.month, name="Team One")
        self.host_assignment = ChallengeStaffAssignment.objects.create(month=self.month, membership=self.host_membership, role=ChallengeStaffAssignment.Role.HOST, assigned_by=self.owner)
        ChallengeStaffAssignment.objects.create(month=self.other_month, membership=self.other_host_membership, role=ChallengeStaffAssignment.Role.HOST, assigned_by=self.owner)
        TeamAssignment.objects.create(month=self.month, team=self.team, participant=self.reader)
        self.list_url = reverse("challenge-floater-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})

    def create_floater(self, membership=None):
        return ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=membership or self.member,
            role=ChallengeStaffAssignment.Role.FLOATER,
            assigned_by=self.host_user,
        )

    def test_host_assigns_multiple_floaters_without_participation_and_audits(self):
        self.client.force_login(self.host_user)
        for membership in (self.member, self.second_member):
            response = self.client.post(self.list_url, {"membership": membership.pk})
            self.assertRedirects(response, self.list_url)
        floaters = self.month.staff_assignments.filter(role=ChallengeStaffAssignment.Role.FLOATER, ended_at__isnull=True)
        self.assertEqual(floaters.count(), 2)
        for floater in floaters:
            self.assertIsNone(floater.team)
            self.assertEqual(floater.assigned_by, self.host_user)
            self.assertFalse(MonthEnrollment.objects.filter(month=self.month, participant=floater.membership).exists())
            self.assertFalse(TeamAssignment.objects.filter(month=self.month, participant=floater.membership).exists())
            self.assertTrue(AuditEvent.objects.filter(action="challenge.floater_assigned", object_id=str(floater.pk), actor=self.host_user).exists())

    def test_enrolled_reader_host_and_duplicate_active_floater_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.create_floater(self.reader)
        with self.assertRaises(ValidationError):
            self.create_floater(self.host_membership)
        self.create_floater()
        with self.assertRaises(ValidationError):
            self.create_floater()

    def test_active_floater_cannot_become_host_or_team_leader(self):
        self.create_floater()
        with self.assertRaises(ValidationError):
            ChallengeStaffAssignment.objects.create(month=self.month, membership=self.member, role=ChallengeStaffAssignment.Role.HOST)
        with self.assertRaises(ValidationError):
            ChallengeStaffAssignment.objects.create(month=self.month, membership=self.member, team=self.team, role=ChallengeStaffAssignment.Role.TEAM_LEADER)

    def test_host_form_rejects_active_floater_without_ending_it(self):
        floater = self.create_floater()
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("challenge-host-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}),
            {"membership": self.member.pk},
        )
        self.assertEqual(response.status_code, 200)
        floater.refresh_from_db()
        self.assertIsNone(floater.ended_at)
        self.assertFalse(self.month.staff_assignments.filter(membership=self.member, role=ChallengeStaffAssignment.Role.HOST).exists())

    def test_active_floater_is_blocked_by_all_enrollment_model_paths(self):
        floater = self.create_floater()
        with self.assertRaises(ValidationError):
            MonthEnrollment.objects.create(month=self.month, participant=self.member)
        with self.assertRaises(ValidationError):
            TeamAssignment.objects.create(month=self.month, participant=self.member, team=self.team)
        floater.refresh_from_db()
        self.assertIsNone(floater.ended_at)

    def test_active_floater_is_blocked_by_participant_and_team_ui_paths(self):
        floater = self.create_floater()
        self.client.force_login(self.host_user)
        enrollment_response = self.client.post(
            reverse("month-participant-add", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}),
            {"participant": self.member.pk, "team": ""},
        )
        self.assertEqual(enrollment_response.status_code, 200)
        self.assertContains(enrollment_response, "End this member&#x27;s active Floater assignment")
        team_response = self.client.post(
            reverse("team-assignment-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}),
            {"participant": self.member.pk, "team": self.team.pk},
        )
        self.assertEqual(team_response.status_code, 200)
        self.assertContains(team_response, "End this member&#x27;s active Floater assignment")
        self.assertFalse(MonthEnrollment.objects.filter(month=self.month, participant=self.member).exists())
        self.assertFalse(TeamAssignment.objects.filter(month=self.month, participant=self.member).exists())
        floater.refresh_from_db()
        self.assertIsNone(floater.ended_at)

    def test_ending_floater_preserves_history_and_allows_enrollment(self):
        floater = self.create_floater()
        self.client.force_login(self.host_user)
        end_url = reverse("challenge-floater-end", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": floater.pk})
        self.assertRedirects(self.client.post(end_url), self.list_url)
        floater.refresh_from_db()
        self.assertIsNotNone(floater.ended_at)
        self.assertEqual(floater.ended_by, self.host_user)
        self.assertTrue(AuditEvent.objects.filter(action="challenge.floater_ended", object_id=str(floater.pk), actor=self.host_user).exists())
        self.assertContains(self.client.get(self.list_url), "Floater History")
        MonthEnrollment.objects.create(month=self.month, participant=self.member, enrolled_by=self.owner)
        self.assertTrue(MonthEnrollment.objects.filter(month=self.month, participant=self.member).exists())

    def test_only_current_host_for_challenge_can_manage_floaters(self):
        for user in (self.owner, self.moderator, self.member_user, self.other_host_user):
            self.client.force_login(user)
            response = self.client.post(self.list_url, {"membership": self.member.pk})
            self.assertEqual(response.status_code, 403)
        self.assertFalse(self.month.staff_assignments.filter(role=ChallengeStaffAssignment.Role.FLOATER).exists())

    def test_floater_cannot_submit_or_gain_group_and_visibility_authority(self):
        self.create_floater()
        self.assertFalse(can_view_team_stats(self.member_user, self.month))
        self.client.force_login(self.member_user)
        response = self.client.post(
            reverse("submission-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}),
            {"title": "Blocked", "author": "Author", "book_format": BookSubmission.Format.EBOOK, "started_on": "2026-08-01", "completed_on": "2026-08-02", "submitted_pages": 100},
        )
        self.assertRedirects(response, self.month.get_absolute_url())
        self.assertFalse(BookSubmission.objects.filter(participant=self.member).exists())
        self.assertEqual(self.client.get(reverse("review-queue", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})).status_code, 200)

    def test_ended_floater_does_not_conflict_with_later_roles(self):
        floater = self.create_floater()
        floater.ended_at = floater.assigned_at
        floater.ended_by = self.host_user
        floater.save()
        host = ChallengeStaffAssignment.objects.create(month=self.month, membership=self.member, role=ChallengeStaffAssignment.Role.HOST, assigned_by=self.owner)
        self.assertIsNotNone(host.pk)
        self.assertTrue(ChallengeStaffAssignment.objects.filter(pk=floater.pk, ended_at__isnull=False).exists())
