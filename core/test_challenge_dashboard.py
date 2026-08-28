from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import ChallengeMonth, ChallengeStaffAssignment, Membership, MonthEnrollment, MonthTheme, ReadingGroup, Team


class ChallengeDashboardCleanupTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner_user = User.objects.create_user("dashboard-owner")
        self.reader_user = User.objects.create_user("dashboard-reader")
        self.group = ReadingGroup.objects.create(name="Dashboard Group", slug="dashboard-group")
        self.owner = Membership.objects.create(
            group=self.group,
            user=self.owner_user,
            role=Membership.Role.OWNER,
            display_name="Dashboard Owner",
        )
        self.reader = Membership.objects.create(group=self.group, user=self.reader_user, display_name="Dashboard Reader")
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="Dashboard Challenge",
            starts_on=date(2026, 9, 1),
            ends_on=date(2026, 9, 30),
            status=ChallengeMonth.Status.ACTIVE,
        )
        ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=self.owner,
            role=ChallengeStaffAssignment.Role.HOST,
        )
        Team.objects.create(month=self.month, name="North Team")
        for name, description in (
            ("Alpha Theme", "Read something adventurous."),
            ("Bravo Theme", ""),
            ("Charlie Theme", "A concise third description."),
            ("Delta Theme", "Fourth preview overflow."),
            ("Echo Theme", "Fifth preview overflow."),
        ):
            MonthTheme.objects.create(
                month=self.month,
                name=name,
                description=description,
                starts_on=self.month.starts_on,
                ends_on=self.month.ends_on,
            )
        self.detail_url = self.month.get_absolute_url()
        self.teams_url = reverse("team-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.themes_url = reverse("theme-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.visibility_url = reverse("challenge-visibility-settings", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})

    def test_header_cleanup_preserves_dashboard_navigation_and_visibility(self):
        self.client.force_login(self.owner_user)
        response = self.client.get(self.detail_url)
        self.assertNotContains(response, f'<a class="button" href="{self.teams_url}">Teams</a>')
        self.assertNotContains(response, f'<a class="button" href="{self.themes_url}">Themes</a>')
        self.assertContains(response, f'href="{self.teams_url}"><span>Teams</span>')
        self.assertNotContains(response, ">Visibility</a>")

    def test_theme_preview_is_bounded_linked_and_in_dashboard_order(self):
        self.client.force_login(self.owner_user)
        response = self.client.get(self.detail_url)
        content = response.content.decode()
        self.assertLess(content.index("Team Comparison"), content.index('class="challenge-themes-preview"'))
        self.assertLess(content.index('class="challenge-themes-preview"'), content.index("My Submissions"))
        self.assertContains(response, f'<a class="card interactive themes-preview-card" href="{self.themes_url}">')
        self.assertContains(response, "Alpha Theme")
        self.assertContains(response, "Read something adventurous.")
        self.assertContains(response, "Bravo Theme")
        self.assertContains(response, "Charlie Theme")
        self.assertNotContains(response, "Delta Theme")
        self.assertNotContains(response, "Echo Theme")
        self.assertContains(response, "+2 more")
        self.assertContains(response, "themes-preview-list")

    def test_theme_preview_has_linked_empty_state(self):
        self.month.themes.all().delete()
        self.client.force_login(self.reader_user)
        response = self.client.get(self.detail_url)
        self.assertContains(response, "No active Themes.")
        self.assertNotContains(response, "No Themes configured.")
        self.assertContains(response, f'<a class="card interactive themes-preview-card" href="{self.themes_url}">')

    def test_detail_removes_helper_copy_but_keeps_registered_badge_and_navigation(self):
        self.month.registration_answer_editing_policy = ChallengeMonth.RegistrationAnswerEditingPolicy.NONE
        self.month.save(update_fields=["registration_answer_editing_policy"])
        MonthEnrollment.objects.create(month=self.month, participant=self.reader)
        floater_url = reverse("challenge-floater-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.client.force_login(self.reader_user)
        response = self.client.get(self.detail_url)
        self.assertContains(response, '<span class="pill">Registered</span>')
        self.assertNotContains(response, "Registration responses are locked.")
        self.assertNotContains(response, ">Edit Registration</a>")
        self.assertNotContains(response, "Non-competing support for this Challenge.")
        self.assertContains(response, f'href="{floater_url}">Floaters</a>')

    def test_theme_management_permissions_are_unchanged(self):
        create_url = reverse("theme-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.client.force_login(self.owner_user)
        self.assertContains(self.client.get(self.themes_url), f'href="{create_url}">Add Theme</a>')
        self.client.force_login(self.reader_user)
        self.assertNotContains(self.client.get(self.themes_url), ">Add Theme</a>")
        self.assertEqual(self.client.get(create_url).status_code, 403)
