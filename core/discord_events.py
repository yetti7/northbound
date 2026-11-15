"""Transactional service observers and a bounded, fail-closed Discord outbox.

No polling of historical business records and no generic save signals. Claimed
requests become uncertain after a crash: retrying those could duplicate a post.
"""
from contextvars import ContextVar
from contextlib import contextmanager
from datetime import timedelta
from functools import wraps
import hashlib
import inspect
import uuid

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

_active = ContextVar('discord_observers', default=frozenset())
MAX_ATTEMPTS = 4
MAX_AGE = timedelta(hours=1)


def fingerprint(config, destination):
    from .discord_notifications import destination_fields
    return hashlib.sha256(getattr(config, destination_fields(destination)[1]).encode()).hexdigest()


def enqueue(*, month, categories, source_kind, source_id, review_category='', participant_id=None, event_key=None):
    """Called in the business transaction. It rolls back with that transaction.

    Collection needs explicit local activation, even when global I/O is disabled.
    Activation timestamps fence off old events across disable/re-enable cycles.
    """
    from .models import DiscordReportSchedule, DiscordEventDelivery, TeamAssignment
    from .discord_notifications import destination_fields
    from .discord_community import community_eligible, results_visible
    assert transaction.get_connection().in_atomic_block
    destination = 'moderator' if review_category else 'community'
    schedule = DiscordReportSchedule.objects.select_related('configuration__group').filter(
        configuration__group_id=month.group_id, destination=destination, immediate_enabled=True).first()
    if not schedule or not schedule.immediate_activated_at:
        return None
    category = next((c for c in categories if c in schedule.immediate_categories), None)
    config = schedule.configuration
    enabled, secret = destination_fields(destination)
    if not category or not month.group.is_active or not getattr(config, enabled) or not getattr(config, secret):
        return None
    if destination == 'community' and (not community_eligible(month) or (category=='results' and not results_visible(month))):
        return None
    now = timezone.now()
    team_id = None
    if participant_id:
        from .discord_notifications import _review_team_attribution
        team_id = _review_team_attribution(month)[0].get(participant_id)
    row, _ = DiscordEventDelivery.objects.get_or_create(event_key=event_key or uuid.uuid4(), destination=destination,
        defaults=dict(group_id=month.group_id, challenge=month, category=category, source_kind=source_kind,
            source_id=source_id, review_category=review_category, team_id=team_id, created_at=now,
            activated_at=schedule.immediate_activated_at, expires_at=now+MAX_AGE, next_attempt_at=now,
            webhook_fingerprint=fingerprint(config,destination)))
    return row


def book_snapshot(pk):
    from .models import BookSubmission
    book = BookSubmission.objects.select_related('month__group').filter(pk=pk).first()
    if not book:
        return None, {}
    states = {('books', 'book', book.pk): book.status if not book.is_removed else 'removed'}
    states.update({('themes', 'theme', pk): status for pk, status in book.theme_claims.values_list('pk','status')})
    from .immersive import pending_review_requests
    immersive_pending=set(pending_review_requests(month=book.month).filter(submission=book).values_list('pk',flat=True))
    states.update({('immersive', 'immersive', pk): 'waiting' if status=='pending' and pk not in immersive_pending else status for pk, status in book.immersive_requests.values_list('pk','status')})
    from .prompt_scoring import pending_submission_ids
    prompt_pending=pending_submission_ids(book.month).filter(submission_id=book.pk).exists()
    states.update({(kind, 'prompt', pk): 'pending' if state=='reserved' and prompt_pending else
        'approved' if state=='qualified' else state for pk,kind,state in
        book.prompt_claims.filter(is_current=True).values_list('pk','category__kind','state')})
    return book, states


def record_book_changes(book, before, after, *, created=False):
    for (review, kind, pk), state in after.items():
        previous = before.get((review, kind, pk))
        if previous == state:
            continue
        # Observe only actionable pending states from the existing review services.
        if state not in {'pending','approved','rejected'}:
            continue
        categories = ['review_required','new_submission'] if state=='pending' and previous is None else \
            ['review_required'] if state=='pending' else [state]
        if created and kind=='book' and state=='approved':
            categories = ['new_submission','approved']
        enqueue(month=book.month, categories=categories, source_kind=kind, source_id=pk,
            review_category=review, participant_id=book.participant_id,
            event_key=uuid.uuid5(uuid.NAMESPACE_URL,f'northbound:{kind}:{pk}:created') if previous is None else None)


