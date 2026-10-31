"""HC.7 safety checks. Provider interactions are mocked; no live credentials."""
import base64
import hashlib
import json
import os
from datetime import timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .hardcover_oauth import OAuthTokenSet, _basic_authorization, canonical_oauth_urls, save_oauth_application
from .hardcover_sync import enqueue_submission_sync, process_hardcover_sync_batch, make_existing_reader_events_due
from .integrations.credentials import BearerCredential, CredentialMethod, CredentialOwner
from .integrations.hardcover_library import LibraryState
from .integrations.http import NoCredentialRedirect
from .integrations.secrets import TokenDecryptionError, decrypt_token, encrypt_token
from .models import HardcoverOAuthApplication, HardcoverOAuthStateUse, HardcoverSyncOutbox, ReaderHardcoverConnection, ReaderHardcoverSyncPreference, safe_audit_summary, BookSubmission
from .reader_hardcover import ReaderHardcoverUnavailable, get_reader_hardcover_credential
from .scoring import apply_submission_review
from .test_hardcover_completed_book_sync import SyncFixtureMixin
from .views import READER_OAUTH_SESSION_KEY


@override_settings(NORTHBOUND_URL="https://northbound.example.com")
class HardcoverReleaseOAuthTests(TestCase):
    def setUp(self):
        self.reader = get_user_model().objects.create_user("hc7-reader")
        self.client.force_login(self.reader)
        self.app = save_oauth_application(application=HardcoverOAuthApplication(), client_id="client", client_secret="fake-secret", enabled=True, urls=canonical_oauth_urls())[0]

    def connection(self):
        return ReaderHardcoverConnection.objects.create(user=self.reader, connection_method="oauth", encrypted_token=encrypt_token("fake-access"), encrypted_refresh_token=encrypt_token("fake-refresh"), is_valid=True, access_expires_at=timezone.now(), granted_scopes=["read:catalog", "read:library", "write:library"])

    def test_stale_session_cannot_replay_consumed_state(self):
        response = self.client.post(reverse("reader-hardcover-oauth-start"))
        state = parse_qs(urlparse(response.url).query)["state"][0]
        old_flows = self.client.session[READER_OAUTH_SESSION_KEY]
        verifier = next(iter(old_flows.values()))["code_verifier"]
        self.assertNotEqual(verifier, decrypt_token(verifier))
        callback = reverse("reader-hardcover-oauth-callback")
        self.client.get(callback, {"state": state, "iss": "https://api.hardcover.app", "error": "denied"})
        session = self.client.session
        session[READER_OAUTH_SESSION_KEY] = old_flows
        session.save()
        with patch("core.views.exchange_authorization_code") as exchange:
            result = self.client.get(callback, {"state": state, "iss": "https://api.hardcover.app", "code": "fake-code"}, follow=True)
        exchange.assert_not_called()
        self.assertContains(result, "could not be verified")
        self.assertEqual(HardcoverOAuthStateUse.objects.count(), 1)

    def test_refresh_claim_prevents_second_exchange_and_survives_ambiguous_result(self):
        saved = self.connection()
        def exchange(**kwargs):
            saved.refresh_from_db()
            self.assertEqual(saved.encrypted_refresh_token, "")
            with self.assertRaises(ReaderHardcoverUnavailable):
                get_reader_hardcover_credential(self.reader)
            return OAuthTokenSet("new-access", "new-refresh", timezone.now() + timedelta(hours=1), ("read:catalog", "read:library", "write:library"))
        with patch("core.reader_hardcover.exchange_refresh_token", side_effect=exchange) as call:
            self.assertEqual(get_reader_hardcover_credential(self.reader).bearer_token, "new-access")
        self.assertEqual(call.call_count, 1)

    def test_refresh_does_not_resurrect_disconnected_connection(self):
        saved = self.connection()
        def exchange(**kwargs):
            ReaderHardcoverConnection.objects.filter(pk=saved.pk).delete()
            return OAuthTokenSet("new-access", "new-refresh", timezone.now(), ("read:catalog", "read:library", "write:library"))
        with patch("core.reader_hardcover.exchange_refresh_token", side_effect=exchange), self.assertRaises(ReaderHardcoverUnavailable):
            get_reader_hardcover_credential(self.reader)
        self.assertFalse(ReaderHardcoverConnection.objects.exists())

    def test_refresh_refuses_outer_transaction_that_could_rollback_spent_claim(self):
        self.connection()
        with transaction.atomic(), patch("core.reader_hardcover.exchange_refresh_token") as exchange, self.assertRaises(ReaderHardcoverUnavailable):
            get_reader_hardcover_credential(self.reader)
        exchange.assert_not_called()

    def test_cancelled_inflight_callback_does_not_reconnect_after_disconnect(self):
        response = self.client.post(reverse("reader-hardcover-oauth-start"))
        state = parse_qs(urlparse(response.url).query)["state"][0]
        state_hash = hashlib.sha256(state.encode()).hexdigest()
        def exchange(**kwargs):
            HardcoverOAuthStateUse.objects.filter(state_hash=state_hash).update(completed_or_cancelled=True)
            return OAuthTokenSet("new-access", "new-refresh", timezone.now(), ("read:catalog", "read:library", "write:library"))
        with patch("core.views.exchange_authorization_code", side_effect=exchange), patch("core.views.test_catalog_connection", return_value=True):
            result = self.client.get(reverse("reader-hardcover-oauth-callback"), {"state": state, "iss": "https://api.hardcover.app", "code": "fake-code"}, follow=True)
        self.assertContains(result, "was cancelled")
        self.assertFalse(ReaderHardcoverConnection.objects.exists())

    def test_changing_client_id_invalidates_old_reader_authorization(self):
        saved = self.connection()
        save_oauth_application(application=self.app, client_id="different-app", client_secret="", enabled=True, urls=canonical_oauth_urls())
        saved.refresh_from_db()
        self.assertTrue(saved.reconnect_required)
        self.assertEqual(saved.encrypted_refresh_token, "")

    def test_disabled_application_blocks_unexpired_oauth_but_not_pat(self):
        saved = self.connection()
        saved.access_expires_at = timezone.now() + timedelta(days=1)
        saved.save()
        self.app.enabled = False
        self.app.save()
        with self.assertRaises(ReaderHardcoverUnavailable):
            get_reader_hardcover_credential(self.reader)
        saved.connection_method = "api_key"
        saved.save()
        self.assertEqual(get_reader_hardcover_credential(self.reader).bearer_token, "fake-access")

    def test_disconnect_removes_local_access_before_slow_revocation(self):
        self.connection()
        def revoke(**kwargs):
            self.assertFalse(ReaderHardcoverConnection.objects.filter(user=self.reader).exists())
        with patch("core.views.revoke_oauth_token", side_effect=revoke):
            self.client.post(reverse("reader-hardcover-disconnect"))

    def test_setup_reader_render_and_optional_disposable_preview(self):
        account = self.client.get(reverse("account"))
        self.assertContains(account, "historical synchronization is not supported")
        self.reader.is_superuser = True
        self.reader.save()
        setup = self.client.get(reverse("platform-hardcover-oauth"))
        self.assertContains(setup, "does not ship a shared hosted OAuth application")
        self.assertNotContains(setup, "fake-secret")
        if os.environ.get("HC7_PREVIEW_DIR"):
            root = Path(os.environ["HC7_PREVIEW_DIR"])
            (root / "account.html").write_bytes(account.content)
            (root / "setup.html").write_bytes(setup.content)


