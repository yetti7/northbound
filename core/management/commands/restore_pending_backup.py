from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from core.backups import apply_pending_restore


class Command(BaseCommand):
    help = "Apply a validated pending Northbound backup before the web server starts."

    def handle(self, *args, **options):
        if connection.vendor != "sqlite":
            return
        connection.close()
        try:
            rollback = apply_pending_restore()
        except Exception as exc:
            raise CommandError(f"Pending restore was not applied: {exc}") from exc
        if rollback:
            self.stdout.write(self.style.SUCCESS(f"Restored pending backup. Previous data: {rollback}"))
