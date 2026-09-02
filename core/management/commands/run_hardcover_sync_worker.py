import time

from django.core.management import call_command
from django.core.management.base import BaseCommand

from core.hardcover_sync import process_hardcover_sync_batch


class Command(BaseCommand):
    help = "Run Northbound's Reader Hardcover synchronization outbox processor."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--interval", type=int, default=30)
        parser.add_argument("--batch-size", type=int, default=20)

    def handle(self, *args, **options):
        call_command("safeguard_restored_hardcover_sync", stdout=self.stdout)
        interval = max(1, options["interval"])
        while True:
            try:
                processed = process_hardcover_sync_batch(batch_size=options["batch_size"])
                if processed:
                    self.stdout.write(self.style.SUCCESS(f"Processed {len(processed)} Hardcover sync event(s)."))
            except Exception:
                self.stderr.write(self.style.ERROR("Hardcover sync processing failed safely. Review synchronization health; no provider details are logged."))
            if options["once"]:
                return
            time.sleep(interval)