class HardcoverReleaseSyncTests(SyncFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.enable()
        self.submission = self.make_submission(status=BookSubmission.Status.APPROVED)
        self.outbox, _ = enqueue_submission_sync(self.submission, HardcoverSyncOutbox.Action.COMPLETED_BOOK)

    def test_consent_withdrawn_during_provider_read_prevents_write(self):
        def read(*args):
            ReaderHardcoverSyncPreference.objects.filter(user=self.reader).update(sync_completed_books=False)
            return LibraryState(None, 3)
        credential = BearerCredential("fake", CredentialMethod.API_KEY, CredentialOwner.READER)
        with patch("core.hardcover_sync.get_reader_hardcover_credential", return_value=credential), patch("core.hardcover_sync.read_library_state", side_effect=read), patch("core.hardcover_sync.create_read_user_book") as create:
            process_hardcover_sync_batch()
        create.assert_not_called()

    def test_consent_enabled_after_approval_before_commit_callback_does_not_queue(self):
        ReaderHardcoverSyncPreference.objects.filter(user=self.reader).update(sync_completed_books=False)
        submission = self.make_submission()
        submission.status, submission.approved_pages = BookSubmission.Status.APPROVED, 100
        with self.captureOnCommitCallbacks(execute=True):
            apply_submission_review(submission, [], self.reviewer, timezone.now())
            ReaderHardcoverSyncPreference.objects.filter(user=self.reader).update(sync_completed_books=True)
        self.assertEqual(HardcoverSyncOutbox.objects.count(), 1)

    def test_restore_also_quarantines_credential_blocked_and_invalidates_spent_oauth(self):
        HardcoverSyncOutbox.objects.filter(pk=self.outbox.pk).update(status="blocked", error_classification="credential_rejected", provider_read_id="987", occurrence_result_detail="created")
        ReaderHardcoverConnection.objects.filter(user=self.reader).update(connection_method="oauth", encrypted_refresh_token=encrypt_token("spent"))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "last-restore.json").write_text(json.dumps({"restored_at": "test-restore"}))
            with patch("core.management.commands.safeguard_restored_hardcover_sync.data_root", return_value=root):
                call_command("safeguard_restored_hardcover_sync", stdout=StringIO())
                call_command("safeguard_restored_hardcover_sync", stdout=StringIO())
        self.outbox.refresh_from_db()
        self.assertEqual(self.outbox.provider_read_id, "987")
        self.assertEqual(self.outbox.error_classification, "restore_reconciliation_required")
        self.assertEqual(self.outbox.provenance.count(), 1)
        saved = ReaderHardcoverConnection.objects.get(user=self.reader)
        self.assertEqual(saved.encrypted_refresh_token, "")
        self.assertTrue(saved.reconnect_required)
        self.assertEqual(make_existing_reader_events_due(self.reader)[0], 0)

    def test_worker_batch_lock_excludes_overlapping_process_attempt(self):
        with patch("core.hardcover_sync.fcntl.flock", side_effect=BlockingIOError), patch("core.hardcover_sync._claim_one") as claim:
            self.assertEqual(process_hardcover_sync_batch(), [])
        claim.assert_not_called()

    def test_postgres_automatic_safeguard_does_not_treat_database_name_as_path(self):
        with patch("core.management.commands.safeguard_restored_hardcover_sync.connection") as database, patch("core.management.commands.safeguard_restored_hardcover_sync.data_root") as root:
            database.vendor = "postgresql"
            call_command("safeguard_restored_hardcover_sync")
        root.assert_not_called()