def record_book_created(book):
    current, after = book_snapshot(book.pk)
    record_book_changes(current, {}, after, created=True)


@contextmanager
def observe_book_changes(book):
    """Observe within the caller's transaction without taking ownership of it."""
    from .models import ChallengeMonth
    if not connection.in_atomic_block:
        raise RuntimeError('Book observation requires a caller-owned transaction.')
    key = ('book',book.pk)
    if key in _active.get():
        yield
        return
    ChallengeMonth.objects.select_for_update().get(pk=book.month_id)
    _, before = book_snapshot(book.pk)
    token = _active.set(_active.get() | {key})
    try:
        yield
        # Correction previews deliberately mark their transaction for rollback.
        # Do not query a rollback-only transaction or record simulated events.
        if not connection.needs_rollback:
            current, after = book_snapshot(book.pk)
            if current:
                record_book_changes(current, before, after)
    finally:
        _active.reset(token)


def observe_book(function):
    """Explicitly attached to service entry points, never to model.save()."""
    signature = inspect.signature(function)
    @wraps(function)
    def wrapped(*args, **kwargs):
        values = signature.bind(*args, **kwargs).arguments
        book = values.get('submission')
        if book is None:
            book = values['request'].submission
        key = ('book',book.pk)
        if key in _active.get():
            return function(*args, **kwargs)
        with transaction.atomic(), observe_book_changes(book):
            return function(*args, **kwargs)
    return wrapped


def observe_photo(function):
    signature = inspect.signature(function)
    @wraps(function)
    def wrapped(*args, **kwargs):
        from .models import ChallengeMonth, ScavengerPhotoSubmission
        values = signature.bind(*args, **kwargs).arguments
        pk = values.get('photo_id')
        with transaction.atomic():
            old = ScavengerPhotoSubmission.objects.filter(pk=pk).first() if pk else None
            if pk and old is None:
                return function(*args, **kwargs)
            month_id = old.month_id if old else values['month_id']
            ChallengeMonth.objects.select_for_update().get(pk=month_id)
            # Re-read under the same serialization lock used by the service.
            old = ScavengerPhotoSubmission.objects.filter(pk=pk).first() if pk else None
            key = ('photo',pk) if pk else ('photo-new',month_id)
            if key in _active.get():
                return function(*args, **kwargs)
            token = _active.set(_active.get() | {key})
            try:
                result = function(*args, **kwargs)
            finally:
                _active.reset(token)
            if old is None or result.status != old.status:
                if result.status in {'pending','approved','rejected'}:
                    enqueue(month=result.month, categories=['review_required','new_submission'] if old is None else
                        ['review_required'] if result.status=='pending' else [result.status],
                        source_kind='photo',source_id=result.pk,review_category='photos',participant_id=result.participant_id,
                        event_key=uuid.uuid5(uuid.NAMESPACE_URL,f'northbound:photo:{result.pk}:created') if old is None else None)
            return result
    return wrapped


