"""Private Group digest configuration, queue counts, and bounded hourly delivery."""
import json
import logging
import re
from datetime import timedelta, timezone as datetime_timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .integrations.secrets import encrypt_token, decrypt_token, TokenDecryptionError
from .models import (AuditEvent, BookSubmission, ChallengeMonth, DiscordDigestDelivery,
                     GroupDiscordNotification, ReadingGroup, ThemeClaim)
from .permissions import can_configure_discord_notifications

logger = logging.getLogger(__name__)


@sensitive_variables()
def validate_webhook(value):
    # Exact HTTPS origins and path only: no credentials, arbitrary hosts, ports,
    # queries, fragments, alternate encodings, or redirects to other destinations.
    if not re.fullmatch(r'https://(?:discord\.com|discordapp\.com)/api(?:/v[0-9]+)?/webhooks/[0-9]{17,20}/[A-Za-z0-9._-]{20,200}', value):
        raise ValidationError('Enter a Discord webhook URL copied from Discord channel integrations.')
    return value


class DiscordNotificationForm(forms.Form):
    enabled = forms.BooleanField(required=False, label='Enable hourly review digest')
    webhook_url = forms.CharField(required=False, max_length=2048, strip=True,
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
        label='New Discord webhook URL', help_text='Leave blank to keep the saved webhook. It is never displayed.')
    remove_webhook = forms.BooleanField(required=False, label='Remove saved webhook and disable notifications')

    @sensitive_variables()
    def clean_webhook_url(self):
        value = self.cleaned_data['webhook_url']
        return validate_webhook(value) if value else ''

    @sensitive_variables()
    def clean(self):
        data = super().clean()
        if data.get('remove_webhook') and data.get('webhook_url'):
            raise forms.ValidationError('Choose either replacement or removal.')
        return data


@sensitive_variables()
@transaction.atomic
def configure_notifications(*, group, actor, enabled, webhook_url='', remove=False):
    group = ReadingGroup.objects.select_for_update().get(pk=group.pk, is_active=True)
    actor = get_user_model().objects.get(pk=actor.pk)
    if not can_configure_discord_notifications(actor, group):
        raise PermissionDenied('Group Owner or Platform Administrator authorization is required.')
    if remove and webhook_url:
        raise ValidationError('Choose either replacement or removal.')
    if webhook_url:
        validate_webhook(webhook_url)
    config, _ = GroupDiscordNotification.objects.get_or_create(group=group)
    before = (config.enabled, config.encrypted_webhook)
    if remove:
        config.encrypted_webhook = ''
        enabled = False
    elif webhook_url:
        config.encrypted_webhook = encrypt_token(webhook_url)
    if enabled and not config.encrypted_webhook:
        raise ValidationError('Save a webhook before enabling notifications.')
    config.enabled = enabled
    if before != (config.enabled, config.encrypted_webhook):
        config.save(update_fields=['enabled', 'encrypted_webhook'])
        AuditEvent.objects.create(actor=actor, group=group, action='group.discord_notifications_updated',
            object_type='ReadingGroup', object_id=str(group.pk),
            summary=json.dumps({'enabled': enabled, 'webhook_changed': bool(webhook_url or remove),
                                'webhook_configured': bool(config.encrypted_webhook)}))
    return config


REVIEW_LABELS = (
    ('books', '📚 Book submissions'),
    ('bingo', '🎯 Bingo prompts'),
    ('scavenger', '🔎 Scavenger Hunt Book prompts'),
    ('photos', '📷 Scavenger Hunt photos'),
    ('themes', 'Theme reviews'),
    ('immersive', 'Immersive Reading requests'),
)


def _review_team_attribution(month):
    """Current Team after transfers; retained credited Team for departed Readers.

    No inferred historical assignment: missing/contradictory evidence stays in
    Unassigned / Group Reviews. This does not write assignment or scoring data.
    """
    current = {}
    for participant, team in month.team_assignments.filter(
            ended_at__isnull=True, team__month=month).values_list('participant_id', 'team_id'):
        current.setdefault(participant, set()).add(team)
    teams = dict(month.teams.values_list('pk', 'name'))
    attribution = {}
    for enrollment in month.enrollments.filter(participant__group=month.group):
        candidates = current.get(enrollment.participant_id, set())
        team = None
        if enrollment.is_active and len(candidates) == 1:
            team = next(iter(candidates))
        elif not candidates and (not enrollment.is_active or enrollment.awaiting_scoring_team):
            team = enrollment.credited_team_id
        if team in teams:
            attribution[enrollment.participant_id] = team
    return attribution, teams


