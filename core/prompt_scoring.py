"""Prompt qualifications use the shared modifier ledger and existing score caches."""
import json
from zoneinfo import ZoneInfo
from django.core import signing
from django.core.exceptions import PermissionDenied,ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.db.models import Q
from .models import (AuditEvent,BookSubmission,PromptClaim,PromptCategory,PromptCompletion,ModifierProvenance,MonthEnrollment,ScavengerPhotoSubmission)
from .permissions import can_review_submission,is_active_floater
from .prompt_configuration import automation_excluded
from .prompt_reservations import require_ready,assert_bingo_allowed,atomic_reservations

TYPES={'bingo':('bingo_prompt','bingo_completion'),'scavenger':('scavenger_prompt','scavenger_completion')}
SOURCE_TYPES=tuple(value for values in TYPES.values() for value in values)
CORRECTION_SALT='northbound.prompt-correction-authority.v1'


def identity(book):
    return {key:str(getattr(book,key)) for key in ('title','author','catalog_book_id','catalog_edition_id','completed_on')}


def _audit(book,actor,action,detail):
    AuditEvent.objects.create(actor=actor,group=book.month.group,action=action,object_type='ScavengerPhotoSubmission' if isinstance(book,ScavengerPhotoSubmission) else 'BookSubmission',object_id=str(book.pk),summary=json.dumps({'challenge_id':book.month_id,**detail},sort_keys=True,default=str))


def _allowed(book,category):
    require_ready()
    if automation_excluded(book.month) or not category.enabled:
        raise ValidationError('This Challenge/category is excluded or disabled for prompt qualifications.')
    if book.participant.user.is_superuser or is_active_floater(book.month,book.participant_id):
        raise ValidationError('This Reader cannot receive prompt qualification.')
    if isinstance(book,ScavengerPhotoSubmission):
        if category.kind != 'scavenger' or book.status != 'approved' or not book.reviewer_verified or not book.reviewed_by_id or not book.reviewed_at:
            raise ValidationError('Verified Photo approval is required before qualification.')
        if book.month.status not in {'active','finalizing'}:
            raise ValidationError('Photo scoring is unavailable in this lifecycle stage.')
    elif book.is_removed or book.status!='approved':raise ValidationError('Approve the book before confirming prompt qualification.')
    if category.kind=='bingo':assert_bingo_allowed(book)


def claim_source_reference(claim):
    reference=claim.earning_evidence.get('source_reference') if claim.photo_submission_id else None
    reference=reference or f'prompt_claim:{claim.pk}'
    import re
    if not re.fullmatch(r'prompt_claim:'+str(claim.pk)+r'(?::correction:[1-9][0-9]*)?',reference):
        raise ValidationError('The prompt receipt reference is invalid.')
    return reference


def _source(claim,active,actor=None):
    if claim.photo_submission_id:
        from .photo_review import validate_photo_scoring_boundary
        validate_photo_scoring_boundary(claim, actor)
    source_type=TYPES[claim.category.kind][0];reference=claim_source_reference(claim)
    row=ModifierProvenance.objects.select_for_update().filter(source_type=source_type,source_reference=reference).first()
    earned=claim.earning_evidence
    if row and (row.submission_id,row.month_id,row.participant_id)!=(claim.submission_id,claim.category.month_id,claim.participant_id):
        raise ValidationError('Prompt provenance ownership changed. Historical recovery review is required.')
    if not active:
        if row and row.is_active:row.is_active=False;row.save(update_fields=['is_active'])
        return
    amount=earned['amount']
    if row and (row.amount!=amount or str(row.effective_date)!=earned['effective_date'] or row.voided_at):
        raise ValidationError('Prompt provenance differs from frozen earning evidence. Explicit recovery is required.')
    if amount==0:
        if row:raise ValidationError('A zero-point qualification has unexpected provenance.')
        return
    if row is None:
        if not getattr(claim,'_first_award',False):raise ValidationError('Earned prompt provenance is missing. Explicit recovery is required.')
        ModifierProvenance.objects.create(month_id=claim.category.month_id,participant_id=claim.participant_id,submission_id=claim.submission_id,
            source_type=source_type,source_reference=reference,source_label=earned['description'][:200],source_context=f'Prompt claim {claim.pk}',
            amount=amount,effective_date=earned['effective_date'],applied_by=actor,applied_at=(parse_datetime(earned['awarded_at']) if claim.photo_submission_id and earned.get('awarded_at') else claim.reviewed_at),is_system_generated=True,is_active=True)
    elif not row.is_active:row.is_active=True;row.save(update_fields=['is_active'])