class HardcoverReleaseSafetyTests(TestCase):
    def test_incomplete_library_response_is_not_interpreted_as_missing_book(self):
        from .integrations.hardcover_library import read_library_state, _mutation_result
        from .integrations.hardcover import HardcoverConnectionError
        for reader in ({}, {"user_books": None}, {"user_books": "invalid"}):
            with patch("core.integrations.hardcover_library.execute_graphql", return_value={"me": [reader], "user_book_statuses": [{"id": 3, "slug": "read"}]}), self.assertRaises(HardcoverConnectionError):
                read_library_state("fixture", "123")
        with self.assertRaises(HardcoverConnectionError):
            _mutation_result({"insert_user_book_read": {"id": "fake-sensitive"}}, "insert_user_book_read")
    def test_oauth_rejects_plain_http_public_origin(self):
        self.assertTrue(canonical_oauth_urls("http://northbound.example.com").error)
        self.assertFalse(canonical_oauth_urls("http://127.0.0.1:8000").error)
    def test_key_roundtrip_wrong_missing_and_malformed_keys_fail_safely(self):
        key = Fernet.generate_key().decode()
        with override_settings(TOKEN_ENCRYPTION_KEY=key):
            ciphertext = encrypt_token("fixture-only")
            self.assertEqual(decrypt_token(ciphertext), "fixture-only")
        for wrong_key in ("", Fernet.generate_key().decode(), "invalid", "é"):
            with self.subTest(key_kind=len(wrong_key)), override_settings(TOKEN_ENCRYPTION_KEY=wrong_key), self.assertRaises(TokenDecryptionError):
                decrypt_token(ciphertext)

    def test_legacy_fallback_requires_same_django_secret(self):
        with override_settings(TOKEN_ENCRYPTION_KEY="", SECRET_KEY="old-fixture-secret"):
            encrypted = encrypt_token("fake-pat")
            self.assertEqual(decrypt_token(encrypted), "fake-pat")
        with override_settings(TOKEN_ENCRYPTION_KEY="", SECRET_KEY="new-fixture-secret"), self.assertRaises(TokenDecryptionError):
            decrypt_token(encrypted)

    def test_secret_representations_headers_and_callback_queries_are_redacted(self):
        credential = BearerCredential("fake-sensitive", CredentialMethod.OAUTH, CredentialOwner.READER)
        tokens = OAuthTokenSet("fake-sensitive", "fake-sensitive", None, ())
        self.assertNotIn("fake-sensitive", repr(credential) + repr(tokens))
        for value in ("Authorization: Bearer fake-sensitive", "Authorization: Basic fake-sensitive", "?code=fake-sensitive&state=fake-sensitive", "client_secret=fake-sensitive"):
            self.assertNotIn("fake-sensitive", safe_audit_summary(value))

    def test_worker_unexpected_errors_do_not_print_exception(self):
        output = StringIO()
        with patch("core.management.commands.run_hardcover_sync_worker.call_command"), patch("core.management.commands.run_hardcover_sync_worker.process_hardcover_sync_batch", side_effect=RuntimeError("fake-sensitive")):
            call_command("run_hardcover_sync_worker", once=True, stderr=output)
        self.assertNotIn("fake-sensitive", output.getvalue())

    def test_credential_http_redirects_are_disabled_and_basic_values_encoded(self):
        self.assertIsNone(NoCredentialRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))
        header = _basic_authorization("id:colon", "secret+value")
        self.assertEqual(base64.b64decode(header.split()[1]).decode(), "id%3Acolon:secret%2Bvalue")

    def test_mutation_allowlist_and_startup_health_contract(self):
        import ast
        import re
        from .integrations import hardcover_library
        source = Path(hardcover_library.__file__).read_text()
        queries = "\n".join(node.value for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Constant) and isinstance(node.value, str))
        mutations = set(re.findall(r"\b((?:insert|update|delete|upsert)_[a-z_]+)\s*\(", queries))
        self.assertEqual(mutations, {"insert_user_book", "update_user_book", "insert_user_book_read", "update_user_book_read"})
        start = (Path(__file__).resolve().parent.parent / "deploy/start.sh").read_text()
        self.assertLess(start.index("migrate --noinput"), start.index("safeguard_restored_hardcover_sync"))
        self.assertLess(start.index("safeguard_restored_hardcover_sync"), start.index("run_hardcover_sync_worker"))
        self.assertIn("%(U)s", start)
        self.assertNotIn("%(r)s", start)
        with patch("core.integrations.hardcover.urlopen", side_effect=RuntimeError("provider down")) as provider:
            self.assertEqual(self.client.get("/health/").content, b"OK")
        provider.assert_not_called()

    def test_authenticated_health_never_creates_reader_profile(self):
        from .models import UserProfile
        reader = get_user_model().objects.create_user("health-without-profile")
        self.client.force_login(reader)
        self.assertEqual(self.client.get("/health/").content, b"OK")
        self.assertFalse(UserProfile.objects.filter(user=reader).exists())


