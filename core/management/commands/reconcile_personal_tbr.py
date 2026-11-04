import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from core.models import ChallengeMonth
from core.tbr_reconciliation import apply, preview


class Command(BaseCommand):
    help = 'Preview missing TBR matches on a private SQLite copy; apply only an explicitly confirmed signed preview.'

    def add_arguments(self, parser):
        parser.add_argument('--group', required=True)
        parser.add_argument('--month', required=True, type=int)
        parser.add_argument('--actor', required=True, help='Username of the authorized administrator')
        parser.add_argument('--output', help='New private JSON preview file')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--preview-file')
        parser.add_argument('--confirm')

    def handle(self, *args, **options):
        try:
            month = ChallengeMonth.objects.select_related('group').get(pk=options['month'], group__slug=options['group'])
            actor = get_user_model().objects.get(username=options['actor'])
            if options['apply']:
                if not options['preview_file'] or not options['confirm'] or options['output']:
                    raise CommandError('Apply requires --preview-file and --confirm, without --output.')
                # Check authority before reading preview contents.
                from core.tbr_reconciliation import _authorize
                _authorize(actor, month)
                document = json.loads(Path(options['preview_file']).read_text())
                result = {'applied': True, 'effects': apply(month, actor, document, options['confirm'])}
            else:
                if options['preview_file'] or options['confirm']:
                    raise CommandError('--preview-file and --confirm require --apply.')
                result = preview(month, actor)
            text = json.dumps(result, indent=2, cls=DjangoJSONEncoder)
            if options['output']:
                import os
                descriptor = os.open(options['output'], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'w') as stream:
                    stream.write(text + '\n')
                self.stdout.write('Read-only preview saved. Review its effects before explicit confirmation.')
            else:
                self.stdout.write(text)
        except (ChallengeMonth.DoesNotExist, get_user_model().DoesNotExist):
            raise CommandError('Challenge or administrator not found.') from None
        except DatabaseError:
            raise CommandError('Database contention or repair failure; the transaction was rolled back. Create a fresh preview before retrying.') from None
        except (PermissionDenied, ValidationError, OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