def group_pending_review_counts(group):
    """Group-wide private task counts, using the existing review queue candidates.

    Book, Theme and prompt decisions are independent tasks. Resolved claims are
    never counted. Pending Theme decisions on rejected books remain actionable,
    exactly as in the unified queue. No Reader or submission details are returned.
    """
    from .immersive import pending_review_requests
    from .models import PromptClaim
    from .photo_review import pending_photo_review_candidates
    from .prompt_scoring import pending_submission_ids
    rows = []
    for month in ChallengeMonth.objects.filter(group=group,
            status__in=['active', 'finalizing', 'completed']).order_by('pk'):
        attribution, team_names = _review_team_attribution(month)
        buckets = {}

        def add(category, participants):
            for participant in participants:
                team = attribution.get(participant)
                bucket = buckets.setdefault(team, {
                    'team_name': team_names.get(team, 'Unassigned / Group Reviews'),
                    **dict.fromkeys((key for key, _ in REVIEW_LABELS), 0), 'total': 0})
                bucket[category] += 1
                bucket['total'] += 1

        if month.status in {'active', 'finalizing'}:
            submissions = month.submissions.filter(is_removed=False)
            add('books', submissions.filter(status='pending').values_list('participant_id', flat=True))
            add('themes', ThemeClaim.objects.filter(submission__in=submissions, status='pending')
                .values_list('submission__participant_id', flat=True))
            claims = PromptClaim.objects.filter(submission_id__in=pending_submission_ids(month),
                is_current=True, state='reserved', category__enabled=True)
            for kind in ('bingo', 'scavenger'):
                add(kind, claims.filter(category__kind=kind).values_list('participant_id', flat=True))
            add('photos', pending_photo_review_candidates(month).values_list('participant_id', flat=True))
        add('immersive', (participant for _, participant in pending_review_requests(month=month)
            .values_list('pk', 'submission__participant_id')))
        total = sum(bucket['total'] for bucket in buckets.values())
        if total:
            grouped = sorted(buckets.values(), key=lambda row: (row['team_name'] == 'Unassigned / Group Reviews', row['team_name']))
            rows.append({'challenge_id': month.pk, 'challenge_name': month.name,
                         **{key: sum(row[key] for row in grouped) for key, _ in REVIEW_LABELS},
                         'teams': grouped, 'total': total,
                         'review_path': reverse('review-queue', args=[group.slug, month.pk])})
    return {'group_id': group.pk, 'total': sum(row['total'] for row in rows), 'challenges': rows}


def digest_payloads(summary):
    """One hourly digest, split into bounded messages without dropping sections."""
    base = settings.NORTHBOUND_URL.rstrip('/')
    parsed = urlsplit(base)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {'', '/'} or parsed.query or parsed.fragment):
        raise ValidationError('Set the Northbound public URL before sending digests.')

    def name(value):
        return re.sub(r'([\\`*_~|<>\[\]()])', r'\\\1', ' '.join(value.split()))

    blocks = [f"**Northbound — Pending Reviews**\nOutstanding review tasks: {summary['total']}"]
    for row in summary['challenges']:
        link = f"{base}{row['review_path']}"
        for team in row['teams']:
            counts = '\n'.join(f"{label}: {team[key]}" for key, label in REVIEW_LABELS if team[key])
            blocks.append(f"**{name(row['challenge_name'])}**\n**{name(team['team_name'])}**\n{counts}\n{link}")
    blocks.append('Open Northbound to review pending submissions.')
    messages = []
    content = ''
    units = lambda value: len(value.encode('utf-16-le')) // 2
    for block in blocks:
        if units(block) > 1900:
            raise ValidationError('A digest section exceeds Discord limits. Shorten the public URL or display names.')
        combined = content + ('\n\n' if content else '') + block
        if units(combined) > 1900:
            messages.append(content)
            content = block
        else:
            content = combined
    if content: messages.append(content)
    return [{'content': message, 'allowed_mentions': {'parse': [], 'users': [], 'roles': [], 'replied_user': False}}
            for message in messages]


