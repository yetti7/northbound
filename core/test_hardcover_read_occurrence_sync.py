from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from .hardcover_sync import enqueue_submission_sync, process_hardcover_sync_batch
from .integrations.credentials import CredentialOwner
from .integrations.hardcover import HardcoverConnectionError
from .integrations.hardcover_library import LibraryState, create_read_occurrence, update_read_occurrence
from .models import BookSubmission, HardcoverSyncOutbox, ReaderHardcoverSyncPreference
from .test_hardcover_completed_book_sync import SyncFixtureMixin


class HardcoverReadOccurrenceTests(SyncFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.enable()
        ReaderHardcoverSyncPreference.objects.filter(user=self.reader).update(sync_completion_dates=True)
        self.submission = self.make_submission(status=BookSubmission.Status.APPROVED)
        self.outbox, _ = enqueue_submission_sync(self.submission, HardcoverSyncOutbox.Action.COMPLETED_BOOK)

    def run_worker(self, batch_size=1):
        credential = SimpleNamespace(bearer_token="reader-token", owner=CredentialOwner.READER)
        with patch("core.hardcover_sync.get_reader_hardcover_credential", return_value=credential):
            return process_hardcover_sync_batch(batch_size=batch_size)

    @patch("core.hardcover_sync.create_read_occurrence", return_value="7001")
    @patch("core.hardcover_sync.read_library_state", return_value=LibraryState({"id": 900, "status_id": 3, "user_book_status": {"slug": "read"}}, 3))
    def test_both_consents_create_one_occurrence_with_actual_completion_date(self, state, create):
        self.run_worker()
        create.assert_called_once_with("reader-token", user_book_id="900", finished_at=date(2026, 9, 10), edition_id=None)
        self.outbox.refresh_from_db()
        self.assertEqual(self.outbox.provider_read_id, "7001")
        self.assertEqual(self.outbox.library_result_detail, "already_satisfied")
        self.assertEqual(self.outbox.occurrence_result_detail, "created")
        self.assertEqual(self.outbox.effective_date, self.submission.completed_on)
        attempt = self.outbox.provenance.get()
        self.assertEqual(attempt.provider_read_id, "7001")
        self.assertEqual(attempt.library_result_detail, "already_satisfied")
        self.assertEqual(attempt.occurrence_result_detail, "created")

    @patch("core.hardcover_sync.create_read_occurrence")
    @patch("core.hardcover_sync.read_library_state", return_value=LibraryState({"id": 900, "status_id": 3}, 3))
    def test_date_consent_off_runs_hc4_only(self, state, create):
        ReaderHardcoverSyncPreference.objects.filter(user=self.reader).update(sync_completion_dates=False)
        self.run_worker()
        create.assert_not_called()
        self.outbox.refresh_from_db()
        self.assertEqual(self.outbox.status, HardcoverSyncOutbox.Status.SUCCEEDED)
        self.assertEqual(self.outbox.occurrence_result_detail, "consent_off")

    @patch("core.hardcover_sync.create_read_occurrence")
    @patch("core.hardcover_sync.read_library_state")
    def test_completed_book_consent_withdrawal_prevents_all_provider_work(self, state, create):
        ReaderHardcoverSyncPreference.objects.filter(user=self.reader).update(sync_completed_books=False, sync_completion_dates=False)
        self.run_worker()
        state.assert_not_called()
        create.assert_not_called()

    @patch("core.hardcover_sync.create_read_occurrence")
    @patch("core.hardcover_sync.read_library_state", return_value=LibraryState({"id": 900, "status_id": 3}, 3))
    def test_date_consent_withdrawal_before_execution_prevents_occurrence_write(self, state, create):
        ReaderHardcoverSyncPreference.objects.filter(user=self.reader).update(sync_completion_dates=False)
        self.run_worker()
        create.assert_not_called()

    @patch("core.hardcover_sync.create_read_occurrence", side_effect=["7001", "7002"])
    @patch("core.hardcover_sync.read_library_state", return_value=LibraryState({"id": 900, "status_id": 3}, 3))
    def test_two_submissions_for_same_book_create_distinct_occurrences(self, state, create):
        second = self.make_submission(status=BookSubmission.Status.APPROVED)
        second.catalog_book = self.submission.catalog_book
        second.completed_on = date(2026, 9, 20)
        second.save(update_fields=["catalog_book", "completed_on"])
        second_outbox, _ = enqueue_submission_sync(second, HardcoverSyncOutbox.Action.COMPLETED_BOOK)
        self.run_worker(batch_size=2)
        self.outbox.refresh_from_db()
        second_outbox.refresh_from_db()
        self.assertEqual((self.outbox.provider_read_id, second_outbox.provider_read_id), ("7001", "7002"))
        self.assertEqual(create.call_args_list[0].kwargs["finished_at"], date(2026, 9, 10))
        self.assertEqual(create.call_args_list[1].kwargs["finished_at"], date(2026, 9, 20))

    @patch("core.hardcover_sync.create_read_occurrence")
    @patch("core.hardcover_sync.update_read_occurrence")
    @patch("core.hardcover_sync.read_occurrence", return_value={"id": 7001, "user_book_id": 900, "finished_at": "2026-09-10"})
    @patch("core.hardcover_sync.read_library_state", return_value=LibraryState({"id": 900, "status_id": 3}, 3))
    def test_known_mapping_is_already_satisfied_without_second_create(self, state, read, update, create):
        HardcoverSyncOutbox.objects.filter(pk=self.outbox.pk).update(provider_read_id="7001")
        self.run_worker()
        create.assert_not_called()
        update.assert_not_called()
        self.outbox.refresh_from_db()
        self.assertEqual(self.outbox.occurrence_result_detail, "already_satisfied")

    @patch("core.hardcover_sync.create_read_occurrence")
    @patch("core.hardcover_sync.update_read_occurrence", return_value="7001")
    @patch("core.hardcover_sync.read_occurrence", return_value={"id": 7001, "user_book_id": 900, "finished_at": "2026-09-09"})
    @patch("core.hardcover_sync.read_library_state", return_value=LibraryState({"id": 900, "status_id": 3}, 3))
    def test_only_known_occurrence_is_updated(self, state, read, update, create):
        HardcoverSyncOutbox.objects.filter(pk=self.outbox.pk).update(provider_read_id="7001")
        self.run_worker()
        update.assert_called_once_with("reader-token", read_id="7001", finished_at=date(2026, 9, 10))
        create.assert_not_called()

    @patch("core.hardcover_sync.create_read_occurrence", return_value="7001")
    @patch("core.hardcover_sync.read_occurrence")
    @patch("core.hardcover_sync.read_library_state", return_value=LibraryState({"id": 900, "status_id": 3}, 3))
    def test_unrelated_existing_history_is_not_adopted_or_modified(self, state, read, create):
        self.run_worker()
        read.assert_not_called()
        create.assert_called_once()

    @patch("core.hardcover_sync.create_read_occurrence", side_effect=HardcoverConnectionError("timeout", classification="temporary_network", retryable=True))
    @patch("core.hardcover_sync.read_library_state", return_value=LibraryState({"id": 900, "status_id": 3}, 3))
    def test_ambiguous_create_is_reconciliation_required_and_not_recreated(self, state, create):
        self.run_worker()
        self.outbox.refresh_from_db()
        self.assertEqual(self.outbox.status, HardcoverSyncOutbox.Status.BLOCKED)
        self.assertEqual(self.outbox.occurrence_result_detail, "reconciliation_required")
        self.assertEqual(self.outbox.library_result_detail, "already_satisfied")
        HardcoverSyncOutbox.objects.filter(pk=self.outbox.pk).update(status=HardcoverSyncOutbox.Status.PENDING)
        self.run_worker()
        self.assertEqual(create.call_count, 1)

    def test_enabling_date_consent_and_empty_worker_do_not_scan_history(self):
        HardcoverSyncOutbox.objects.all().delete()
        ReaderHardcoverSyncPreference.objects.filter(user=self.reader).update(sync_completion_dates=False)
        ReaderHardcoverSyncPreference.objects.filter(user=self.reader).update(sync_completion_dates=True)
        with patch("core.hardcover_sync.get_reader_hardcover_credential") as credential:
            self.assertEqual(process_hardcover_sync_batch(), [])
        credential.assert_not_called()

    def test_later_consent_skip_does_not_erase_existing_occurrence_mapping(self):
        HardcoverSyncOutbox.objects.filter(pk=self.outbox.pk).update(provider_book_id="123", provider_user_book_id="900", provider_read_id="7001")
        ReaderHardcoverSyncPreference.objects.filter(user=self.reader).update(sync_completed_books=False, sync_completion_dates=False)
        self.run_worker()
        self.outbox.refresh_from_db()
        self.assertEqual((self.outbox.provider_book_id, self.outbox.provider_user_book_id, self.outbox.provider_read_id), ("123", "900", "7001"))


class HardcoverReadOccurrenceContractTests(TestCase):
    @patch("core.integrations.hardcover_library.execute_graphql", return_value={"insert_user_book_read": {"id": 71, "error": None}})
    def test_create_uses_date_only_without_precision_time_or_delete(self, execute):
        self.assertEqual(create_read_occurrence("token", user_book_id=9, finished_at=date(2026, 6, 10), edition_id=4), "71")
        query, variables = execute.call_args.args[1:]
        self.assertEqual(variables, {"userBookId": 9, "read": {"finished_at": "2026-06-10", "edition_id": 4}})
        self.assertNotIn("finished_at_precision", variables["read"])
        self.assertNotIn("action_at", variables["read"])
        self.assertNotIn("delete_user_book_read", query)

    @patch("core.integrations.hardcover_library.execute_graphql", return_value={"update_user_book_read": {"id": 72, "error": None}})
    def test_update_changes_only_mapped_occurrence_finished_date(self, execute):
        self.assertEqual(update_read_occurrence("token", read_id=72, finished_at=date(2026, 8, 22)), "72")
        query, variables = execute.call_args.args[1:]
        self.assertEqual(variables, {"id": 72, "read": {"finished_at": "2026-08-22"}})
        self.assertNotIn("upsert_user_book_reads", query)
        self.assertNotIn("delete_user_book_read", query)
