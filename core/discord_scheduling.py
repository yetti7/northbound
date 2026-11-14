"""Independent opt-in report scheduling; no lifecycle processing or event hooks."""
import re
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone as utc_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction, IntegrityError
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from . import discord_notifications as discord
from .discord_manual import DESTINATIONS, FAILURES, _blocking_reason, _webhook
from .models import (ReadingGroup, GroupDiscordNotification, DiscordReportSchedule,
    DiscordReportDelivery, DiscordSchedulerHeartbeat, DiscordManualDelivery, AuditEvent)
from .permissions import can_configure_discord_notifications

UTC = utc_timezone.utc
INTERVALS = {'30m': 1800, '1h': 3600, '6h': 21600, '12h': 43200}
WEEKDAYS = [(str(i), value) for i, value in enumerate(('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'))]
MODERATOR_CATEGORIES = [(key, label) for key, label in discord.REVIEW_LABELS]
COMMUNITY_CATEGORIES = [('active','Active Challenges'),('upcoming','Upcoming Challenges'),
    ('registration','Registration availability'),('dates','Challenge dates'),
    ('participation','Participation summaries'),('results','Published results and standings')]
IMMEDIATE_CATEGORIES = {
    'moderator': [('new_submission','New submissions'),('review_required','Submissions requiring review'),
        ('approved','Submission approval'),('rejected','Submission rejection')],
    'community': [('registration_open','Registration opening'),('registration_close','Registration closing'),
        ('challenge_start','Challenge starting'),('challenge_close','Challenge closing'),
        ('challenge_complete','Challenge completion'),('results','Published results')],
}
GRACE_SECONDS = 90


def valid_zone(value):
    try:
        if not value or value not in available_timezones():
            raise ValueError()
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValidationError('Choose a valid IANA Group timezone in Group Settings.') from None


class ReportScheduleForm(forms.Form):
    scheduled_enabled = forms.BooleanField(required=False, label='Enable scheduled reports')
    frequency = forms.ChoiceField(required=False, choices=DiscordReportSchedule.Frequency.choices)
    categories = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple, label='Scheduled report categories')
    times = forms.CharField(required=False, max_length=1024, label='Local delivery times',
        help_text='24-hour HH:MM, separated by commas or new lines. Daily/Weekly use one time; Custom Times can use several.',
        widget=forms.Textarea(attrs={'rows':2,'placeholder':'08:00, 17:00'}))
    weekdays = forms.MultipleChoiceField(required=False, choices=WEEKDAYS, widget=forms.CheckboxSelectMultiple,
        label='Delivery days (local timezone)')
    send_empty = forms.BooleanField(required=False, label='Send scheduled reports when there is nothing new to report')
    immediate_categories = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple,
        label='Planned immediate categories — Coming in Stage C')
    retire_legacy = forms.BooleanField(required=False, label='Turn off the existing hourly digest (keep its history)')

    def __init__(self, *args, group, destination, schedule=None, legacy_enabled=False, **kwargs):
        initial = {'frequency':'disabled','send_empty':False}
        if schedule:
            initial.update({key:getattr(schedule,key) for key in ('frequency','categories','weekdays','send_empty','immediate_categories')})
            initial['times'] = ', '.join(schedule.times)
            initial['weekdays'] = [str(x) for x in schedule.weekdays]
            initial['scheduled_enabled'] = schedule.frequency != 'disabled'
        kwargs['initial'] = initial
        kwargs.setdefault('auto_id', destination+'_report_%s')
        super().__init__(*args, **kwargs)
        # The compact UI no longer edits Stage C preferences. Omission must not
        # erase values saved through the earlier editor; explicit values still
        # undergo the existing choice validation.
        if self.is_bound and schedule and 'immediate_categories' not in self.data:
            self.data = self.data.copy()
            if hasattr(self.data, 'setlist'):
                self.data.setlist('immediate_categories', schedule.immediate_categories)
            else:
                self.data['immediate_categories'] = list(schedule.immediate_categories)
        self.group = group
        self.destination = destination
        self.legacy_enabled = legacy_enabled and destination=='moderator'
        self.fields['categories'].choices = MODERATOR_CATEGORIES if destination=='moderator' else []
        self.fields['immediate_categories'].choices = IMMEDIATE_CATEGORIES[destination]
        if not self.legacy_enabled:
            self.fields.pop('retire_legacy')

    def clean_times(self):
        raw = self.cleaned_data['times'].strip()
        values = [] if not raw else [s.strip() for s in re.split(r'[,\n]+', raw) if s.strip()]
        if any(not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value) for value in values):
            raise ValidationError('Enter valid 24-hour times such as 08:00 or 17:30.')
        if len(values) != len(set(values)):
            raise ValidationError('Each delivery time must be unique.')
        return sorted(values)

    def clean_weekdays(self):
        return sorted(set(map(int, self.cleaned_data['weekdays'])))

    def clean(self):
        data = super().clean()
        valid_zone(self.group.timezone)
        frequency = data.get('frequency')
        if not data.get('scheduled_enabled'):
            data['frequency'] = 'disabled'
        elif frequency in {None,'','disabled'}:
            self.add_error('frequency', 'Select a delivery frequency or disable scheduled reports.')
        elif self.destination=='moderator' and not data.get('categories'):
            self.add_error('categories', 'Select at least one review category for scheduled reports.')
        if data.get('frequency') in {'daily','weekly','custom'}:
            times = data.get('times', [])
            days = data.get('weekdays', [])
            if not times:
                self.add_error('times','Choose at least one delivery time.')
            if frequency in {'daily','weekly'} and len(times)!=1:
                self.add_error('times','Daily and Weekly reports require exactly one delivery time.')
            if frequency in {'weekly','custom'} and not days:
                self.add_error('weekdays','Choose at least one delivery day.')
            if frequency=='weekly' and len(days)!=1:
                self.add_error('weekdays','Weekly reports require exactly one weekday.')
        if data.get('frequency')!='disabled' and self.legacy_enabled and not data.get('retire_legacy'):
            self.add_error('retire_legacy','Turn off the existing hourly digest before using the new schedule, to avoid duplicate reports.')
        return data


