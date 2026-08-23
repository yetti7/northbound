import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse


class MediaServingTests(TestCase):
    def setUp(self):
        self.media_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.media_directory.cleanup)
        self.media_root = Path(self.media_directory.name)
        (self.media_root / "profile-pictures").mkdir()
        (self.media_root / "profile-pictures" / "user-4_fkuYvxY.png").write_bytes(b"northbound-image")
        self.settings_override = override_settings(
            DEBUG=False,
            MEDIA_ROOT=self.media_root,
            NORTHBOUND_SERVE_MEDIA=True,
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def test_serves_media_with_safe_headers(self):
        response = self.client.get("/media/profile-pictures/user-4_fkuYvxY.png")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"northbound-image")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["Cache-Control"], "public, max-age=3600")

    def test_missing_media_returns_404(self):
        self.assertEqual(self.client.get("/media/missing.png").status_code, 404)

    def test_media_can_be_disabled(self):
        with override_settings(NORTHBOUND_SERVE_MEDIA=False):
            self.assertEqual(self.client.get("/media/profile-pictures/user-4_fkuYvxY.png").status_code, 404)

    def test_media_rejects_write_methods(self):
        response = self.client.post("/media/profile-pictures/user-4_fkuYvxY.png")
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.headers["Allow"], "GET, HEAD")

    def test_media_path_cannot_escape_media_root(self):
        response = self.client.get("/media/../manage.py")
        self.assertEqual(response.status_code, 404)


class RequestSizeLimitTests(SimpleTestCase):
    @override_settings(NORTHBOUND_MAX_REQUEST_BYTES=32)
    def test_oversized_request_is_rejected_before_routing(self):
        response = self.client.post("/accounts/login/", {"payload": "x" * 64})

        self.assertEqual(response.status_code, 413)

    def test_invalid_content_length_is_rejected(self):
        response = self.client.generic("POST", "/accounts/login/", CONTENT_LENGTH="invalid")

        self.assertEqual(response.status_code, 400)


class PortableSettingsTests(SimpleTestCase):
    setting_names = {
        "DATABASE_URL",
        "DJANGO_ALLOWED_HOSTS",
        "DJANGO_CSRF_TRUSTED_ORIGINS",
        "DJANGO_DEBUG",
        "DJANGO_SECRET_KEY",
        "NORTHBOUND_TRUST_PROXY_HEADERS",
        "NORTHBOUND_URL",
        "POSTGRES_HOST",
        "POSTGRES_PASSWORD",
    }

    def read_isolated_settings(self, expression, **environment):
        process_environment = os.environ.copy()
        for name in self.setting_names:
            process_environment.pop(name, None)
        process_environment.update(
            {
                "DJANGO_SETTINGS_MODULE": "reading_challenge.settings",
                "DJANGO_DEBUG": "0",
                "DJANGO_SECRET_KEY": "0123456789abcdef" * 4,
                **environment,
            }
        )
        result = subprocess.run(
            [sys.executable, "-c", f"import json; from django.conf import settings; print(json.dumps({expression}))"],
            check=False,
            capture_output=True,
            text=True,
            env=process_environment,
        )
        return result

    def test_media_route_has_stable_name(self):
        self.assertEqual(reverse("northbound-media", kwargs={"path": "avatar.png"}), "/media/avatar.png")

    def test_public_https_url_derives_security_settings(self):
        result = self.read_isolated_settings(
            "[settings.ALLOWED_HOSTS, settings.CSRF_TRUSTED_ORIGINS, settings.SESSION_COOKIE_SECURE, settings.CSRF_COOKIE_SECURE]",
            NORTHBOUND_URL="https://northbound.example.com",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        allowed_hosts, csrf_origins, session_secure, csrf_secure = json.loads(result.stdout)
        self.assertIn("northbound.example.com", allowed_hosts)
        self.assertIn("https://northbound.example.com", csrf_origins)
        self.assertTrue(session_secure)
        self.assertTrue(csrf_secure)

    def test_proxy_headers_are_opt_in(self):
        disabled = self.read_isolated_settings("settings.SECURE_PROXY_SSL_HEADER")
        enabled = self.read_isolated_settings(
            "settings.SECURE_PROXY_SSL_HEADER",
            NORTHBOUND_TRUST_PROXY_HEADERS="1",
        )

        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        self.assertIsNone(json.loads(disabled.stdout))
        self.assertEqual(json.loads(enabled.stdout), ["HTTP_X_FORWARDED_PROTO", "https"])

    def test_invalid_public_url_is_rejected(self):
        result = self.read_isolated_settings("settings.NORTHBOUND_URL", NORTHBOUND_URL="northbound.example.com")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NORTHBOUND_URL must be a complete", result.stderr)

    def test_production_placeholder_secret_is_rejected(self):
        result = self.read_isolated_settings("settings.SECRET_KEY", DJANGO_SECRET_KEY="replace-me")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY must be set", result.stderr)
