from datetime import date, datetime
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.test import TestCase

from .models import ChallengeMonth, ReadingGroup
from .scheduling import process_due_challenge_schedules


class ChallengeSchedulingServiceTests(TestCase):
    def setUp(self):
        self.group = ReadingGroup.objects.create(
            name="Scheduler Group",
            slug="scheduler-group",
            timezone="America/New_York",
        )
        self.due = datetime(2026, 8, 26, 9, 0, tzinfo=ZoneInfo("America/New_York"))

    def challenge(self, name, status, **overrides):
        values = {
            "group": self.group,
            "name": name,
            "starts_on": date(2026, 8, 26),
            "ends_on": date(2026, 8, 31),
            "status": status,
        }
        values.update(overrides)
        return ChallengeMonth.objects.create(**values)

    def test_service_processes_due_draft_registration_open_and_is_idempotent(self):
        month = self.challenge(
            "Draft Due",
            ChallengeMonth.Status.DRAFT,
            registration_opens_at=self.due,
            registration_is_open=False,
        )
        self.assertEqual(
            process_due_challenge_schedules(now=self.due),
            [(month.pk, ["registration_opened", "lifecycle_upcoming"])],
        )
        month.refresh_from_db()
        self.assertEqual(month.status, ChallengeMonth.Status.UPCOMING)
        self.assertTrue(month.registration_is_open)
        self.assertEqual(process_due_challenge_schedules(now=self.due), [])

    def test_service_catches_up_registration_open_in_upcoming_and_active_only(self):
        expected_open = []
        expected_closed = []
        for status in (ChallengeMonth.Status.UPCOMING, ChallengeMonth.Status.ACTIVE):
            expected_open.append(self.challenge(
                f"Catch Up {status}",
                status,
                registration_opens_at=self.due,
                registration_is_open=False,
            ))
        for status in (
            ChallengeMonth.Status.FINALIZING,
            ChallengeMonth.Status.COMPLETED,
            ChallengeMonth.Status.ARCHIVED,
        ):
            expected_closed.append(self.challenge(
                f"Stay Closed {status}",
                status,
                registration_opens_at=self.due,
                registration_is_open=False,
            ))
        processed = process_due_challenge_schedules(now=self.due)
        self.assertEqual({challenge_id for challenge_id, _ in processed}, {month.pk for month in expected_open})
        for month in expected_open:
            month.refresh_from_db()
            self.assertTrue(month.registration_is_open)
        for month in expected_closed:
            month.refresh_from_db()
            self.assertFalse(month.registration_is_open)

    def test_registration_close_is_lifecycle_independent(self):
        upcoming = self.challenge(
            "Close Upcoming",
            ChallengeMonth.Status.UPCOMING,
            registration_closes_at=self.due,
            registration_is_open=True,
        )
        active = self.challenge(
            "Close Active",
            ChallengeMonth.Status.ACTIVE,
            registration_closes_at=self.due,
            registration_is_open=True,
        )
        process_due_challenge_schedules(now=self.due)
        for month in (upcoming, active):
            original_status = month.status
            month.refresh_from_db()
            self.assertFalse(month.registration_is_open)
            self.assertEqual(month.status, original_status)

    def test_overdue_lifecycle_events_advance_only_one_adjacent_stage_per_pass(self):
        month = self.challenge(
            "Sequential Overdue",
            ChallengeMonth.Status.DRAFT,
            registration_opens_at=self.due,
            starts_at=self.due,
            ends_at=self.due,
            final_announcement_at=self.due,
            auto_complete_challenge=True,
        )
        process_due_challenge_schedules(now=self.due)
        month.refresh_from_db()
        self.assertEqual(month.status, ChallengeMonth.Status.UPCOMING)
        process_due_challenge_schedules(now=self.due)
        month.refresh_from_db()
        self.assertEqual(month.status, ChallengeMonth.Status.ACTIVE)
        process_due_challenge_schedules(now=self.due)
        month.refresh_from_db()
        self.assertEqual(month.status, ChallengeMonth.Status.FINALIZING)
        process_due_challenge_schedules(now=self.due)
        month.refresh_from_db()
        self.assertEqual(month.status, ChallengeMonth.Status.COMPLETED)
        self.assertEqual(process_due_challenge_schedules(now=self.due), [])

    def test_handled_events_do_not_undo_the_opposite_manual_action(self):
        from django.contrib.auth import get_user_model

        from .scheduling import set_manual_registration

        actor = get_user_model().objects.create_superuser("schedule-owner")
        for action in ("open", "close"):
            with self.subTest(action=action):
                month = self.challenge(
                    action, ChallengeMonth.Status.UPCOMING,
                    auto_open_registration=action == "close",
                    auto_close_registration=action == "open",
                    registration_opens_at=self.due, registration_closes_at=self.due,
                    registration_is_open=action == "open",
                )
                process_due_challenge_schedules(now=self.due)
                self.assertTrue(set_manual_registration(month_id=month.pk, actor=actor, action=action))
                self.assertEqual(process_due_challenge_schedules(now=self.due), [])
                month.refresh_from_db()
                self.assertEqual(month.registration_is_open, action == "open")


class ChallengeSchedulerCommandTests(TestCase):
    @patch("core.management.commands.run_challenge_scheduler.process_due_challenge_schedules")
    def test_once_invokes_service_and_exits(self, process_due):
        process_due.return_value = [(7, ["registration_opened"])]
        output = StringIO()
        call_command("run_challenge_scheduler", "--once", stdout=output)
        process_due.assert_called_once_with()
        self.assertIn("Processed Challenge 7: registration_opened", output.getvalue())
