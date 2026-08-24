from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.demo_data import DEMO_PASSWORD, DemoDataSeeder


class Command(BaseCommand):
    help = "Create Northbound's deterministic development/demo dataset. Refuses to run when DEBUG is disabled."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Remove only the explicitly marked canonical demo dataset, then recreate it.",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Demo data seeding is disabled when DJANGO_DEBUG=0.")

        summary = DemoDataSeeder().seed(reset=options["reset"])
        action = "Created" if summary["created"] else "Verified existing"
        self.stdout.write(self.style.SUCCESS(
            f"{action} canonical Northbound demo data: "
            f"{summary['accounts']} accounts, {summary['groups']} groups, "
            f"{summary['months']} months, {summary['teams']} teams, "
            f"{summary['themes']} themes, {summary['books']} books, "
            f"{summary['submissions']} submissions, and {summary['claims']} theme claims."
        ))
        self.stdout.write(f"Demo account password: {DEMO_PASSWORD}")
        self.stdout.write(
            f"Existing Platform Owners preserved: {summary['platform_owners']}; "
            f"Platform Owner invitations preserved: {summary['platform_owner_invitations']}."
        )
