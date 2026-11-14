"""Small signed, user-bound list origins for the shared navigation resolver."""
from urllib.parse import urlencode, urlsplit, parse_qsl
from django.core import signing
from django.urls import reverse, resolve, Resolver404

SALT = 'northbound.navigation.v1'
COMMON = {'page','q','search','sort','direction','from','to','status','themes','tbr','botm','bonuses','show_discord','show_average','show_last','show_completed'}
ORIGINS = {
    'submission-activity': ('Back to Recent Activity', {'page'}),
    'platform-submission-activity': ('Back to Recent Activity', {'page'}),
    'review-queue': ('Back to Review Queue', COMMON),
    'team-detail': ('Back to Team Books', COMMON | {'view','books_page'}),
    'month-detail': ('Back to Challenge', COMMON),
    'participant-detail': ('Back to Reader History', COMMON),
    'challenge-participant-detail': ('Back to Reader History', COMMON),
    'prompt-progress-report': ('Back to Prompt Report', {'reader','team','category','completion','page'}),
    'recap-dashboard': ('Back to Chronicle', {'group','challenge','perspective','reader','team','period','anchor','start','end'}),
}
SCREENS = {'submission-manage','submission-editor','submission-edit','submission-fix-book','submission-fix-pages','submission-credit','submission-review','submission-history','submission-activity','submission-restore','submission-immersive','team-book-detail','prompt-progress','recap-wrapped'}


def _authorized(request, name, kwargs, query):
    from .context_processors import _authorized_parent
    from .models import ChallengeMonth, Team, Membership, MonthEnrollment, TeamAssignment
    from .permissions import can_view_team_books, membership_for, challenge_review_scope
    if not request.user.is_authenticated or not request.user.is_active:return False
    if (name=='prompt-progress-report' and getattr(request,'_navigation_validated_prompt_report',False)
        and request.resolver_match.view_name==name and request.resolver_match.kwargs==kwargs
        and query=={k:v for k,v in request.GET.items() if k in ORIGINS[name][1] and len(v)<=200}):return True
    if name in {'submission-activity','platform-submission-activity'}:
        from .submission_activity import can_view_staff_activity
        month=ChallengeMonth.objects.filter(pk=kwargs.get('month_pk'),group__slug=kwargs.get('group_slug')).first() if kwargs.get('month_pk') else None
        return bool((month or name=='platform-submission-activity') and can_view_staff_activity(request.user,month))
    if name == 'recap-dashboard':
        from .recap_forms import RecapFilterForm
        from .recaps import build_recap
        from django.core.exceptions import ValidationError, PermissionDenied
        form=RecapFilterForm(query,viewer=request.user)
        if not form.is_valid():return False
        v=form.cleaned_data
        try:
            build_recap(viewer=request.user,perspective=v['perspective'],target_id=(v.get('reader') if v['perspective']=='reader' else v.get('team') if v['perspective']=='team' else None) or None,group_id=v['group'] or None,challenge_id=v['challenge'] or None,period=v['period'],anchor=v['anchor'],start=v['start'],end=v['end'])
        except (ValidationError, PermissionDenied):return False
        return True
    if name == 'participant-detail':
        p=Membership.objects.select_related('group','user').filter(pk=kwargs.get('pk'),group__slug=kwargs.get('group_slug'),group__is_active=True,user__is_superuser=False).first()
        return bool(p and (request.user.is_superuser or membership_for(request.user,p.group)))
    if name != 'challenge-participant-detail' and not _authorized_parent(request,name,kwargs):return False
    month=ChallengeMonth.objects.filter(pk=kwargs.get('month_pk',kwargs.get('pk') if name=='month-detail' else None),group__slug=kwargs.get('group_slug')).first()
    if not month:return False
    from .permissions import can_view_challenge
    if not can_view_challenge(request.user,month):return False
    if name=='team-detail':
        team=Team.objects.filter(pk=kwargs.get('pk'),month=month).first()
        return bool(team and can_view_team_books(request.user,team))
    if name=='challenge-participant-detail':
        from .views import _can_view_challenge_participant
        e=MonthEnrollment.objects.filter(month=month,participant_id=kwargs.get('pk')).first()
        team=TeamAssignment.objects.filter(month=month,participant_id=kwargs.get('pk'),ended_at__isnull=True).values_list('team_id',flat=True).first()
        return bool(e and _can_view_challenge_participant(request.user,e,challenge_review_scope(request.user,month),team))
    if name=='prompt-progress-report':
        from .prompt_progress import categories,enrollments,reporting_scope
        from django.core.exceptions import PermissionDenied
        configured=categories(month)
        if not configured.exists():return False
        if query.get('category') and not configured.filter(kind=query['category']).exists():return False
        if query.get('completion','') not in {'','complete','incomplete'}:return False
        try:readers=enrollments(request.user,month,staff=True)
        except PermissionDenied:return False
        scope=reporting_scope(request.user,month)
        if query.get('reader') and not readers.filter(participant_id=query['reader']).exists():return False
        if query.get('team') and not month.teams.filter(pk=query['team']).filter(**({} if scope[0]=='challenge' else {'pk__in':scope[1]})).exists():return False
    return True


