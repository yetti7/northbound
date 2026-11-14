from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone as utc_timezone
from threading import Event
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import RequestFactory, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from .models import (
    BookSubmission, CatalogBook, CatalogEdition, ChallengeMonth, Membership, MonthEnrollment,
    ParticipationPeriod, ReadingGroup, Team,
)
from .participation import activate_participation, assign_participant_to_team, deactivate_participation


class ParticipationEligibilityTests(TestCase):
    def setUp(self):
        users = get_user_model().objects
        self.owner = users.create_superuser("platform-owner")
        self.user = users.create_user("period-reader")
        self.group = ReadingGroup.objects.create(name="Eligibility", slug="eligibility", timezone="America/New_York")
        self.reader = Membership.objects.create(group=self.group, user=self.user, display_name="Reader")
        self.today = timezone.localdate(timezone=ZoneInfo(self.group.timezone))
        self.month = ChallengeMonth.objects.create(
            group=self.group, name="Challenge", status=ChallengeMonth.Status.ACTIVE,
            starts_on=self.today - timedelta(days=5), ends_on=self.today + timedelta(days=5),
        )
        self.team = Team.objects.create(month=self.month, name="Team A")
        self.other_team = Team.objects.create(month=self.month, name="Team B")
        self.url = reverse("submission-create", args=[self.group.slug, self.month.pk])
        self.client.force_login(self.user)

    def activate(self):
        return activate_participation(
            month=self.month, participant=self.reader, actor=self.user, origin=MonthEnrollment.Origin.SELF,
        )[0]

    def withdraw(self, enrollment):
        return deactivate_participation(
            enrollment=enrollment, actor=self.user, reason=MonthEnrollment.InactiveReason.WITHDRAWN,
        )

    def assign(self, team):
        assign_participant_to_team(month=self.month, participant=self.reader, team=team, actor=self.owner)

    def submit(self, **overrides):
        payload = {
            "title": "Book", "author": "Author", "book_format": "ebook",
            "completed_on": self.today.isoformat(), "submitted_pages": 100,
        }
        return self.client.post(self.url, payload | overrides)

    def test_withdrawal_reactivation_assignment_transfer_and_staff_removal(self):
        enrollment = self.activate()
        first = enrollment.participation_periods.get()
        self.assertEqual(first.started_at, enrollment.enrolled_at)
        self.assertEqual(first.start_reason, ParticipationPeriod.StartReason.ENROLLMENT)
        self.activate()
        self.assertEqual(enrollment.participation_periods.count(), 1)
        departed, _ = self.withdraw(enrollment)
        self.assertFalse(self.withdraw(departed)[1])
        first.refresh_from_db()
        self.assertEqual((first.ended_at, first.end_reason), (departed.inactivated_at, "withdrawn"))
        enrollment = self.activate()
        self.assertTrue(enrollment.awaiting_scoring_team)
        self.submit()
        self.assertFalse(BookSubmission.objects.exists())
        self.assertEqual(enrollment.participation_periods.count(), 1)
        self.assign(self.team)
        second = enrollment.participation_periods.get(ended_at__isnull=True)
        self.assertEqual(second.start_reason, ParticipationPeriod.StartReason.TEAM_ASSIGNMENT)
        self.assertGreaterEqual(second.started_at, first.ended_at)
        self.assign(self.team)
        self.assign(self.other_team)
        self.assertEqual(enrollment.participation_periods.get(ended_at__isnull=True).pk, second.pk)
        self.assertEqual(enrollment.participation_periods.count(), 2)
        removed, _ = deactivate_participation(
            enrollment=enrollment, actor=self.owner, reason=MonthEnrollment.InactiveReason.REMOVED,
            ordinary_staff_removal=True,
        )
        second.refresh_from_db()
        self.assertEqual((second.ended_at, second.end_reason), (removed.inactivated_at, "removed"))

    def test_manual_evidence_survives_date_edit_and_withdrawal(self):
        enrollment = self.activate()
        self.assertEqual(self.submit().status_code, 302)
        submission = BookSubmission.objects.get()
        self.assertEqual(submission.status, BookSubmission.Status.PENDING)
        self.assertEqual(submission.participation_period_id, enrollment.participation_periods.get().pk)
        self.assertTrue(submission.has_verified_completion_period)
        self.assertLessEqual(submission.accepted_at, submission.submitted_at)
        original_timestamp = submission.accepted_at
        submission.completed_on -= timedelta(days=1)
        submission.save(update_fields=["completed_on"])
        self.withdraw(enrollment)
        submission.refresh_from_db()
        self.assertTrue(submission.has_verified_completion_period)
        self.assertEqual(submission.accepted_completed_on, self.today)
        self.assertEqual(submission.accepted_at, original_timestamp)
        submission.accepted_completed_on = submission.completed_on
        with self.assertRaisesMessage(ValidationError, "Acceptance evidence cannot be changed"):
            submission.save(update_fields=["accepted_completed_on"])

    def test_hardcover_acceptance_preserves_autoapproval_and_configuration_evidence(self):
        self.month.tbr_enabled = True
        self.month.tbr_book_bonus_pages = 20
        self.month.save(update_fields=["tbr_enabled", "tbr_book_bonus_pages"])
        enrollment = self.activate()
        book = CatalogBook.objects.create(provider="hardcover", provider_book_id="book", title="Book", author="Author")
        edition = CatalogEdition.objects.create(
            book=book, provider="hardcover", provider_edition_id="edition", page_count=155, format_name="ebook",
        )
        token = signing.dumps(
            {"selected": edition.pk, "scoring": edition.pk, "method": "hardcover"},
            salt="northbound.catalog-selection",
        )
        self.assertEqual(self.submit(catalog_selection=token).status_code, 302)
        submission = BookSubmission.objects.get()
        self.assertEqual((submission.status, submission.final_scored_pages), ("approved", 155))
        self.assertEqual(submission.participation_period_id, enrollment.participation_periods.get().pk)
        self.assertEqual(submission.acceptance_claims["book"]["catalog_edition_id"], edition.pk)
        self.month.tbr_book_bonus_pages = 50
        self.month.save(update_fields=["tbr_book_bonus_pages"])
        submission.refresh_from_db()
        self.assertEqual(submission.acceptance_claims["tbr"]["book_bonus_pages"], 20)

    def test_legacy_acceptance_starts_prospective_period_and_rollback_keeps_it_absent(self):
        enrollment = MonthEnrollment.objects.create(month=self.month, participant=self.reader)
        with patch("core.views.synchronize_personal_tbr_submission", side_effect=RuntimeError("rollback")):
            with self.assertRaisesMessage(RuntimeError, "rollback"):
                self.submit()
        self.assertFalse(enrollment.participation_periods.exists())
        self.assertFalse(BookSubmission.objects.exists())
        self.assertEqual(self.submit(completed_on=(self.today - timedelta(days=1)).isoformat()).status_code, 302)
        period = enrollment.participation_periods.get()
        submission = BookSubmission.objects.get()
        self.assertEqual(period.start_reason, ParticipationPeriod.StartReason.OBSERVED)
        self.assertEqual(period.started_at, submission.accepted_at)
        self.assertFalse(submission.has_verified_completion_period)

    def test_withdrawal_rollback_keeps_period_open(self):
        enrollment = self.activate()
        with patch("core.botm_matching.synchronize_reader", side_effect=RuntimeError("rollback")):
            with self.assertRaisesMessage(RuntimeError, "rollback"):
                self.withdraw(enrollment)
        enrollment.refresh_from_db()
        self.assertTrue(enrollment.is_active)
        self.assertIsNone(enrollment.participation_periods.get().ended_at)

    def test_group_local_boundary_requires_acceptance_before_departure(self):
        utc = utc_timezone.utc
        period = ParticipationPeriod(
            started_at=datetime(2026, 11, 4, 5, tzinfo=utc),
            ended_at=datetime(2026, 11, 5, 3, tzinfo=utc),
        )
        zone = self.group.timezone
        accepted = period.ended_at - timedelta(seconds=1)
        completion = timezone.localdate(accepted, timezone=ZoneInfo(zone))
        self.assertEqual(completion.isoformat(), "2026-11-04")
        self.assertTrue(period.includes_completion(completion, accepted, zone))
        self.assertFalse(period.includes_completion(completion, period.ended_at, zone))
        self.assertFalse(period.includes_completion(completion - timedelta(days=1), accepted, zone))

    def test_cross_challenge_reference_is_rejected_on_save(self):
        enrollment = self.activate()
        period = enrollment.participation_periods.get()
        another = ChallengeMonth.objects.create(group=self.group, name="Another Challenge")
        submission = BookSubmission(
            month=another, participant=self.reader, title="Book", author="Author",
            completed_on=self.today, submitted_pages=100,
            participation_period=period, accepted_at=period.started_at, accepted_completed_on=self.today,
            acceptance_claims={"group_timezone": self.group.timezone},
        )
        with self.assertRaisesMessage(ValidationError, "must belong to this Reader and Challenge"):
            submission.save()
        self.assertFalse(BookSubmission.objects.exists())


class ParticipationEligibilityLockTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_submission_waits_for_in_flight_withdrawal_and_rechecks_enrollment(self):
        user = get_user_model().objects.create_user("locking-reader")
        group = ReadingGroup.objects.create(name="Locking", slug="locking")
        today = timezone.localdate(timezone=ZoneInfo(group.timezone))
        month = ChallengeMonth.objects.create(
            group=group, name="Challenge", status=ChallengeMonth.Status.ACTIVE,
            starts_on=today - timedelta(days=1), ends_on=today + timedelta(days=1),
        )
        reader = Membership.objects.create(group=group, user=user, display_name="Reader")
        enrollment, _, _ = activate_participation(
            month=month, participant=reader, actor=user, origin=MonthEnrollment.Origin.SELF,
        )
        request = RequestFactory().post("/", {
            "title": "Book", "author": "Author", "book_format": "ebook",
            "completed_on": today.isoformat(), "submitted_pages": 100,
        })
        request.user = user
        reached_lock = Event()
        from . import views
        lock = views._lock_challenge_for_edit

        def signal_before_lock(challenge):
            reached_lock.set()
            return lock(challenge)

        def submit():
            try:
                return views.submission_create(request, group.slug, month.pk)
            finally:
                connections.close_all()

        with patch.object(views, "_lock_challenge_for_edit", signal_before_lock):
            with ThreadPoolExecutor(max_workers=1) as pool:
                with transaction.atomic():
                    deactivate_participation(
                        enrollment=enrollment, actor=user, reason=MonthEnrollment.InactiveReason.WITHDRAWN,
                    )
                    result = pool.submit(submit)
                    self.assertTrue(reached_lock.wait(timeout=10))
                self.assertEqual(result.result(timeout=10).status_code, 403)
        self.assertFalse(BookSubmission.objects.exists())
        self.assertIsNotNone(enrollment.participation_periods.get().ended_at)


