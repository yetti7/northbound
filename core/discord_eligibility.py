"""Destination consent and internal Challenge identity for Discord transport."""


class ChallengeMessage(dict):
    """Discord JSON stays unchanged; identity is transport-local, never serialized."""
    def __init__(self, value, *, challenge_id, group_id, destination, sensitive_categories=()):
        super().__init__(value)
        self.challenge_id = challenge_id
        self.group_id = group_id
        self.destination = destination
        self.sensitive_categories = frozenset(sensitive_categories)

    @property
    def scope(self):
        return self.group_id, self.challenge_id, self.destination


def scoped(value, month, destination, *, sensitive_categories=()):
    return ChallengeMessage(value, challenge_id=month.pk, group_id=month.group_id, destination=destination, sensitive_categories=sensitive_categories)


def challenge_eligible(month, destination):
    if not month or not month.pk or not month.group.is_active:
        return False
    if destination == 'community':
        return bool(month.discord_community_opt_in and month.status in
                    {'upcoming', 'active', 'finalizing', 'completed', 'archived'})
    if destination == 'moderator':
        return bool(month.discord_moderator_opt_in and month.status in {'active', 'finalizing', 'completed'})
    return False


def payload_eligible(payload, *, legacy=False):
    """Reload consent and destination gates immediately before each external POST."""
    if not isinstance(payload, ChallengeMessage):
        return True  # Only generic connectivity/empty notices have no Challenge.
    from .models import ChallengeMonth, GroupDiscordNotification
    from .discord_manual import _blocking_reason
    from .discord_notifications import delivery_enabled
    month = ChallengeMonth.objects.select_related('group').filter(
        pk=payload.challenge_id, group_id=payload.group_id).first()
    if not challenge_eligible(month, payload.destination):
        return False
    if payload.destination == 'community':
        from .discord_community import participation_visible, results_visible
        if 'participation' in payload.sensitive_categories and not participation_visible(month):return False
        if 'results' in payload.sensitive_categories and not results_visible(month):return False
    config = GroupDiscordNotification.objects.filter(group_id=payload.group_id).first()
    if legacy:
        return bool(config and config.enabled and config.encrypted_webhook and delivery_enabled())
    return not _blocking_reason(config, payload.destination)


def skip_disabled_events(month):
    """Persist opt-out cancellation so later re-enabling cannot revive old events."""
    from django.utils import timezone
    from .models import ChallengeMonth, DiscordEventDelivery
    current=ChallengeMonth.objects.filter(pk=month.pk).values(
        'discord_community_opt_in','discord_moderator_opt_in').first()
    if not current:return
    for destination in ('community','moderator'):
        if not current['discord_'+destination+'_opt_in']:
            DiscordEventDelivery.objects.filter(challenge_id=month.pk,destination=destination,
                state__in=['pending','claimed']).update(state='skipped',
                    failure_category='challenge_opted_out',finished_at=timezone.now())
