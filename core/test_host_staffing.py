from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .models import AuditEvent, BookSubmission, ChallengeMonth, ChallengeStaffAssignment, Membership, MonthEnrollment, ReadingGroup, Team, TeamAssignment


class ChallengeHostStaffingTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("host-owner", password="test-password")
        self.delegated_moderator = User.objects.create_user("host-delegated", password="test-password")
        self.moderator = User.objects.create_user("host-moderator", password="test-password")
        self.member_user = User.objects.create_user("host-member", password="test-password")
        self.second_member_user = User.objects.create_user("host-member-two", password="test-password")
        self.inactive_user = User.objects.create_user("host-inactive", password="test-password")
        self.platform_owner = User.objects.create_superuser("host-platform-owner", password="test-password")
        self.group = ReadingGroup.objects.create(name="Host Group", slug="host-group")
        self.owner_membership = Membership.objects.create(
            group=self.group, user=self.owner, role=Membership.Role.OWNER, display_name="Owner"
        )
        self.delegated_membership = Membership.objects.create(
            group=self.group,
            user=self.delegated_moderator,
            role=Membership.Role.MODERATOR,
            display_name="Delegated Moderator",
            permission_overrides={"manage_months": True},
        )
        self.moderator_membership = Membership.objects.create(
            group=self.group, user=self.moderator, role=Membership.Role.MODERATOR, display_name="Moderator"
        )
        self.member = Membership.objects.create(
            group=self.group, user=self.member_user, role=Membership.Role.MEMBER, display_name="Host Member"
        )
        self.second_member = Membership.objects.create(
            group=self.group, user=self.second_member_user, role=Membership.Role.MEMBER, display_name="Second Host"
        )
        self.inactive_membership = Membership.objects.create(
            group=self.group,
            user=self.inactive_user,
            role=Membership.Role.MEMBER,
            display_name="Inactive Member",
            is_active=False,
        )
        self.platform_membership = Membership.objects.create(
            group=self.group,
            user=self.platform_owner,
            role=Membership.Role.OWNER,
            display_name="Platform Owner",
        )
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="Hosted Month",
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 8, 31),
            status=ChallengeMonth.Status.ACTIVE,
        )
        self.other_month = ChallengeMonth.objects.create(
            group=self.group,
            name="Other Hosted Month",
            starts_on=date(2026, 9, 1),
            ends_on=date(2026, 9, 30),
            status=ChallengeMonth.Status.DRAFT,
        )
        self.list_url = reverse(
            "challenge-host-list",
            kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk},
        )

    def assign(self, membership, month=None, assigned_by=None):
        return ChallengeStaffAssignment.objects.create(
            month=month or self.month,
            membership=membership,
            assigned_by=assigned_by or self.owner,
        )

    def test_multiple_hosts_and_same_person_across_challenges_are_supported(self):
        first = self.assign(self.member)
        second = self.assign(self.second_member)
        other_month = self.assign(self.member, month=self.other_month)

        self.assertNotEqual(first.pk, second.pk)
        self.assertNotEqual(first.pk, other_month.pk)
        self.assertEqual(self.month.staff_assignments.filter(ended_at__isnull=True).count(), 2)

    def test_duplicate_active_host_assignment_is_prevented_but_reassignment_after_ending_is_allowed(self):
        assignment = self.assign(self.member)
        with self.assertRaises(ValidationError):
            self.assign(self.member)

        assignment.ended_at = assignment.assigned_at
        assignment.ended_by = self.owner
        assignment.save(update_fields=["ended_at", "ended_by"])
        replacement = self.assign(self.member)
        self.assertNotEqual(assignment.pk, replacement.pk)

    def test_host_must_be_an_active_normal_membership_in_the_challenge_group(self):
        other_group = ReadingGroup.objects.create(name="Other Group", slug="other-host-group")
        other_membership = Membership.objects.create(
            group=other_group,
            user=get_user_model().objects.create_user("other-host-member"),
            role=Membership.Role.MEMBER,
            display_name="Other Member",
        )
        for ineligible in (other_membership, self.inactive_membership, self.platform_membership):
            with self.subTest(membership=ineligible.display_name), self.assertRaises(ValidationError):
                self.assign(ineligible)

    def test_host_assignment_does_not_create_participation_or_book_entry_access(self):
        self.assign(self.member)
        self.assertFalse(MonthEnrollment.objects.filter(month=self.month, participant=self.member).exists())
        self.assertFalse(TeamAssignment.objects.filter(month=self.month, participant=self.member).exists())

        self.client.force_login(self.member_user)
        response = self.client.get(
            reverse("submission-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        )
        self.assertRedirects(response, self.month.get_absolute_url())
        self.assertFalse(BookSubmission.objects.exists())

    def test_independently_enrolled_host_can_participate_normally(self):
        self.assign(self.member)
        MonthEnrollment.objects.create(month=self.month, participant=self.member, enrolled_by=self.owner)
        team = Team.objects.create(month=self.month, name="Host Team")
        TeamAssignment.objects.create(month=self.month, team=team, participant=self.member)

        self.client.force_login(self.member_user)
        response = self.client.post(
            reverse("submission-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}),
            {
                "title": "Host Reader Book",
                "author": "Author",
                "book_format": BookSubmission.Format.EBOOK,
                "started_on": "2026-08-01",
                "completed_on": "2026-08-12",
                "submitted_pages": 240,
                "reference_url": "",
                "notes": "",
            },
        )
        self.assertRedirects(response, self.month.get_absolute_url())
        self.assertTrue(BookSubmission.objects.filter(participant=self.member, title="Host Reader Book").exists())

    def test_owner_assignment_and_removal_are_attributed_audited_and_non_destructive(self):
        self.client.force_login(self.owner)
        response = self.client.post(self.list_url, {"membership": self.member.pk})
        self.assertRedirects(response, self.list_url)
        assignment = ChallengeStaffAssignment.objects.get(membership=self.member)
        self.assertEqual(assignment.assigned_by, self.owner)
        self.assertTrue(
            AuditEvent.objects.filter(
                action="challenge.host_assigned",
                actor=self.owner,
                object_id=str(assignment.pk),
            ).exists()
        )

        end_url = reverse(
            "challenge-host-end",
            kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk, "pk": assignment.pk},
        )
        response = self.client.post(end_url)
        self.assertRedirects(response, self.list_url)
        assignment.refresh_from_db()
        self.assertIsNotNone(assignment.ended_at)
        self.assertEqual(assignment.ended_by, self.owner)
        self.assertTrue(ChallengeStaffAssignment.objects.filter(pk=assignment.pk).exists())
        self.assertFalse(self.month.staff_assignments.filter(pk=assignment.pk, ended_at__isnull=True).exists())
        self.assertTrue(
            AuditEvent.objects.filter(
                action="challenge.host_ended",
                actor=self.owner,
                object_id=str(assignment.pk),
            ).exists()
        )

        history = self.client.get(self.list_url)
        self.assertContains(history, "Host History")
        self.assertContains(history, self.member.display_name)

    def test_host_management_uses_existing_month_capability(self):
        self.client.force_login(self.member_user)
        self.assertEqual(self.client.post(self.list_url, {"membership": self.second_member.pk}).status_code, 403)

        self.client.force_login(self.moderator)
        self.assertEqual(self.client.post(self.list_url, {"membership": self.second_member.pk}).status_code, 403)

        self.client.force_login(self.delegated_moderator)
        response = self.client.post(self.list_url, {"membership": self.second_member.pk})
        self.assertRedirects(response, self.list_url)
        self.assertTrue(
            ChallengeStaffAssignment.objects.filter(
                month=self.month,
                membership=self.second_member,
                assigned_by=self.delegated_moderator,
                ended_at__isnull=True,
            ).exists()
        )

    def test_platform_owner_can_manage_hosts_without_becoming_a_host(self):
        self.client.force_login(self.platform_owner)
        response = self.client.post(self.list_url, {"membership": self.member.pk})
        self.assertRedirects(response, self.list_url)
        assignment = ChallengeStaffAssignment.objects.get(month=self.month, membership=self.member)
        self.assertEqual(assignment.assigned_by, self.platform_owner)
        self.assertFalse(ChallengeStaffAssignment.objects.filter(membership=self.platform_membership).exists())