def next_run(schedule, after, zone_name=None):
    """Strictly future slot. UTC interval grid; fixed local times use fold 0 only.

    Nonexistent spring times are skipped. Fall-back wall times run once, at the
    first occurrence. This deliberately avoids duplicate local-time reports.
    """
    zone = valid_zone(zone_name or schedule.configuration.group.timezone)
    if timezone.is_naive(after):
        raise ValidationError('Schedule calculations require an aware timestamp.')
    if schedule.frequency=='disabled':
        return None
    if schedule.frequency in INTERVALS:
        step = INTERVALS[schedule.frequency]
        boundary = (int(after.timestamp()) // step + 1) * step
        return datetime.fromtimestamp(boundary, UTC)
    if schedule.frequency not in {'daily','weekly','custom'}:
        raise ValidationError('Unsupported report frequency.')
    days = set(range(7)) if schedule.frequency=='daily' else set(schedule.weekdays)
    for offset in range(16):
        day = after.astimezone(zone).date() + timedelta(days=offset)
        if day.weekday() not in days:
            continue
        for value in sorted(schedule.times):
            hour, minute = map(int,value.split(':'))
            wall = datetime(day.year,day.month,day.day,hour,minute)
            local = wall.replace(tzinfo=zone,fold=0)
            instant = local.astimezone(UTC)
            if instant.astimezone(zone).replace(tzinfo=None) != wall:
                continue
            if instant > after:
                return instant
    raise ValidationError('No valid delivery time is configured.')


@transaction.atomic
def configure_report_schedule(*, group, actor, destination, data, now=None):
    if destination not in DESTINATIONS:
        raise ValidationError('Choose a valid destination.')
    group = ReadingGroup.objects.select_for_update().get(pk=group.pk, is_active=True)
    actor = get_user_model().objects.get(pk=actor.pk)
    if not can_configure_discord_notifications(actor,group):
        raise PermissionDenied('Group Owner or Platform Administrator authorization is required.')
    configuration = GroupDiscordNotification.objects.select_for_update().filter(group=group).first()
    schedule = DiscordReportSchedule.objects.select_for_update().filter(configuration=configuration,destination=destination).first() if configuration else None
    form = ReportScheduleForm(data,group=group,destination=destination,schedule=schedule,
        legacy_enabled=bool(configuration and configuration.enabled))
    if not form.is_valid():
        return form, False
    if not configuration:
        configuration = GroupDiscordNotification.objects.create(group=group)
    if not schedule:
        schedule = DiscordReportSchedule(configuration=configuration,destination=destination)
    keys = ('frequency','categories','times','weekdays','send_empty','immediate_categories')
    values = {key:form.cleaned_data[key] for key in keys}
    if schedule.pk and all(getattr(schedule,key)==value for key,value in values.items()) and schedule.timezone_name==group.timezone and not form.cleaned_data.get('retire_legacy'):
        return form, False
    now = now or timezone.now()
    for key,value in values.items():setattr(schedule,key,value)
    schedule.configured_at = now
    schedule.timezone_name = group.timezone
    schedule.revision += 1
    schedule.next_run_at = next_run(schedule,max(now,schedule.cooldown_until-timedelta(microseconds=1)) if schedule.cooldown_until else now)
    schedule.save()
    retired = bool(form.cleaned_data.get('retire_legacy'))
    if retired:
        configuration.enabled = False
        configuration.save(update_fields=['enabled'])
    AuditEvent.objects.create(actor=actor,group=group,action='group.discord_schedule_updated',
        object_type='DiscordReportSchedule',object_id=str(schedule.pk),
        summary=json.dumps({'destination':destination,**values,'legacy_digest_disabled':retired,'immediate_delivery_active':False}))
    return form, True


def scheduled_payloads(schedule):
    from .discord_embeds import pack, community_status_embed, moderator_status_embeds
    group = schedule.configuration.group
    if schedule.destination=='community':
        # Opt-in is necessary, but no anonymous/public Challenge audience policy
        # exists yet. Even opted-in private Challenges remain excluded.
        return pack([community_status_embed(group,timezone.now().isoformat())]) if schedule.send_empty else []
    summary = deepcopy(discord.group_pending_review_counts(group))
    selected = set(schedule.categories)
    rows = []
    for row in summary['challenges']:
        teams = []
        for team in row['teams']:
            for key,_ in discord.REVIEW_LABELS:
                if key not in selected:team[key]=0
            team['total'] = sum(team[key] for key,_ in discord.REVIEW_LABELS)
            if team['total']:teams.append(team)
        row['teams']=teams
        row['total']=sum(team['total'] for team in teams)
        if row['total']:rows.append(row)
    summary['challenges']=rows
    summary['total']=sum(row['total'] for row in rows)
    if not summary['total'] and not schedule.send_empty:
        return []
    return pack(moderator_status_embeds(group,timezone.now().isoformat(),summary=summary,selected_only=True))


def slot_key(schedule, due):
    if schedule.frequency in INTERVALS:
        return 'utc:'+due.astimezone(UTC).isoformat()
    return 'local:'+schedule.timezone_name+':'+due.astimezone(valid_zone(schedule.timezone_name)).strftime('%Y-%m-%dT%H:%M')


@sensitive_variables()
def deliver_scheduled_report(schedule_id, *, now=None):
    if not settings.NORTHBOUND_DISCORD_SCHEDULER_ENABLED or not discord.delivery_enabled():
        return 'environment_disabled'
    now = now or timezone.now()
    row = DiscordReportSchedule.objects.filter(pk=schedule_id).values('configuration_id','configuration__group_id').first()
    if not row:return 'unavailable'
    with transaction.atomic(durable=True):
        group = ReadingGroup.objects.select_for_update().get(pk=row['configuration__group_id'])
        configuration = GroupDiscordNotification.objects.select_for_update().get(pk=row['configuration_id'])
        schedule = DiscordReportSchedule.objects.select_for_update().get(pk=schedule_id)
        schedule.configuration = configuration
        configuration.group = group
        if schedule.frequency=='disabled':return 'disabled'
        if schedule.timezone_name!=group.timezone or not schedule.next_run_at:
            schedule.timezone_name=group.timezone
            schedule.configured_at=now
            schedule.revision+=1
            schedule.next_run_at=next_run(schedule,now)
            schedule.save(update_fields=['timezone_name','configured_at','revision','next_run_at'])
            return 'rescheduled'
        due = schedule.next_run_at
        if due > now:return 'not_due'
        schedule.next_run_at=next_run(schedule,max(now,schedule.cooldown_until) if schedule.cooldown_until else now)
        schedule.save(update_fields=['next_run_at'])
        key = slot_key(schedule,due)
        try:
            with transaction.atomic():
                receipt = DiscordReportDelivery.objects.create(schedule=schedule,slot_key=key,scheduled_for=due,attempted_at=now)
        except IntegrityError:
            return 'duplicate'
        failure = _blocking_reason(configuration,schedule.destination)
        if not group.is_active:failure='destination_disabled'
        if schedule.destination=='moderator' and configuration.enabled:failure='legacy_digest_enabled'
        cooling = schedule.cooldown_until and schedule.cooldown_until>now
        if DiscordManualDelivery.objects.filter(group=group,destination=schedule.destination,retry_at__gt=now).exists():cooling=True
        if cooling:failure='rate_limited'
        missed = (now-due).total_seconds()>GRACE_SECONDS or due<=schedule.configured_at
        if missed or failure:
            receipt.outcome = 'missed' if missed else 'blocked'
            receipt.failure_category = failure
            receipt.save(update_fields=['outcome','failure_category'])
            schedule.last_outcome=receipt.outcome
            schedule.save(update_fields=['last_outcome'])
            return receipt.outcome
        url,failure = _webhook(configuration,schedule.destination)
        if failure:
            receipt.outcome='failed';receipt.failure_category=failure
            receipt.save(update_fields=['outcome','failure_category'])
            schedule.last_attempt_at=now;schedule.last_failure_at=now
            schedule.last_failure_category=failure;schedule.last_outcome='failed'
            schedule.save(update_fields=['last_attempt_at','last_failure_at','last_failure_category','last_outcome'])
            return 'failed'
        revision = schedule.revision
        _,webhook_field = discord.destination_fields(schedule.destination)
        ciphertext = getattr(configuration,webhook_field)
        schedule.last_attempt_at=now;schedule.last_outcome='claimed'
        schedule.save(update_fields=['last_attempt_at','last_outcome'])
    # Claim committed before I/O; a crash leaves an uncertain claim, never a retry.
    cooldown = 0
    try:
        payloads=scheduled_payloads(schedule)
        for payload in payloads:
            current = DiscordReportSchedule.objects.filter(pk=schedule_id,revision=revision,frequency=schedule.frequency,
                configuration__group__is_active=True,configuration__group__timezone=schedule.timezone_name).exists()
            latest = GroupDiscordNotification.objects.get(pk=configuration.pk)
            if (not current or getattr(latest,webhook_field)!=ciphertext or _blocking_reason(latest,schedule.destination)
                    or (schedule.destination=='moderator' and latest.enabled)
                    or not settings.NORTHBOUND_DISCORD_SCHEDULER_ENABLED or not discord.delivery_enabled()):
                failure='configuration_changed';break
            status,cooldown = discord.send_webhook(url,payload,detailed=True)
            if status!='sent':
                failure=status if status in FAILURES else 'delivery_failed';break
    except Exception:
        failure='delivery_failed'
        payloads=None
    outcome = 'failed' if failure else 'sent' if payloads else 'empty'
    with transaction.atomic():
        locked=DiscordReportSchedule.objects.select_for_update().get(pk=schedule_id)
        receipt.outcome=outcome;receipt.failure_category=failure
        receipt.save(update_fields=['outcome','failure_category'])
        locked.last_outcome=outcome
        fields=['last_outcome']
        if outcome=='sent':locked.last_sent_at=now;fields.append('last_sent_at')
        if failure:
            locked.last_failure_at=now;locked.last_failure_category=failure
            fields+=['last_failure_at','last_failure_category']
        if cooldown:
            locked.cooldown_until=now+timedelta(seconds=min(86400,max(1,float(cooldown))))
            fields.append('cooldown_until')
            if locked.revision==revision and locked.frequency!='disabled':
                locked.next_run_at=next_run(locked,max(now,locked.cooldown_until-timedelta(microseconds=1)))
                fields.append('next_run_at')
        locked.save(update_fields=fields)
    return outcome


def process_due_reports(*, now=None, daemon=False):
    if not settings.NORTHBOUND_DISCORD_SCHEDULER_ENABLED:return []
    now = now or timezone.now()
    DiscordSchedulerHeartbeat.objects.update_or_create(name='discord',defaults={'last_seen':now,'daemon':daemon})
    if not discord.delivery_enabled():return []
    result=[]
    for pk in DiscordReportSchedule.objects.exclude(frequency='disabled').values_list('pk',flat=True):
        try:result.append((pk,deliver_scheduled_report(pk,now=now)))
        except Exception:result.append((pk,'configuration_error'))
    return result


def schedule_diagnostics(schedule, *, group, now=None):
    now=now or timezone.now()
    heartbeat=DiscordSchedulerHeartbeat.objects.filter(name='discord').first()
    if not settings.NORTHBOUND_DISCORD_SCHEDULER_ENABLED:
        operational='Not activated. Recurring delivery requires separate service activation.'
    elif heartbeat and 0 <= (now-heartbeat.last_seen).total_seconds() <= GRACE_SECONDS:
        operational='Recently active scheduler' if heartbeat.daemon else 'Recent one-time scheduler check'
    else:operational='Not running: no recent scheduler heartbeat.'
    frequency=schedule.frequency if schedule else 'disabled'
    upcoming=None;error=''
    try:
        valid_zone(group.timezone)
        display_zone=group.timezone
    except ValidationError:
        display_zone='UTC'
        error='Invalid Group timezone. Ask a Group Owner to correct it in Group Settings.'
    if schedule and frequency!='disabled':
        try:upcoming=next_run(schedule,max(now,schedule.cooldown_until-timedelta(microseconds=1)) if schedule.cooldown_until else now,group.timezone)
        except ValidationError:error='Invalid schedule or Group timezone; no report is ready.'
        if (schedule.next_run_at and schedule.next_run_at>now and schedule.timezone_name==group.timezone
                and (not schedule.cooldown_until or schedule.next_run_at>=schedule.cooldown_until)):
            upcoming=schedule.next_run_at
    readiness='Scheduled reporting disabled.'
    if schedule and frequency!='disabled':
        reason=_blocking_reason(schedule.configuration,schedule.destination)
        readiness=FAILURES.get(reason,'Configuration ready. Delivery still requires an activated scheduler.')
        if schedule.cooldown_until and schedule.cooldown_until>now:readiness=FAILURES['rate_limited']
        if error:readiness=error
        elif schedule.destination=='moderator' and schedule.configuration.enabled:
            readiness='The existing hourly digest must be disabled before using this schedule.'
        elif schedule.destination=='community' and not schedule.send_empty:
            readiness='Configured for checks, but no public categories are approved and empty reports are suppressed.'
    outcome_labels={'claimed':'Started; outcome may be unknown. No automatic retry.', 'sent':'Accepted by Discord',
        'empty':'No eligible content; no message sent', 'failed':'Delivery failed',
        'blocked':'Delivery blocked by configuration or cooldown', 'missed':'Missed slot skipped; not replayed'}
    return {'readiness':readiness,'outcome_label':outcome_labels.get(schedule.last_outcome,'No scheduled attempt') if schedule else 'No scheduled attempt',
        'enabled':frequency!='disabled','frequency':dict(DiscordReportSchedule.Frequency.choices).get(frequency,'Unavailable'),
        'timezone':group.timezone,'display_timezone':display_zone,'next_run':upcoming,'operational':operational,'error':error,
        'last_attempt':schedule.last_attempt_at if schedule else None,'last_sent':schedule.last_sent_at if schedule else None,
        'last_failure':schedule.last_failure_at if schedule else None,
        'failure_label':FAILURES.get(schedule.last_failure_category,'Configuration or delivery changed; inspect settings.') if schedule and schedule.last_failure_category else '',
        'last_outcome':schedule.last_outcome if schedule else '',
        'categories_unavailable':schedule.destination=='community' if schedule else False}
