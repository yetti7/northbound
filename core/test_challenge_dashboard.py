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
            MonthTheme.objects.create(bonus_rule="legacy_fixed",
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

    def test_navigable_sections_use_accessible_shared_heading_links(self):
        self.client.force_login(self.reader_user)
        response = self.client.get(self.detail_url)
        destinations = (
            ("Hosts", reverse("challenge-host-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})),
            ("Floaters", reverse("challenge-floater-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})),
            ("Teams", self.teams_url),
            ("Themes", self.themes_url),
        )
        for label, destination in destinations:
            with self.subTest(label=label):
                # UI.2 retains the destinations, but consolidates staffing and
                # removes the duplicate Teams preview. Only Themes uses a heading link.
                if label in ("Hosts", "Floaters"):
                    self.assertContains(response, f'class="card interactive navigation-card compact-navigation-card" href="{destination}"')
                    self.assertContains(response, f"<h3>{label}</h3>", html=True)
                elif label == "Teams":
                    self.assertContains(response, f'<a class="stat stat-link" href="{destination}"><span>Teams</span>')
                else:
                    self.assertContains(response, f'<a class="challenge-section-heading-link" href="{destination}"><span>{label}</span><span class="challenge-section-arrow" aria-hidden="true">→</span></a>')
                self.assertEqual(self.client.get(destination).status_code, 200)
        self.assertNotContains(response, "Assign Host")
        self.assertNotContains(response, "Assign Floater")
        self.assertNotContains(response, "Add Theme")

    def test_theme_preview_is_bounded_linked_and_in_dashboard_order(self):
        self.client.force_login(self.owner_user)
        response = self.client.get(self.detail_url)
        content = response.content.decode()
        self.assertLess(content.index("Team Comparison"), content.index('class="challenge-themes-preview"'))
        self.assertLess(content.index('class="challenge-themes-preview"'), content.index("My Submissions"))
        self.assertContains(response, f'<a class="challenge-section-heading-link" href="{self.themes_url}">')
        self.assertContains(response, "Alpha Theme")
        self.assertContains(response, "Read something adventurous.")
        self.assertContains(response, "Bravo Theme")
        self.assertContains(response, "Charlie Theme")
        self.assertNotContains(response, "Delta Theme")
        self.assertNotContains(response, "Echo Theme")
        self.assertContains(response, "+2 more")
        self.assertContains(response, "themes-preview-list")
        self.assertContains(response, 'class="card theme-preview-item"', count=3)
        self.assertNotContains(response, "View Themes →")
        self.assertNotContains(response, "View all Themes →")

    def test_theme_preview_visibility_and_team_card_navigation_are_unchanged(self):
        hidden = self.month.themes.get(name="Alpha Theme")
        hidden.is_visible = False
        hidden.save(update_fields=["is_visible"])
        self.client.force_login(self.reader_user)
        reader_response = self.client.get(self.detail_url)
        self.assertNotContains(reader_response, hidden.name)
        # Ordinary Readers without standings access see the Teams stat only.
        self.assertEqual(reader_response.content.decode().count(f'href="{self.teams_url}"'), 1)
        self.assertContains(reader_response, f'<a class="stat stat-link" href="{self.teams_url}">')

        self.client.force_login(self.owner_user)
        owner_response = self.client.get(self.detail_url)
        self.assertContains(owner_response, hidden.name)

    def test_theme_preview_has_linked_empty_state(self):
        self.month.themes.all().delete()
        self.client.force_login(self.reader_user)
        response = self.client.get(self.detail_url)
        self.assertContains(response, "No active Themes.")
        self.assertNotContains(response, "No Themes configured.")
        self.assertContains(response, '<div class="empty-state challenge-section-empty">No active Themes.</div>')
        self.assertNotContains(response, "themes-preview-card")
        self.assertNotContains(response, "View Themes →")

    def test_empty_teams_and_themes_share_the_normalized_empty_state(self):
        self.month.teams.all().delete()
        self.month.themes.all().delete()
        self.client.force_login(self.reader_user)
        response = self.client.get(self.detail_url)
        # UI.2 shows zero Teams in its navigation stat, not an extra empty preview.
        self.assertContains(response, 'class="empty-state challenge-section-empty"', count=1)
        self.assertContains(response, f'<a class="stat stat-link" href="{self.teams_url}"><span>Teams</span><strong>0</strong>')
        self.assertNotContains(response, "No teams have been added.")
        self.assertContains(response, "No active Themes.")

    def test_no_future_games_or_botm_section_is_introduced(self):
        self.client.force_login(self.owner_user)
        response = self.client.get(self.detail_url)
        self.assertNotContains(response, '<span>Games</span>')
        self.assertNotContains(response, '<span>Book of the Month</span>')

    def test_detail_removes_helper_copy_but_keeps_registered_badge_and_navigation(self):
        self.month.registration_answer_editing_policy = ChallengeMonth.RegistrationAnswerEditingPolicy.NONE
        self.month.save(update_fields=["registration_answer_editing_policy"])
        MonthEnrollment.objects.create(month=self.month, participant=self.reader)
        floater_url = reverse("challenge-floater-list", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.client.force_login(self.reader_user)
        response = self.client.get(self.detail_url)
        self.assertContains(response, '<span class="pill status-badge status-success">Registered</span>', html=True)
        self.assertNotContains(response, "Registration responses are locked.")
        self.assertNotContains(response, ">Edit Registration</a>")
        self.assertNotContains(response, "Non-competing support for this Challenge.")
        self.assertContains(response, f'class="card interactive navigation-card compact-navigation-card" href="{floater_url}"')
        self.assertContains(response, "<h3>Floaters</h3>", html=True)

    def test_theme_management_permissions_are_unchanged(self):
        create_url = reverse("theme-create", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})
        self.client.force_login(self.owner_user)
        self.assertContains(self.client.get(self.themes_url), f'href="{create_url}">Add Theme</a>')
        self.client.force_login(self.reader_user)
        self.assertNotContains(self.client.get(self.themes_url), ">Add Theme</a>")
        self.assertEqual(self.client.get(create_url).status_code, 403)
