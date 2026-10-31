from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from .hardcover_oauth import (
    OAUTH_SCOPES,
    canonical_oauth_urls,
    oauth_application_status,
    save_oauth_application,
)
from .integrations.credentials import CredentialMethod, CredentialOwner
from .integrations.secrets import decrypt_token, encrypt_token
from .models import AuditEvent, HardcoverOAuthApplication, ReaderHardcoverConnection
from .reader_hardcover import get_reader_hardcover_credential, save_reader_hardcover_connection


class HardcoverOAuthUrlTests(TestCase):
    def test_canonical_urls_normalize_trailing_slash(self):
        urls = canonical_oauth_urls("https://northbound.example.com/")
        self.assertEqual(urls.website_url, "https://northbound.example.com")
        self.assertEqual(urls.redirect_uri, "https://northbound.example.com/account/hardcover/oauth/callback/")
        self.assertEqual(urls.error, "")

    def test_port_and_loopback_are_preserved_without_claiming_provider_support(self):
        urls = canonical_oauth_urls("http://localhost:8000/")
        self.assertEqual(urls.website_url, "http://localhost:8000")
        self.assertEqual(urls.redirect_uri, "http://localhost:8000/account/hardcover/oauth/callback/")

    def test_missing_and_malformed_urls_fail_without_host_header_fallback(self):
        self.assertIn("Set NORTHBOUND_URL", canonical_oauth_urls("").error)
        for value in ("northbound.example.com", "https://user:pass@example.com", "https://example.com/path"):
            with self.subTest(value=value):
                self.assertTrue(canonical_oauth_urls(value).error)


class HardcoverOAuthApplicationModelTests(TestCase):
    def setUp(self):
        self.urls = canonical_oauth_urls("https://northbound.example.com")

    def save_application(self, secret="local-client-secret", enabled=True):
        return save_oauth_application(
            application=HardcoverOAuthApplication(),
            client_id="northbound-client",
            client_secret=secret,
            enabled=enabled,
            urls=self.urls,
        )[0]

    def test_singleton_is_enforced_even_if_key_is_overridden(self):
        self.save_application()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                HardcoverOAuthApplication.objects.create(singleton_key=True)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                HardcoverOAuthApplication.objects.create(singleton_key=False)

    def test_secret_round_trip_is_encrypted_and_model_is_not_in_admin(self):
        secret = "plaintext-must-not-be-stored"
        application = self.save_application(secret=secret)
        self.assertNotIn(secret, application.encrypted_client_secret)
        self.assertEqual(decrypt_token(application.encrypted_client_secret), secret)
        self.assertNotIn(HardcoverOAuthApplication, admin.site._registry)

    def test_replacement_changes_ciphertext_and_blank_preserves_it(self):
        application = self.save_application(secret="first-secret")
        original = application.encrypted_client_secret
        application, replaced = save_oauth_application(
            application=application, client_id="northbound-client", client_secret="",
            enabled=True, urls=self.urls,
        )
        self.assertFalse(replaced)
        self.assertEqual(application.encrypted_client_secret, original)
        application, replaced = save_oauth_application(
            application=application, client_id="northbound-client", client_secret="second-secret",
            enabled=True, urls=self.urls,
        )
        self.assertTrue(replaced)
        self.assertNotEqual(application.encrypted_client_secret, original)
        self.assertEqual(decrypt_token(application.encrypted_client_secret), "second-secret")

    def test_status_covers_configured_disabled_stale_and_decryption_failure(self):
        application = self.save_application()
        self.assertEqual(oauth_application_status(application, self.urls)["key"], "configured")
        application.enabled = False
        application.save(update_fields=["enabled"])
        self.assertEqual(oauth_application_status(application, self.urls)["key"], "disabled")
        stale = canonical_oauth_urls("https://new.example.com")
        self.assertEqual(oauth_application_status(application, stale)["key"], "needs_attention")
        application.encrypted_client_secret = "broken-ciphertext"
        application.save(update_fields=["encrypted_client_secret"])
        status = oauth_application_status(application, self.urls)
        self.assertEqual(status["key"], "needs_attention")
        self.assertTrue(any("cannot be decrypted" in reason for reason in status["reasons"]))