def observe_challenge(function):
    signature = inspect.signature(function)
    @wraps(function)
    def wrapped(*args, **kwargs):
        from .models import ChallengeMonth
        values = signature.bind(*args, **kwargs).arguments
        month = values.get('self') or values.get('month')
        pk = month.pk if month else values['month_id']
        key = ('challenge',pk)
        if key in _active.get():
            return function(*args, **kwargs)
        with transaction.atomic():
            old = ChallengeMonth.objects.select_for_update().get(pk=pk)
            token = _active.set(_active.get() | {key})
            try:
                result = function(*args, **kwargs)
                new = ChallengeMonth.objects.select_related('group').get(pk=pk)
                events = []
                if old.registration_is_open != new.registration_is_open:
                    events.append('registration_open' if new.registration_is_open else 'registration_close')
                forward = {('upcoming','active'):'challenge_start',('active','finalizing'):'challenge_close',
                    ('finalizing','completed'):'challenge_complete'}
                event = forward.get((old.status,new.status))
                if event:
                    events.append(event)
                    if event=='challenge_complete':events.append('results')
                for category in events:
                    # Scheduled actions that were already overdue are not announced
                    # as fresh events when the lifecycle scheduler is activated.
                    due_field = {'registration_open':'registration_opens_at','registration_close':'registration_closes_at',
                        'challenge_start':'starts_at','challenge_close':'ends_at','challenge_complete':'final_announcement_at',
                        'results':'final_announcement_at'}[category]
                    due = getattr(new,due_field)
                    now = values.get('now') or timezone.now()
                    if function.__name__ in {'apply_scheduled_actions','_save_challenge_schedule'} and due and now-due>timedelta(seconds=90):
                        continue
                    enqueue(month=new,categories=[category],source_kind='challenge',source_id=pk)
                return result
            finally:
                _active.reset(token)
    return wrapped


def eligible(row, schedule, now):
    from .discord_manual import _blocking_reason
    from .discord_community import community_eligible, results_visible
    if not schedule or not schedule.immediate_enabled or schedule.immediate_activated_at != row.activated_at:
        return 'activation_changed'
    if row.category not in schedule.immediate_categories:
        return 'category_disabled'
    config = schedule.configuration
    if not config.group.is_active or _blocking_reason(config,row.destination):
        return 'destination_disabled'
    if fingerprint(config,row.destination) != row.webhook_fingerprint:
        return 'webhook_changed'
    if now >= row.expires_at:
        return 'expired'
    if row.challenge.group_id != row.group_id:
        return 'unavailable'
    if row.destination=='moderator':
        from .models import BookSubmission, ThemeClaim, ImmersiveRequest, PromptClaim, ScavengerPhotoSubmission
        models={'book':BookSubmission,'theme':ThemeClaim,'immersive':ImmersiveRequest,
            'prompt':PromptClaim,'photo':ScavengerPhotoSubmission}
        model=models.get(row.source_kind)
        source=model.objects.filter(pk=row.source_id).first() if model else None
        if source is None:return 'source_unavailable'
        book=source if row.source_kind in {'book','photo'} else source.submission
        if book is None or book.month_id!=row.challenge_id or getattr(book,'is_removed',False) or book.status in {'voided','withdrawn'}:return 'source_unavailable'
        if row.category=='review_required':
            state=getattr(source,'status',getattr(source,'state',''))
            if state not in {'pending','reserved'}:return 'superseded'
            if row.source_kind=='prompt':
                from .prompt_scoring import pending_submission_ids
                if not source.is_current or not source.category.enabled or not pending_submission_ids(row.challenge).filter(submission_id=book.pk).exists():return 'superseded'
            if row.source_kind=='immersive':
                from .immersive import pending_review_requests
                if not pending_review_requests(month=row.challenge).filter(pk=source.pk).exists():return 'superseded'
            if row.source_kind=='photo':
                from .photo_review import pending_photo_review_candidates
                if not pending_photo_review_candidates(row.challenge).filter(pk=source.pk).exists():return 'superseded'
            if row.source_kind in {'book','theme'} and row.challenge.status not in {'active','finalizing'}:return 'superseded'
    if row.destination=='community':
        if not community_eligible(row.challenge):return 'visibility_changed'
        if row.category=='results' and not results_visible(row.challenge):return 'visibility_changed'
        expected={'registration_open':row.challenge.registration_is_open,
            'registration_close':not row.challenge.registration_is_open,
            'challenge_start':row.challenge.status=='active','challenge_close':row.challenge.status=='finalizing',
            'challenge_complete':row.challenge.status in {'completed','archived'},'results':results_visible(row.challenge)}
        if not expected.get(row.category):return 'superseded'
    return ''


