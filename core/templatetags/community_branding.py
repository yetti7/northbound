from django import template
from ..branding import community_branding, can_view_branding_settings, BRANDING_MODE_LABELS
from ..models import ChallengeMonth
register = template.Library()


@register.simple_tag(takes_context=True)
def artwork(context, target):
    viewer = context['request'].user
    month = target if isinstance(target, ChallengeMonth) else None
    return community_branding(viewer, month.group if month else target, month)


@register.simple_tag(takes_context=True)
def branding_settings_visible(context, target):
    return can_view_branding_settings(context['request'].user, target)


@register.filter
def branding_mode_label(value):
    return BRANDING_MODE_LABELS.get(value, value)
