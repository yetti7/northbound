from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import AuditEvent, ReadingGroup


class HealthEndpointTests(TestCase):
    def test_anonymous_health_is_minimal_non_redirecting_and_read_only(self):
        before = {
            "accounts": get_user_model().objects.count(),
            "groups": ReadingGroup.objects.count(),
            "audits": AuditEvent.objects.count(),
        }

        response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/plain")
        self.assertEqual(response.content, b"OK")
        self.assertNotIn("Location", response)
        self.assertEqual(before, {
            "accounts": get_user_model().objects.count(),
            "groups": ReadingGroup.objects.count(),
            "audits": AuditEvent.objects.count(),
        })

    def test_reader_receives_the_same_non_privileged_response(self):
        reader = get_user_model().objects.create_user("health-reader", password="password")
        self.client.force_login(reader)

        response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"OK")
        for sensitive_value in ("health-reader", "NORTHBOUND_URL", "DATABASE_URL", "restore_pending"):
            self.assertNotContains(response, sensitive_value)

    @override_settings(ALLOWED_HOSTS=["northbound.example.com"])
    def test_internal_loopback_health_request_does_not_weaken_host_configuration(self):
        response = self.client.get(
            reverse("health"), HTTP_HOST="127.0.0.1:8000", REMOTE_ADDR="127.0.0.1"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"OK")

        non_health = self.client.get(
            reverse("login"), HTTP_HOST="127.0.0.1:8000", REMOTE_ADDR="127.0.0.1"
        )
        self.assertEqual(non_health.status_code, 400)

        non_loopback_health = self.client.get(
            reverse("health"), HTTP_HOST="127.0.0.1:8000", REMOTE_ADDR="172.18.0.2"
        )
        self.assertEqual(non_loopback_health.status_code, 400)

    def test_database_failure_is_generic_and_safe(self):
        with patch("core.health.connection.cursor", side_effect=DatabaseError("private database detail")):
            response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.content, b"UNAVAILABLE")
        self.assertNotContains(response, "private database detail", status_code=503)

    def test_login_behavior_remains_unchanged(self):
        get_user_model().objects.create_user("login-reader", password="password")
        response = self.client.get(reverse("login"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sign In")

    def test_health_rejects_mutating_methods(self):
        response = self.client.post(reverse("health"))

        self.assertEqual(response.status_code, 405)
