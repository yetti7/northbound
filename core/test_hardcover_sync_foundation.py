from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .hardcover_sync import enqueue_submission_sync, record_sync_attempt
from .integrations.secrets import encrypt_token
from .models import (
    AuditEvent,
    BookSubmission,
    ChallengeMonth,
    HardcoverConnection,
    HardcoverSyncOutbox,
    HardcoverSyncProvenance,
    Membership,
    ReaderHardcoverConnection,
    ReaderHardcoverSyncPreference,
    ReadingGroup,
)


class HardcoverSyncConsentTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.reader = user_model.objects.create_user("sync-reader", password="password")
        self.other = user_model.objects.create_user("other-reader", password="password")
        self.owner = user_model.objects.create_superuser("sync-owner", password="password")
        self.url = reverse("reader-hardcover-sync-preferences")

    def connect_pat(self, user=None):
        return ReaderHardcoverConnection.objects.create(
            user=user or self.reader,
            connection_method=ReaderHardcoverConnection.ConnectionMethod.API_KEY,
            encrypted_token=encrypt_token("reader-pat"),
            is_valid=True,
        )

    def connect_oauth(self, scopes=None):
        return ReaderHardcoverConnection.objects.create(
            user=self.reader,
            connection_method=ReaderHardcoverConnection.ConnectionMethod.OAUTH,
            encrypted_token=encrypt_token("oauth-access"),
            encrypted_refresh_token=encrypt_token("oauth-refresh"),
            granted_scopes=scopes or ["read:catalog", "read:library", "write:library"],
            is_valid=True,
        )

    def test_defaults_off_without_creating_preferences_on_account_view(self):
        self.client.force_login(self.reader)
        response = self.client.get(reverse("account"))

        self.assertContains(response, "Connecting Hardcover alone does not enable it")
        self.assertContains(response, "Synchronization unavailable")
        self.assertFalse(ReaderHardcoverSyncPreference.objects.filter(user=self.reader).exists())
        self.assertFalse(response.context["reader_hardcover_sync_form"].initial.get("sync_completed_books", False))
        self.assertFalse(response.context["reader_hardcover_sync_form"].initial.get("sync_completion_dates", False))

    def test_oauth_and_pat_connections_do_not_imply_consent(self):
        for method in ("oauth", "pat"):
            with self.subTest(method=method):
                ReaderHardcoverConnection.objects.filter(user=self.reader).delete()
                ReaderHardcoverSyncPreference.objects.filter(user=self.reader).delete()
                self.connect_oauth() if method == "oauth" else self.connect_pat()
                self.client.force_login(self.reader)
                self.client.get(reverse("account"))
                self.assertFalse(ReaderHardcoverSyncPreference.objects.filter(user=self.reader).exists())

    def test_reader_can_enable_pat_sync_and_dependency_is_enforced(self):
        self.connect_pat()
        self.client.force_login(self.reader)
        account = self.client.get(reverse("account"))
        self.assertContains(account, "Northbound cannot inspect PAT scope metadata")
        self.assertContains(account, "write:library")
        response = self.client.post(self.url, {
            "sync_completed_books": "on",
            "sync_completion_dates": "on",
        })

        self.assertRedirects(response, reverse("account"))
        preference = ReaderHardcoverSyncPreference.objects.get(user=self.reader)
        self.assertTrue(preference.sync_completed_books)
        self.assertTrue(preference.sync_completion_dates)
        self.assertTrue(AuditEvent.objects.filter(
            actor=self.reader,
            action="reader_hardcover.sync_completed_books_enabled",
        ).exists())
        self.assertTrue(AuditEvent.objects.filter(
            actor=self.reader,
            action="reader_hardcover.sync_completion_dates_enabled",
        ).exists())

        self.client.post(self.url, {"sync_completion_dates": "on"})
        preference.refresh_from_db()
        self.assertFalse(preference.sync_completed_books)
        self.assertFalse(preference.sync_completion_dates)

    def test_missing_oauth_write_scope_prevents_enablement(self):
        self.connect_oauth(scopes=["read:catalog", "read:library"])
        self.client.force_login(self.reader)
        response = self.client.post(self.url, {"sync_completed_books": "on"})

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "approve library-write permission", status_code=400)
        self.assertFalse(ReaderHardcoverSyncPreference.objects.filter(
            user=self.reader, sync_completed_books=True,
        ).exists())

    def test_group_credential_never_enables_reader_sync(self):
        group = ReadingGroup.objects.create(name="Group credential", slug="group-credential")
        HardcoverConnection.objects.create(
            group=group,
            encrypted_token=encrypt_token("group-token"),
            is_valid=True,
        )
        self.client.force_login(self.reader)
        response = self.client.post(self.url, {"sync_completed_books": "on"})

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Connect your personal Hardcover account", status_code=400)
        self.assertFalse(ReaderHardcoverSyncPreference.objects.filter(
            user=self.reader, sync_completed_books=True,
        ).exists())

    def test_platform_owner_and_other_reader_can_only_change_their_own_preferences(self):
        self.connect_pat()
        target = ReaderHardcoverSyncPreference.objects.create(user=self.reader)
        for actor in (self.other, self.owner):
            with self.subTest(actor=actor.username):
                ReaderHardcoverConnection.objects.filter(user=actor).delete()
                ReaderHardcoverConnection.objects.create(
                    user=actor,
                    encrypted_token=encrypt_token("actor-token"),
                    is_valid=True,
                )
                self.client.force_login(actor)
                self.client.post(self.url, {"sync_completed_books": "on"})
                target.refresh_from_db()
                self.assertFalse(target.sync_completed_books)
                self.assertTrue(ReaderHardcoverSyncPreference.objects.get(user=actor).sync_completed_books)

    def test_enabling_consent_does_not_backfill_existing_approved_submissions(self):
        self.connect_pat()
        self.make_submission(self.reader, title="Historical")
        self.client.force_login(self.reader)

        with patch("urllib.request.urlopen") as provider_call:
            self.client.post(self.url, {"sync_completed_books": "on"})

        self.assertFalse(provider_call.called)
        self.assertFalse(HardcoverSyncOutbox.objects.exists())
        self.assertFalse(HardcoverSyncProvenance.objects.exists())

    def test_disconnect_clears_active_consent(self):
        self.connect_pat()
        preference = ReaderHardcoverSyncPreference.objects.create(
            user=self.reader,
            sync_completed_books=True,
            sync_completion_dates=True,
        )
        self.client.force_login(self.reader)
        self.client.post(reverse("reader-hardcover-disconnect"))

        preference.refresh_from_db()
        self.assertFalse(preference.sync_completed_books)
        self.assertFalse(preference.sync_completion_dates)

    @staticmethod
    def make_submission(user, title="Book", completed_on=date(2026, 9, 5)):
        group = ReadingGroup.objects.create(name=f"{title} Group", slug=f"{title.lower()}-group")
        member = Membership.objects.create(group=group, user=user, display_name=user.username)
        month = ChallengeMonth.objects.create(
            group=group,
            name="September",
            starts_on=date(2026, 9, 1),
            ends_on=date(2026, 9, 30),
            status=ChallengeMonth.Status.ACTIVE,
        )
        return BookSubmission.objects.create(
            month=month,
            participant=member,
            title=title,
            author="Author",
            book_format=BookSubmission.Format.EBOOK,
            completed_on=completed_on,
            submitted_pages=100,
            approved_pages=100,
            status=BookSubmission.Status.APPROVED,
        )


