"""Explicit manual Discord delivery; never claims or updates scheduled digests."""
import os
import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from . import discord_notifications as discord
from .integrations.secrets import decrypt_token, TokenDecryptionError
from .models import DiscordManualDelivery, GroupDiscordNotification, ReadingGroup
from .permissions import can_configure_discord_notifications

DESTINATIONS = dict(DiscordManualDelivery.Destination.choices)
ACTIONS = dict(DiscordManualDelivery.Action.choices)
FAILURES = {
    'missing_webhook': 'No webhook configured.',
    'destination_disabled': 'This destination is disabled.',
    'global_disabled': 'Global Discord delivery is disabled.',
    'development_blocked': 'Development delivery is blocked.',
    'unreadable_webhook': 'The saved webhook cannot be read. Ask a Platform Administrator to check the encryption configuration or replace it.',
    'invalid_webhook': 'The saved webhook URL is invalid. Replace it with a URL copied from Discord.',
    'timeout': 'Discord timed out. Delivery is uncertain; check the channel before sending again.',
    'rate_limited': 'Discord rate limited this destination. Wait before sending again.',
    'rejected': 'Discord rejected the request. Check that the webhook still exists and can post in its channel.',
    'network_error': 'Discord could not be reached. Delivery may be uncertain; check the channel before sending again.',
    'delivery_failed': 'Delivery failed. Check the channel before sending again.',
    'snapshot_unavailable': 'The current status could not be prepared. No message was sent.',
}


def manual_request_token(*, actor, group, destination, action):
    return signing.dumps({'actor': actor.pk, 'group': group.pk, 'destination': destination,
        'action': action, 'request_id': str(uuid.uuid4())}, salt='discord-manual')


def read_manual_request(token, *, actor, group, destination, action):
    try:
        data = signing.loads(token, salt='discord-manual', max_age=3600)
        if any(data.get(key) != value for key, value in {'actor': actor.pk, 'group': group.pk,
                'destination': destination, 'action': action}.items()):
            raise ValueError()
        return uuid.UUID(data['request_id'])
    except (signing.BadSignature, ValueError, KeyError, TypeError, AttributeError):
        raise ValidationError('This message request expired or is invalid. Reload the page and use its message button.') from None


@sensitive_variables()
def _webhook(config, destination):
    _, field = discord.destination_fields(destination)
    try:
        url = decrypt_token(getattr(config, field))
    except TokenDecryptionError:
        return '', 'unreadable_webhook'
    try:
        discord.validate_webhook(url)
    except ValidationError:
        return '', 'invalid_webhook'
    return url, ''


def _blocking_reason(config, destination):
    enabled, webhook = discord.destination_fields(destination)
    if not config or not getattr(config, webhook):
        return 'missing_webhook'
    if not getattr(config, enabled):
        return 'destination_disabled'
    if not settings.NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED:
        return 'global_disabled'
    if (settings.DEBUG or settings.NORTHBOUND_DEVELOPMENT_BANNER) and not settings.NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT:
        return 'development_blocked'
    return ''


@sensitive_variables()
def destination_diagnostics(config, destination, *, group):
    enabled, webhook = discord.destination_fields(destination)
    notices = []
    configured = bool(config and getattr(config, webhook))
    active = bool(config and getattr(config, enabled))
    if not configured: notices.append(FAILURES['missing_webhook'])
    if not active: notices.append(FAILURES['destination_disabled'])
    if not settings.NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED: notices.append(FAILURES['global_disabled'])
    if (settings.DEBUG or settings.NORTHBOUND_DEVELOPMENT_BANNER) and not settings.NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT:
        notices.append(FAILURES['development_blocked'])
    if configured:
        _, failure = _webhook(config, destination)
        if failure: notices.append(FAILURES[failure])
    cooling = DiscordManualDelivery.objects.filter(group=group, destination=destination, retry_at__gt=timezone.now()).exists()
    if cooling: notices.append(FAILURES['rate_limited'])
    if not notices: notices.append('Ready for manual testing. A saved URL is not proof of successful Discord delivery.')
    latest = DiscordManualDelivery.objects.filter(group=group, destination=destination).first()
    last_result = 'No manual delivery attempted.'
    if latest:
        last_result = {'sent': 'Last manual delivery succeeded.', 'failed': 'Last manual delivery failed.',
            'pending': 'Last manual delivery outcome is unknown. Check the channel before trying again.'}.get(latest.outcome, 'Manual outcome unavailable.')
        if latest.outcome == 'failed': last_result += ' ' + FAILURES.get(latest.failure_category, FAILURES['delivery_failed'])
    scheduler = ('Scheduler disabled; manual messages do not require it.'
        if os.getenv('NORTHBOUND_CHALLENGE_SCHEDULER_ENABLED', '1').strip().lower() not in {'1','true','yes','on'}
        else 'Scheduler availability is not verified here; manual messages do not require it.')
    return {'configured': configured, 'enabled': active, 'notices': notices, 'last_result': last_result,
        'last_attempt': latest.attempted_at if latest else None,
        'last_action': latest.get_action_display() if latest else '', 'scheduler': scheduler,
        'legacy_digest_enabled': bool(config and config.enabled) if destination == 'moderator' else False}


