"""Read-only prompt progress, scoped before any private claim is fetched."""
from django.core.exceptions import PermissionDenied
from django.db.models import Prefetch, Q, F
from .models import (PromptCategory,ChallengePrompt,PromptClaim,PromptCompletion,MonthEnrollment,TeamAssignment,Membership)
from .permissions import can_view_challenge,challenge_roster_read_scope,membership_for,restricted_host_team_ids
from .prompt_configuration import automation_excluded
from .prompt_reservations import reservations_ready


def categories(month):
    if month.status not in {'active','finalizing','completed','archived'}:
        return PromptCategory.objects.none()
    configured=PromptCategory.objects.filter(month=month,prompts__is_active=True)
    if reservations_ready() and not automation_excluded(month):
        configured=configured.filter(Q(enabled=True)|Q(claims__isnull=False)|Q(completions__isnull=False))
    else:
        configured=configured.filter(Q(claims__isnull=False)|Q(completions__isnull=False))
    return configured.distinct().prefetch_related(Prefetch('prompts',queryset=ChallengePrompt.objects.order_by('number')))


def reporting_scope(user,month):
    if not user.is_active or not can_view_challenge(user,month):return None
    scope=challenge_roster_read_scope(user,month)
    # A participating Host's restriction wins over overlapping Group authority.
    if restricted_host_team_ids(user,month) is not None:return scope
    member=membership_for(user,month.group)
    if user.is_superuser or member and member.role==Membership.Role.OWNER:return ('challenge',frozenset())
    return scope


def enrollments_and_scope(user,month,*,staff=False):
    if not user.is_active or not can_view_challenge(user,month):raise PermissionDenied
    query=MonthEnrollment.objects.filter(month=month,participant__group=month.group).select_related('participant__user','credited_team')
    if not staff:
        return query.filter(participant__user=user),None
    scope=reporting_scope(user,month)
    if scope is None:raise PermissionDenied
    if scope[0]=='team':
        query=query.filter(is_active=True,participant_id__in=TeamAssignment.objects.filter(month=month,ended_at__isnull=True,team_id__in=scope[1]).values('participant_id'))
    return query.order_by('participant__user__username','pk'),scope


def enrollments(user,month,*,staff=False):
    return enrollments_and_scope(user,month,staff=staff)[0]


def progress_rows(month,readers,configured):
    """Batch a paginated set of authorized Readers; no scoring reconciliation."""
    readers=list(readers);ids=[e.participant_id for e in readers];category_ids=[c.pk for c in configured]
    claims=PromptClaim.objects.filter(category_id__in=category_ids,participant_id__in=ids,is_current=True).filter(
        Q(submission__is_removed=False,submission__status__in=['pending','approved'],state__in=['reserved','qualified']) |
        Q(photo_submission__isnull=False,photo_submission__month=month,photo_submission__participant_id=F('participant_id'))).select_related('submission','photo_submission','category').defer('photo_submission__discord_message_url').order_by('pk')
    states={}
    for claim in claims:
        target=claim.photo_submission if claim.photo_submission_id else claim.submission
        state=claim.state
        if claim.photo_submission_id:
            if target.status=='approved' and state!='qualified':continue
            if target.status in {'rejected','withdrawn','voided'}:state=target.status
        elif state=='qualified' and target.status!='approved':continue
        key=(claim.participant_id,claim.prompt_id)
        existing=states.get(key)
        if existing and existing[0] in {'reserved','qualified'} and state not in {'reserved','qualified'}:continue
        states[key]=(state,target,claim.earning_evidence.get('amount'),bool(claim.photo_submission_id))
    awards={(a.participant_id,a.category_id):a for a in PromptCompletion.objects.filter(category_id__in=category_ids,participant_id__in=ids)}
    teams={a.participant_id:a.team for a in TeamAssignment.objects.filter(month=month,participant_id__in=ids,ended_at__isnull=True).select_related('team')}
    result=[]
    for enrollment in readers:
        for category in configured:
            required={p['id'] for p in category.frozen_prompt_set} or {p.pk for p in category.prompts.all() if p.is_active}
            prompts=[]
            for prompt in category.prompts.all():
                if prompt.pk not in required and category.kind=='bingo':continue
                state,book,earned_points,is_photo=states.get((enrollment.participant_id,prompt.pk),('available',None,None,prompt.submission_type=='photo'))
                prompts.append({'number':prompt.number,'description':prompt.description,'points':prompt.points,
                    'status':{'reserved':'Pending confirmation','qualified':'Completed','available':'Available','rejected':'Rejected','withdrawn':'Withdrawn','voided':'Voided'}[state] if prompt.pk in required else 'Retired · Not required',
                    'state':state,'submission':None if is_photo else book,'photo':book if is_photo else None,
                    'kind':'SH' if is_photo else 'BOOK','earned_points':earned_points,'required':prompt.pk in required})
            completed=sum(p['state']=='qualified' and p['required'] for p in prompts);pending=sum(p['state']=='reserved' and p['required'] for p in prompts)
            award=awards.get((enrollment.participant_id,category.pk))
            team=teams.get(enrollment.participant_id)
            if team is None and (not enrollment.is_active or enrollment.awaiting_scoring_team):team=enrollment.credited_team
            result.append({'reader':enrollment.participant,'team':team,'category':category,
                'prompts':prompts,'completed':completed,'pending':pending,'remaining':len(required)-completed-pending,'total':len(required),
                'bonus_earned':bool(award and award.is_active),'earned_bonus':award.bonus_amount_snapshot if award else None,
                'complete':bool(required and completed==len(required))})
    return result