def _completion(category,participant_id,actor,book,*,earned_only=False,late=False):
    category=PromptCategory.objects.select_for_update().get(pk=category.pk)
    if isinstance(book,ScavengerPhotoSubmission):
        from .photo_review import validate_photo_scoring_boundary
        photo_claim=PromptClaim.objects.select_related('category','photo_submission__month__group','photo_submission__participant__user').get(photo_submission=book)
        if photo_claim.category_id!=category.pk:
            raise ValidationError('A Photo cannot trigger another prompt category completion.')
        photo_claim._photo_credit_action=getattr(book,'_photo_credit_action','')
        validate_photo_scoring_boundary(photo_claim,actor)
    claims=list(PromptClaim.objects.select_for_update().filter(category=category,participant_id=participant_id,is_current=True,state='qualified').filter(
        Q(submission__status='approved',submission__is_removed=False) |
        Q(photo_submission__status='approved',photo_submission__reviewer_verified=True)
    ).order_by('prompt_id'))
    required={row['id'] for row in category.frozen_prompt_set}
    complete=bool(required and required=={claim.prompt_id for claim in claims})
    award=PromptCompletion.objects.select_for_update().filter(category=category,participant_id=participant_id).first()
    row=ModifierProvenance.objects.select_for_update().filter(source_type=TYPES[category.kind][1],source_reference=f'prompt_completion:{award.pk}').first() if award else None
    if row and (row.month_id,row.participant_id,row.submission_id)!=(category.month_id,participant_id,None):
        raise ValidationError('Completion provenance ownership differs from its frozen award. Explicit recovery is required.')
    if complete and any(not claim.earning_evidence for claim in claims):
        raise ValidationError('Completion requires verified prompt earning evidence for every qualification.')
    if not complete:
        if award and award.is_active:award.is_active=False;award.save(update_fields=['is_active'])
        if row and row.is_active:row.is_active=False;row.save(update_fields=['is_active'])
        return
    _allowed(book,category)
    first=award is None
    if first:
        if earned_only:raise ValidationError('Historical completion has no frozen award evidence. Explicit recovery is required.')
        amount=category.completion_bonus
        if late:
            rates={claim.selection_evidence.get('completion_bonus') for claim in claims}
            if None in rates or len(rates)!=1:raise ValidationError('Late prompt completion lacks a consistent captured bonus. Historical review is required.')
            amount=rates.pop()
        now=timezone.now();day=timezone.localtime(now,ZoneInfo(book.month.group.timezone)).date()
        award=PromptCompletion.objects.create(category=category,participant_id=participant_id,frozen_prompt_set=category.frozen_prompt_set,
            bonus_amount_snapshot=amount,is_active=True,qualified_at=now,effective_date=day,
            evidence={'claims':[{'id':c.pk,'prompt_id':c.prompt_id,'earning':c.earning_evidence} for c in claims],'reviewer_id':actor.pk if actor else None})
    else:
        if award.frozen_prompt_set!=category.frozen_prompt_set:raise ValidationError('The frozen completion set changed.')
        if row and (row.amount!=award.bonus_amount_snapshot or row.effective_date!=award.effective_date or row.voided_at):
            raise ValidationError('Completion provenance differs from its frozen award; explicit recovery is required.')
        if award.bonus_amount_snapshot and row is None:raise ValidationError('Completion provenance is missing; explicit recovery is required.')
        if not award.is_active:award.is_active=True;award.save(update_fields=['is_active'])
    if award.bonus_amount_snapshot:
        if first:
            ModifierProvenance.objects.create(month=book.month,participant_id=participant_id,source_type=TYPES[category.kind][1],
                source_reference=f'prompt_completion:{award.pk}',source_label=category.get_kind_display()+' completion',source_context=f'Completion {award.pk}',
                amount=award.bonus_amount_snapshot,effective_date=award.effective_date,applied_by=actor,applied_at=award.qualified_at,is_system_generated=True,is_active=True)
        elif not row.is_active:row.is_active=True;row.save(update_fields=['is_active'])
    _audit(book,actor,'prompt.completion_checked',{'category':category.kind,'award_id':award.pk,'qualifying_claim_ids':[c.pk for c in claims]})


