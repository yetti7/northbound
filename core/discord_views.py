from .discord_events import event_diagnostics
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.http import HttpResponseForbidden
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods

from .discord_notifications import DiscordNotificationForm, configure_notifications, destination_fields
from .discord_manual import (DESTINATIONS, ACTIONS, FAILURES, destination_diagnostics,
    manual_request_token, read_manual_request, deliver_manual)
from .models import GroupDiscordNotification, ReadingGroup, DiscordReportSchedule, DiscordManualDelivery, DiscordReportDelivery, DiscordDigestDelivery, ChallengeStaffAssignment
from .discord_scheduling import ReportScheduleForm, configure_report_schedule, schedule_diagnostics, COMMUNITY_CATEGORIES
from .permissions import (can_configure_discord_notifications, can_view_discord_notifications,
    visible_challenges_for, can_manage_months, can_operate_challenge, challenge_history_locked)


def community_challenge_rows(actor, group):
    """Read-only projection of visible Challenges; the existing checkbox owns opt-in."""
    from .discord_community import community_eligible
    months=list(visible_challenges_for(actor,group.challenge_months.select_related('group').order_by('pk')))
    manage=can_manage_months(actor,group)
    # Avoid repeated permission queries for every row in a read-only Moderator list.
    hosted=set() if manage else set(ChallengeStaffAssignment.objects.filter(
        month__group=group,membership__user=actor,membership__is_active=True,
        role=ChallengeStaffAssignment.Role.HOST,ended_at__isnull=True).values_list('month_id',flat=True))
    rows=[]
    for month in months:
        eligible=community_eligible(month)
        reason=('Draft' if month.status=='draft' else 'Group inactive' if not group.is_active
            else 'Announcements not enabled' if not month.discord_community_opt_in
            else 'Lifecycle not eligible' if not eligible else '')
        settings_url=''
        if not challenge_history_locked(month) and (manage or (month.pk in hosted and can_operate_challenge(actor,month))):
            settings_url=reverse('challenge-general-settings',args=[group.slug,month.pk])
        rows.append({'name':month.name,'lifecycle':month.get_status_display(),
            'enabled':month.discord_community_opt_in,'eligible':eligible,'reason':reason,'settings_url':settings_url})
    return rows,months


def settings_redirect(group, destination):
    destination = destination if destination in DESTINATIONS else 'community'
    return redirect(reverse('group-discord-notifications', kwargs={'group_slug':group.slug}) + '?destination=' + destination)


def history_rows(group, destination):
    labels={'sent':'Accepted by Discord','failed':'Failed','claimed':'Outcome unknown','pending':'Outcome unknown',
        'empty':'No eligible items','blocked':'Blocked','missed':'Missed slot skipped','rate_limited':'Rate limited',
        'configuration_error':'Check configuration'}
    manual=[{'at':row.attempted_at,'action':row.get_action_display(),'result':labels.get(row.outcome,'Unavailable'),
        'failure':FAILURES.get(row.failure_category,'')}
        for row in DiscordManualDelivery.objects.filter(group=group,destination=destination).order_by('-attempted_at','-pk')[:10]]
    scheduled=[{'at':row.attempted_at,'slot':row.scheduled_for,'result':labels.get(row.outcome,'Unavailable'),
        'failure':FAILURES.get(row.failure_category,'Check configuration' if row.failure_category else '')}
        for row in DiscordReportDelivery.objects.filter(schedule__configuration__group=group,schedule__destination=destination).order_by('-attempted_at','-pk')[:10]]
    legacy=[{'at':row.attempted_at,'result':labels.get(row.status,'Unavailable')}
        for row in DiscordDigestDelivery.objects.filter(group=group).order_by('-attempted_at','-pk')[:10]] if destination=='moderator' else []
    return {'manual':manual,'scheduled':scheduled,'legacy':legacy}


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
    selected = request.POST.get('destination', 'moderator') if request.method == 'POST' else request.GET.get('destination', 'community')
    action = request.POST.get('action', 'save') if request.method == 'POST' else None
    if request.method == 'POST' and (selected not in DESTINATIONS or action not in {'save', 'preferences', *ACTIONS}):
        messages.error(request, 'Choose a valid destination and message action.')
        return settings_redirect(group, selected)
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
        return settings_redirect(group, selected)
    invalid_preferences = None
    if request.method == 'POST' and action == 'preferences':
        invalid_preferences, saved = configure_report_schedule(group=group, actor=request.user,
            destination=selected, data=request.POST)
        if invalid_preferences.is_valid():
            messages.success(request, 'Notification preferences saved. Automatic delivery requires separate global worker activation.')
            return settings_redirect(group, selected)
    challenge_rows,community_months=community_challenge_rows(request.user,group)
    destinations = []
    for destination, label in DESTINATIONS.items():
        enabled_field, _ = destination_fields(destination)
        form = DiscordNotificationForm(request.POST if request.method == 'POST' and action == 'save' and selected == destination else None,
            initial={'enabled': bool(config and getattr(config, enabled_field))}, auto_id=destination+'_%s') if editable else None
        if request.method == 'POST' and action == 'save' and selected == destination and form.is_valid():
            try:
                configure_notifications(group=group, actor=request.user, destination=destination,
                    enabled=form.cleaned_data['enabled'], webhook_url=form.cleaned_data['webhook_url'],
                    remove=form.cleaned_data['remove_webhook'])
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(request, f'{label}: settings saved. No message was sent.')
                return settings_redirect(group, selected)
        report = DiscordReportSchedule.objects.filter(configuration=config,destination=destination).first() if config else None
        report_form = invalid_preferences if selected==destination and invalid_preferences else ReportScheduleForm(
            group=group,destination=destination,schedule=report,legacy_enabled=bool(config and config.enabled))
        destinations.append({'key': destination, 'label': label, 'form': form,
            'status': destination_diagnostics(config, destination, group=group),
            'schedule_form': report_form, 'schedule_status': schedule_diagnostics(report,group=group,community_months=community_months),
            'unavailable_categories': [],
            'history': history_rows(group,destination),
            'events': event_diagnostics(group,destination),
            'active': destination == (selected if selected in DESTINATIONS else 'community'),
            'actions': [{'key': action_key, 'label': action_label, 'token': manual_request_token(
                actor=request.user, group=group, destination=destination, action=action_key)}
                for action_key, action_label in ACTIONS.items()] if editable else []})
    # Never pass models containing encrypted webhooks or decrypted values to templates.
    return render(request, 'core/group_discord_notifications.html',
        {'group': group, 'editable': editable, 'destinations': destinations,'community_challenges':challenge_rows})
