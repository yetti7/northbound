"""Preview real service effects on a private SQLite copy; apply only signed plans."""
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from django.apps import apps
from django.contrib.auth import get_user_model
from django.conf import settings
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction

from .models import (
    AuditEvent, BookSubmission, ChallengeMonth, ModifierProvenance,
    PersonalTBRMatch, PersonalTBRCompletionAward, PersonalTBRCompletionAwardBook,
    ProgressCheckpoint, ProgressCheckpointResult,
)
from .permissions import can_reconcile_personal_tbr
from .personal_tbr_matching import synchronize_submission

SALT = 'northbound.tbr-reconciliation.v1'
VOLATILE = {'created_at', 'updated_at', 'decided_at', 'qualified_at', 'last_qualified_at', 'applied_at'}


def _json(value):
    return json.dumps(value, cls=DjangoJSONEncoder, sort_keys=True)


def _authorize(actor, month):
    if not can_reconcile_personal_tbr(actor, month):
        raise PermissionDenied('An active Host, Group Owner or Platform Owner is required.')
    if month.status != ChallengeMonth.Status.ACTIVE:
        raise ValidationError('Reconciliation is restricted to Active Challenges.')


def _fingerprint():
    # Conservative: unrelated core changes also invalidate a preview. Only the
    # digest leaves memory; credentials and registration answers are not output.
    digest = hashlib.sha256()
    models = list(apps.get_app_config('core').get_models()) + [get_user_model()]
    for model in sorted(models, key=lambda model: model._meta.label):
        digest.update(model._meta.label.encode())
        for row in model.objects.order_by('pk').values().iterator():
            digest.update(_json(row).encode())
    for name in ('personal_tbr_matching.py', 'personal_tbr_rewards.py',
                 'personal_tbr_completion.py', 'scoring.py', 'models.py', 'permissions.py', 'tbr_reconciliation.py'):
        digest.update(Path(__file__).with_name(name).read_bytes())
    return digest.hexdigest()


def _candidate_ids(month):
    from io import StringIO
    from django.core.management import call_command
    output = StringIO()
    call_command('report_missing_personal_tbr_matches', group=month.group.slug,
                 month=month.pk, stdout=output)
    return [row['submission_id'] for row in json.loads(output.getvalue())['submissions']]


def _rows(model, queryset):
    return {str(row['id']): {key: value for key, value in row.items() if key not in VOLATILE}
            for row in queryset.order_by('pk').values()}


def _state(month):
    models = (BookSubmission, PersonalTBRMatch, PersonalTBRCompletionAward,
              ModifierProvenance, ProgressCheckpoint)
    state = {model.__name__: _rows(model, model.objects.filter(month=month)) for model in models}
    for model, lookup in ((PersonalTBRCompletionAwardBook, 'award__month'),
                          (ProgressCheckpointResult, 'checkpoint__month')):
        state[model.__name__] = _rows(model, model.objects.filter(**{lookup: month}))
    return state


def _diff(before, after):
    return {name: {key: {'before': rows.get(key), 'after': after[name].get(key)}
                   for key in sorted(set(rows) | set(after[name]))
                   if rows.get(key) != after[name].get(key)}
            for name, rows in before.items()
            if rows != after[name]}


def _repair(month, actor, submission_ids):
    _authorize(actor, month)
    if submission_ids != _candidate_ids(month):
        raise ValidationError('Missing-match candidates changed; create a new preview.')
    before = _state(month)
    frozen_models = (ModifierProvenance, PersonalTBRCompletionAward, PersonalTBRCompletionAwardBook)
    frozen = {model: list(model.objects.order_by('pk').values()) for model in frozen_models}
    for submission in BookSubmission.objects.filter(pk__in=submission_ids).order_by('pk'):
        synchronize_submission(submission)
    after = _state(month)
    if set(after['BookSubmission']) != set(before['BookSubmission']):
        raise ValidationError('Repair must preserve submission IDs.')
    for pk, row in after['ModifierProvenance'].items():
        if pk not in before['ModifierProvenance'] and row['source_type'] not in {
            ModifierProvenance.SourceType.TBR_BOOK, ModifierProvenance.SourceType.TBR_COMPLETION,
        }:
            raise ValidationError('Repair would establish an unrelated bonus; investigate separately.')
    for model, rows in frozen.items():
        current = {row['id']: row for row in model.objects.filter(pk__in=[row['id'] for row in rows]).values()}
        if any(current.get(row['id']) != row for row in rows):
            raise ValidationError('Repair would rewrite an existing award or provenance; investigate separately.')
    # Never rewrite an established award or provenance. An inconsistent starting
    # state is an investigation case, not an invitation to repair more data.
    for name in ('ModifierProvenance', 'PersonalTBRCompletionAward', 'PersonalTBRCompletionAwardBook'):
        for pk, row in before[name].items():
            if after[name].get(pk) != row:
                raise ValidationError('Repair would change an existing award or provenance; investigate separately.')
    for name in ('ProgressCheckpoint', 'ProgressCheckpointResult'):
        if before[name] != after[name]:
            raise ValidationError('Repair would change a historical checkpoint.')
    for pk, row in before['BookSubmission'].items():
        current = after['BookSubmission'][pk]
        if any(current[key] != value for key, value in row.items()
               if key not in {'bonus_pages', 'final_scored_pages'}):
            raise ValidationError('Repair would change submission evidence.')
    return _diff(before, after)


