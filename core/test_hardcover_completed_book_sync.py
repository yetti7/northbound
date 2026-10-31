from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from .hardcover_sync import enqueue_submission_sync, process_hardcover_sync_batch
from .integrations.hardcover import HardcoverConnectionError
from .integrations.hardcover_library import LibraryState, create_read_user_book, update_user_book_to_read
from .integrations.credentials import CredentialOwner
from .models import BookSubmission, CatalogBook, ChallengeMonth, HardcoverConnection, HardcoverSyncOutbox, HardcoverSyncProvenance, Membership, ReaderHardcoverConnection, ReaderHardcoverSyncPreference, ReadingGroup
from .scoring import apply_submission_review


class SyncFixtureMixin:
    def make_submission(self, *, status=BookSubmission.Status.PENDING, catalog=True, title="Matched Book", author="Exact Author"):
        book = CatalogBook.objects.create(provider="hardcover", provider_book_id=str(CatalogBook.objects.count() + 123), title=title, author=author) if catalog else None
        return BookSubmission.objects.create(
            month=self.month, participant=self.member, catalog_book=book, title=title, author=author,
            book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 9, 10), submitted_pages=100,
            approved_pages=100 if status == BookSubmission.Status.APPROVED else None, status=status,
        )

    def enable(self, *, method=ReaderHardcoverConnection.ConnectionMethod.API_KEY):
        ReaderHardcoverConnection.objects.create(
            user=self.reader, connection_method=method, encrypted_token="encrypted", is_valid=True,
            granted_scopes=["read:catalog", "read:library", "write:library"] if method == ReaderHardcoverConnection.ConnectionMethod.OAUTH else [],
        )
        ReaderHardcoverSyncPreference.objects.create(user=self.reader, sync_completed_books=True)

    def setUp(self):
        self.reader = get_user_model().objects.create_user("hc4-reader")
        self.reviewer = get_user_model().objects.create_user("hc4-reviewer")
        self.group = ReadingGroup.objects.create(name="HC4 Group", slug="hc4-group")
        self.member = Membership.objects.create(group=self.group, user=self.reader, display_name="Reader")
        self.month = ChallengeMonth.objects.create(group=self.group, name="September", starts_on=date(2026, 9, 1), ends_on=date(2026, 9, 30), status=ChallengeMonth.Status.ACTIVE)


class HardcoverApprovalEventTests(SyncFixtureMixin, TestCase):
    def test_new_approval_queues_once_after_commit(self):
        self.enable()
        submission = self.make_submission()
        submission.status = BookSubmission.Status.APPROVED
        submission.approved_pages = 100
        with self.captureOnCommitCallbacks(execute=True):
            apply_submission_review(submission, [], self.reviewer, timezone.now())
        self.assertEqual(HardcoverSyncOutbox.objects.count(), 1)
        self.assertEqual(HardcoverSyncOutbox.objects.get().event_key, f"book_submission:{submission.pk}:completed_book:v1")

        with self.captureOnCommitCallbacks(execute=True):
            apply_submission_review(submission, [], self.reviewer, timezone.now())
        self.assertEqual(HardcoverSyncOutbox.objects.count(), 1)

    def test_consent_off_or_connection_alone_queues_nothing(self):
        ReaderHardcoverConnection.objects.create(user=self.reader, encrypted_token="encrypted", is_valid=True)
        submission = self.make_submission()
        submission.status, submission.approved_pages = BookSubmission.Status.APPROVED, 100
        with self.captureOnCommitCallbacks(execute=True):
            apply_submission_review(submission, [], self.reviewer, timezone.now())
        self.assertFalse(HardcoverSyncOutbox.objects.exists())

    def test_enabling_consent_does_not_queue_old_approval(self):
        self.make_submission(status=BookSubmission.Status.APPROVED)
        self.enable()
        self.assertFalse(HardcoverSyncOutbox.objects.exists())

    def test_distinct_new_approvals_are_distinct_events(self):
        self.enable()
        submissions = [self.make_submission(title=f"Book {index}") for index in range(2)]
        with self.captureOnCommitCallbacks(execute=True):
            for submission in submissions:
                submission.status, submission.approved_pages = BookSubmission.Status.APPROVED, 100
                apply_submission_review(submission, [], self.reviewer, timezone.now())
        self.assertEqual(HardcoverSyncOutbox.objects.count(), 2)

    @patch("core.hardcover_sync.enqueue_eligible_approved_submission", side_effect=RuntimeError("database unavailable"))
    def test_post_commit_enqueue_failure_does_not_undo_approval(self, enqueue):
        self.enable()
        submission = self.make_submission()
        submission.status, submission.approved_pages = BookSubmission.Status.APPROVED, 100
        with self.captureOnCommitCallbacks(execute=True):
            reviewed = apply_submission_review(submission, [], self.reviewer, timezone.now())
        self.assertEqual(reviewed.status, BookSubmission.Status.APPROVED)
        self.assertEqual(BookSubmission.objects.get(pk=submission.pk).status, BookSubmission.Status.APPROVED)