def correction_ticket(submission,actor,selections):
    from .prompt_reservations import prompt_state
    return signing.dumps({'submission':submission.pk,'actor':actor.pk,'selections':selections,'state':json.loads(json.dumps(prompt_state(submission),default=str))},salt=CORRECTION_SALT,compress=True)


def validate_ticket(ticket,submission,actor,selections):
    from .prompt_reservations import prompt_state
    try:data=signing.loads(ticket,salt=CORRECTION_SALT,max_age=600)
    except (signing.BadSignature,TypeError):raise ValidationError('Preview earned prompt corrections before applying.') from None
    if data!={'submission':submission.pk,'actor':actor.pk,'selections':selections,'state':json.loads(json.dumps(prompt_state(submission),default=str))}:
        raise ValidationError('The prompt correction preview is stale.')


@atomic_reservations
def review_prompts(submission,actor,decisions,*,correction_authorization=None):
    if not decisions:return
    require_ready()
    from django.contrib.auth import get_user_model
    actor=get_user_model().objects.get(pk=actor.pk)
    from .models import ChallengeMonth
    ChallengeMonth.objects.select_for_update().get(pk=submission.month_id)
    book=BookSubmission.objects.select_for_update().select_related('month__group','participant__user').get(pk=submission.pk)
    if not actor.is_active or not can_review_submission(actor,book) or book.month.status not in {'active','finalizing'}:
        raise PermissionDenied('Current team-scoped prompt review authority is required.')
    if not isinstance(decisions,dict) or set(decisions)-set(TYPES) or any(v not in {'qualified','rejected'} for v in decisions.values()):
        raise ValidationError('Choose supported prompt decisions.')
    claims={c.category.kind:c for c in PromptClaim.objects.select_for_update().filter(submission=book,is_current=True).select_related('category','prompt')}
    if any(c.earning_evidence and c.category.kind in decisions and c.state!=decisions[c.category.kind] for c in claims.values()):
        validate_ticket(correction_authorization,book,actor,{'decisions':decisions})
    for kind,decision in decisions.items():
        claim=claims.get(kind)
        if not claim:raise ValidationError('This prompt selection is no longer current.')
        _allowed(book,claim.category)
        if claim.state==decision:continue
        enrollment=MonthEnrollment.objects.filter(month=book.month,participant=book.participant).first()
        late=bool(enrollment and not enrollment.is_active)
        if is_active_floater(book.month,book.participant_id):raise ValidationError('Floaters cannot receive prompt points.')
        if decision=='qualified':
            from .prompt_reservations import _validate_prompt
            _validate_prompt(book,kind,claim.prompt_id)
            from .late_approval import departure_evidence
            if late and not (book.late_approval_evidence and departure_evidence(book,enrollment)):
                raise ValidationError('Late prompt approval requires verified historical participation evidence.')
            if not enrollment or enrollment.awaiting_scoring_team or not book.participant.is_active or not book.participant.user.is_active or book.participant.user.is_superuser:
                raise ValidationError('Reader eligibility is required for qualification.')
            if claim.earning_evidence and claim.earning_evidence.get('identity')!=identity(book):
                raise ValidationError('The book identity changed after this qualification. Preview removal and select a new claim for review.')
            if not claim.earning_evidence:
                if late and 'points' not in claim.selection_evidence:raise ValidationError('Late prompt approval lacks captured point evidence.')
                claim.earning_evidence={'amount':claim.selection_evidence['points'] if late else claim.prompt.points,
                    'description':claim.selection_evidence['description'],'revision':claim.prompt.revision,
                    'effective_date':str(book.completed_on),'identity':identity(book),'first_qualified_at':timezone.now().isoformat(),'reviewer_id':actor.pk}
                claim._first_award=True
        before=claim.state;claim.state=decision;claim.reviewed_by=actor;claim.reviewed_at=timezone.now();claim.save()
        _source(claim,decision=='qualified',actor)
        _completion(claim.category,claim.participant_id,actor,book,late=late)
        _audit(book,actor,'prompt.reviewed',{'category':kind,'claim_id':claim.pk,'before':before,'after':decision,'earning':claim.earning_evidence})
    from .scoring import refresh_submission_score
    refresh_submission_score(book)