def _simulate(month_id, actor_id):
    """Only called in an isolated subprocess configured to the private copy."""
    with transaction.atomic():
        month = ChallengeMonth.objects.select_related('group').get(pk=month_id)
        actor = get_user_model().objects.get(pk=actor_id)
        _authorize(actor, month)
        fingerprint = _fingerprint()
        ids = _candidate_ids(month)
        submissions = list(BookSubmission.objects.filter(pk__in=ids).select_related('participant').order_by('pk'))
        affected = [{'submission_id': item.pk, 'participant_id': item.participant_id,
                     'reader': item.participant.user.username, 'title': item.title,
                     'completed_on': item.completed_on} for item in submissions]
        reader_ids = sorted({item.participant_id for item in submissions})
        def completed(reader_id):
            return PersonalTBRMatch.objects.filter(month=month, participant_id=reader_id,
                status='confirmed', is_qualifying=True).values('personal_tbr_book_id').distinct().count()
        before_counts = {reader_id: completed(reader_id) for reader_id in reader_ids}
        effects = _repair(month, actor, ids)
        progress = [{'participant_id': reader_id, 'completed_before': before_counts[reader_id],
                     'completed_after': completed(reader_id)} for reader_id in reader_ids]
        return {'month_id': month.pk, 'group': month.group.slug, 'actor_id': actor.pk,
                'fingerprint': fingerprint, 'submission_ids': ids,
                'affected': affected, 'reader_progress': progress, 'effects': effects}


def preview(month, actor):
    _authorize(actor, month)
    if connection.vendor != 'sqlite' or connection.settings_dict.get('OPTIONS', {}).get('transaction_mode', '').upper() != 'IMMEDIATE':
        raise ValidationError('Exact reconciliation requires SQLite IMMEDIATE transactions; Apply is unavailable for this configuration.')
    if connection.in_atomic_block:
        raise ValidationError('Preview must run outside a write transaction; no source writes are simulated.')
    connection.ensure_connection()
    with tempfile.TemporaryDirectory(prefix='northbound-tbr-preview-') as directory:
        database = Path(directory) / 'snapshot.sqlite3'
        # SQLite backup reads a consistent image including WAL contents. Only
        # the private destination is written; normalize its journal mode there.
        target = sqlite3.connect(database)
        try:
            connection.connection.backup(target)
            target.execute('PRAGMA journal_mode=DELETE')
        finally:
            target.close()
        database.chmod(0o600)
        script = '''
import json, sys, os
os.environ['DJANGO_SETTINGS_MODULE'] = 'reading_challenge.settings'
from django.conf import settings
settings.DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': sys.argv[1],
    'OPTIONS': {'transaction_mode': 'IMMEDIATE', 'timeout': 20}}}
import django
django.setup()
from core.tbr_reconciliation import _simulate, _json
try:
    print(_json(_simulate(int(sys.argv[2]), int(sys.argv[3]))))
except Exception:
    sys.exit(2)
'''
        result = subprocess.run([sys.executable, '-c', script, str(database), str(month.pk), str(actor.pk)],
                                capture_output=True, text=True, timeout=45, cwd=settings.BASE_DIR)
        if result.returncode:
            raise ValidationError('Isolated preview failed. No source writes occurred; investigate data before Apply.')
        plan = json.loads(result.stdout)
    return {'exact_scoring_preview': True, 'confirmation_text': f'RECONCILE {month.group.slug} {month.pk}',
            'plan': plan, 'signed_plan': signing.dumps(plan, salt=SALT, compress=True),
            'notes': ['Exact scoring and qualification effects were executed on a private database copy.',
                      'Operational timestamps are assigned on Apply; effective award dates are checked exactly.',
                      'Plan expires in 30 minutes and any core data change invalidates it.',
                      'Existing awards, provenance and checkpoint snapshots may not be rewritten.']}


@transaction.atomic
def apply(month, actor, document, confirmation):
    if connection.vendor != 'sqlite' or connection.settings_dict.get('OPTIONS', {}).get('transaction_mode', '').upper() != 'IMMEDIATE':
        raise ValidationError('Apply requires SQLite IMMEDIATE transactions.')
    month = ChallengeMonth.objects.select_for_update().select_related('group').get(pk=month.pk)
    actor = get_user_model().objects.get(pk=actor.pk)
    _authorize(actor, month)
    try:
        plan = signing.loads(document['signed_plan'], salt=SALT, max_age=1800)
    except (signing.BadSignature, KeyError, TypeError) as exc:
        raise ValidationError('Invalid or expired preview; create a new preview.') from exc
    required = f'RECONCILE {month.group.slug} {month.pk}'
    if document.get('plan') != plan:
        raise ValidationError('Preview display differs from its signed plan; create a new preview.')
    if confirmation != required:
        raise ValidationError(f'Type {required} exactly to confirm.')
    if (plan['month_id'], plan['group'], plan['actor_id']) != (month.pk, month.group.slug, actor.pk):
        raise ValidationError('Preview belongs to another Challenge or administrator.')
    if _fingerprint() != plan['fingerprint']:
        raise ValidationError('Preview is stale or already applied; create a new preview.')
    effects = _repair(month, actor, plan['submission_ids'])
    if json.loads(_json(effects)) != plan['effects']:
        raise ValidationError('Effects differ from preview (including award dates); all changes rolled back.')
    AuditEvent.objects.create(actor=actor, group=month.group, action='personal_tbr.reconciled',
                              object_type='ChallengeMonth', object_id=str(month.pk),
                              summary=_json({'submission_ids': plan['submission_ids'], 'effects': effects}))
    return effects
