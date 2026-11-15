import time
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from core.discord_scheduling import process_due_reports, schedule_diagnostics
from core.models import DiscordReportSchedule


class Command(BaseCommand):
    help = 'Run only Discord scheduled reports; never process Challenge lifecycle transitions.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--dry-run', action='store_true', help='Read-only configuration preview; no claims, heartbeat or delivery.')
        parser.add_argument('--interval', type=int, default=30)

    def handle(self, *args, **options):
        if options['dry_run']:
            for schedule in DiscordReportSchedule.objects.select_related('configuration__group').exclude(frequency='disabled'):
                status=schedule_diagnostics(schedule,group=schedule.configuration.group)
                self.stdout.write(f"Report {schedule.pk} ({schedule.get_destination_display()}): next {status['next_run']}; {status['operational']}")
            self.stdout.write('Read-only preview complete. No claims, heartbeat or messages were created.')
            return
        if not settings.NORTHBOUND_DISCORD_SCHEDULER_ENABLED:
            raise CommandError('Discord scheduling is not activated. Use --dry-run for a read-only preview.')
        interval=max(15,min(60,options['interval']))
        while True:
            try:
                for pk,outcome in process_due_reports(now=timezone.now(),daemon=not options['once']):
                    if outcome not in {'not_due','disabled'}:
                        self.stdout.write(f'Discord report {pk}: {outcome}')
            except Exception:
                self.stderr.write('Discord scheduler check failed; no exception details are logged.')
            if options['once']:return
            time.sleep(interval)
