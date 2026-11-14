"""Verified Photo decisions using the shared prompt provenance and completion ledger."""
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone
from .models import (ChallengeMonth, MonthEnrollment, PromptClaim, PromptCompletion,
    ModifierProvenance, ScavengerPhotoSubmission, TeamAssignment)
from .permissions import challenge_review_scope, can_view_challenge, is_active_floater
from .photo_submissions import can_review_photo, _audit, _prompt
from .prompt_configuration import automation_excluded
from .prompt_reservations import atomic_reservations, require_ready, category_available

SALT='northbound.scavenger-photo-review.v1'


def scope_reviewable_photos(actor,month,query=None):
    query=query if query is not None else ScavengerPhotoSubmission.objects.all()
    if (not actor.is_authenticated or not actor.is_active or not can_view_challenge(actor,month)
            or not category_available(month,'scavenger')):
        return query.none()
    scope=challenge_review_scope(actor,month)
    if scope is None:return query.none()
    query=query.filter(month=month)
    if scope[0]=='team':
        query=query.filter(participant__team_assignments__month=month,
            participant__team_assignments__team_id__in=scope[1],participant__team_assignments__ended_at__isnull=True,
            participant__month_enrollments__month=month,participant__month_enrollments__is_active=True).distinct()
    return query


def pending_photo_review_candidates(month):
    """Unscoped actionable candidates shared by reviewer and private Group queues."""
    query = ScavengerPhotoSubmission.objects.filter(month=month, status='pending',
        prompt_claim__is_current=True, prompt_claim__state='reserved',
        prompt_claim__category__enabled=True)
    return query if category_available(month, 'scavenger') else query.none()


def pending_review_photos(actor,month):
    query = pending_photo_review_candidates(month)
    if not actor.is_authenticated or not actor.is_active or not query.exists():
        return query.none()
    return scope_reviewable_photos(actor,month,query)


def _state(photo):
    return json.loads(json.dumps({
        'photo':list(ScavengerPhotoSubmission.objects.filter(pk=photo.pk).values()),
        'claims':list(PromptClaim.objects.filter(category__month_id=photo.month_id,participant_id=photo.participant_id).order_by('pk').values(
            'id','category_id','prompt_id','state','is_current','selection_evidence','earning_evidence','reviewed_by_id','reviewed_at',
            'submission__status','submission__is_removed','photo_submission__status')),
        'category':list(photo.month.prompt_categories.filter(kind='scavenger').order_by('pk').values()),
        'prompts':list(photo.month.prompt_categories.filter(kind='scavenger').order_by('prompts__pk').values('prompts__id','prompts__revision','prompts__points','prompts__is_active','prompts__submission_type')),
        'enrollment':list(MonthEnrollment.objects.filter(month_id=photo.month_id,participant_id=photo.participant_id).values()),
        'period':list(photo.participant.month_enrollments.filter(month=photo.month).order_by('participation_periods__pk').values('participation_periods__id','participation_periods__started_at','participation_periods__ended_at','participation_periods__end_reason')),
        'teams':list(TeamAssignment.objects.filter(month_id=photo.month_id,participant_id=photo.participant_id).order_by('pk').values()),
        'awards':list(PromptCompletion.objects.filter(category__month_id=photo.month_id,participant_id=photo.participant_id).order_by('pk').values()),
        'provenance':list(ModifierProvenance.objects.filter(month_id=photo.month_id,participant_id=photo.participant_id,source_type__in=['scavenger_prompt','scavenger_completion']).order_by('pk').values()),
        'group_timezone':photo.month.group.timezone,'lifecycle':photo.month.status,'excluded':automation_excluded(photo.month),
    },sort_keys=True,default=str))


def photo_review_version(photo,actor):
    return signing.dumps({'photo':photo.pk,'actor':actor.pk,'state':_state(photo)},salt=SALT,compress=True)


def _validate_version(version,photo,actor):
    try: data=signing.loads(version,salt=SALT,max_age=1800)
    except (signing.BadSignature,TypeError):raise ValidationError('This review expired or changed. Reload before reviewing.') from None
    if data!={'photo':photo.pk,'actor':actor.pk,'state':_state(photo)}:
        raise ValidationError('This Photo or its review evidence changed. Reload before reviewing.')


