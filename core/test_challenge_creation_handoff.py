from datetime import datetime
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import ChallengeMonth, ChallengeStaffAssignment, Membership, MonthEnrollment, ReadingGroup, Team, TeamAssignment
from .review_attention import needs_attention_summary


class ChallengeCreationHandoffTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner_user = User.objects.create_user("handoff-owner")
        self.moderator_user = User.objects.create_user("handoff-moderator")
        self.host_one_user = User.objects.create_user("handoff-host-one")
        self.host_two_user = User.objects.create_user("handoff-host-two")
        self.reader_user = User.objects.create_user("handoff-reader")
        self.leader_user = User.objects.create_user("handoff-leader")
        self.floater_user = User.objects.create_user("handoff-floater")
        self.platform_owner = User.objects.create_superuser("handoff-platform")
        self.group = ReadingGroup.objects.create(name="Handoff Group", slug="handoff-group")
        self.owner = self.member(self.owner_user, "Owner", Membership.Role.OWNER)
        self.moderator = self.member(
            self.moderator_user,
            "Moderator",
            Membership.Role.MODERATOR,
            {"manage_months": True},
        )
        self.host_one = self.member(self.host_one_user, "Host One")
        self.host_two = self.member(self.host_two_user, "Host Two")
        self.reader = self.member(self.reader_user, "Reader")
        self.leader = self.member(self.leader_user, "Leader")
        self.floater = self.member(self.floater_user, "Floater")
        self.create_url = reverse("month-create", kwargs={"group_slug": self.group.slug})

    def member(self, user, display_name, role=Membership.Role.MEMBER, overrides=None):
        return Membership.objects.create(
            group=self.group,
            user=user,
            display_name=display_name,
            role=role,
            permission_overrides=overrides or {},
        )

    def create_challenge(self, name="Handoff Challenge", hosts=None):
        self.client.force_login(self.owner_user)
        return self.client.post(self.create_url, {
            "name": name,
            "hosts": [membership.pk for membership in (hosts or [self.host_one])],
        })

    def test_create_page_only_exposes_title_and_eligible_host_selection(self):
        inactive_user = get_user_model().objects.create_user("handoff-inactive")
        inactive = self.member(inactive_user, "Inactive")
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])
        self.client.force_login(self.owner_user)
        response = self.client.get(self.create_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.context["form"].fields), {"name", "hosts"})
        choices = response.context["form"].fields["hosts"].queryset
        self.assertIn(self.host_one, choices)
        self.assertNotIn(inactive, choices)
        self.assertNotContains(response, "Description")
        self.assertNotContains(response, "Registration Opens")
        self.assertContains(response, "Add Host")
        self.assertContains(response, "Selected Hosts")
        self.assertContains(response, "No Hosts selected.")
        self.assertNotContains(response, 'type="checkbox"')

    def test_one_host_creation_is_draft_closed_blank_and_has_no_implicit_participation(self):
        response = self.create_challenge()
        month = ChallengeMonth.objects.get(name="Handoff Challenge")
        self.assertRedirects(response, reverse("challenge-settings", kwargs={
            "group_slug": self.group.slug,
            "month_pk": month.pk,
        }))
        self.assertEqual(month.status, ChallengeMonth.Status.DRAFT)
        self.assertFalse(month.registration_is_open)
        self.assertEqual(month.description, "")
        for field in (
            "registration_opens_at", "registration_closes_at", "starts_at", "ends_at",
            "final_announcement_at", "starts_on", "ends_on",
        ):
            self.assertIsNone(getattr(month, field))
        self.assertEqual(month.staff_assignments.filter(role=ChallengeStaffAssignment.Role.HOST).count(), 1)
        self.assertFalse(month.staff_assignments.filter(membership=self.owner).exists())
        self.assertFalse(MonthEnrollment.objects.filter(month=month).exists())
        self.assertFalse(TeamAssignment.objects.filter(month=month).exists())

    def test_multiple_selected_hosts_are_assigned_once_each(self):
        self.create_challenge("Multiple Hosts", [self.host_one, self.host_one, self.host_two])
        month = ChallengeMonth.objects.get(name="Multiple Hosts")
        assignments = month.staff_assignments.filter(role=ChallengeStaffAssignment.Role.HOST)
        self.assertEqual(assignments.count(), 2)
        self.assertEqual(set(assignments.values_list("membership_id", flat=True)), {self.host_one.pk, self.host_two.pk})
        self.assertEqual(needs_attention_summary(self.host_one_user)["total"], 1)
        self.assertEqual(needs_attention_summary(self.host_two_user)["total"], 1)
        self.assertEqual(needs_attention_summary(self.owner_user)["total"], 0)
        self.assertEqual(needs_attention_summary(self.platform_owner)["total"], 0)

    def test_host_assignment_notice_is_single_and_opens_correct_settings(self):
        self.create_challenge()
        month = ChallengeMonth.objects.get(name="Handoff Challenge")
        assignment = month.staff_assignments.get(membership=self.host_one)
        self.client.force_login(self.host_one_user)
        attention = self.client.get(reverse("needs-attention"))
        self.assertContains(attention, f"You were assigned as a Host for {month.name}.")
        notice_url = reverse("host-assignment-notice-open", kwargs={
            "group_slug": self.group.slug,
            "month_pk": month.pk,
            "pk": assignment.pk,
        })
        self.assertContains(attention, f'href="{notice_url}"')
        response = self.client.get(notice_url)
        self.assertRedirects(response, reverse("challenge-settings", kwargs={
            "group_slug": self.group.slug,
            "month_pk": month.pk,
        }))
        assignment.refresh_from_db()
        self.assertIsNotNone(assignment.host_assignment_notice_seen_at)
        self.assertEqual(needs_attention_summary(self.host_one_user)["total"], 0)
        self.client.get(notice_url)
        self.assertEqual(month.staff_assignments.filter(membership=self.host_one, ended_at__isnull=True).count(), 1)

    def test_removing_and_reassigning_host_creates_a_new_notice(self):
        self.create_challenge()
        month = ChallengeMonth.objects.get(name="Handoff Challenge")
        first = month.staff_assignments.get(membership=self.host_one)
        first.host_assignment_notice_seen_at = datetime(2026, 8, 1, tzinfo=ZoneInfo("UTC"))
        first.save(update_fields=["host_assignment_notice_seen_at"])
        remove_url = reverse("challenge-host-end", kwargs={
            "group_slug": self.group.slug, "month_pk": month.pk, "pk": first.pk,
        })
        self.client.force_login(self.owner_user)
        self.client.post(remove_url)
        host_list_url = reverse("challenge-host-list", kwargs={"group_slug": self.group.slug, "month_pk": month.pk})
        self.client.post(host_list_url, {"membership": self.host_one.pk})
        self.assertEqual(month.staff_assignments.filter(membership=self.host_one).count(), 2)
        self.assertEqual(needs_attention_summary(self.host_one_user)["total"], 1)

    def test_host_operates_all_settings_but_cannot_manage_host_staffing(self):
        self.create_challenge()
        month = ChallengeMonth.objects.get(name="Handoff Challenge")
        settings_kwargs = {"group_slug": self.group.slug, "month_pk": month.pk}
        self.client.force_login(self.host_one_user)
        for name in (
            "challenge-settings", "challenge-general-settings", "challenge-schedule-settings",
            "challenge-signup-settings", "challenge-progress-checkpoints",
        ):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name, kwargs=settings_kwargs)).status_code, 200)
        lifecycle = reverse("challenge-lifecycle-transition", kwargs={
            "group_slug": self.group.slug,
            "pk": month.pk,
            "target_status": ChallengeMonth.Status.UPCOMING,
        })
        self.assertEqual(self.client.get(lifecycle).status_code, 200)
        host_list = reverse("challenge-host-list", kwargs=settings_kwargs)
        self.assertEqual(self.client.post(host_list, {"membership": self.host_two.pk}).status_code, 403)

        general_url = reverse("challenge-general-settings", kwargs=settings_kwargs)
        self.assertRedirects(self.client.post(general_url, {
            "name": "Host Updated Handoff",
            "description": "Host supplied the operational description.",
        }), reverse("challenge-settings", kwargs=settings_kwargs))
        schedule_url = reverse("challenge-schedule-settings", kwargs=settings_kwargs)
        self.assertRedirects(self.client.post(schedule_url, {
            "registration_opens_at": "2026-09-01T09:00",
            "auto_open_registration": "on",
            "registration_closes_at": "2026-09-10T17:00",
            "auto_close_registration": "on",
            "starts_at": "2026-10-01T08:00",
            "auto_start_challenge": "on",
            "ends_at": "2026-10-31T20:00",
            "auto_end_challenge": "on",
            "final_announcement_at": "2026-11-01T10:00",
            "auto_complete_challenge": "on",
        }), reverse("challenge-settings", kwargs=settings_kwargs))
        month.refresh_from_db()
        self.assertEqual(month.name, "Host Updated Handoff")
        self.assertEqual(month.description, "Host supplied the operational description.")
        self.assertIsNotNone(month.registration_opens_at)
        self.assertEqual(month.starts_on.isoformat(), "2026-10-01")
        self.assertTrue(month.auto_complete_challenge)

    def test_reader_team_leader_and_floater_do_not_gain_challenge_settings(self):
        self.create_challenge()
        month = ChallengeMonth.objects.get(name="Handoff Challenge")
        team = Team.objects.create(month=month, name="Handoff Team")
        MonthEnrollment.objects.create(month=month, participant=self.leader)
        TeamAssignment.objects.create(month=month, participant=self.leader, team=team)
        ChallengeStaffAssignment.objects.create(
            month=month,
            membership=self.leader,
            team=team,
            role=ChallengeStaffAssignment.Role.TEAM_LEADER,
        )
        ChallengeStaffAssignment.objects.create(
            month=month,
            membership=self.floater,
            role=ChallengeStaffAssignment.Role.FLOATER,
        )
        settings_url = reverse("challenge-settings", kwargs={"group_slug": self.group.slug, "month_pk": month.pk})
        for user in (self.reader_user, self.leader_user, self.floater_user):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get(settings_url).status_code, 403)

    def test_group_manager_and_platform_override_remain_without_implicit_staffing(self):
        self.create_challenge()
        month = ChallengeMonth.objects.get(name="Handoff Challenge")
        general_url = reverse("challenge-general-settings", kwargs={"group_slug": self.group.slug, "month_pk": month.pk})
        for user in (self.owner_user, self.moderator_user, self.platform_owner):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get(general_url).status_code, 200)
        self.assertFalse(month.staff_assignments.filter(membership__user=self.moderator_user).exists())
        self.assertFalse(month.staff_assignments.filter(membership__user=self.platform_owner).exists())
