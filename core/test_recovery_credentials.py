from datetime import date
import inspect
import json
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import views
from .integrations.secrets import encrypt_token
from .integrations.hardcover import HardcoverConnectionError, execute_graphql
from .models import (
    AuditEvent, BookSubmission, BotmBook, BotmCompletionAward,
    BotmCompletionAwardBook, BotmMatch, CatalogBook,
    CatalogEdition, ChallengeMonth, ChallengeStaffAssignment,
    Game, GameRewardApplication, GameRewardRecipient, HardcoverConnection,
    Membership, ModifierProvenance, MonthEnrollment, PersonalTBR,
    PersonalTBRBook, PersonalTBRCompletionAward,
    PersonalTBRCompletionAwardBook, PersonalTBRMatch, ProgressCheckpoint,
    ProgressCheckpointResult, ReaderHardcoverConnection, ReadingGroup,
    RecoveryOperation, Team, TeamAssignment,
)
from .reader_hardcover import save_reader_hardcover_connection
from .recovery import RecoveryRequest
from .recovery_credentials import (
    clear_group_hardcover_connection, clear_reader_hardcover_connection,
    group_credential_label, reader_credential_label,
    sanitized_credential_status,
)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class HardcoverCredentialRecoveryTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_superuser(
            "credential-platform-owner", password="owner-password-741!",
        )
        self.reader_user = User.objects.create_user("credential-reader", password="test-password")
        self.group_owner_user = User.objects.create_user("credential-group-owner", password="test-password")
        self.moderator_user = User.objects.create_user("credential-moderator", password="test-password")
        self.host_user = User.objects.create_user("credential-host", password="test-password")
        self.floater_user = User.objects.create_user("credential-floater", password="test-password")
        self.leader_user = User.objects.create_user("credential-leader", password="test-password")
        self.ordinary_user = User.objects.create_user("credential-ordinary", password="test-password")
        self.inactive_owner = User.objects.create_superuser(
            "credential-inactive-owner", password="test-password",
        )
        self.inactive_owner.is_active = False
        self.inactive_owner.save(update_fields=["is_active"])
        self.group = ReadingGroup.objects.create(
            name="Credential Recovery Group", slug="credential-recovery",
        )

        def member(user, name, role=Membership.Role.MEMBER):
            return Membership.objects.create(
                group=self.group, user=user, display_name=name, role=role,
            )

        self.reader = member(self.reader_user, "Credential Reader")
        self.group_owner = member(self.group_owner_user, "Group Owner", Membership.Role.OWNER)
        self.moderator = member(self.moderator_user, "Moderator", Membership.Role.MODERATOR)
        self.host = member(self.host_user, "Host")
        self.floater = member(self.floater_user, "Floater")
        self.leader = member(self.leader_user, "Leader")
        self.month = ChallengeMonth.objects.create(
            group=self.group, name="Credential History Challenge",
            starts_on=date(2026, 8, 1), ends_on=date(2026, 8, 31),
            status=ChallengeMonth.Status.ACTIVE,
        )
        self.enrollment = MonthEnrollment.objects.create(
            month=self.month, participant=self.reader,
        )
        self.team = Team.objects.create(month=self.month, name="Credential Team")
        TeamAssignment.objects.create(
            month=self.month, participant=self.reader, team=self.team,
        )
        MonthEnrollment.objects.create(month=self.month, participant=self.leader)
        TeamAssignment.objects.create(month=self.month, participant=self.leader, team=self.team)
        ChallengeStaffAssignment.objects.create(
            month=self.month, membership=self.host,
            role=ChallengeStaffAssignment.Role.HOST,
        )
        ChallengeStaffAssignment.objects.create(
            month=self.month, membership=self.floater,
            role=ChallengeStaffAssignment.Role.FLOATER,
        )
        ChallengeStaffAssignment.objects.create(
            month=self.month, membership=self.leader, team=self.team,
            role=ChallengeStaffAssignment.Role.TEAM_LEADER,
        )
        self.catalog = CatalogBook.objects.create(
            provider="hardcover", provider_book_id="credential-book",
            title="Durable Credential Book", author="Writer",
        )
        self.edition = CatalogEdition.objects.create(
            book=self.catalog, provider="hardcover", provider_edition_id="credential-edition",
            page_count=250,
        )
        self.submission = BookSubmission.objects.create(
            month=self.month, participant=self.reader, catalog_book=self.catalog,
            catalog_edition=self.edition, title="Durable Credential Book", author="Writer",
            book_format=BookSubmission.Format.EBOOK, completed_on=date(2026, 8, 10),
            submitted_pages=250, approved_pages=250,
            status=BookSubmission.Status.APPROVED,
        )
        self.botm_book = BotmBook.objects.create(
            month=self.month, position=1, catalog_book=self.catalog,
            catalog_edition=self.edition, title_snapshot="Durable Credential Book",
            author_snapshot="Writer", page_count_snapshot=250,
        )
        self.tbr = PersonalTBR.objects.create(enrollment=self.enrollment)
        self.tbr_book = PersonalTBRBook.objects.create(
            personal_tbr=self.tbr, position=1, catalog_book=self.catalog,
            catalog_edition=self.edition, title_snapshot="Durable Credential Book",
            author_snapshot="Writer", normalized_title="durablecredentialbook",
            normalized_author="writer", page_count_snapshot=250,
        )
        PersonalTBR.objects.filter(pk=self.tbr.pk).update(confirmed_at=timezone.now())
        self.tbr.refresh_from_db()
        self.reader_secret = "reader-super-secret-credential"
        self.group_secret = "group-super-secret-credential"
        self.reader_connection = ReaderHardcoverConnection.objects.create(
            user=self.reader_user, encrypted_token=encrypt_token(self.reader_secret),
            token_hint="R741", tested_at=timezone.now(), is_valid=True,
            last_error=f"api_token={self.reader_secret}",
        )
        self.group_connection = HardcoverConnection.objects.create(
            group=self.group, encrypted_token=encrypt_token(self.group_secret),
            token_hint="G741", tested_at=timezone.now(), is_valid=False,
            last_error=f"api_token={self.group_secret}",
        )

    def request(self, connection, *, reader):
        label = (
            reader_credential_label(connection)
            if reader else group_credential_label(connection)
        )
        return RecoveryRequest(
            actor=self.owner, tier=2,
            action=(
                "hardcover.reader_connection_cleared"
                if reader else "hardcover.group_connection_cleared"
            ),
            target_type=connection.__class__.__name__, target_id=str(connection.pk),
            target_label=label, reason="Clear an operationally broken connection",
            required_confirmation=label, supplied_confirmation=label,
            confirmation_method="exact target label",
            group=None if reader else self.group,
        )

    def durable_counts(self):
        return {
            model.__name__: model.objects.count()
            for model in (
                CatalogBook, CatalogEdition, BookSubmission, BotmBook,
                PersonalTBR, PersonalTBRBook, Membership, MonthEnrollment,
                TeamAssignment, ChallengeMonth,
            )
        }

    def test_recovery_list_and_confirmation_expose_only_sanitized_status(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("recovery-hardcover-list"))
        self.assertContains(response, "Group Connections")
        self.assertContains(response, "Reader Connections")
        self.assertContains(response, "Personal, private Reader credentials")
        self.assertContains(response, "Needs reconnection")
        for forbidden in (
            self.reader_secret, self.group_secret,
            self.reader_connection.encrypted_token, self.group_connection.encrypted_token,
            "R741", "G741", "api_token=",
        ):
            self.assertNotContains(response, forbidden)
        confirmation = self.client.get(reverse(
            "recovery-reader-hardcover-clear", kwargs={"pk": self.reader_connection.pk},
        ))
        self.assertContains(confirmation, "private Reader-owned connection")
        self.assertNotContains(confirmation, self.reader_secret)
        self.assertNotContains(confirmation, "R741")

    def test_undecryptable_reader_and_group_connections_can_be_cleared(self):
        ReaderHardcoverConnection.objects.filter(pk=self.reader_connection.pk).update(
            encrypted_token="corrupt-reader-ciphertext",
        )
        HardcoverConnection.objects.filter(pk=self.group_connection.pk).update(
            encrypted_token="corrupt-group-ciphertext",
        )
        self.reader_connection.refresh_from_db()
        self.group_connection.refresh_from_db()
        self.assertFalse(sanitized_credential_status(self.reader_connection)["is_decryptable"])
        self.assertFalse(sanitized_credential_status(self.group_connection)["is_decryptable"])
        clear_reader_hardcover_connection(
            connection=self.reader_connection,
            recovery_request=self.request(self.reader_connection, reader=True),
        )
        clear_group_hardcover_connection(
            connection=self.group_connection,
            recovery_request=self.request(self.group_connection, reader=False),
        )
        self.assertFalse(ReaderHardcoverConnection.objects.exists())
        self.assertFalse(HardcoverConnection.objects.exists())

    def test_reader_clear_preserves_history_and_allows_normal_reconnect(self):
        before = self.durable_counts()
        operation = clear_reader_hardcover_connection(
            connection=self.reader_connection,
            recovery_request=self.request(self.reader_connection, reader=True),
        )
        self.assertFalse(ReaderHardcoverConnection.objects.filter(pk=self.reader_connection.pk).exists())
        self.assertEqual(self.durable_counts(), before)
        serialized = str(RecoveryOperation.objects.values().get(pk=operation.pk))
        for forbidden in (
            self.reader_secret, self.reader_connection.encrypted_token,
            self.reader_connection.token_hint, "api_token=",
        ):
            self.assertNotIn(forbidden, serialized)
        with patch("core.reader_hardcover.test_catalog_connection"):
            reconnected, created = save_reader_hardcover_connection(
                self.reader_user, "replacement-reader-token",
            )
        self.assertTrue(created)
        self.assertTrue(reconnected.is_valid)

    def test_group_clear_preserves_catalog_and_challenge_history(self):
        before = self.durable_counts()
        operation = clear_group_hardcover_connection(
            connection=self.group_connection,
            recovery_request=self.request(self.group_connection, reader=False),
        )
        self.assertFalse(HardcoverConnection.objects.filter(pk=self.group_connection.pk).exists())
        self.assertEqual(self.durable_counts(), before)
        serialized = str(RecoveryOperation.objects.values().get(pk=operation.pk))
        for forbidden in (
            self.group_secret, self.group_connection.encrypted_token,
            self.group_connection.token_hint, "api_token=",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_clear_requires_reason_and_exact_confirmation(self):
        self.client.force_login(self.owner)
        url = reverse("recovery-reader-hardcover-clear", kwargs={"pk": self.reader_connection.pk})
        label = reader_credential_label(self.reader_connection)
        for payload in (
            {"reason": "", "confirmation": label},
            {"reason": "Clear broken connection", "confirmation": "wrong"},
        ):
            response = self.client.post(url, payload)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(ReaderHardcoverConnection.objects.filter(pk=self.reader_connection.pk).exists())

    def test_clear_failure_rolls_back_and_stores_no_exception_or_secret(self):
        request = self.request(self.reader_connection, reader=True)
        with self.assertRaises(RuntimeError):
            clear_reader_hardcover_connection(
                connection=self.reader_connection, recovery_request=request,
                fail_after_step="delete",
            )
        self.assertTrue(ReaderHardcoverConnection.objects.filter(pk=self.reader_connection.pk).exists())
        operation = RecoveryOperation.objects.get(operation_id=request.operation_id)
        self.assertEqual(operation.result, RecoveryOperation.Result.FAILED)
        serialized = str(RecoveryOperation.objects.values().get(pk=operation.pk))
        self.assertNotIn("Injected", serialized)
        self.assertNotIn(self.reader_secret, serialized)
        self.assertFalse(RecoveryOperation.objects.filter(
            operation_id=request.operation_id, result=RecoveryOperation.Result.SUCCEEDED,
        ).exists())

    def test_normal_roles_and_inactive_platform_owner_are_denied(self):
        routes = (
            reverse("advanced-recovery"), reverse("recovery-history"),
            reverse("recovery-hardcover-list"),
            reverse("recovery-reader-hardcover-clear", kwargs={"pk": self.reader_connection.pk}),
            reverse("recovery-group-hardcover-clear", kwargs={"pk": self.group_connection.pk}),
        )
        baseline = (
            Membership.objects.count(), ChallengeStaffAssignment.objects.count(),
            MonthEnrollment.objects.count(), TeamAssignment.objects.count(),
            BookSubmission.objects.count(),
        )
        users = (
            self.group_owner_user, self.moderator_user, self.host_user,
            self.floater_user, self.leader_user, self.reader_user,
            self.ordinary_user,
        )
        for user in users:
            self.client.force_login(user)
            for route in routes:
                with self.subTest(user=user.username, route=route):
                    self.assertEqual(self.client.get(route).status_code, 403)
        self.client.force_login(self.inactive_owner)
        for route in routes:
            with self.subTest(user=self.inactive_owner.username, route=route):
                self.assertEqual(self.client.get(route).status_code, 302)
        request = RequestFactory().get("/config/recovery/")
        request.user = self.inactive_owner
        self.assertEqual(views._recovery_owner_denied(request).status_code, 403)
        self.assertEqual(baseline, (
            Membership.objects.count(), ChallengeStaffAssignment.objects.count(),
            MonthEnrollment.objects.count(), TeamAssignment.objects.count(),
            BookSubmission.objects.count(),
        ))
        self.assertFalse(Membership.objects.filter(user=self.owner).exists())

    def test_every_first_party_recovery_view_uses_the_central_active_owner_gate(self):
        for name, value in vars(views).items():
            if not callable(value) or not (name.startswith("recovery_") or name == "advanced_recovery"):
                continue
            source = inspect.getsource(value)
            with self.subTest(view=name):
                self.assertIn("_recovery_owner_denied(request)", source)

    def test_django_admin_contains_credentials_domain_history_and_ledgers(self):
        contained = (
            HardcoverConnection, ReaderHardcoverConnection, ModifierProvenance,
            BotmBook, BotmMatch, BotmCompletionAward, BotmCompletionAwardBook,
            PersonalTBR, PersonalTBRBook, PersonalTBRMatch,
            PersonalTBRCompletionAward, PersonalTBRCompletionAwardBook,
            Game, GameRewardApplication, GameRewardRecipient,
            ProgressCheckpoint, ProgressCheckpointResult,
            BookSubmission, ChallengeMonth, Membership, MonthEnrollment,
            ChallengeStaffAssignment, TeamAssignment,
        )
        for model in contained:
            with self.subTest(model=model.__name__):
                self.assertFalse(admin.site.is_registered(model))
        self.assertTrue(admin.site.is_registered(AuditEvent))
        self.assertTrue(admin.site.is_registered(RecoveryOperation))

    def test_recovery_history_uses_human_readable_action_without_secrets(self):
        operation = clear_reader_hardcover_connection(
            connection=self.reader_connection,
            recovery_request=self.request(self.reader_connection, reader=True),
        )
        self.client.force_login(self.owner)
        response = self.client.get(reverse("recovery-history"))
        self.assertContains(response, "Hardcover Reader Connection Cleared")
        self.assertContains(response, str(operation.operation_id))
        self.assertNotContains(response, self.reader_secret)
        self.assertNotContains(response, self.reader_connection.encrypted_token)
        self.assertNotContains(response, "R741")

    @patch("core.integrations.hardcover.urlopen")
    def test_provider_graphql_error_content_is_not_exposed(self, mocked_urlopen):
        provider_secret = "provider-response-secret-token"
        response = mocked_urlopen.return_value.__enter__.return_value
        response.read.return_value = json.dumps({
            "errors": [{"message": f"Authorization failed for {provider_secret}"}],
        }).encode("utf-8")
        with self.assertRaises(HardcoverConnectionError) as raised:
            execute_graphql("request-secret-token", "query { test }")
        self.assertEqual(str(raised.exception), "Hardcover rejected the catalog query.")
        self.assertNotIn(provider_secret, str(raised.exception))