def authorized(request, name, kwargs, query):
    # Per-request only: subsequent workflow requests always check current authority.
    cache=request.__dict__.setdefault('_navigation_origin_permissions', {})
    key=(request.user.pk,name,tuple(sorted(kwargs.items())),tuple(sorted(query.items())))
    if key not in cache:cache[key]=_authorized(request,name,kwargs,query)
    return cache[key]


def unpack(request, token):
    try:
        data=signing.loads(token,salt=SALT,max_age=7200)
        if data['user']!=request.user.pk or data['name'] not in ORIGINS:return None
        name,kwargs,query=data['name'],data['kwargs'],data['query']
        if set(query)-ORIGINS[name][1] or not authorized(request,name,kwargs,query):return None
        current=request.resolver_match.kwargs
        if current.get('group_slug') and kwargs.get('group_slug') and current['group_slug']!=kwargs['group_slug']:return None
        origin_month=kwargs.get('month_pk',kwargs.get('pk') if name=='month-detail' else None)
        if current.get('month_pk') and origin_month and current['month_pk']!=origin_month:return None
        return data
    except (signing.BadSignature,KeyError,TypeError,ValueError):return None


def contextual_parent(request):
    if request.resolver_match.view_name not in SCREENS:return None
    data=unpack(request,request.GET.get('nav',''))
    if not data:return None
    url=reverse(data['name'],kwargs=data['kwargs'])
    if data['query']:url+='?'+urlencode(data['query'])
    return {'logical_parent_url':url,'logical_parent_label':ORIGINS[data['name']][0]}


def contextual_url(request, url):
    """Only decorate known GET screens; never Save/Cancel/action destinations."""
    parts=urlsplit(url)
    if parts.scheme or parts.netloc:return url
    try:target=resolve(parts.path)
    except Resolver404:return url
    if target.view_name not in SCREENS:return url
    token=request.GET.get('nav','')
    if not unpack(request,token):
        name=request.resolver_match.view_name
        if name not in ORIGINS:return url
        query={k:v for k,v in request.GET.items() if k in ORIGINS[name][1] and len(v)<=200}
        if name=='team-detail':query['view']='books'
        kwargs=request.resolver_match.kwargs
        if not authorized(request,name,kwargs,query):return url
        token=signing.dumps({'user':request.user.pk,'name':name,'kwargs':kwargs,'query':query},salt=SALT,compress=True)
    # Validate against the target too, preventing context crossing a Group/Challenge.
    from copy import copy
    destination=copy(request);destination.resolver_match=target
    if not unpack(destination,token):return url
    query=dict(parse_qsl(parts.query));query['nav']=token
    return parts._replace(query=urlencode(query)).geturl()