def participation_evidence(photo):
    """Validate original acceptance, never infer it from the Reader's review-time Team."""
    photo.clean()
    participant=photo.participant
    if not participant.is_active or not participant.user.is_active or participant.user.is_superuser or is_active_floater(photo.month,participant.pk):
        raise ValidationError('This Reader is not eligible for Photo points.')
    enrollment=MonthEnrollment.objects.select_for_update().filter(month=photo.month,participant=participant).first()
    period=photo.participation_period
    assignment=photo.accepted_team_assignment
    if not enrollment or period.enrollment_id!=enrollment.pk or not assignment:
        raise ValidationError('Original participation and Team evidence are required for Photo approval.')
    try: zone=ZoneInfo(photo.acceptance_evidence['group_timezone'])
    except (KeyError,ValueError,TypeError,ZoneInfoNotFoundError):raise ValidationError('The captured Challenge timezone is invalid.') from None
    current=list(TeamAssignment.objects.select_for_update().filter(month=photo.month,participant=participant,ended_at__isnull=True))
    if enrollment.awaiting_scoring_team:
        raise ValidationError('Credited Team evidence is unresolved.')
    if enrollment.is_active:
        if len(current)!=1 or (enrollment.credited_team_id and enrollment.credited_team_id!=current[0].team_id):
            raise ValidationError('Current scoring Team attribution is missing or contradictory.')
    else:
        last=TeamAssignment.objects.filter(month=photo.month,participant=participant,ended_at__lte=period.ended_at).order_by('-ended_at','-pk').first() if period.ended_at else None
        if (current or not period.ended_at or period.ended_at!=enrollment.inactivated_at
                or period.end_reason!=enrollment.inactive_reason or not last or last.team_id!=enrollment.credited_team_id):
            raise ValidationError('Departed Reader participation and credited Team evidence cannot be verified.')
    # Team totals deliberately retain the existing current/credited-Team transfer rule.
    return enrollment,zone


def validate_photo_scoring_boundary(claim,actor):
    require_ready()
    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied('An active Photo reviewer is required.')
    stored=PromptClaim.objects.get(pk=claim.pk)
    if any(getattr(stored,key)!=getattr(claim,key) for key in ('photo_submission_id','submission_id','category_id','prompt_id','participant_id','state','is_current','earning_evidence','reviewed_by_id','reviewed_at')):
        raise ValidationError('The Photo claim changed or its earning receipt was modified.')
    photo=ScavengerPhotoSubmission.objects.select_related('month__group','participant__user','participation_period__enrollment','accepted_team_assignment').get(pk=claim.photo_submission_id)
    actor=get_user_model().objects.get(pk=actor.pk) if actor and actor.is_authenticated else None
    action=getattr(claim,'_photo_credit_action','')
    credit_change=action in {'void','restore','correct'}
    if credit_change:
        from .photo_actions import can_change_photo_credit,can_correct_photo,_receipt
        if not (can_correct_photo(actor,photo) if action=='correct' else can_change_photo_credit(actor,photo,action)):raise PermissionDenied('Current Photo credit authority is required.')
        if action != 'correct' or not getattr(claim,'_first_award',False):
            _receipt(photo,claim)
    elif not actor or not can_review_photo(actor,photo):raise PermissionDenied('Current Photo review authority is required.')
    if not category_available(photo.month,'scavenger') or automation_excluded(photo.month):raise ValidationError('Photo scoring is unavailable.')
    expected_status='voided' if action=='void' else 'approved'
    expected_claim='released' if action=='void' else 'qualified'
    if (photo.status!=expected_status or not photo.reviewer_verified or not photo.reviewed_by_id or not photo.reviewed_at
            or claim.category.month_id!=photo.month_id or claim.participant_id!=photo.participant_id
            or claim.category.kind!='scavenger' or claim.prompt.category_id!=claim.category_id
            or claim.prompt.submission_type!='photo' or claim.state!=expected_claim or not claim.is_current
            or claim.reviewed_by_id!=photo.reviewed_by_id or claim.reviewed_at!=photo.reviewed_at):
        raise ValidationError('Verified Photo qualification and ownership are required.')
    if action=='void':
        return None
    enrollment,zone=participation_evidence(photo)
    if (not claim.earning_evidence or claim.earning_evidence.get('effective_date')!=str(timezone.localtime(photo.submitted_at,zone).date())
            or claim.earning_evidence.get('photo_id')!=photo.pk or claim.earning_evidence.get('acceptance')!=photo.acceptance_evidence):
        raise ValidationError('Frozen Photo earning evidence is incomplete or contradictory.')
    return enrollment


