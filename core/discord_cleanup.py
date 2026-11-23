"""Known scheduled-report message sets only; no channel discovery or event cleanup."""
import hashlib
import json
import re
import uuid
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from . import discord_notifications as discord
from .discord_manual import _blocking_reason, _webhook
from .models import DiscordReportGeneration, DiscordReportSchedule

MAX_CLEANUP_ATTEMPTS = 5


def fingerprint(configuration, destination):
    _, field = discord.destination_fields(destination)
    return hashlib.sha256(getattr(configuration, field).encode()).hexdigest()


def enabled(configuration, destination):
    return getattr(configuration, destination + '_cleanup') == 'scheduled'


def record_message(schedule, receipt, payload, message_id, identity):
    """Persist each confirmed ID before the next POST; partial runs never supersede."""
    challenge = getattr(payload, 'challenge_id', None)
    stream = 'challenge:' + str(challenge) if challenge is not None else 'group'
    generation, _ = DiscordReportGeneration.objects.get_or_create(
        delivery=receipt, stream_key=stream,
        defaults={'schedule': schedule, 'webhook_fingerprint': identity})
    generation.message_ids = [*generation.message_ids, message_id]
    generation.save(update_fields=['message_ids'])


def finish_generations(receipt, now):
    """Called under the schedule lock, after the entire replacement was confirmed."""
    for generation in DiscordReportGeneration.objects.filter(delivery=receipt):
        old = DiscordReportGeneration.objects.filter(
            schedule_id=generation.schedule_id, notification_type='scheduled_report',
            stream_key=generation.stream_key, webhook_fingerprint=generation.webhook_fingerprint,
            complete=True, superseded=False).exclude(pk=generation.pk)
        old.update(superseded=True, cleanup_state='pending', cleanup_after=now)
        generation.complete = True
        generation.save(update_fields=['complete'])


def _allowed(schedule, identity):
    return (settings.NORTHBOUND_DISCORD_SCHEDULER_ENABLED and schedule.frequency != 'disabled'
            and schedule.configuration.group.is_active
            and enabled(schedule.configuration, schedule.destination)
            and fingerprint(schedule.configuration, schedule.destination) == identity
            and not _blocking_reason(schedule.configuration, schedule.destination)
            and not (schedule.destination == 'moderator' and schedule.configuration.enabled))


@sensitive_variables()
def delete_message(url, message_id):
    """Only this webhook's specific recorded message, never the webhook itself."""
    discord.validate_webhook(url)
    if not isinstance(message_id, str) or not re.fullmatch(r'[0-9]{17,20}', message_id):
        return 'invalid_message', 0
    # Recheck immediately before every DELETE, including continuation pages.
    if not discord.delivery_enabled() or not settings.NORTHBOUND_DISCORD_SCHEDULER_ENABLED:
        return 'paused', 0
    request = Request(url + '/messages/' + message_id, method='DELETE',
                      headers={'User-Agent': 'Northbound scheduled report cleanup'})
    try:
        with discord.build_opener(discord.NoRedirect()).open(request, timeout=10) as response:
            return ('deleted', 0) if response.status == 204 else ('rejected', 0)
    except HTTPError as error:
        if error.code == 404:
            return 'deleted', 0
        if error.code == 429:
            try:
                delay = float(json.loads(error.read(4096)).get('retry_after', 60))
                return 'rate_limited', min(86400, max(1, delay))
            except (ValueError, TypeError, AttributeError):
                return 'rate_limited', 60
        return 'server_error' if error.code >= 500 else 'rejected', 0
    except (TimeoutError, URLError, OSError):
        return 'network_error', 0


@sensitive_variables()
def clean_generation(pk, *, now=None):
    now = now or timezone.now()
    generation = DiscordReportGeneration.objects.select_related('schedule__configuration__group').get(pk=pk)
    if (generation.notification_type != 'scheduled_report' or not generation.complete or not generation.superseded
            or generation.cleanup_state not in {'pending', 'cleaning'}
            or generation.cleanup_attempts >= MAX_CLEANUP_ATTEMPTS
            or generation.cleanup_after and generation.cleanup_after > now):
        return 'not_due'
    schedule = generation.schedule
    if not _allowed(schedule, generation.webhook_fingerprint):
        return 'paused'
    if schedule.cooldown_until and schedule.cooldown_until > now:
        return 'cooldown'
    token = uuid.uuid4()
    # Conditional claim is atomic on SQLite as well as PostgreSQL. DELETE retries
    # are idempotent; a stale claim can recover without repeating a successful POST.
    claimed = DiscordReportGeneration.objects.filter(
        pk=pk, cleanup_state=generation.cleanup_state,
        cleanup_attempts=generation.cleanup_attempts, cleanup_claim=generation.cleanup_claim).update(
            cleanup_state='cleaning', cleanup_claim=token,
            cleanup_attempts=generation.cleanup_attempts + 1,
            cleanup_after=now + timedelta(minutes=5))
    if not claimed:
        return 'busy'
    failure, cooldown = '', 0
    deleted = list(generation.deleted_ids)
    for message_id in generation.message_ids:
        if message_id in deleted:
            continue
        # Re-read destination policy and webhook identity between individual requests.
        current = DiscordReportSchedule.objects.select_related('configuration__group').get(pk=schedule.pk)
        if not _allowed(current, generation.webhook_fingerprint):
            failure = 'paused'
            break
        url, error = _webhook(current.configuration, current.destination)
        if error:
            failure = error
            break
        try:
            status, cooldown = delete_message(url, message_id)
        except Exception:
            status = 'cleanup_error'
        if status != 'deleted':
            failure = status
            break
        deleted.append(message_id)
        if not DiscordReportGeneration.objects.filter(pk=pk, cleanup_claim=token).update(deleted_ids=deleted):
            return 'superseded_claim'
    attempts = generation.cleanup_attempts + 1
    state = 'done' if not failure else 'abandoned' if attempts >= MAX_CLEANUP_ATTEMPTS else 'pending'
    with transaction.atomic():
        DiscordReportGeneration.objects.filter(pk=pk, cleanup_claim=token).update(
            cleanup_state=state, cleanup_claim=None, cleanup_failure=failure,
            cleanup_after=now + timedelta(seconds=cooldown or min(3600, 60 * 2 ** attempts)))
        if cooldown:
            locked = DiscordReportSchedule.objects.select_for_update().get(pk=schedule.pk)
            until = now + timedelta(seconds=cooldown)
            if not locked.cooldown_until or locked.cooldown_until < until:
                locked.cooldown_until = until
                locked.save(update_fields=['cooldown_until'])
    return state


def retry_cleanup(*, now=None):
    if not settings.NORTHBOUND_DISCORD_SCHEDULER_ENABLED or not discord.delivery_enabled():
        return []
    now = now or timezone.now()
    ids = DiscordReportGeneration.objects.filter(cleanup_state__in=['pending', 'cleaning'],
        cleanup_after__lte=now, cleanup_attempts__lt=MAX_CLEANUP_ATTEMPTS).order_by('cleanup_after','pk').values_list('pk',flat=True)[:50]
    results = []
    for pk in ids:
        try:
            results.append((pk, clean_generation(pk, now=now)))
        except Exception:
            results.append((pk, 'cleanup_error'))
    return results
