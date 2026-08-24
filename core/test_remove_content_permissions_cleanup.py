from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .forms import MembershipPermissionsForm
from .models import ChallengeMonth, ChallengeStaffAssignment, Membership, ReadingGroup, Team
from .permissions import CAPABILITIES, DELEGABLE_CAPABILITIES


class RemoveContentPermissionsCleanupTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner_user = User.objects.create_user("remove-cleanup-owner", password="test-password")
        self.month_manager_user = User.objects.create_user("remove-cleanup-manager", password="test-password")
        self.remove_only_user = User.objects.create_user("remove-cleanup-remove-only", password="test-password")
        self.host_user = User.objects.create_user("remove-cleanup-host", password="test-password")
        self.reader_user = User.objects.create_user("remove-cleanup-reader", password="test-password")
        self.platform_owner = User.objects.create_superuser("remove-cleanup-platform", password="test-password")
        self.group = ReadingGroup.objects.create(name="Remove Cleanup", slug="remove-cleanup")
        self.owner = Membership.objects.create(group=self.group, user=self.owner_user, role=Membership.Role.OWNER, display_name="Owner")
        self.month_manager = Membership.objects.create(
            group=self.group,
            user=self.month_manager_user,
            role=Membership.Role.MEMBER,
            display_name="Month Manager",
            permission_overrides={"manage_months": True},
        )
        self.remove_only = Membership.objects.create(
            group=self.group,
            user=self.remove_only_user,
            role=Membership.Role.MEMBER,
            display_name="Remove Only",
            permission_overrides={"remove_content": True},
        )
        self.host = Membership.objects.create(group=self.group, user=self.host_user, role=Membership.Role.MEMBER, display_name="Host")
        self.reader = Membership.objects.create(group=self.group, user=self.reader_user, role=Membership.Role.MEMBER, display_name="Reader")

    def make_month(self, name, status=ChallengeMonth.Status.DRAFT):
        return ChallengeMonth.objects.create(
            group=self.group,
            name=name,
            starts_on=date(2027, 1, 1),
            ends_on=date(2027, 1, 31),
            status=status,
        )

    def delete_url(self, month):
        return reverse("month-delete", kwargs={"group_slug": self.group.slug, "pk": month.pk})

    def test_remove_content_remains_internal_but_is_absent_from_permissions_ui(self):
        self.assertIn("remove_content", CAPABILITIES)
        self.assertNotIn("remove_content", DELEGABLE_CAPABILITIES)
        self.client.force_login(self.owner_user)
        response = self.client.get(reverse(
            "participant-permissions-edit",
            kwargs={"group_slug": self.group.slug, "pk": self.remove_only.pk},
        ))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("remove_content", response.context["form"].fields)
        self.assertNotContains(response, "Delete Draft Months and manage legacy team-stat visibility")
        self.assertIn("view_hidden_stats", response.context["form"].fields)

    def test_permissions_save_preserves_remove_content_and_unknown_overrides(self):
        self.remove_only.permission_overrides = {
            "remove_content": False,
            "unknown_deferred_key": {"keep": True},
            "view_hidden_stats": True,
        }
        self.remove_only.save(update_fields=["permission_overrides"])
        payload = {"role": Membership.Role.MODERATOR}
        payload.update({capability: "inherit" for capability in DELEGABLE_CAPABILITIES})
        payload["manage_group_settings"] = "allow"
        payload["view_hidden_stats"] = "allow"
        form = MembershipPermissionsForm(payload, membership=self.remove_only)
        self.assertTrue(form.is_valid(), form.errors)
        updated = form.save()
        self.assertEqual(updated.permission_overrides, {
            "remove_content": False,
            "unknown_deferred_key": {"keep": True},
            "manage_group_settings": True,
            "view_hidden_stats": True,
        })

    def test_manage_months_authorizes_draft_deletion(self):
        month = self.make_month("Delegated Draft")
        self.client.force_login(self.month_manager_user)
        self.assertEqual(self.client.get(self.delete_url(month)).status_code, 200)
        response = self.client.post(self.delete_url(month))
        self.assertRedirects(response, reverse("group-detail", kwargs={"group_slug": self.group.slug}))
        self.assertFalse(ChallengeMonth.objects.filter(pk=month.pk).exists())

    def test_remove_content_without_manage_months_no_longer_authorizes_deletion(self):
        month = self.make_month("Remove Only Draft")
        self.client.force_login(self.remove_only_user)
        self.assertEqual(self.client.get(self.delete_url(month)).status_code, 403)
        self.assertEqual(self.client.post(self.delete_url(month)).status_code, 403)
        self.assertTrue(ChallengeMonth.objects.filter(pk=month.pk).exists())

    def test_host_staffing_alone_does_not_authorize_draft_deletion(self):
        month = self.make_month("Hosted Draft")
        ChallengeStaffAssignment.objects.create(
            month=month,
            membership=self.host,
            role=ChallengeStaffAssignment.Role.HOST,
            assigned_by=self.owner_user,
        )
        self.client.force_login(self.host_user)
        self.assertEqual(self.client.post(self.delete_url(month)).status_code, 403)
        self.assertTrue(ChallengeMonth.objects.filter(pk=month.pk).exists())

    def test_platform_owner_can_delete_without_identity_side_effects(self):
        month = self.make_month("Platform Draft")
        self.client.force_login(self.platform_owner)
        response = self.client.post(self.delete_url(month))
        self.assertRedirects(response, reverse("group-detail", kwargs={"group_slug": self.group.slug}))
        self.assertFalse(ChallengeMonth.objects.filter(pk=month.pk).exists())
        self.assertFalse(Membership.objects.filter(user=self.platform_owner).exists())
        self.assertFalse(ChallengeStaffAssignment.objects.filter(membership__user=self.platform_owner).exists())

    def test_non_draft_deletion_restriction_is_unchanged(self):
        month = self.make_month("Open Month", ChallengeMonth.Status.OPEN)
        self.client.force_login(self.month_manager_user)
        response = self.client.post(self.delete_url(month))
        self.assertRedirects(response, month.get_absolute_url())
        self.assertTrue(ChallengeMonth.objects.filter(pk=month.pk).exists())

    def test_remove_content_still_controls_legacy_visibility_configuration(self):
        month = self.make_month("Visibility Month", ChallengeMonth.Status.OPEN)
        Team.objects.create(month=month, name="Visibility Team")
        url = reverse("team-stats-settings", kwargs={"group_slug": self.group.slug, "month_pk": month.pk})
        self.client.force_login(self.remove_only_user)
        response = self.client.post(url, {"team_stats_visibility": ChallengeMonth.TeamStatsVisibility.EVERYONE})
        self.assertRedirects(response, month.get_absolute_url())
        month.refresh_from_db()
        self.assertEqual(month.team_stats_visibility, ChallengeMonth.TeamStatsVisibility.EVERYONE)

        self.remove_only.permission_overrides["remove_content"] = False
        self.remove_only.save(update_fields=["permission_overrides"])
        self.assertEqual(self.client.get(url).status_code, 403)

        self.client.force_login(self.platform_owner)
        self.assertEqual(self.client.get(url).status_code, 200)
