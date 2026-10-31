import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .hardcover_sync import make_existing_reader_events_due, process_hardcover_sync_batch, reader_sync_health
from .integrations.credentials import CredentialOwner
from .integrations.hardcover import HardcoverConnectionError
from .integrations.hardcover_library import LibraryState
from .models import BookSubmission, HardcoverSyncOutbox, HardcoverSyncProvenance, ReaderHardcoverSyncPreference
from .test_hardcover_completed_book_sync import SyncFixtureMixin
from .hardcover_sync import enqueue_submission_sync


class HardcoverSyncRecoveryTests(SyncFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.enable()
        self.submission = self.make_submission(status=BookSubmission.Status.APPROVED)
        self.outbox, _ = enqueue_submission_sync(self.submission, HardcoverSyncOutbox.Action.COMPLETED_BOOK)

    def credential(self):
        return SimpleNamespace(bearer_token="reader-token", owner=CredentialOwner.READER)

    def test_retry_waits_until_due_then_processes(self):
        future = timezone.now() + timedelta(minutes=5)
        HardcoverSyncOutbox.objects.filter(pk=self.outbox.pk).update(status=HardcoverSyncOutbox.Status.RETRYABLE, next_attempt_at=future)
        with patch("core.hardcover_sync.get_reader_hardcover_credential", return_value=self.credential()), patch("core.hardcover_sync.read_library_state", return_value=LibraryState({"id": 900, "status_id": 3}, 3)) as state:
            self.assertEqual(process_hardcover_sync_batch(now=future - timedelta(seconds=1)), [])
            self.assertEqual(process_hardcover_sync_batch(now=future), [self.outbox.pk])
        self.assertTrue(state.called)

    @patch("core.hardcover_sync.read_library_state", side_effect=HardcoverConnectionError("temporary", classification="temporary_network", retryable=True))
    def test_sixth_failed_attempt_becomes_terminal_recovery_needed(self, state):
        HardcoverSyncOutbox.objects.filter(pk=self.outbox.pk).update(attempt_count=5)
        with patch("core.hardcover_sync.get_reader_hardcover_credential", return_value=self.credential()):
            process_hardcover_sync_batch(batch_size=1)
        self.outbox.refresh_from_db()
        self.assertEqual(self.outbox.status, HardcoverSyncOutbox.Status.BLOCKED)
        self.assertEqual(self.outbox.error_classification, "retry_exhausted")
        self.assertIsNone(self.outbox.next_attempt_at)

    def test_permanent_and_reconciliation_events_are_not_automatically_retried(self):
        HardcoverSyncOutbox.objects.filter(pk=self.outbox.pk).update(status=HardcoverSyncOutbox.Status.BLOCKED, error_classification="reconciliation_required")
        self.assertEqual(process_hardcover_sync_batch(), [])
        count, reason = make_existing_reader_events_due(self.reader)
        self.assertEqual((count, reason), (0, ""))
        self.outbox.refresh_from_db()
        self.assertEqual(self.outbox.status, HardcoverSyncOutbox.Status.BLOCKED)

    def test_sync_now_wakes_only_signed_in_readers_existing_events(self):
        other = self.reviewer
        other_submission = self.make_submission(status=BookSubmission.Status.APPROVED)
        other_submission.participant = self.member
        other_submission.save(update_fields=["participant"])
        other_outbox, _ = enqueue_submission_sync(other_submission, HardcoverSyncOutbox.Action.COMPLETED_BOOK)
        HardcoverSyncOutbox.objects.filter(pk=other_outbox.pk).update(user=other)
        HardcoverSyncOutbox.objects.filter(pk__in=[self.outbox.pk, other_outbox.pk]).update(status=HardcoverSyncOutbox.Status.RETRYABLE, next_attempt_at=timezone.now() + timedelta(days=1))
        self.client.force_login(self.reader)
        response = self.client.post(reverse("reader-hardcover-sync-now"), follow=True)
        self.assertContains(response, "Made 1 existing Hardcover synchronization event")
        self.outbox.refresh_from_db()
        other_outbox.refresh_from_db()
        self.assertEqual(self.outbox.status, HardcoverSyncOutbox.Status.PENDING)
        self.assertEqual(other_outbox.status, HardcoverSyncOutbox.Status.RETRYABLE)

    def test_sync_now_with_no_work_is_harmless_and_creates_nothing(self):
        HardcoverSyncOutbox.objects.all().delete()
        self.client.force_login(self.reader)
        response = self.client.post(reverse("reader-hardcover-sync-now"), follow=True)
        self.assertContains(response, "no existing eligible")
        self.assertFalse(HardcoverSyncOutbox.objects.exists())

    def test_sync_now_respects_consent_and_never_scans_approved_submissions(self):
        ReaderHardcoverSyncPreference.objects.filter(user=self.reader).update(sync_completed_books=False)
        HardcoverSyncOutbox.objects.all().delete()
        self.make_submission(status=BookSubmission.Status.APPROVED)
        self.client.force_login(self.reader)
        response = self.client.post(reverse("reader-hardcover-sync-now"), follow=True)
        self.assertContains(response, "Enable completed-book synchronization")
        self.assertFalse(HardcoverSyncOutbox.objects.exists())

    def test_repaired_credential_unblocks_only_existing_reconnect_event(self):
        HardcoverSyncOutbox.objects.filter(pk=self.outbox.pk).update(status=HardcoverSyncOutbox.Status.BLOCKED, error_classification="reconnect_required")
        count, reason = make_existing_reader_events_due(self.reader)
        self.assertEqual((count, reason), (1, ""))
        self.outbox.refresh_from_db()
        self.assertEqual(self.outbox.status, HardcoverSyncOutbox.Status.PENDING)
        self.assertEqual(HardcoverSyncOutbox.objects.count(), 1)

    @patch("core.hardcover_sync.create_read_occurrence")
    @patch("core.hardcover_sync.read_library_state", return_value=LibraryState({"id": 900, "status_id": 3}, 3))
    def test_expired_claim_with_ambiguous_marker_does_not_repeat_create(self, state, create):
        ReaderHardcoverSyncPreference.objects.filter(user=self.reader).update(sync_completion_dates=True)
        HardcoverSyncOutbox.objects.filter(pk=self.outbox.pk).update(
            status=HardcoverSyncOutbox.Status.PROCESSING,
            next_attempt_at=timezone.now() - timedelta(seconds=1),
            provider_book_id="123", provider_user_book_id="900",
            library_result_detail="already_satisfied", occurrence_result_detail="create_started",
        )
        with patch("core.hardcover_sync.get_reader_hardcover_credential", return_value=self.credential()):
            process_hardcover_sync_batch(batch_size=1)
        create.assert_not_called()
        self.outbox.refresh_from_db()
        self.assertEqual(self.outbox.error_classification, "reconciliation_required")

    def test_reader_health_is_self_scoped_and_safe(self):
        HardcoverSyncOutbox.objects.filter(pk=self.outbox.pk).update(status=HardcoverSyncOutbox.Status.BLOCKED, error_classification="reconciliation_required")
        health = reader_sync_health(self.reader)
        self.assertEqual(health["reconciliation_required"], 1)
        self.assertNotIn("token", str(health).casefold())

    def test_restore_safeguard_quarantines_unfinished_without_provider_call(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "last-restore.json").write_text(json.dumps({"restored_at": "2026-09-02T12:00:00Z"}))
            with patch("core.management.commands.safeguard_restored_hardcover_sync.data_root", return_value=root), patch("core.hardcover_sync.get_reader_hardcover_credential") as credential:
                call_command("safeguard_restored_hardcover_sync")
            credential.assert_not_called()
            self.outbox.refresh_from_db()
            self.assertEqual(self.outbox.status, HardcoverSyncOutbox.Status.BLOCKED)
            self.assertEqual(self.outbox.error_classification, "restore_reconciliation_required")
            self.assertTrue(self.outbox.provenance.filter(outcome=HardcoverSyncProvenance.Outcome.BLOCKED).exists())
            with patch("core.management.commands.safeguard_restored_hardcover_sync.data_root", return_value=root):
                call_command("safeguard_restored_hardcover_sync")
            self.assertEqual(self.outbox.provenance.count(), 1)
