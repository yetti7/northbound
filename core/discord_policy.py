"""Uncached, fail-closed installation policy for every Discord transport."""
from dataclasses import dataclass
from django.conf import settings
from django.db import DatabaseError

POLICY_BLOCKS = frozenset({'global_disabled', 'global_disabled_gui', 'policy_unavailable', 'development_blocked'})


@dataclass(frozen=True)
class DeliveryPolicy:
    globally_enabled: bool
    source: str
    blocking_reason: str = ''

    @property
    def allowed(self):
        return self.globally_enabled and not self.blocking_reason


def delivery_policy():
    from .models import PlatformSettings
    # No process cache, permissive PlatformSettings.load fallback, or writes here.
    try:
        override = PlatformSettings.objects.filter(pk=1).values_list('discord_notifications_override', flat=True).first()
    except DatabaseError:
        return DeliveryPolicy(False, 'unavailable', 'policy_unavailable')
    if override is not None and not isinstance(override, bool):
        return DeliveryPolicy(False, 'unavailable', 'policy_unavailable')
    inherited = override is None
    enabled = settings.NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED if inherited else override
    source = 'environment' if inherited else 'gui'
    if not enabled:
        return DeliveryPolicy(False, source, 'global_disabled' if inherited else 'global_disabled_gui')
    if (settings.DEBUG or settings.NORTHBOUND_DEVELOPMENT_BANNER) and not settings.NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT:
        return DeliveryPolicy(True, source, 'development_blocked')
    return DeliveryPolicy(True, source)