@override_settings(NORTHBOUND_URL="https://northbound.example.com")
class HardcoverOAuthPlatformViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_superuser("platform-owner", "owner@example.com", "password")
        self.reader = User.objects.create_user("ordinary-reader", password="password")
        self.url = reverse("platform-hardcover-oauth")

    def test_platform_owner_only_authority(self):
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_setup_guidance_urls_scopes_device_grant_and_icon(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.url, HTTP_HOST="127.0.0.1:8000")
        self.assertContains(response, 'class="page-heading page-header hc-oauth-page-header"')
        self.assertContains(response, 'class="form-card hc-oauth-setup-card"')
        self.assertContains(response, "Web app (server-side)")
        self.assertContains(response, "Device Authorization Grant")
        self.assertContains(response, "<strong>OFF</strong>", html=True)
        for scope in OAUTH_SCOPES:
            self.assertContains(response, scope)
        self.assertContains(response, "configured public <code>NORTHBOUND_URL</code>")
        self.assertContains(response, "Exact Website URL to paste into Hardcover")
        self.assertContains(response, "https://northbound.example.com")
        self.assertContains(response, "https://northbound.example.com/account/hardcover/oauth/callback/")
        self.assertNotContains(response, "http://127.0.0.1:8000/account/hardcover/oauth/callback/")
        self.assertContains(response, "branding/northbound-icon-512.png")

    @override_settings(NORTHBOUND_URL="https://northbound.example.com:8443")
    def test_setup_guidance_preserves_configured_public_port(self):
        self.client.force_login(self.owner)
        response = self.client.get(self.url, HTTP_HOST="127.0.0.1:8000")
        self.assertContains(response, 'value="https://northbound.example.com:8443"')
        self.assertContains(
            response,
            'value="https://northbound.example.com:8443/account/hardcover/oauth/callback/"',
        )

    def test_initial_save_reload_blank_edit_replace_disable_and_audit_are_secret_safe(self):
        self.client.force_login(self.owner)
        first_secret = "first-client-secret-value"
        response = self.client.post(self.url, {
            "enabled": "on", "client_id": "client-id", "client_secret": first_secret,
        })
        self.assertRedirects(response, self.url)
        application = HardcoverOAuthApplication.objects.get()
        original = application.encrypted_client_secret
        self.assertEqual(decrypt_token(original), first_secret)
        page = self.client.get(self.url)
        self.assertContains(page, "Configured and encrypted")
        self.assertNotContains(page, first_secret)
        self.assertNotContains(page, original)

        self.assertRedirects(self.client.post(self.url, {
            "enabled": "on", "client_id": "edited-client", "client_secret": "",
        }), self.url)
        application.refresh_from_db()
        self.assertEqual(application.encrypted_client_secret, original)

        second_secret = "replacement-client-secret"
        self.assertRedirects(self.client.post(self.url, {
            "enabled": "on", "client_id": "edited-client", "client_secret": second_secret,
        }), self.url)
        application.refresh_from_db()
        self.assertEqual(decrypt_token(application.encrypted_client_secret), second_secret)

        self.assertRedirects(self.client.post(self.url, {
            "client_id": "edited-client", "client_secret": "",
        }), self.url)
        application.refresh_from_db()
        self.assertFalse(application.enabled)
        self.assertEqual(decrypt_token(application.encrypted_client_secret), second_secret)
        summaries = " ".join(AuditEvent.objects.values_list("summary", flat=True))
        self.assertNotIn(first_secret, summaries)
        self.assertNotIn(second_secret, summaries)

    def test_secret_post_is_sensitive_and_not_redisplayed_on_error(self):
        self.client.force_login(self.owner)
        secret = "sensitive-client-secret"
        with override_settings(NORTHBOUND_URL=""):
            response = self.client.post(self.url, {
                "enabled": "on", "client_id": "client-id", "client_secret": secret,
            })
        self.assertEqual(response.wsgi_request.sensitive_post_parameters, ("client_secret",))
        self.assertNotContains(response, secret)
        self.assertFalse(HardcoverOAuthApplication.objects.exists())


class HardcoverCredentialCompatibilityTests(TestCase):
    def setUp(self):
        self.reader = get_user_model().objects.create_user("credential-reader", password="password")

    def test_existing_reader_method_defaults_to_api_key_and_helper_is_owner_aware(self):
        connection = ReaderHardcoverConnection.objects.create(
            user=self.reader, encrypted_token=encrypt_token("reader-token"), is_valid=True,
        )
        self.assertEqual(connection.connection_method, ReaderHardcoverConnection.ConnectionMethod.API_KEY)
        credential = get_reader_hardcover_credential(self.reader)
        self.assertEqual(credential.bearer_token, "reader-token")
        self.assertEqual(credential.method, CredentialMethod.API_KEY)
        self.assertEqual(credential.owner, CredentialOwner.READER)

    @patch("core.reader_hardcover.test_catalog_connection", return_value=True)
    def test_api_key_replacement_explicitly_preserves_api_key_method(self, test_connection):
        ReaderHardcoverConnection.objects.create(
            user=self.reader,
            connection_method=ReaderHardcoverConnection.ConnectionMethod.OAUTH,
            encrypted_token=encrypt_token("future-placeholder"),
            is_valid=True,
        )
        save_reader_hardcover_connection(self.reader, "replacement-api-key")
        connection = ReaderHardcoverConnection.objects.get(user=self.reader)
        self.assertEqual(connection.connection_method, ReaderHardcoverConnection.ConnectionMethod.API_KEY)
        self.assertEqual(decrypt_token(connection.encrypted_token), "replacement-api-key")
