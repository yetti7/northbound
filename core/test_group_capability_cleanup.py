from datetime import date

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from .forms import MembershipPermissionsForm
from .models import ChallengeMonth, ChallengeStaffAssignment, Membership, ReadingGroup
from .permissions import CAPABILITIES, membership_has_capability


class GroupCapabilityCleanupTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("cleanup-owner", password="test-password")
        self.delegate = User.objects.create_user("cleanup-delegate", password="test-password")
        self.reader = User.objects.create_user("cleanup-reader", password="test-password")
        self.group = ReadingGroup.objects.create(name="Cleanup Group", slug="cleanup-group")
        self.owner_membership = Membership.objects.create(
            group=self.group, user=self.owner, role=Membership.Role.OWNER, display_name="Owner"
        )
        self.delegate_membership = Membership.objects.create(
            group=self.group, user=self.delegate, role=Membership.Role.MEMBER, display_name="Delegate"
        )
        self.reader_membership = Membership.objects.create(
            group=self.group, user=self.reader, role=Membership.Role.MEMBER, display_name="Reader"
        )
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="Cleanup Month",
            starts_on=date(2026, 9, 1),
            ends_on=date(2026, 9, 30),
            status=ChallengeMonth.Status.DRAFT,
        )

    def grant(self, capability):
        self.delegate_membership.permission_overrides = {capability: True}
        self.delegate_membership.save(update_fields=["permission_overrides"])
        self.client.force_login(self.delegate)

    def test_manage_teams_is_absent_from_registry_and_permissions_page(self):
        self.assertNotIn("manage_teams", CAPABILITIES)
        self.client.force_login(self.owner)
        response = self.client.get(reverse(
            "participant-permissions-edit",
            kwargs={"group_slug": self.group.slug, "pk": self.delegate_membership.pk},
        ))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("manage_teams", response.context["form"].fields)
        self.assertNotContains(response, "Create teams and manage team assignments")

    def test_permissions_form_preserves_unknown_and_deferred_overrides(self):
        self.delegate_membership.permission_overrides = {
            "unknown_restored_key": True,
            "review_submissions": False,
            "remove_content": True,
            "view_hidden_stats": False,
        }
        self.delegate_membership.save(update_fields=["permission_overrides"])
        payload = {"role": Membership.Role.MODERATOR}
        payload.update({capability: "inherit" for capability in CAPABILITIES})
        payload["manage_group_settings"] = "allow"
        payload["remove_content"] = "allow"
        payload["view_hidden_stats"] = "deny"
        form = MembershipPermissionsForm(payload, membership=self.delegate_membership)
        self.assertTrue(form.is_valid(), form.errors)
        updated = form.save()
        self.assertEqual(updated.role, Membership.Role.MODERATOR)
        self.assertEqual(updated.permission_overrides, {
            "unknown_restored_key": True,
            "manage_group_settings": True,
            "review_submissions": False,
            "remove_content": True,
            "view_hidden_stats": False,
        })

    def test_manage_group_settings_controls_group_configuration_not_challenge_teams(self):
        self.grant("manage_group_settings")
        self.assertEqual(self.client.get(reverse("group-edit", kwargs={"group_slug": self.group.slug})).status_code, 200)
        response = self.client.post(reverse("group-access-code", kwargs={"group_slug": self.group.slug}), {
            "access_code_visibility": ReadingGroup.AccessCodeVisibility.OWNER,
            "regenerate_code": True,
        })
        self.assertRedirects(response, reverse("group-detail", kwargs={"group_slug": self.group.slug}))
        self.assertEqual(self.client.get(reverse("group-hardcover-connection", kwargs={"group_slug": self.group.slug})).status_code, 302)
        self.assertEqual(self.client.get(reverse("team-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})).status_code, 403)

    def test_manage_participants_controls_group_membership_not_challenge_enrollment(self):
        self.grant("manage_participants")
        self.assertEqual(self.client.get(reverse("member-create", kwargs={"group_slug": self.group.slug})).status_code, 200)
        self.assertEqual(self.client.get(reverse("participant-deactivate", kwargs={"group_slug": self.group.slug, "pk": self.reader_membership.pk})).status_code, 200)
        self.assertEqual(self.client.get(reverse("month-participant-add", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})).status_code, 403)

    def test_manage_months_controls_core_month_and_host_management_not_themes(self):
        self.grant("manage_months")
        self.assertEqual(self.client.get(reverse("month-create", kwargs={"group_slug": self.group.slug})).status_code, 200)
        self.assertEqual(self.client.get(reverse("month-edit", kwargs={"group_slug": self.group.slug, "pk": self.month.pk})).status_code, 200)
        self.assertEqual(self.client.get(reverse("challenge-host-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})).status_code, 200)
        self.assertEqual(self.client.get(reverse("theme-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})).status_code, 403)

    def test_manage_announcements_controls_group_announcement_not_month_announcement(self):
        self.group.announcement_enabled = True
        self.group.announcement = "Old"
        self.group.save(update_fields=["announcement_enabled", "announcement"])
        self.grant("manage_announcements")
        response = self.client.post(reverse("group-announcement-update", kwargs={"group_slug": self.group.slug}), {"announcement": "Updated"})
        self.assertRedirects(response, reverse("group-detail", kwargs={"group_slug": self.group.slug}))
        self.group.refresh_from_db()
        self.assertEqual(self.group.announcement, "Updated")
        self.assertEqual(self.client.post(reverse("month-announcement-update", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk}), {"announcement_mode": "custom", "announcement": "No"}).status_code, 403)

    def test_manage_permissions_controls_role_changes(self):
        self.grant("manage_permissions")
        response = self.client.post(
            reverse("participant-role-edit", kwargs={"group_slug": self.group.slug, "pk": self.reader_membership.pk}),
            {"role": Membership.Role.MODERATOR, "is_active": True},
        )
        self.assertRedirects(response, reverse("participant-list", kwargs={"group_slug": self.group.slug}))
        self.reader_membership.refresh_from_db()
        self.assertEqual(self.reader_membership.role, Membership.Role.MODERATOR)

    def test_deferred_capabilities_remain_registered_and_effective(self):
        self.assertNotIn("review_submissions", CAPABILITIES)
        for capability in ("remove_content", "view_hidden_stats"):
            self.assertIn(capability, CAPABILITIES)
        self.delegate_membership.permission_overrides = {
            "review_submissions": True,
            "remove_content": False,
            "view_hidden_stats": True,
        }
        self.delegate_membership.save(update_fields=["permission_overrides"])
        self.assertFalse(membership_has_capability(self.delegate_membership, "review_submissions"))
        self.assertFalse(membership_has_capability(self.delegate_membership, "remove_content"))
        self.assertTrue(membership_has_capability(self.delegate_membership, "view_hidden_stats"))

    def test_legacy_group_override_does_not_create_host_authority(self):
        self.delegate_membership.permission_overrides = {
            "manage_group_settings": True,
            "manage_participants": True,
            "manage_months": True,
            "manage_announcements": True,
            "remove_content": True,
        }
        self.delegate_membership.save(update_fields=["permission_overrides"])
        self.client.force_login(self.delegate)
        self.assertFalse(ChallengeStaffAssignment.objects.filter(month=self.month, membership=self.delegate_membership).exists())
        self.assertEqual(self.client.get(reverse("team-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})).status_code, 403)
        self.assertEqual(self.client.get(reverse("theme-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})).status_code, 403)


class RetireManageTeamsMigrationTests(TransactionTestCase):
    migrate_from = [("core", "0030_alter_challengestaffassignment_role")]
    migrate_to = [("core", "0031_retire_manage_teams_capability")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        User = old_apps.get_model("auth", "User")
        ReadingGroup = old_apps.get_model("core", "ReadingGroup")
        Membership = old_apps.get_model("core", "Membership")
        user = User.objects.create(username="migration-cleanup-user")
        group = ReadingGroup.objects.create(name="Migration Cleanup", slug="migration-cleanup")
        self.membership_id = Membership.objects.create(
            group=group,
            user=user,
            display_name="Migration User",
            role="moderator",
            permission_overrides={
                "manage_teams": True,
                "review_submissions": False,
                "remove_content": True,
                "view_hidden_stats": False,
                "unknown_restored_key": True,
            },
        ).pk
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_migration_removes_only_manage_teams_override(self):
        from .models import Membership

        membership = Membership.objects.get(pk=self.membership_id)
        self.assertEqual(membership.permission_overrides, {
            "review_submissions": False,
            "remove_content": True,
            "view_hidden_stats": False,
            "unknown_restored_key": True,
        })
