import time

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone

from core.backups import create_automatic_backup
from core.models import PlatformBackupSettings
from core.platform_config import get_platform_timezone


class Command(BaseCommand):
    help = "Run Northbound's automatic SQLite backup scheduler."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        if connection.vendor != "sqlite":
            self.stdout.write("Automatic in-app backups are disabled because this installation uses PostgreSQL.")
            return
        while True:
            try:
                self.run_if_due()
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f"Automatic backup failed: {exc}"))
            if options["once"]:
                return
            time.sleep(30)

    def run_if_due(self):
        local_now = timezone.localtime(timezone=get_platform_timezone())
        with transaction.atomic():
            backup_settings = PlatformBackupSettings.objects.select_for_update().get_or_create(pk=1)[0]
            due = (
                backup_settings.enabled
                and local_now.weekday() in backup_settings.weekdays
                and local_now.time().replace(tzinfo=None) >= backup_settings.backup_time
                and backup_settings.last_run_date != local_now.date()
            )
            if not due:
                return
            backup_settings.last_run_date = local_now.date()
            backup_settings.save(update_fields=["last_run_date"])
        try:
            path = create_automatic_backup(backup_settings.retention_count)
            PlatformBackupSettings.objects.filter(pk=backup_settings.pk).update(last_success_at=timezone.now())
            self.stdout.write(self.style.SUCCESS(f"Created automatic backup: {path}"))
        except Exception as exc:
            PlatformBackupSettings.objects.filter(pk=backup_settings.pk, last_run_date=local_now.date()).update(
                last_run_date=None,
                last_failure_at=timezone.now(),
                last_error=str(exc)[:2000],
            )
            raise