class HardcoverApprovalRollbackTests(SyncFixtureMixin, TransactionTestCase):
    reset_sequences = True

    def test_rollback_queues_nothing(self):
        self.enable()
        submission = self.make_submission()
        try:
            with transaction.atomic():
                submission.status, submission.approved_pages = BookSubmission.Status.APPROVED, 100
                apply_submission_review(submission, [], self.reviewer, timezone.now())
                raise RuntimeError("rollback")
        except RuntimeError:
            pass
        self.assertFalse(HardcoverSyncOutbox.objects.exists())


class HardcoverProcessorTests(SyncFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.enable()
        self.submission = self.make_submission(status=BookSubmission.Status.APPROVED)
        self.outbox, _ = enqueue_submission_sync(self.submission, HardcoverSyncOutbox.Action.COMPLETED_BOOK)

    def run_worker(self):
        with patch("core.hardcover_sync.get_reader_hardcover_credential", return_value=SimpleNamespace(bearer_token="reader-token", owner=CredentialOwner.READER)):
            return process_hardcover_sync_batch(batch_size=1)

    @patch("core.hardcover_sync.create_read_user_book", return_value="900")
    @patch("core.hardcover_sync.read_library_state", return_value=LibraryState(None, 3))
    def test_absent_book_creates_minimal_read_library_record(self, read_state, create):
        self.run_worker()
        create.assert_called_once_with("reader-token", book_id="123", read_status_id=3, edition_id=None)
        self.outbox.refresh_from_db()
        self.assertEqual(self.outbox.result_detail, "created")
        self.assertEqual(self.outbox.provider_book_id, "123")
        self.assertEqual(self.outbox.status, HardcoverSyncOutbox.Status.SUCCEEDED)

    @patch("core.hardcover_sync.update_user_book_to_read", return_value="901")
    @patch("core.hardcover_sync.read_library_state", return_value=LibraryState({"id": 901, "status_id": 1, "user_book_status": {"slug": "currently-reading"}}, 3))
    def test_existing_incomplete_book_updates_status_only(self, read_state, update):
        self.run_worker()
        update.assert_called_once_with("reader-token", user_book_id=901, read_status_id=3)
        self.outbox.refresh_from_db()
        self.assertEqual(self.outbox.result_detail, "updated")

    @patch("core.hardcover_sync.update_user_book_to_read")
    @patch("core.hardcover_sync.create_read_user_book")
    @patch("core.hardcover_sync.read_library_state", return_value=LibraryState({"id": 902, "status_id": 3, "user_book_status": {"slug": "read"}}, 3))
    def test_already_read_is_satisfied_without_mutation(self, read_state, create, update):
        self.run_worker()
        create.assert_not_called()
        update.assert_not_called()
        self.outbox.refresh_from_db()
        self.assertEqual(self.outbox.result_detail, "already_satisfied")

    @patch("core.hardcover_sync.search_books", return_value=([{"book_id": "456", "title": "Matched Book", "author": "Exact Author"}], False))
    @patch("core.hardcover_sync.read_library_state", return_value=LibraryState({"id": 5, "status_id": 3}, 3))
    def test_fallback_requires_exact_title_and_author(self, read_state, search):
        self.submission.catalog_book = None
        self.submission.save(update_fields=["catalog_book"])
        self.run_worker()
        self.outbox.refresh_from_db()
        self.assertEqual(self.outbox.provider_book_id, "456")

    @patch("core.hardcover_sync.read_library_state")
    @patch("core.hardcover_sync.search_books", return_value=([{"book_id": "1", "title": "Matched Book", "author": "Exact Author"}, {"book_id": "2", "title": "Matched Book", "author": "Exact Author"}], False))
    def test_ambiguous_fallback_skips_without_library_call(self, search, read_state):
        self.submission.catalog_book = None
        self.submission.save(update_fields=["catalog_book"])
        self.run_worker()
        read_state.assert_not_called()
        self.outbox.refresh_from_db()
        self.assertEqual(self.outbox.status, HardcoverSyncOutbox.Status.SKIPPED)
        self.assertEqual(self.outbox.result_detail, "ambiguous_match")

    @patch("core.hardcover_sync.search_books", return_value=([], False))
    @patch("core.hardcover_sync.read_library_state")
    def test_no_match_skips_without_library_mutation(self, read_state, search):
        self.submission.catalog_book = None
        self.submission.save(update_fields=["catalog_book"])
        self.run_worker()
        read_state.assert_not_called()
        self.outbox.refresh_from_db()
        self.assertEqual(self.outbox.result_detail, "no_match")

    @patch("core.hardcover_sync.search_books")
    @patch("core.hardcover_sync.read_library_state")
    def test_title_alone_is_rejected_without_search_or_library_mutation(self, read_state, search):
        self.submission.catalog_book = None
        self.submission.author = ""
        self.submission.save(update_fields=["catalog_book", "author"])
        self.run_worker()
        search.assert_not_called()
        read_state.assert_not_called()
        self.outbox.refresh_from_db()
        self.assertEqual(self.outbox.result_detail, "insufficient_metadata")

    @patch("core.hardcover_sync.read_library_state")
    def test_withdrawn_consent_prevents_provider_read_or_write(self, read_state):
        ReaderHardcoverSyncPreference.objects.filter(user=self.reader).update(sync_completed_books=False)
        self.run_worker()
        read_state.assert_not_called()
        self.outbox.refresh_from_db()
        self.assertEqual(self.outbox.result_detail, "consent_withdrawn")

    @patch("core.hardcover_sync.read_library_state", side_effect=HardcoverConnectionError("timeout access_token=secret", classification="temporary_network", retryable=True))
    def test_transient_failure_retries_without_secret(self, read_state):
        self.run_worker()
        self.outbox.refresh_from_db()
        self.assertEqual(self.outbox.status, HardcoverSyncOutbox.Status.RETRYABLE)
        self.assertIsNotNone(self.outbox.next_attempt_at)
        self.assertNotIn("secret", self.outbox.error_message)
        self.assertNotIn("secret", self.outbox.provenance.get().error_message)

    @patch("core.hardcover_sync.read_library_state", side_effect=HardcoverConnectionError("denied", classification="insufficient_permission", reconnect_required=True))
    def test_permission_failure_requires_reconnect(self, read_state):
        self.run_worker()
        self.outbox.refresh_from_db()
        self.assertEqual(self.outbox.status, HardcoverSyncOutbox.Status.BLOCKED)
        self.assertEqual(self.outbox.result_detail, "reconnect_required")

    def test_group_credential_is_never_consulted(self):
        HardcoverConnection.objects.create(group=self.group, encrypted_token="group-token", is_valid=True)
        ReaderHardcoverConnection.objects.filter(user=self.reader).delete()
        with patch("core.hardcover_sync.read_library_state") as provider:
            process_hardcover_sync_batch(batch_size=1)
        provider.assert_not_called()
        self.outbox.refresh_from_db()
        self.assertEqual(self.outbox.status, HardcoverSyncOutbox.Status.BLOCKED)

    def test_worker_start_does_not_discover_historical_approvals(self):
        HardcoverSyncOutbox.objects.all().delete()
        with patch("core.hardcover_sync.get_reader_hardcover_credential") as credential:
            self.assertEqual(process_hardcover_sync_batch(), [])
        credential.assert_not_called()

    def test_provenance_contains_safe_occurrence_and_result(self):
        with patch("core.hardcover_sync.read_library_state", return_value=LibraryState({"id": 902, "status_id": 3}, 3)):
            self.run_worker()
        attempt = HardcoverSyncProvenance.objects.get()
        self.assertEqual(attempt.source_id, str(self.submission.pk))
        self.assertEqual(attempt.provider_book_id, "123")
        self.assertEqual(attempt.result_detail, "already_satisfied")


class HardcoverLibraryContractTests(TestCase):
    @patch("core.integrations.hardcover_library.execute_graphql", return_value={"insert_user_book": {"id": 77, "error": None}})
    def test_create_payload_contains_only_library_identity_and_read_status(self, execute):
        self.assertEqual(create_read_user_book("token", book_id="123", read_status_id=3, edition_id=8), "77")
        variables = execute.call_args.args[2]
        self.assertEqual(variables, {"object": {"book_id": 123, "status_id": 3, "edition_id": 8}})
        self.assertNotIn("date", str(variables).lower())

    @patch("core.integrations.hardcover_library.execute_graphql", return_value={"update_user_book": {"id": 78, "error": None}})
    def test_update_payload_changes_status_only(self, execute):
        self.assertEqual(update_user_book_to_read("token", user_book_id=78, read_status_id=3), "78")
        variables = execute.call_args.args[2]
        self.assertEqual(variables, {"id": 78, "object": {"status_id": 3}})
        query = execute.call_args.args[1]
        self.assertNotIn("user_book_read", query)
        self.assertNotIn("delete_", query)