def event_payload(row):
    from .discord_embeds import embed, name, clip, link_field, application_url, pack, MODERATOR_COLOR, COMMUNITY_COLOR, REVIEW_FIELDS
    month = row.challenge
    stamp = row.created_at.isoformat()
    if row.destination=='community':
        from django.core.exceptions import ValidationError
        from .discord_community import results_visible
        if row.category=='results' and not results_visible(month):
            raise ValidationError('Results are not authorized for the Discord audience.')
        titles={'registration_open':'Registration Is Open!','registration_close':'Registration Closed',
            'challenge_start':'Challenge Begins!','challenge_close':'Challenge Submissions Closed',
            'challenge_complete':'Challenge Complete!','results':'Final Results'}
        text={'registration_open':'Registration is now available.','registration_close':'Registration has closed.',
            'challenge_start':'The Challenge is officially underway.','challenge_close':'The submission window has closed; final review is underway.',
            'challenge_complete':'The Challenge is complete.','results':'Final results are now available.'}
        value=embed(group=month.group,title=clip('📚 '+name(month.group.name)+' — '+titles[row.category],256),
            description='**'+name(month.name,markdown=True)+'**\n'+text[row.category],color=COMMUNITY_COLOR,timestamp=stamp)
        fields=[]
        if month.starts_on and month.ends_on:
            fields.append({'name':'Challenge Dates','value':f'{month.starts_on:%b %d, %Y} – {month.ends_on:%b %d, %Y}','inline':False})
        link=link_field('View Final Standings' if row.category=='results' else 'View Challenge',
            application_url('month-detail',[month.group.slug,month.pk],public=True))
    else:
        title={'new_submission':'New Submission','review_required':'Review Needed','approved':'Submission Approved','rejected':'Submission Rejected'}[row.category]
        label=dict(REVIEW_FIELDS)[row.review_category]
        value=embed(group=month.group,title='📚 Northbound — '+title,
            description='**'+name(month.name,markdown=True)+'**\n'+label+' · '+title.lower()+'.',color=MODERATOR_COLOR,timestamp=stamp)
        fields=[{'name':'⚔️ '+clip(name(row.team.name),245) if row.team_id and row.team.month_id==month.pk else '📋 Unassigned / Group Reviews',
            'value':'Open the review queue for details.','inline':False}]
        link=link_field('Review Submission',application_url('review-queue',[month.group.slug,month.pk]))
    if link:fields.append(link)
    value['fields']=fields
    return pack([value])[0]


@sensitive_variables()
def deliver_event(pk, *, now=None):
    from .models import DiscordEventDelivery, DiscordReportSchedule, DiscordManualDelivery
    from . import discord_notifications as discord
    from .discord_manual import _webhook
    now=now or timezone.now()
    if not settings.NORTHBOUND_DISCORD_EVENTS_ENABLED or not discord.delivery_enabled():return 'environment_disabled'
    with transaction.atomic(durable=True):
        row=DiscordEventDelivery.objects.select_related('challenge__group','team').get(pk=pk)
        # Conditional UPDATE is the claim authority, also on SQLite where row locks
        # are advisory. No I/O can occur without winning this exact state change.
        if row.state!='pending' or row.next_attempt_at>now:return row.state
        schedule=DiscordReportSchedule.objects.select_related('configuration__group').filter(
            configuration__group_id=row.group_id,destination=row.destination).first()
        reason=eligible(row,schedule,now)
        if reason:
            DiscordEventDelivery.objects.filter(pk=pk,state='pending').update(state='skipped',failure_category=reason,finished_at=now)
            return 'skipped'
        if DiscordEventDelivery.objects.filter(group_id=row.group_id,destination=row.destination,pk__lt=pk,
                state__in=['pending','claimed']).exists():return 'ordered_wait'
        cooling=DiscordManualDelivery.objects.filter(group_id=row.group_id,destination=row.destination,retry_at__gt=now).exists()
        if cooling or (schedule.cooldown_until and schedule.cooldown_until>now):return 'cooldown'
        claim=uuid.uuid4()
        if not DiscordEventDelivery.objects.filter(pk=pk,state='pending',next_attempt_at__lte=now).update(
            state='claimed',claim_id=claim,claimed_at=now,attempts=row.attempts+1):return 'duplicate'
        row.attempts+=1
    # Claim is committed before preparation and transport. Unexpected failure is
    # deliberately ambiguous; never recycle a stale claim into a second POST.
    failure='';delay=0
    try:
        row=DiscordEventDelivery.objects.select_related('challenge__group','team').get(pk=pk)
        schedule=DiscordReportSchedule.objects.select_related('configuration__group').get(
            configuration__group_id=row.group_id,destination=row.destination)
        failure=eligible(row,schedule,timezone.now())
        if not settings.NORTHBOUND_DISCORD_EVENTS_ENABLED or not discord.delivery_enabled():failure='environment_disabled'
        payload=event_payload(row) if not failure else None
        url,error=_webhook(schedule.configuration,row.destination) if not failure else ('','')
        failure=failure or error
        if failure:status='skipped'
        else:
            status,delay=discord.send_webhook(url,payload,detailed=True,retryable=True)
            failure='' if status=='sent' else status
    except Exception:
        status='uncertain';failure='delivery_failed'
    state='sent' if status=='sent' else 'skipped' if status=='skipped' else \
        'pending' if status in {'rate_limited','server_error'} and row.attempts<MAX_ATTEMPTS else \
        'uncertain' if status in {'timeout','network_error','uncertain'} else 'failed'
    with transaction.atomic():
        updated=DiscordEventDelivery.objects.filter(pk=pk,state='claimed',claim_id=claim).update(state=state,
            failure_category=failure,finished_at=None if state=='pending' else now,
            next_attempt_at=now+timedelta(seconds=max(30*2**(row.attempts-1),min(86400,max(0,float(delay))))))
        if updated and delay:
            DiscordReportSchedule.objects.filter(pk=schedule.pk).update(cooldown_until=now+timedelta(seconds=min(86400,max(1,float(delay)))))
    return state