def _payload(group, destination, action):
    # Neutralize formatting and mentions without exposing names or details of Readers.
    name = re.sub(r'([\\`*_~|<>\[\]()])', r'\\\1', ' '.join(group.name.split()))
    if action == 'test':
        content = f'Northbound test message for {name} — {DESTINATIONS[destination]}. This is a manual webhook test.'
    elif destination == 'community':
        # No public audience policy exists for Group/Challenge data. Do not treat
        # an Owner's private visibility as permission to publish it to a channel.
        content = (f'Northbound — Community Status for {name}\nCommunity messaging: enabled for manual requests.\n'
            'Automatic community announcements: off.\nChallenge details, participation and results are not included in this snapshot.')
    else:
        summary = discord.group_pending_review_counts(group)
        counts = {key: sum(row[key] for row in summary['challenges']) for key, _ in discord.REVIEW_LABELS}
        content = (f'Northbound — Moderator Status for {name}\nOutstanding review tasks: {summary["total"]}\n'
            + '\n'.join(f'{label}: {counts[key]}' for key, label in discord.REVIEW_LABELS)
            + '\nThese are review tasks, not unique book submissions; a book can need more than one review.')
    return {'content': content, 'allowed_mentions': {'parse': [], 'users': [], 'roles': [], 'replied_user': False}}


@sensitive_variables()
def deliver_manual(*, group, actor, destination, action, request_id):
    if destination not in DESTINATIONS or action not in ACTIONS:
        raise ValidationError('Choose a valid destination and message action.')
    request_id = uuid.UUID(str(request_id))
    # Persist a distinct claim before external I/O. Refreshes and concurrent reuse
    # never resend an uncertain request. Never touch hourly claims or timestamps.
    with transaction.atomic():
        group = ReadingGroup.objects.select_for_update().filter(pk=group.pk, is_active=True).first()
        if group is None:
            raise PermissionDenied('This Group is unavailable for Discord messages.')
        actor = get_user_model().objects.get(pk=actor.pk)
        if not can_configure_discord_notifications(actor, group):
            raise PermissionDenied('Group Owner or Platform Administrator authorization is required.')
        existing = DiscordManualDelivery.objects.filter(request_id=request_id).first()
        if existing:
            if (existing.group_id, existing.destination, existing.action) != (group.pk, destination, action):
                raise ValidationError('This message request is invalid.')
            return existing, False
        try:
            with transaction.atomic():
                attempt = DiscordManualDelivery.objects.create(group=group, destination=destination, action=action, request_id=request_id)
        except IntegrityError:
            return DiscordManualDelivery.objects.get(request_id=request_id), False
        config = GroupDiscordNotification.objects.filter(group=group).first()
        failure = _blocking_reason(config, destination)
        if not failure and DiscordManualDelivery.objects.filter(group=group, destination=destination, retry_at__gt=timezone.now()).exists():
            failure = 'rate_limited'
        url = ''
        if not failure: url, failure = _webhook(config, destination)
    if not failure:
        try:
            payload = _payload(group, destination, action)
        except Exception:
            failure = 'snapshot_unavailable'
        else:
            try:
                status, cooldown = discord.send_webhook(url, payload, detailed=True)
                if status != 'sent':
                    failure = status if status in FAILURES else 'delivery_failed'
                if status == 'rate_limited' and cooldown:
                    attempt.retry_at = timezone.now() + timedelta(seconds=min(86400, max(1, float(cooldown))))
            except Exception:
                failure = 'delivery_failed'
    attempt.outcome = 'failed' if failure else 'sent'
    attempt.failure_category = failure
    attempt.save(update_fields=['outcome', 'failure_category', 'retry_at'])
    return attempt, True
