from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"


    def ready(self):
        from .email_logging import install_reset_log_redaction
        install_reset_log_redaction()
        from django.contrib.auth import get_user_model
        from .identity import public_name
        get_user_model().add_to_class("public_name", property(public_name))