class ParticipationEligibilityMigrationTests(TransactionTestCase):
    def test_legacy_submission_and_credited_team_are_preserved_without_backfill(self):
        before = [("core", "0068_enrollment_scoring_team")]
        after = [("core", "0069_participation_eligibility")]
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        apps = executor.loader.project_state(before).apps
        user = apps.get_model("auth", "User").objects.create(username="legacy-reader")
        group = apps.get_model("core", "ReadingGroup").objects.create(name="Legacy", slug="legacy")
        reader = apps.get_model("core", "Membership").objects.create(group=group, user=user, display_name="Reader")
        month = apps.get_model("core", "ChallengeMonth").objects.create(group=group, name="Legacy")
        team = apps.get_model("core", "Team").objects.create(month=month, name="Team")
        enrollment = apps.get_model("core", "MonthEnrollment").objects.create(
            month=month, participant=reader, credited_team=team,
        )
        submission = apps.get_model("core", "BookSubmission").objects.create(
            month=month, participant=reader, title="Book", author="Author", book_format="ebook",
            completed_on=date(2026, 11, 1), submitted_pages=100, approved_pages=100,
            bonus_pages=20, final_scored_pages=120, status="approved",
        )
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        apps = executor.loader.project_state(after).apps
        current = apps.get_model("core", "BookSubmission").objects.get(pk=submission.pk)
        self.assertEqual((current.approved_pages, current.bonus_pages, current.final_scored_pages), (100, 20, 120))
        self.assertEqual(current.submitted_at, submission.submitted_at)
        self.assertIsNone(current.participation_period_id)
        self.assertIsNone(current.accepted_at)
        self.assertIsNone(current.accepted_completed_on)
        self.assertEqual(current.acceptance_claims, {})
        self.assertFalse(apps.get_model("core", "ParticipationPeriod").objects.exists())
        self.assertEqual(apps.get_model("core", "MonthEnrollment").objects.get(pk=enrollment.pk).credited_team_id, team.pk)
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        upgraded = BookSubmission.objects.get(pk=submission.pk)
        self.assertEqual(upgraded.late_approval_evidence, {})
        self.assertEqual(upgraded.void_earning_evidence, {})
        self.assertEqual(upgraded.final_scored_pages, current.final_scored_pages)

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()
