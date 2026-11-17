"""Opt-in fixtures for legacy feature tests unrelated to identity onboarding.

Explicit confirmation/authentication tests continue using Django's TestCase.
No middleware, authentication backend, or production validation is patched.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from .models import UserProfile


class ConfirmedIdentityTestCase(TestCase):
    def _callSetUp(self):
        super()._callSetUp()
        for user in get_user_model().objects.all():
            if not user.email:
                user.email = f"fixture-{user.pk}@example.invalid"
                user.save(update_fields=["email"])
            profile, _ = UserProfile.objects.get_or_create(user=user)
            if not profile.discord_username:
                profile.discord_username = user.username
            profile.display_name_confirmed_at = timezone.now()
            profile.save(update_fields=["discord_username", "display_name_confirmed_at"])