def digest_payload(summary):
    """Compatibility for callers expecting one message; never silently truncate."""
    payloads = digest_payloads(summary)
    if len(payloads) != 1:
        raise ValidationError('This digest requires multiple messages; use digest_payloads.')
    return payloads[0]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@sensitive_variables()
def send_webhook(url, payload):
    """Return only safe status/cooldown values; never return provider messages."""
    try:
        validate_webhook(url)
        request = Request(url, data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json', 'User-Agent': 'Northbound review digest'}, method='POST')
        with build_opener(NoRedirect()).open(request, timeout=10) as response:
            return ('sent', 0) if 200 <= response.status < 300 else ('failed', 0)
    except HTTPError as exc:
        if exc.code == 429:
            try:
                body = json.loads(exc.read(4096))
                delay = float(body.get('retry_after', exc.headers.get('Retry-After', 3600)))
                delay = min(86400, max(3600, delay))
            except (ValueError, TypeError, AttributeError):
                delay = 3600
            return 'rate_limited', delay
        return 'failed', 0
    except (URLError, OSError, ValidationError, ValueError):
        return 'failed', 0


def delivery_enabled():
    return (settings.NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED and
        (not (settings.DEBUG or settings.NORTHBOUND_DEVELOPMENT_BANNER) or settings.NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT))


@sensitive_variables()
def deliver_group_digest(group_id, *, now=None):
    if not delivery_enabled():
        return 'disabled'
    now = now or timezone.now()
    hour = now.astimezone(datetime_timezone.utc).replace(minute=0, second=0, microsecond=0)
    try:
        with transaction.atomic(durable=True):
            config = GroupDiscordNotification.objects.select_for_update().select_related('group').get(group_id=group_id)
            if not config.enabled or not config.encrypted_webhook or not config.group.is_active:
                return 'disabled'
            if config.next_attempt_at and config.next_attempt_at > now:
                return 'not_due'
            if DiscordDigestDelivery.objects.filter(group_id=group_id, hour=hour).exists():
                return 'duplicate'
            summary = group_pending_review_counts(config.group)
            if not summary['total']:
                config.last_status = 'empty'
                config.save(update_fields=['last_status'])
                return 'empty'
            summary['group_slug'] = config.group.slug
            try:
                payloads = digest_payloads(summary)
                url = decrypt_token(config.encrypted_webhook)
                validate_webhook(url)
            except (ValidationError, TokenDecryptionError):
                config.last_status = 'configuration_error'
                config.save(update_fields=['last_status'])
                return 'configuration_error'
            delivery = DiscordDigestDelivery.objects.create(group_id=group_id, hour=hour, attempted_at=now)
            config.last_attempt_at = now
            config.next_attempt_at = now + timedelta(hours=1)
            config.last_status = 'claimed'
            config.save(update_fields=['last_attempt_at', 'next_attempt_at', 'last_status'])
    except IntegrityError:
        return 'duplicate'
    # Persist reservation before external I/O; uncertain failures are never retried
    # inside this hour. A crash here leaves a visible, intentionally uncertain claim.
    try:
        for payload in payloads:
            status, cooldown = send_webhook(url, payload)
            if status != 'sent':
                break
    except Exception:
        status, cooldown = 'failed', 0
    with transaction.atomic():
        DiscordDigestDelivery.objects.filter(pk=delivery.pk).update(status=status)
        config = GroupDiscordNotification.objects.select_for_update().get(pk=config.pk)
        config.last_status = status
        fields = ['last_status']
        if status == 'sent':
            config.last_sent_at = now
            fields.append('last_sent_at')
        if cooldown:
            config.next_attempt_at = max(config.next_attempt_at, now + timedelta(seconds=cooldown))
            fields.append('next_attempt_at')
        config.save(update_fields=fields)
    if status != 'sent':
        logger.warning('Discord digest delivery %s for Group %s', status, group_id)
    return status


def process_due_discord_digests(*, now=None):
    if not delivery_enabled():
        return []
    results = []
    for group_id in GroupDiscordNotification.objects.filter(enabled=True, group__is_active=True).values_list('group_id', flat=True):
        try:
            results.append((group_id, deliver_group_digest(group_id, now=now)))
        except Exception:
            # A failing Group never interrupts other Groups or Challenge scheduling.
            logger.warning('Discord digest processing failed for Group %s', group_id)
            results.append((group_id, 'failed'))
    return results
