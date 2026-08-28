import time

from django.core.management.base import BaseCommand

from core.scheduling import process_due_challenge_schedules


class Command(BaseCommand):
    help = "Run Northbound's Challenge schedule-event processor."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--interval", type=int, default=30)

    def handle(self, *args, **options):
        interval = max(1, options["interval"])
        while True:
            try:
                processed = process_due_challenge_schedules()
                for challenge_id, actions in processed:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"Processed Challenge {challenge_id}: {', '.join(actions)}"
                        )
                    )
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f"Challenge schedule processing failed: {exc}"))
            if options["once"]:
                return
            time.sleep(interval)
