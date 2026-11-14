from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.http import HttpResponseForbidden
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods
from django.conf import settings

from .discord_notifications import DiscordNotificationForm, configure_notifications, delivery_enabled
from .models import GroupDiscordNotification, ReadingGroup
from .permissions import can_configure_discord_notifications, can_view_discord_notifications

STATUS_LABELS = {'': 'No delivery attempted', 'empty': 'No outstanding review items',
    'claimed': 'Delivery started; outcome may be unknown', 'sent': 'Delivered',
    'failed': 'Delivery failed; next hourly attempt will use current counts',
    'rate_limited': 'Discord rate limit; waiting before next attempt',
    'configuration_error': 'Check the saved webhook, encryption key, and Northbound public URL'}


@login_required
@require_http_methods(['GET', 'POST'])
@sensitive_post_parameters('webhook_url')
@sensitive_variables()
def discord_notification_settings(request, group_slug):
    group = get_object_or_404(ReadingGroup, slug=group_slug, is_active=True)
    if not can_view_discord_notifications(request.user, group):
        return HttpResponseForbidden('Group notification settings are restricted to Group Owners, Moderators and Platform Administrators.')
    editable = can_configure_discord_notifications(request.user, group)
    if request.method == 'POST' and not editable:
        return HttpResponseForbidden('Only Group Owners and Platform Administrators may change notification settings.')
    config = GroupDiscordNotification.objects.filter(group=group).first()
    form = DiscordNotificationForm(request.POST if request.method == 'POST' else None,
        initial={'enabled': bool(config and config.enabled)}) if editable else None
    if request.method == 'POST' and form.is_valid():
        try:
            configure_notifications(group=group, actor=request.user, enabled=form.cleaned_data['enabled'],
                webhook_url=form.cleaned_data['webhook_url'], remove=form.cleaned_data['remove_webhook'])
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, 'Discord notification settings saved.')
            return redirect('group-discord-notifications', group_slug=group.slug)
    # Only noncredential values are passed to the template; no model or ciphertext.
    status = {'configured': bool(config and config.encrypted_webhook),
        'enabled': bool(config and config.enabled),
        'label': STATUS_LABELS.get(config.last_status if config else '', 'Delivery status unavailable'),
        'last_attempt': config.last_attempt_at if config else None,
        'last_sent': config.last_sent_at if config else None,
        'next_attempt': config.next_attempt_at if config else None}
    return render(request, 'core/group_discord_notifications.html', {'group': group,
        'form': form, 'editable': editable, 'status': status, 'delivery_enabled': delivery_enabled(),
        'public_url_configured': bool(settings.NORTHBOUND_URL)})
