from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    ChallengeMonth,
    ChallengeSignupQuestion,
    ChallengeStaffAssignment,
    Membership,
    ProgressCheckpoint,
    ReadingGroup,
)


class ChallengeSettingsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner_user = User.objects.create_user("settings-owner")
        self.host_user = User.objects.create_user("settings-host")
        self.reader_user = User.objects.create_user("settings-reader")
        self.group = ReadingGroup.objects.create(name="Settings Group", slug="settings-group")
        self.owner = Membership.objects.create(
            group=self.group,
            user=self.owner_user,
            role=Membership.Role.OWNER,
            display_name="Owner",
        )
        self.host = Membership.objects.create(group=self.group, user=self.host_user, display_name="Host")
        self.reader = Membership.objects.create(group=self.group, user=self.reader_user, display_name="Reader")
        self.month = ChallengeMonth.objects.create(
            group=self.group,
            name="Settings Challenge",
            description="A concise Challenge description.",
            starts_on=date(2026, 9, 1),
            ends_on=date(2026, 9, 30),
            starts_at=datetime(2026, 9, 1, 9, 0, tzinfo=ZoneInfo("America/New_York")),
            ends_at=datetime(2026, 9, 30, 21, 0, tzinfo=ZoneInfo("America/New_York")),
            registration_opens_at=datetime(2026, 8, 20, 9, 0, tzinfo=ZoneInfo("America/New_York")),
            registration_closes_at=datetime(2026, 9, 5, 21, 0, tzinfo=ZoneInfo("America/New_York")),
            status=ChallengeMonth.Status.UPCOMING,
            registration_is_open=True,
        )
        ChallengeStaffAssignment.objects.create(
            month=self.month,
            membership=self.host,
            role=ChallengeStaffAssignment.Role.HOST,
            host_assignment_notice_seen_at=timezone.now(),
        )
        ChallengeSignupQuestion.objects.create(
            month=self.month,
            wording="Planning question",
            question_type=ChallengeSignupQuestion.QuestionType.SHORT_TEXT,
            position=1,
        )
        ProgressCheckpoint.objects.create(
            month=self.month,
            scheduled_at=datetime(2026, 9, 7, 9, 0, tzinfo=ZoneInfo("America/New_York")),
            position=1,
        )

    @property
    def detail_url(self):
        return self.month.get_absolute_url()

    @property
    def settings_url(self):
        return reverse("challenge-settings", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})

    @property
    def general_url(self):
        return reverse("challenge-general-settings", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})

    @property
    def schedule_url(self):
        return reverse("challenge-schedule-settings", kwargs={"group_slug": self.group.slug, "month_pk": self.month.pk})

    def schedule_payload(self, **overrides):
        values = {
            "registration_opens_at": "2026-08-21T08:00",
            "auto_open_registration": "on",
            "registration_closes_at": "2026-09-06T20:00",
            "starts_at": "2026-09-02T10:00",
            "auto_start_challenge": "on",
            "ends_at": "2026-10-01T20:00",
            "auto_end_challenge": "on",
            "final_announcement_at": "2026-10-02T12:00",
            "auto_complete_challenge": "on",
        }
        values.update(overrides)
        return values

    def test_detail_replaces_configuration_buttons_with_single_settings_entry(self):
        self.client.force_login(self.owner_user)
        response = self.client.get(self.detail_url)
        self.assertContains(response, f'href="{self.settings_url}">Challenge Settings</a>')
        self.assertNotContains(response, ">Signup Questions</a>")
        self.assertNotContains(response, ">Progress Checkpoints</a>")
        self.assertNotContains(response, ">Change Stage")
        self.assertContains(response, "themes-preview-card")
        self.assertNotContains(response, ">Visibility</a>")

    def test_settings_landing_summarizes_existing_workflows(self):
        self.client.force_login(self.owner_user)
        response = self.client.get(self.settings_url)
        self.assertEqual(response.status_code, 200)
        for heading in ("General", "Schedule", "Registration", "Progress Checkpoints", "Visibility", "Lifecycle"):
            self.assertContains(response, heading)
        self.assertContains(response, "1 custom question configured")
        self.assertContains(response, "1 checkpoint configured")
        self.assertContains(response, "Auto on")
        self.assertContains(response, "Edit General Settings")
        self.assertContains(response, "Edit Schedule")
        self.assertContains(response, f'href="{self.general_url}">Edit General Settings</a>')
        self.assertContains(response, f'href="{self.schedule_url}">Edit Schedule</a>')
        self.assertContains(response, "Manage Registration")
        self.assertContains(response, "Manage Checkpoints")
        self.assertContains(response, "Manage Visibility")
        self.assertContains(response, "Move Back to Draft")
        self.assertContains(response, "Move Forward to Active")
        self.assertContains(response, "challenge-settings-grid")

    def test_host_can_use_all_operational_settings_and_teams_navigation(self):
        self.client.force_login(self.host_user)
        settings = self.client.get(self.settings_url)
        self.assertEqual(settings.status_code, 200)
        self.assertContains(settings, "Edit General Settings")
        self.assertContains(settings, "Edit Schedule")
        self.assertContains(settings, "Manage Registration")
        self.assertContains(settings, "Manage Checkpoints")
        self.assertContains(settings, "Move Forward to Active")
        detail = self.client.get(self.detail_url)
        self.assertContains(detail, "Challenge Settings")
        self.assertContains(detail, ">Teams</a>")
        self.assertNotContains(detail, ">Add Team</a>")
        self.assertContains(detail, "themes-preview-card")

    def test_ordinary_reader_does_not_gain_settings_access_and_keeps_register_action(self):
        self.client.force_login(self.reader_user)
        detail = self.client.get(self.detail_url)
        self.assertNotContains(detail, "Challenge Settings")
        self.assertContains(detail, ">Register</a>")
        self.assertContains(detail, "themes-preview-card")
        self.assertEqual(self.client.get(self.settings_url).status_code, 403)

    def test_settings_and_child_workflows_have_logical_settings_navigation(self):
        self.client.force_login(self.owner_user)
        response = self.client.get(self.settings_url)
        self.assertEqual(response.context["logical_parent_url"], self.detail_url)
        for name, kwargs in (
            ("month-edit", {"pk": self.month.pk}),
            ("challenge-general-settings", {"month_pk": self.month.pk}),
            ("challenge-schedule-settings", {"month_pk": self.month.pk}),
            ("challenge-signup-settings", {"month_pk": self.month.pk}),
            ("challenge-progress-checkpoints", {"month_pk": self.month.pk}),
            ("challenge-lifecycle-transition", {"pk": self.month.pk, "target_status": ChallengeMonth.Status.ACTIVE}),
        ):
            with self.subTest(name=name):
                child = self.client.get(reverse(name, kwargs={"group_slug": self.group.slug, **kwargs}))
                self.assertEqual(child.context["logical_parent_url"], self.settings_url)

    def test_general_settings_updates_identity_without_touching_schedule(self):
        self.client.force_login(self.owner_user)
        schedule_before = {
            field: getattr(self.month, field)
            for field in (
                "registration_opens_at", "registration_closes_at", "starts_at", "ends_at",
                "final_announcement_at", "auto_open_registration", "auto_close_registration",
                "auto_start_challenge", "auto_end_challenge", "auto_complete_challenge",
            )
        }
        response = self.client.post(self.general_url, {
            "name": "Renamed Challenge",
            "description": "Updated general description.",
        })
        self.assertRedirects(response, self.settings_url)
        self.month.refresh_from_db()
        self.assertEqual(self.month.name, "Renamed Challenge")
        self.assertEqual(self.month.description, "Updated general description.")
        for field, expected in schedule_before.items():
            self.assertEqual(getattr(self.month, field), expected)
        form = self.client.get(self.general_url).context["form"]
        self.assertEqual(set(form.fields), {"name", "description"})

    def test_schedule_settings_updates_existing_scheduler_fields_without_touching_identity(self):
        self.client.force_login(self.owner_user)
        original_identity = (self.month.name, self.month.description)
        response = self.client.post(self.schedule_url, self.schedule_payload())
        self.assertRedirects(response, self.settings_url)
        self.month.refresh_from_db()
        self.assertEqual((self.month.name, self.month.description), original_identity)
        self.assertEqual(timezone.localtime(self.month.registration_opens_at, ZoneInfo(self.group.timezone)).strftime("%Y-%m-%dT%H:%M"), "2026-08-21T08:00")
        self.assertFalse(self.month.auto_close_registration)
        self.assertTrue(self.month.auto_start_challenge)
        self.assertTrue(self.month.auto_complete_challenge)
        self.assertEqual(set(self.client.get(self.schedule_url).context["form"].fields), {
            "registration_opens_at", "auto_open_registration", "registration_closes_at",
            "auto_close_registration", "starts_at", "auto_start_challenge", "ends_at",
            "auto_end_challenge", "final_announcement_at", "auto_complete_challenge",
        })

    def test_schedule_validation_remains_enforced(self):
        self.client.force_login(self.owner_user)
        response = self.client.post(self.schedule_url, self.schedule_payload(
            registration_opens_at="2026-09-10T08:00",
            registration_closes_at="2026-09-01T08:00",
        ))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Registration closing date/time cannot precede registration opening date/time.")
        self.month.refresh_from_db()
        self.assertEqual(self.month.registration_opens_at, datetime(2026, 8, 20, 9, 0, tzinfo=ZoneInfo("America/New_York")))

    def test_host_can_update_general_and_schedule_settings(self):
        self.client.force_login(self.host_user)
        general = self.client.post(self.general_url, {
            "name": "Host Configured Challenge",
            "description": "Configured by its assigned Host.",
        })
        self.assertRedirects(general, self.settings_url)
        schedule = self.client.post(self.schedule_url, self.schedule_payload())
        self.assertRedirects(schedule, self.settings_url)
        self.month.refresh_from_db()
        self.assertEqual(self.month.name, "Host Configured Challenge")
        self.assertEqual(self.month.description, "Configured by its assigned Host.")
        self.assertTrue(self.month.auto_open_registration)
        self.assertTrue(self.month.auto_start_challenge)

    def test_reader_cannot_access_general_or_schedule(self):
        self.client.force_login(self.reader_user)
        self.assertEqual(self.client.get(self.general_url).status_code, 403)
        self.assertEqual(self.client.get(self.schedule_url).status_code, 403)

    def test_scheduler_uses_schedule_values_saved_through_schedule_form(self):
        self.client.force_login(self.owner_user)
        self.month.registration_is_open = False
        self.month.save(update_fields=["registration_is_open"])
        due_local = timezone.localtime(timezone.now(), ZoneInfo(self.group.timezone)).replace(second=0, microsecond=0)
        payload = self.schedule_payload(registration_opens_at=due_local.strftime("%Y-%m-%dT%H:%M"))
        self.client.post(self.schedule_url, payload)
        self.month.refresh_from_db()
        actions = self.month.apply_scheduled_actions(now=timezone.now())
        self.assertIn("registration_opened", actions)
        self.month.refresh_from_db()
        self.assertTrue(self.month.registration_is_open)
