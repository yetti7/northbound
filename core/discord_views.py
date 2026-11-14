from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.http import HttpResponseForbidden
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods

from .discord_notifications import DiscordNotificationForm, configure_notifications, destination_fields
from .discord_manual import (DESTINATIONS, ACTIONS, FAILURES, destination_diagnostics,
    manual_request_token, read_manual_request, deliver_manual)
from .models import GroupDiscordNotification, ReadingGroup
from .permissions import can_configure_discord_notifications, can_view_discord_notifications


@login_required
@never_cache
@csrf_protect
@require_http_methods(['GET', 'POST'])
@sensitive_post_parameters('webhook_url')
@sensitive_variables()
def discord_notification_settings(request, group_slug):
    group = get_object_or_404(ReadingGroup, slug=group_slug, is_active=True)
    if not can_view_discord_notifications(request.user, group):
        return HttpResponseForbidden('Group notification settings are restricted to Group Owners, Moderators and Platform Administrators.')
    editable = can_configure_discord_notifications(request.user, group)
    if request.method == 'POST' and not editable:
        return HttpResponseForbidden('Only Group Owners and Platform Administrators may configure or send Discord messages.')
    config = GroupDiscordNotification.objects.filter(group=group).first()
    selected = request.POST.get('destination', 'moderator') if request.method == 'POST' else None
    action = request.POST.get('action', 'save') if request.method == 'POST' else None
    if request.method == 'POST' and (selected not in DESTINATIONS or action not in {'save', *ACTIONS}):
        messages.error(request, 'Choose a valid destination and message action.')
        return redirect('group-discord-notifications', group_slug=group.slug)
    if request.method == 'POST' and action in ACTIONS:
        try:
            request_id = read_manual_request(request.POST.get('request_token', ''), actor=request.user,
                group=group, destination=selected, action=action)
            attempt, fresh = deliver_manual(group=group, actor=request.user, destination=selected,
                action=action, request_id=request_id)
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
        else:
            if not fresh:
                messages.info(request, 'This message request was already recorded and was not sent again. Check the latest delivery result.')
            elif attempt.outcome == 'sent':
                messages.success(request, f'{DESTINATIONS[selected]}: message delivered to Discord.')
            else:
                messages.error(request, f'{DESTINATIONS[selected]}: ' + FAILURES.get(attempt.failure_category, FAILURES['delivery_failed']))
        return redirect('group-discord-notifications', group_slug=group.slug)
    destinations = []
    for destination, label in DESTINATIONS.items():
        enabled_field, _ = destination_fields(destination)
        form = DiscordNotificationForm(request.POST if request.method == 'POST' and selected == destination else None,
            initial={'enabled': bool(config and getattr(config, enabled_field))}, auto_id=destination+'_%s') if editable else None
        if request.method == 'POST' and selected == destination and form.is_valid():
            try:
                configure_notifications(group=group, actor=request.user, destination=destination,
                    enabled=form.cleaned_data['enabled'], webhook_url=form.cleaned_data['webhook_url'],
                    remove=form.cleaned_data['remove_webhook'])
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(request, f'{label}: settings saved. No message was sent.')
                return redirect('group-discord-notifications', group_slug=group.slug)
        destinations.append({'key': destination, 'label': label, 'form': form,
            'status': destination_diagnostics(config, destination, group=group),
            'actions': [{'key': action_key, 'label': action_label, 'token': manual_request_token(
                actor=request.user, group=group, destination=destination, action=action_key)}
                for action_key, action_label in ACTIONS.items()] if editable else []})
    # Never pass models containing encrypted webhooks or decrypted values to templates.
    return render(request, 'core/group_discord_notifications.html',
        {'group': group, 'editable': editable, 'destinations': destinations})
