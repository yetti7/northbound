from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse

from .models import ReadingGroup


class SharedUiConventionTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner = user_model.objects.create_superuser(
            "ui-owner", "ui-owner@example.com", "test-password"
        )
        self.reader = user_model.objects.create_user(
            "ui-reader", "ui-reader@example.com", "test-password"
        )
        self.active_group = ReadingGroup.objects.create(
            name="Active UI Group", slug="active-ui-group", is_active=True
        )
        self.inactive_group = ReadingGroup.objects.create(
            name="Inactive UI Group", slug="inactive-ui-group", is_active=False
        )
        self.client.force_login(self.owner)

    def test_platform_dashboard_proves_header_navigation_and_summary_cards(self):
        response = self.client.get(reverse("config-dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="page-heading page-header"')
        self.assertContains(response, "navigation-card", count=11)
        self.assertContains(response, "summary-card", count=2)
        self.assertNotContains(response, 'class="button primary"')
        for section in ("System Management", "Operations &amp; Data", "Governance &amp; Recovery"):
            self.assertContains(response, section)
        for destination in (
            "config-user-list", "config-group-list", "platform-general-settings",
            "platform-hardcover-oauth", "platform-backups", "platform-system-status",
            "platform-storage-maintenance",
        ):
            with self.subTest(destination=destination):
                self.assertContains(response, reverse(destination))

    def test_platform_owner_menu_exposes_existing_my_account_password_path(self):
        dashboard = self.client.get(reverse("config-dashboard"))
        self.assertContains(dashboard, f'href="{reverse("account")}">My Account</a>')

        account = self.client.get(reverse("account"))
        self.assertContains(account, f'href="{reverse("password-change")}">Change Password</a>')

    def test_retired_settings_landing_redirects_to_platform_administration(self):
        response = self.client.get(reverse("platform-settings"))
        self.assertRedirects(response, reverse("config-dashboard"))

        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(reverse("platform-settings")).status_code, 403)

    def test_group_directory_keeps_single_authorized_primary_action_and_status_meaning(self):
        response = self.client.get(reverse("config-group-list"))

        self.assertContains(response, 'class="action-row page-actions"')
        self.assertContains(response, 'class="button primary"', count=1)
        self.assertContains(response, "status-success")
        self.assertContains(response, "status-inactive")
        self.assertContains(response, "Back to Platform Administration")

        self.client.force_login(self.reader)
        denied = self.client.get(reverse("config-group-list"))
        self.assertEqual(denied.status_code, 403)
        self.assertNotContains(denied, "Create Group", status_code=403)

    def test_general_settings_uses_shared_task_form_without_changing_destination(self):
        response = self.client.get(reverse("platform-general-settings"))

        self.assertContains(response, 'class="form-card task-card"')
        self.assertContains(response, 'class="stacked-form standard-form"')
        self.assertContains(response, 'class="action-row form-actions"')
        self.assertContains(response, "Back to Platform Administration")
        self.assertEqual(response.context["logical_parent_url"], reverse("config-dashboard"))

    def test_ui6_shared_forms_filters_and_mobile_table_contracts(self):
        account = self.client.get(reverse("account"))
        # HC.3 added the separately consented synchronization form to profile/PAT.
        self.assertContains(account, 'class="stacked-form standard-form"', count=3)
        self.assertContains(account, f'action="{reverse("reader-hardcover-sync-preferences")}"')
        self.assertContains(account, "Connecting Hardcover alone does not enable it")
        self.assertContains(account, 'class="form-actions"', count=3)

        audit = self.client.get(reverse("config-audit"))
        self.assertContains(audit, 'class="audit-filter-form filter-panel"')
        self.assertContains(audit, 'class="audit-filter-actions filter-actions"')
        self.assertContains(audit, 'class="table-wrap operational-table-region"')

        css_path = finders.find("css/theme.css")
        css = Path(css_path).read_text()
        self.assertIn(".stacked-form input,.stacked-form select,.stacked-form textarea{max-width:100%;min-width:0}", css)
        self.assertIn(".filter-actions", css)
        self.assertIn(".empty-state{max-width:100%;overflow-wrap:anywhere}", css)
        self.assertNotIn(".operational-table-region{display:none}", css)

    def test_shared_css_defines_button_status_focus_and_responsive_action_foundations(self):
        css_path = finders.find("css/theme.css")
        self.assertIsNotNone(css_path)
        css = Path(css_path).read_text()

        for convention in (
            ".page-header",
            ".page-actions",
            ".form-actions",
            ".navigation-card",
            ".summary-card",
            ".entity-card",
            ".task-card",
            ".button.secondary",
            ".button.subtle",
            ".button.danger",
            ".button.recovery",
            ".status-success",
            ".status-warning",
            ".status-attention",
            ".status-inactive",
            ".status-destructive",
            ".status-recovery-hold",
            ":focus-visible",
            "html[data-theme=\"light\"] .status-warning",
            "@media(max-width:420px)",
        ):
            self.assertIn(convention, css)
