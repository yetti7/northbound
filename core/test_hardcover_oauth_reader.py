import hashlib
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .hardcover_oauth import (
    HardcoverOAuthError, OAuthTokenSet, _parse_token_payload,
    canonical_oauth_urls, save_oauth_application,
)
from .integrations.secrets import decrypt_token, encrypt_token
from .models import AuditEvent, HardcoverConnection, HardcoverOAuthApplication, ReaderHardcoverConnection, ReadingGroup
from .reader_hardcover import ReaderHardcoverUnavailable, get_reader_hardcover_credential
from .views import READER_OAUTH_SESSION_KEY


class HardcoverOAuthTokenContractTests(TestCase):
    def test_authorization_exchange_requires_refresh_token_and_uses_documented_week_lifetime(self):
        with self.assertRaises(HardcoverOAuthError):
            _parse_token_payload(
                {"access_token": "hc_at_access", "token_type": "Bearer"},
                require_refresh_token=True,
            )
        before = timezone.now() + timedelta(days=6, hours=23)
        token_set = _parse_token_payload(
            {"access_token": "hc_at_access", "refresh_token": "hc_rt_refresh", "token_type": "Bearer"},
            require_refresh_token=True,
        )
        self.assertGreater(token_set.expires_at, before)

    def test_refresh_requires_a_new_rotated_refresh_token(self):
        base = {
            "access_token": "hc_at_new", "refresh_token": "hc_rt_spent", "token_type": "Bearer",
            "scope": "read:catalog read:library write:library",
        }
        with self.assertRaises(HardcoverOAuthError):
            _parse_token_payload(
                base,
                prior_refresh_token="hc_rt_spent",
                require_refresh_token=True,
                require_rotated_refresh=True,
            )


