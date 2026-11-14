from datetime import date, datetime
from unittest.mock import patch
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
        self.assertContains(response, "challenge-themes-preview")
        self.assertNotContains(response, ">Visibility</a>")

    def test_settings_landing_summarizes_existing_workflows(self):
        self.client.force_login(self.owner_user)
        response = self.client.get(self.settings_url)
        self.assertEqual(response.status_code, 200)
        for heading in ("General", "Schedule", "Registration", "Progress Checkpoints", "Visibility", "Manage this Challenge"):
            self.assertContains(response, heading)
        self.assertContains(response, "1 custom question configured")
        self.assertContains(response, "1 checkpoint configured")
        self.assertContains(response, "Automatic transitions")
        self.assertContains(response, f'href="{self.general_url}"')
        self.assertContains(response, f'href="{self.schedule_url}"')
        self.assertContains(response, "Signup Questions")
        self.assertContains(response, "Progress Checkpoints")
        self.assertContains(response, "Competition Visibility")
        self.assertContains(response, "Return to an earlier stage")
        self.assertContains(response, "Start Challenge")
        self.assertContains(response, "challenge-settings-grid")

    def test_host_can_use_all_operational_settings_and_teams_navigation(self):
        self.client.force_login(self.host_user)
        settings = self.client.get(self.settings_url)
        self.assertEqual(settings.status_code, 200)
        self.assertContains(settings, self.general_url)
        self.assertContains(settings, self.schedule_url)
        self.assertContains(settings, "Signup Questions")
        self.assertContains(settings, "Progress Checkpoints")
        self.assertContains(settings, "Start Challenge")
        detail = self.client.get(self.detail_url)
        self.assertContains(detail, "Challenge Settings")
        self.assertContains(detail, "><span>Teams</span>")
        self.assertNotContains(detail, ">Add Team</a>")
        self.assertContains(detail, "challenge-themes-preview")

    def test_ordinary_reader_does_not_gain_settings_access_and_keeps_register_action(self):
        self.client.force_login(self.reader_user)
        detail = self.client.get(self.detail_url)
        self.assertNotContains(detail, "Challenge Settings")
        self.assertContains(detail, ">Register</a>")
        self.assertContains(detail, "challenge-themes-preview")
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
        due_local = datetime(2026, 8, 21, 8, 0, tzinfo=ZoneInfo(self.group.timezone))
        payload = self.schedule_payload(registration_opens_at=due_local.strftime("%Y-%m-%dT%H:%M"))
        with patch("core.views.timezone.now", return_value=due_local):
            response = self.client.post(self.schedule_url, payload)
        self.assertEqual(response.status_code, 302)
        self.month.refresh_from_db()
        self.assertTrue(self.month.registration_is_open)
        self.assertTrue(self.month.registration_open_handled)
        self.assertEqual(self.month.apply_scheduled_actions(now=due_local), [])

    def registration_action_url(self, action):
        return reverse("challenge-registration-action", kwargs={
            "group_slug": self.group.slug, "month_pk": self.month.pk, "action": action,
        })

    def lifecycle_url(self, target):
        return reverse("challenge-lifecycle-transition", kwargs={
            "group_slug": self.group.slug, "pk": self.month.pk, "target_status": target,
        })

    def test_registration_controls_follow_each_automation_toggle(self):
        self.client.force_login(self.host_user)
        for auto_open in (False, True):
            for auto_close in (False, True):
                for is_open in (False, True):
                    with self.subTest(auto_open=auto_open, auto_close=auto_close, is_open=is_open):
                        ChallengeMonth.objects.filter(pk=self.month.pk).update(
                            auto_open_registration=auto_open, auto_close_registration=auto_close,
                            registration_is_open=is_open,
                        )
                        response = self.client.get(self.settings_url)
                        self.assertContains(response, 'class="challenge-immediate"')
                        self.assertContains(response, 'id="manage-registration-title"')
                        self.assertContains(response, 'id="manage-challenge-title"')
                        for action, available in {
                            "open": not is_open and not auto_open, "close": is_open and not auto_close,
                        }.items():
                            self.assertContains(response, self.registration_action_url(action), count=2 * int(available))

    def test_manual_registration_is_post_only_authorized_audited_and_idempotent(self):
        from .models import AuditEvent, MonthEnrollment

        enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.reader)
        self.month.auto_close_registration = False
        self.month.save(update_fields=["auto_close_registration"])
        url = self.registration_action_url("close")
        self.client.force_login(self.reader_user)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_login(self.host_user)
        self.assertEqual(self.client.get(url).status_code, 405)
        for _ in range(2):
            self.assertRedirects(self.client.post(url), self.schedule_url)
        self.month.refresh_from_db()
        enrollment.refresh_from_db()
        self.assertFalse(self.month.registration_is_open)
        self.assertTrue(enrollment.is_active)
        self.assertEqual(AuditEvent.objects.filter(action="challenge.registration_closed").count(), 1)

    def test_registration_save_reschedules_without_advancing_lifecycle(self):
        from .scheduling import process_due_challenge_schedules

        now = datetime(2026, 9, 3, 12, tzinfo=ZoneInfo(self.group.timezone))
        self.client.force_login(self.owner_user)
        payload = self.schedule_payload(
            starts_at="2026-09-01T09:00", ends_at="2026-09-30T21:00",
            auto_complete_challenge="", final_announcement_at="",
            registration_opens_at="2026-09-04T08:00", auto_close_registration="on",
            registration_closes_at="2026-09-05T21:00",
        )
        with patch("core.views.timezone.now", return_value=now):
            self.assertEqual(self.client.post(self.schedule_url, payload).status_code, 302)
            self.month.refresh_from_db()
            self.assertEqual(self.month.status, self.month.Status.UPCOMING)
            self.assertFalse(self.month.registration_is_open)
            payload["registration_opens_at"] = "2026-09-02T08:00"
            payload["registration_closes_at"] = "2026-09-02T21:00"
            self.assertEqual(self.client.post(self.schedule_url, payload).status_code, 302)
            self.month.refresh_from_db()
            self.assertFalse(self.month.registration_is_open)
            self.assertTrue(self.month.registration_close_handled)
            payload["registration_closes_at"] = "2026-09-05T21:00"
            self.assertEqual(self.client.post(self.schedule_url, payload).status_code, 302)
        self.month.refresh_from_db()
        self.assertTrue(self.month.registration_is_open)
        self.assertFalse(self.month.registration_close_handled)
        process_due_challenge_schedules(now=now)
        self.month.refresh_from_db()
        self.assertEqual(self.month.status, self.month.Status.ACTIVE)

    def test_save_holds_later_overdue_milestones_until_separate_confirmations(self):
        from .scheduling import process_due_challenge_schedules

        self.client.force_login(self.owner_user)
        now = datetime(2026, 10, 3, 12, tzinfo=ZoneInfo(self.group.timezone))
        with patch("core.views.timezone.now", return_value=now):
            self.assertEqual(self.client.post(self.schedule_url, self.schedule_payload()).status_code, 302)
            self.month.refresh_from_db()
            self.assertEqual(self.month.status, self.month.Status.ACTIVE)
            self.assertCountEqual(self.month.held_schedule_milestones, ["finalizing", "completed"])
            for target in ("finalizing", "completed"):
                process_due_challenge_schedules(now=now)
                self.month.refresh_from_db()
                self.assertNotEqual(self.month.status, target)
                url = self.lifecycle_url(target)
                self.assertRedirects(self.client.post(url), url)
                confirmation = self.client.get(url).context["transition_confirmation"]
                self.assertRedirects(self.client.post(url, {"transition_confirmation": confirmation}), self.detail_url)
        self.month.refresh_from_db()
        self.assertEqual(self.month.status, self.month.Status.COMPLETED)
        self.assertEqual(self.month.held_schedule_milestones, [])

    def test_schedule_save_requires_confirmation_for_immediate_finalization(self):
        self.month.transition_to(self.month.Status.ACTIVE)
        self.month.transition_to(self.month.Status.FINALIZING)
        self.client.force_login(self.host_user)
        now = datetime(2026, 10, 3, 12, tzinfo=ZoneInfo(self.group.timezone))
        payload = self.schedule_payload()
        with patch("core.views.timezone.now", return_value=now):
            preview = self.client.post(self.schedule_url, payload)
            self.assertContains(preview, "Save and Make Results Final")
            self.month.refresh_from_db()
            self.assertEqual(self.month.status, self.month.Status.FINALIZING)
            self.assertIsNone(self.month.final_announcement_at)
            payload["confirm_schedule"] = preview.context["schedule_confirmation"]
            self.assertEqual(self.client.post(self.schedule_url, payload).status_code, 302)
        self.month.refresh_from_db()
        self.assertEqual(self.month.status, self.month.Status.COMPLETED)

    def test_backward_transition_rejects_overdue_but_allows_future_automation(self):
        self.month.transition_to(self.month.Status.ACTIVE)
        self.client.force_login(self.host_user)
        url = self.lifecycle_url("upcoming")
        with patch("core.views.timezone.now", return_value=self.month.starts_at):
            response = self.client.post(url, follow=True)
        self.assertContains(response, "Can’t move this challenge back yet.")
        self.month.refresh_from_db()
        self.assertEqual(self.month.status, self.month.Status.ACTIVE)
        with patch("core.views.timezone.now", return_value=self.month.registration_opens_at):
            self.assertRedirects(self.client.post(url), self.detail_url)
        self.month.refresh_from_db()
        self.assertEqual(self.month.status, self.month.Status.UPCOMING)

    def test_future_lifecycle_schedule_does_not_reverse_or_change_registration(self):
        self.month.transition_to(self.month.Status.ACTIVE)
        self.client.force_login(self.host_user)
        now = datetime(2026, 8, 21, 12, tzinfo=ZoneInfo(self.group.timezone))
        payload = self.schedule_payload(
            registration_opens_at="2026-08-20T09:00", auto_close_registration="on",
            registration_closes_at="2026-09-05T21:00",
        )
        with patch("core.views.timezone.now", return_value=now):
            self.assertEqual(self.client.post(self.schedule_url, payload).status_code, 302)
        self.month.refresh_from_db()
        self.assertEqual(self.month.status, self.month.Status.ACTIVE)
        self.assertTrue(self.month.registration_is_open)
        self.assertFalse(self.month.registration_open_handled)

    def test_completed_and_archived_schedule_and_registration_are_read_only(self):
        self.client.force_login(self.owner_user)
        for stage in (self.month.Status.ACTIVE, self.month.Status.FINALIZING, self.month.Status.COMPLETED, self.month.Status.ARCHIVED):
            self.month.transition_to(stage)
            if stage in (self.month.Status.COMPLETED, self.month.Status.ARCHIVED):
                self.assertEqual(self.client.get(self.schedule_url).status_code, 403)
                self.assertEqual(self.client.post(self.schedule_url, self.schedule_payload()).status_code, 403)
                self.client.post(self.registration_action_url("close"))
                self.month.refresh_from_db()
                self.assertTrue(self.month.registration_is_open)

    def test_registration_dates_are_required_only_for_enabled_automation(self):
        self.client.force_login(self.host_user)
        payload = self.schedule_payload(
            registration_opens_at="", registration_closes_at="",
            auto_start_challenge="", auto_end_challenge="", auto_complete_challenge="",
        )
        response = self.client.post(self.schedule_url, payload)
        self.assertContains(response, "Choose a date and time for this automatic action.")
        payload["auto_open_registration"] = ""
        self.assertEqual(self.client.post(self.schedule_url, payload).status_code, 302)
        self.month.refresh_from_db()
        self.assertIsNone(self.month.registration_opens_at)
        self.assertIsNone(self.month.registration_closes_at)
        self.assertTrue(self.month.registration_is_open)

    def test_immediate_draft_publication_holds_an_overdue_start_for_review(self):
        from .scheduling import process_due_challenge_schedules

        self.month.transition_to(self.month.Status.DRAFT, confirm_reversal=True)
        self.client.force_login(self.host_user)
        now = datetime(2026, 9, 3, 12, tzinfo=ZoneInfo(self.group.timezone))
        payload = self.schedule_payload(
            starts_at="2026-09-01T09:00", ends_at="2026-09-30T21:00",
            auto_close_registration="on", registration_closes_at="2026-09-05T21:00",
            auto_complete_challenge="", final_announcement_at="",
        )
        with patch("core.views.timezone.now", return_value=now):
            self.assertEqual(self.client.post(self.schedule_url, payload).status_code, 302)
        self.month.refresh_from_db()
        self.assertEqual(self.month.status, self.month.Status.UPCOMING)
        self.assertTrue(self.month.registration_is_open)
        self.assertEqual(self.month.held_schedule_milestones, ["active"])
        self.assertEqual(process_due_challenge_schedules(now=now), [])

    def test_immediate_lifecycle_dialog_confirms_with_csrf_and_audits_once(self):
        from django.test import Client

        from .models import AuditEvent

        client = Client(enforce_csrf_checks=True)
        client.force_login(self.host_user)
        response = client.get(self.settings_url)
        self.assertContains(response, 'aria-labelledby="lifecycle-dialog-title"')
        self.assertContains(response, 'id="lifecycle-dialog-title">Start Challenge?')
        confirmation = response.context["forward_action"]["confirmation"]
        url = self.lifecycle_url("active")
        self.assertEqual(client.post(url, {"transition_confirmation": confirmation}).status_code, 403)
        data = {"transition_confirmation": confirmation, "csrfmiddlewaretoken": client.cookies["csrftoken"].value}
        client.post(url, data)
        client.post(url, data)
        self.month.refresh_from_db()
        self.assertEqual(self.month.status, self.month.Status.ACTIVE)
        self.assertEqual(AuditEvent.objects.filter(action="challenge.lifecycle_changed").count(), 1)

    def test_immediate_registration_dialog_rechecks_automation_and_requires_csrf(self):
        from django.test import Client

        from .models import AuditEvent

        self.month.auto_close_registration = False
        self.month.save(update_fields=["auto_close_registration"])
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.host_user)
        response = client.get(self.settings_url)
        self.assertContains(response, 'id="registration-dialog-title">Close Registration?')
        url = self.registration_action_url("close")
        self.assertEqual(client.post(url).status_code, 403)
        self.month.auto_close_registration = True
        self.month.save(update_fields=["auto_close_registration"])
        client.post(url, {"csrfmiddlewaretoken": client.cookies["csrftoken"].value})
        self.month.refresh_from_db()
        self.assertTrue(self.month.registration_is_open)
        self.assertFalse(AuditEvent.objects.filter(action="challenge.registration_closed").exists())
        self.assertNotContains(client.get(self.settings_url), 'id="registration-dialog"')
        client.force_login(self.reader_user)
        denied = client.get(self.settings_url)
        self.assertNotContains(denied, "data-challenge-dialog", status_code=403)

    def test_registration_actions_hidden_outside_eligible_stages(self):
        self.client.force_login(self.host_user)
        self.month.auto_close_registration = False
        self.month.save(update_fields=["auto_close_registration"])
        for status in (self.month.Status.ACTIVE, self.month.Status.FINALIZING, self.month.Status.COMPLETED, self.month.Status.ARCHIVED):
            self.month.transition_to(status)
            response = self.client.get(self.settings_url)
            self.assertContains(response, 'id="registration-dialog"', count=int(status == self.month.Status.ACTIVE))
            self.assertContains(response, 'id="lifecycle-dialog"', count=int(status != self.month.Status.ARCHIVED))

    def test_stale_start_dialog_cannot_resume_submissions(self):
        from .models import AuditEvent

        self.client.force_login(self.host_user)
        response = self.client.get(self.settings_url)
        token = response.context["forward_action"]["confirmation"]
        self.month.auto_end_challenge = False
        self.month.save(update_fields=["auto_end_challenge"])
        self.month.transition_to(self.month.Status.ACTIVE)
        self.month.transition_to(self.month.Status.FINALIZING)
        url = self.lifecycle_url("active")
        self.assertRedirects(self.client.post(url, {"transition_confirmation": token}), url)
        self.month.refresh_from_db()
        self.assertEqual(self.month.status, self.month.Status.FINALIZING)
        self.assertFalse(AuditEvent.objects.filter(action="challenge.lifecycle_changed").exists())
