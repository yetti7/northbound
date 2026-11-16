from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"


    def ready(self):
        from .email_logging import install_reset_log_redaction
        install_reset_log_redaction()