@atomic_reservations
def review_photo_submission(*,photo_id,actor,version,decision,reviewer_verified=False):
    require_ready()
    if not actor.is_authenticated or not actor.is_active:raise PermissionDenied('An active reviewer is required.')
    actor=get_user_model().objects.get(pk=actor.pk)
    month_id=ScavengerPhotoSubmission.objects.values_list('month_id',flat=True).get(pk=photo_id)
    month=ChallengeMonth.objects.select_for_update().select_related('group').get(pk=month_id)
    photo=ScavengerPhotoSubmission.objects.select_for_update().select_related(
        'participant__user','participation_period__enrollment','accepted_team_assignment').get(pk=photo_id)
    photo.month=month
    if not can_review_photo(actor,photo):raise PermissionDenied('Current scoped Photo review authority is required.')
    if not category_available(month,'scavenger'):raise ValidationError('Photo review is disabled or unavailable in this Challenge.')
    _validate_version(version,photo,actor)
    if decision not in {'approved','rejected'}:raise ValidationError('Choose Approve or Reject.')
    if decision=='approved' and reviewer_verified is not True:
        raise ValidationError('Confirm that you have verified this photo in the designated Discord channel.')
    claim=PromptClaim.objects.select_for_update().select_related('prompt','category').get(photo_submission=photo)
    if photo.status==decision:
        if decision=='approved':
            validate_photo_scoring_boundary(claim,actor)
            from .prompt_scoring import claim_source_reference
            row=ModifierProvenance.objects.filter(source_type='scavenger_prompt',source_reference=claim_source_reference(claim)).first()
            amount=claim.earning_evidence['amount']
            if (amount and (not row or not row.is_active or row.voided_at or row.amount!=amount or str(row.effective_date)!=claim.earning_evidence['effective_date']
                    or row.submission_id is not None or row.month_id!=month.pk or row.participant_id!=photo.participant_id)) or (not amount and row):
                raise ValidationError('Existing Photo credit differs from its receipt. Historical correction is required.')
        return photo
    if photo.status!='pending' or claim.state!='reserved' or not claim.is_current or claim.earning_evidence:
        raise ValidationError('Only a current Pending Photo reservation may be reviewed. Corrections are a separate workflow.')
    if claim.category.month_id!=month.pk or claim.participant_id!=photo.participant_id:
        raise ValidationError('The Photo reservation ownership is inconsistent.')
    prompt=_prompt(month,photo.participant_id,claim.prompt_id,photo_id=photo.pk)
    if prompt.category_id!=claim.category_id:raise ValidationError('The reservation category changed.')
    now=timezone.now();late=False
    if decision=='approved':
        enrollment,zone=participation_evidence(photo);late=not enrollment.is_active
        if ModifierProvenance.objects.filter(source_type='scavenger_prompt',source_reference=f'prompt_claim:{claim.pk}').exists():
            raise ValidationError('Unexpected existing credit requires historical review.')
        amount=claim.selection_evidence.get('points') if late else prompt.points
        if type(amount) is not int or amount<0:raise ValidationError('Captured prompt points are missing or invalid.')
        claim.earning_evidence={'amount':amount,'description':claim.selection_evidence['description'],'revision':prompt.revision,
            'effective_date':str(timezone.localtime(photo.submitted_at,zone).date()),'photo_id':photo.pk,
            'acceptance':photo.acceptance_evidence,'first_qualified_at':now.isoformat(),'reviewer_id':actor.pk}
        claim._first_award=True
    photo.status=decision;photo.reviewer_verified=decision=='approved';photo.reviewed_by=actor;photo.reviewed_at=now
    photo._photo_review_actor=actor;photo.save()
    claim.state='qualified' if decision=='approved' else 'rejected'
    claim.reviewed_by=actor;claim.reviewed_at=now;claim._photo_review_actor=actor;claim.save()
    if decision=='approved':
        from .prompt_scoring import _source,_completion
        _source(claim,True,actor)
        _completion(claim.category,photo.participant_id,actor,photo,late=late)
    _audit(photo,actor,'scavenger_photo.reviewed',decision=decision,reviewer_verified=photo.reviewer_verified,
        reviewed_at=now.isoformat(),claim_id=claim.pk)
    return photo