@override_settings(NORTHBOUND_URL="https://northbound.example.com")
class ReaderHardcoverOAuthTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.reader = User.objects.create_user("oauth-reader", password="password")
        self.other = User.objects.create_user("other-reader", password="password")
        self.urls = canonical_oauth_urls()
        self.application = save_oauth_application(
            application=HardcoverOAuthApplication(),
            client_id="client-id",
            client_secret="client-secret",
            enabled=True,
            urls=self.urls,
        )[0]
        self.start_url = reverse("reader-hardcover-oauth-start")
        self.callback_url = reverse("reader-hardcover-oauth-callback")
        self.client.force_login(self.reader)

    def begin(self):
        response = self.client.post(self.start_url)
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response["Location"]).query)
        return response, query

    def test_start_uses_canonical_redirect_scopes_state_and_s256(self):
        response, query = self.begin()
        self.assertEqual(urlparse(response["Location"]).geturl().split("?", 1)[0], "https://hardcover.app/oauth2/authorize")
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["client_id"], ["client-id"])
        self.assertEqual(query["redirect_uri"], [self.urls.redirect_uri])
        self.assertEqual(query["scope"], ["read:catalog read:library write:library"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        state = query["state"][0]
        flows = self.client.session[READER_OAUTH_SESSION_KEY]
        self.assertNotIn(state, flows)
        self.assertIn(hashlib.sha256(state.encode("ascii")).hexdigest(), flows)
        self.assertNotIn("client-secret", str(flows))

    def test_start_requires_current_valid_enabled_nonstale_configuration(self):
        self.application.enabled = False
        self.application.save(update_fields=["enabled"])
        response = self.client.post(self.start_url, follow=True)
        self.assertContains(response, "Reader OAuth is unavailable")
        self.assertEqual(self.client.session.get(READER_OAUTH_SESSION_KEY, {}), {})

    @patch("core.views.test_catalog_connection", return_value=True)
    @patch("core.views.exchange_authorization_code")
    def test_success_stores_encrypted_oauth_credentials_and_safe_metadata(self, exchange, catalog):
        exchange.return_value = OAuthTokenSet(
            "access-secret", "refresh-secret", timezone.now() + timedelta(hours=1),
            ("read:catalog", "read:library", "write:library"),
        )
        _, query = self.begin()
        response = self.client.get(self.callback_url, {"code": "private-code", "state": query["state"][0], "iss": "https://api.hardcover.app"})
        self.assertRedirects(response, reverse("account"))
        connection = ReaderHardcoverConnection.objects.get(user=self.reader)
        self.assertEqual(connection.connection_method, ReaderHardcoverConnection.ConnectionMethod.OAUTH)
        self.assertEqual(decrypt_token(connection.encrypted_token), "access-secret")
        self.assertEqual(decrypt_token(connection.encrypted_refresh_token), "refresh-secret")
        self.assertNotIn("access-secret", connection.encrypted_token)
        self.assertEqual(connection.token_hint, "")
        self.assertEqual(connection.granted_scopes, ["read:catalog", "read:library", "write:library"])
        self.assertNotIn("private-code", AuditEvent.objects.get(action="reader_hardcover.connected").summary)
        catalog.assert_called_once_with("access-secret")

    @patch("core.views.exchange_authorization_code", side_effect=HardcoverOAuthError("provider secret detail"))
    def test_failed_oauth_replacement_preserves_working_api_key(self, exchange):
        connection = ReaderHardcoverConnection.objects.create(
            user=self.reader, encrypted_token=encrypt_token("working-api-key"), token_hint="-key", is_valid=True,
        )
        _, query = self.begin()
        response = self.client.get(self.callback_url, {"code": "private-code", "state": query["state"][0], "iss": "https://api.hardcover.app"}, follow=True)
        connection.refresh_from_db()
        self.assertEqual(connection.connection_method, ReaderHardcoverConnection.ConnectionMethod.API_KEY)
        self.assertEqual(decrypt_token(connection.encrypted_token), "working-api-key")
        self.assertContains(response, "previous connection was not changed")
        self.assertNotContains(response, "provider secret detail")
        self.assertNotContains(response, "private-code")

    def test_state_is_one_time_and_bound_to_initiating_reader(self):
        _, query = self.begin()
        state = query["state"][0]
        with patch("core.views.exchange_authorization_code", side_effect=HardcoverOAuthError("stop")):
            self.client.get(self.callback_url, {"code": "code", "state": state, "iss": "https://api.hardcover.app"})
        replay = self.client.get(self.callback_url, {"code": "code", "state": state, "iss": "https://api.hardcover.app"}, follow=True)
        self.assertContains(replay, "could not be verified")

        mismatched_state = "reader-bound-state"
        session = self.client.session
        session[READER_OAUTH_SESSION_KEY] = {
            hashlib.sha256(mismatched_state.encode()).hexdigest(): {
                "user_id": self.other.pk, "created_at": int(timezone.now().timestamp()),
                "code_verifier": "verifier", "redirect_uri": self.urls.redirect_uri,
                "return_to": reverse("account"),
            }
        }
        session.save()
        mismatch = self.client.get(self.callback_url, {"code": "code", "state": mismatched_state, "iss": "https://api.hardcover.app"}, follow=True)
        self.assertContains(mismatch, "could not be verified")

    def test_callback_requires_exact_provider_issuer_before_consuming_state(self):
        _, query = self.begin()
        state = query["state"][0]
        response = self.client.get(
            self.callback_url,
            {"code": "code", "state": state, "iss": "https://malicious.example"},
            follow=True,
        )
        self.assertContains(response, "unexpected issuer")
        state_hash = hashlib.sha256(state.encode("ascii")).hexdigest()
        self.assertIn(state_hash, self.client.session[READER_OAUTH_SESSION_KEY])

    def test_concurrent_flows_are_separate(self):
        _, first = self.begin()
        _, second = self.begin()
        flows = self.client.session[READER_OAUTH_SESSION_KEY]
        self.assertEqual(len(flows), 2)
        self.assertNotEqual(first["state"][0], second["state"][0])

    @patch("core.views.revoke_oauth_token", return_value=True)
    def test_disconnect_revokes_best_effort_then_removes_only_reader_oauth(self, revoke):
        group = ReadingGroup.objects.create(name="Separate", slug="separate-oauth")
        group_connection = HardcoverConnection.objects.create(
            group=group, encrypted_token=encrypt_token("group-token"), is_valid=True,
        )
        connection = ReaderHardcoverConnection.objects.create(
            user=self.reader, connection_method=ReaderHardcoverConnection.ConnectionMethod.OAUTH,
            encrypted_token=encrypt_token("access-token"), encrypted_refresh_token=encrypt_token("refresh-token"), is_valid=True,
        )
        self.client.post(reverse("reader-hardcover-disconnect"))
        self.assertFalse(ReaderHardcoverConnection.objects.filter(pk=connection.pk).exists())
        self.assertTrue(HardcoverConnection.objects.filter(pk=group_connection.pk).exists())
        self.assertEqual(revoke.call_count, 2)

    def test_account_prefers_oauth_and_keeps_api_key_advanced(self):
        response = self.client.get(reverse("account"))
        self.assertContains(response, "Recommended: Connect with Hardcover OAuth")
        self.assertContains(response, "Advanced: scoped API key")
        self.assertNotContains(response, "read:me")
        self.assertContains(response, "does not enable synchronization")


@override_settings(NORTHBOUND_URL="https://northbound.example.com")
class ReaderHardcoverOAuthRefreshTests(TestCase):
    def setUp(self):
        self.reader = get_user_model().objects.create_user("refresh-reader", password="password")
        urls = canonical_oauth_urls()
        self.application = save_oauth_application(
            application=HardcoverOAuthApplication(), client_id="client", client_secret="secret", enabled=True, urls=urls,
        )[0]

    @patch("core.reader_hardcover.exchange_refresh_token")
    def test_expiring_access_refreshes_atomically_and_rotates_refresh_token(self, exchange):
        connection = ReaderHardcoverConnection.objects.create(
            user=self.reader, connection_method=ReaderHardcoverConnection.ConnectionMethod.OAUTH,
            encrypted_token=encrypt_token("old-access"), encrypted_refresh_token=encrypt_token("old-refresh"),
            access_expires_at=timezone.now() + timedelta(seconds=20),
            granted_scopes=["read:catalog", "read:library", "write:library"], is_valid=True,
        )
        exchange.return_value = OAuthTokenSet(
            "new-access", "new-refresh", timezone.now() + timedelta(hours=1),
            ("read:catalog", "read:library", "write:library"),
        )
        credential = get_reader_hardcover_credential(self.reader)
        connection.refresh_from_db()
        self.assertEqual(credential.bearer_token, "new-access")
        self.assertEqual(decrypt_token(connection.encrypted_refresh_token), "new-refresh")
        self.assertTrue(connection.is_valid)
        self.assertTrue(AuditEvent.objects.filter(action="reader_hardcover.refreshed").exists())

    @patch("core.reader_hardcover.exchange_refresh_token", side_effect=HardcoverOAuthError("private", permanent=True))
    def test_permanent_refresh_failure_requires_reconnect_without_exposing_provider_detail(self, exchange):
        connection = ReaderHardcoverConnection.objects.create(
            user=self.reader, connection_method=ReaderHardcoverConnection.ConnectionMethod.OAUTH,
            encrypted_token=encrypt_token("old-access"), encrypted_refresh_token=encrypt_token("old-refresh"),
            access_expires_at=timezone.now(), granted_scopes=["read:catalog", "read:library", "write:library"], is_valid=True,
        )
        with self.assertRaises(ReaderHardcoverUnavailable):
            get_reader_hardcover_credential(self.reader)
        connection.refresh_from_db()
        self.assertFalse(connection.is_valid)
        self.assertTrue(connection.reconnect_required)
        self.assertEqual(connection.encrypted_refresh_token, "")
        self.assertNotIn("private", connection.last_error)

    @patch("core.reader_hardcover.exchange_refresh_token", side_effect=HardcoverOAuthError("timeout", ambiguous=True))
    def test_ambiguous_refresh_outcome_clears_spent_token_and_requires_reconnect(self, exchange):
        connection = ReaderHardcoverConnection.objects.create(
            user=self.reader, connection_method=ReaderHardcoverConnection.ConnectionMethod.OAUTH,
            encrypted_token=encrypt_token("old-access"), encrypted_refresh_token=encrypt_token("possibly-spent-refresh"),
            access_expires_at=timezone.now(), granted_scopes=["read:catalog", "read:library", "write:library"], is_valid=True,
        )
        with self.assertRaises(ReaderHardcoverUnavailable):
            get_reader_hardcover_credential(self.reader)
        connection.refresh_from_db()
        self.assertFalse(connection.is_valid)
        self.assertTrue(connection.reconnect_required)
        self.assertEqual(connection.encrypted_refresh_token, "")
        self.assertEqual(exchange.call_count, 1)
