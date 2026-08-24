from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import DatabaseError

from .models import PlatformSettings


def get_platform_settings():
    try:
        return PlatformSettings.load()
    except DatabaseError:
        return PlatformSettings(
            display_name="My Northbound",
            timezone=settings.TIME_ZONE,
            allow_public_registration=True,
            allow_user_group_creation=True,
        )


def get_platform_timezone():
    timezone_name = get_platform_settings().timezone
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(settings.TIME_ZONE)