class HardcoverSyncOutboxTests(TestCase):
    def setUp(self):
        self.reader = get_user_model().objects.create_user("outbox-reader", password="password")
        self.first = HardcoverSyncConsentTests.make_submission(self.reader, title="First")

    def test_outbox_creation_defaults_and_idempotent_dedupe(self):
        first, created = enqueue_submission_sync(self.first, HardcoverSyncOutbox.Action.COMPLETED_BOOK)
        duplicate, duplicate_created = enqueue_submission_sync(
            self.first, HardcoverSyncOutbox.Action.COMPLETED_BOOK
        )

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first, duplicate)
        self.assertEqual(first.user, self.reader)
        self.assertEqual(first.source_type, "BookSubmission")
        self.assertEqual(first.source_id, str(self.first.pk))
        self.assertEqual(first.effective_date, self.first.completed_on)
        self.assertEqual(first.status, HardcoverSyncOutbox.Status.PENDING)
        self.assertEqual(first.attempt_count, 0)
        self.assertIsNone(first.last_attempt_at)
        self.assertIsNone(first.next_attempt_at)

    def test_distinct_submission_occurrences_remain_distinct_rereads(self):
        second = HardcoverSyncConsentTests.make_submission(
            self.reader, title="Second", completed_on=date(2026, 9, 12)
        )
        first_event, _ = enqueue_submission_sync(self.first, HardcoverSyncOutbox.Action.COMPLETED_BOOK)
        second_event, _ = enqueue_submission_sync(second, HardcoverSyncOutbox.Action.COMPLETED_BOOK)

        self.assertNotEqual(first_event.event_key, second_event.event_key)
        self.assertEqual(HardcoverSyncOutbox.objects.count(), 2)

    def test_provenance_is_attributed_redacted_and_append_only(self):
        outbox, _ = enqueue_submission_sync(self.first, HardcoverSyncOutbox.Action.COMPLETED_BOOK)
        provenance = record_sync_attempt(
            outbox=outbox,
            outcome=HardcoverSyncProvenance.Outcome.RETRYABLE_FAILURE,
            provider_identifier="read-id-123",
            error_classification="temporary_network",
            error_message="access_token=must-not-remain timeout",
        )

        outbox.refresh_from_db()
        self.assertEqual(provenance.user, self.reader)
        self.assertEqual(provenance.source_id, str(self.first.pk))
        self.assertEqual(provenance.action, HardcoverSyncOutbox.Action.COMPLETED_BOOK)
        self.assertEqual(provenance.provider_identifier, "read-id-123")
        self.assertNotIn("must-not-remain", provenance.error_message)
        self.assertNotIn("must-not-remain", outbox.error_message)
        self.assertEqual(outbox.status, HardcoverSyncOutbox.Status.RETRYABLE)
        self.assertEqual(outbox.attempt_count, 1)

        provenance.error_message = "changed"
        with self.assertRaises(ValidationError):
            provenance.save()
        with self.assertRaises(ValidationError):
            provenance.delete()

    def test_models_have_no_credential_storage_fields(self):
        forbidden = {"token", "secret", "credential", "authorization", "ciphertext"}
        for model in (HardcoverSyncOutbox, HardcoverSyncProvenance):
            names = {field.name.lower() for field in model._meta.fields}
            self.assertFalse(any(fragment in name for name in names for fragment in forbidden))