def process_events(*, now=None, daemon=False):
    from .models import DiscordEventDelivery, DiscordSchedulerHeartbeat
    from . import discord_notifications as discord
    now=now or timezone.now()
    if not settings.NORTHBOUND_DISCORD_EVENTS_ENABLED or not discord.delivery_enabled():return []
    DiscordSchedulerHeartbeat.objects.update_or_create(name='discord-events',defaults={'last_seen':now,'daemon':daemon})
    # A first delivery missed during downtime is not a historical catch-up job.
    DiscordEventDelivery.objects.filter(state='pending',attempts=0,created_at__lt=now-timedelta(seconds=90)).update(
        state='skipped',failure_category='missed',finished_at=now)
    DiscordEventDelivery.objects.filter(state='claimed',claimed_at__lt=now-timedelta(minutes=5)).update(
        state='uncertain',failure_category='interrupted',finished_at=now)
    results=[]
    for pk in DiscordEventDelivery.objects.filter(state='pending',next_attempt_at__lte=now).order_by('pk').values_list('pk',flat=True)[:100]:
        try:results.append((pk,deliver_event(pk,now=now)))
        except Exception:results.append((pk,'configuration_error'))
    return results


def event_diagnostics(group,destination):
    from .models import DiscordEventDelivery, DiscordSchedulerHeartbeat, DiscordReportSchedule
    rows=DiscordEventDelivery.objects.filter(group=group,destination=destination)
    heartbeat=DiscordSchedulerHeartbeat.objects.filter(name='discord-events').first()
    active=settings.NORTHBOUND_DISCORD_EVENTS_ENABLED
    recent=heartbeat and 0<=(timezone.now()-heartbeat.last_seen).total_seconds()<90
    schedule=DiscordReportSchedule.objects.filter(configuration__group=group,destination=destination).first()
    return {'enabled':bool(schedule and schedule.immediate_enabled),'worker':
        'Not activated' if not active else 'Recent worker heartbeat' if recent and heartbeat.daemon else
        'Recent one-time check' if recent else 'No recent heartbeat',
        'pending':rows.filter(state='pending').count(),'last_sent':rows.filter(state='sent').order_by('-finished_at').first(),
        'last_failure':rows.exclude(state__in=['sent','pending']).order_by('-created_at').first(),
        'history':rows.order_by('-created_at','-pk')[:10]}