def release_claim(claim,actor,book):
    claim.state='released';claim.is_current=False;claim.save(update_fields=['state','is_current'])
    _source(claim,False,actor);_completion(claim.category,claim.participant_id,actor,book)


@atomic_reservations
def lifecycle(book,previous=None,actor=None):
    claims=list(PromptClaim.objects.select_for_update().filter(submission=book,is_current=True).select_related('category','prompt'))
    restoring=bool(previous and previous['is_removed'] and not book.is_removed)
    target='released' if book.is_removed else 'rejected' if book.status=='rejected' else 'reserved'
    for claim in claims:
        desired='qualified' if restoring and claim.state=='released' and claim.earning_evidence and book.status=='approved' else target
        if claim.state==desired or claim.state=='qualified' and desired=='reserved':continue
        require_ready()
        if automation_excluded(book.month):raise ValidationError('This Challenge is excluded from prompt processing.')
        if desired in {'qualified','reserved'}:
            if desired=='qualified':
                _allowed(book,claim.category)
                if claim.earning_evidence.get('identity')!=identity(book):raise ValidationError('Prompt earning identity changed; historical recovery review is required.')
                from .recovery_scoring import ordinary_restore_preserves_attribution
                if not ordinary_restore_preserves_attribution(book):
                    from copy import copy
                    from .historical_restoration import ordinary_historical_eligible
                    voided=copy(book);voided.is_removed=True;voided.removed_at=previous.get('removed_at')
                    if not ordinary_historical_eligible(voided):
                        raise ValidationError('Prompt historical attribution cannot safely be restored through this path.')
                # Historical/terminal restoration may reuse only its frozen earned prompt, never a new one.
                competing=PromptClaim.objects.filter(participant_id=claim.participant_id,prompt_id=claim.prompt_id,is_current=True,state__in=['reserved','qualified']).exclude(pk=claim.pk).exists()
                if competing or not claim.prompt.is_active:raise ValidationError('The original prompt is unavailable for restoration.')
            else:
                from .prompt_reservations import _validate_prompt
                _validate_prompt(book,claim.category.kind,claim.prompt_id)
        before=claim.state;claim.state=desired;claim.save(update_fields=['state'])
        _source(claim,desired=='qualified',actor)
        late=MonthEnrollment.objects.filter(month=book.month,participant=book.participant,is_active=False).exists()
        _completion(claim.category,claim.participant_id,actor,book,earned_only=restoring and (late or book.month.status in {'completed','archived'}))
        _audit(book,actor,'prompt.lifecycle_changed',{'claim_id':claim.pk,'before':before,'after':desired})
    from .scoring import refresh_submission_score
    refresh_submission_score(book)


def pending_submission_ids(month):
    from .prompt_reservations import reservations_ready
    if not reservations_ready() or automation_excluded(month) or month.status not in {'active','finalizing'}:return PromptClaim.objects.none().values('submission_id')
    return PromptClaim.objects.filter(category__month=month,category__enabled=True,is_current=True,state='reserved',submission__is_removed=False,
        submission__status__in=['pending','approved']).values('submission_id')
