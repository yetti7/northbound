from .platform_config import get_platform_settings


def platform_configuration(request):
    platform_settings = get_platform_settings()
    return {
        "platform_display_name": platform_settings.display_name,
        "public_registration_enabled": platform_settings.allow_public_registration,
        "user_group_creation_enabled": platform_settings.allow_user_group_creation,
    }
