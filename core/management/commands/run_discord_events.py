import time
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from core.discord_events import process_events
from core.models import DiscordEventDelivery


class Command(BaseCommand):
    help = 'Process the Stage C outbox only; never scan history or run lifecycle/scheduled reports.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--interval', type=int, default=30)

    def handle(self,*args,**options):
        if options['dry_run']:
            self.stdout.write(f"Pending event records: {DiscordEventDelivery.objects.filter(state='pending').count()}. No claims, heartbeat or messages created.")
            return
        if not settings.NORTHBOUND_DISCORD_EVENTS_ENABLED:
            raise CommandError('Immediate event delivery is not activated. Use --dry-run for read-only diagnostics.')
        while True:
            for pk,outcome in process_events(daemon=not options['once']):
                self.stdout.write(f'Discord event {pk}: {outcome}')
            if options['once']:return
            time.sleep(max(15,min(60,options['interval'])))