class HardcoverBackupRoundTripTests(SyncFixtureMixin, TransactionTestCase):
    def test_real_sqlite_zip_restore_preserves_encryption_and_sync_mapping(self):
        import sqlite3
        from types import SimpleNamespace
        from .backups import create_stored_backup, stage_stored_restore, apply_pending_restore
        self.enable()
        saved = ReaderHardcoverConnection.objects.get(user=self.reader)
        saved.encrypted_token = encrypt_token("backup-fixture-pat")
        saved.save()
        event, _ = enqueue_submission_sync(self.make_submission(status="approved"), HardcoverSyncOutbox.Action.COMPLETED_BOOK)
        HardcoverSyncOutbox.objects.filter(pk=event.pk).update(provider_book_id="123", provider_user_book_id="456", provider_read_id="789", occurrence_result_detail="created")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "media"
            media.mkdir()
            target = root / "restored.sqlite3"
            with patch("core.backups.data_root", return_value=root), patch("core.maintenance_lock.maintenance_root", return_value=root), patch("core.backups.settings", SimpleNamespace(MEDIA_ROOT=media, DATABASES={"default": {"NAME": str(target)}})):
                archive = create_stored_backup()
                stage_stored_restore(archive)
                real_replace = os.replace
                def replace(source, destination):
                    if Path(destination) == target:
                        self.assertTrue((root / "last-restore.json").exists())
                    return real_replace(source, destination)
                with patch("core.backups.os.replace", side_effect=replace):
                    apply_pending_restore()
                self.assertIsNone(apply_pending_restore())
            with sqlite3.connect(target) as restored:
                ciphertext = restored.execute("SELECT encrypted_token FROM core_readerhardcoverconnection WHERE id=?", (saved.pk,)).fetchone()[0]
                self.assertEqual(decrypt_token(ciphertext), "backup-fixture-pat")
                self.assertEqual(restored.execute("SELECT provider_read_id FROM core_hardcoversyncoutbox WHERE id=?", (event.pk,)).fetchone()[0], "789")
                self.assertEqual(restored.execute("SELECT COUNT(*) FROM core_hardcoversyncoutbox").fetchone()[0], 1)
                with override_settings(TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode()), self.assertRaises(TokenDecryptionError):
                    decrypt_token(ciphertext)


class HardcoverReleaseMigrationTests(TransactionTestCase):
    def test_0059_adds_empty_replay_fence_without_changing_existing_credentials(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        executor = MigrationExecutor(connection)
        old_target = [("core", "0058_hardcover_read_occurrence_sync")]
        executor.migrate(old_target)
        old = executor.loader.project_state(old_target).apps
        user = old.get_model("auth", "User").objects.create(username="hc7-migration")
        ciphertext = encrypt_token("migration-fixture")
        saved = old.get_model("core", "ReaderHardcoverConnection").objects.create(user_id=user.pk, encrypted_token=ciphertext, is_valid=True)
        executor = MigrationExecutor(connection)
        executor.migrate([("core", "0059_hardcover_oauth_state_use")])
        self.assertEqual(ReaderHardcoverConnection.objects.get(pk=saved.pk).encrypted_token, ciphertext)
        self.assertFalse(HardcoverOAuthStateUse.objects.exists())
        self.assertFalse(HardcoverSyncOutbox.objects.exists())
