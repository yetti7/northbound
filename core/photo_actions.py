"""Scoped Photo metadata corrections and evidence-preserving credit transitions."""
import hashlib
import json
import re
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.utils import timezone
from .models import (ChallengeMonth, Membership, ModifierProvenance, PromptClaim, PromptCompletion, PromptCategory,
                     ScavengerPhotoSubmission, ChallengePrompt, TeamAssignment)
from .permissions import (membership_for, challenge_review_scope, restricted_host_team_ids,
    host_scope_allows_reader, can_view_challenge)
from .photo_submissions import can_review_photo, _audit, edit_pending_photo
from .photo_review import _state, participation_evidence
from .prompt_reservations import atomic_reservations, require_ready, category_available
from .photo_validation import validate_discord_message_url

SALT='northbound.photo-management.v1'


def _scoped_staff(actor,photo,roles):
    if not actor.is_authenticated or not actor.is_active or not can_view_challenge(actor,photo.month):return False
    if actor.is_superuser:return True
    member=membership_for(actor,photo.month.group)
    if not member:return False
    if restricted_host_team_ids(actor,photo.month) is not None:
        return host_scope_allows_reader(actor,photo.month,photo.participant_id)
    if member.role in roles:return True
    scope=challenge_review_scope(actor,photo.month)
    return bool(scope and (scope[0]=='challenge' or TeamAssignment.objects.filter(month=photo.month,participant=photo.participant,
        team_id__in=scope[1],ended_at__isnull=True,participant__month_enrollments__month=photo.month,
        participant__month_enrollments__is_active=True).exists()))


def correction_authority(actor,photo):return _scoped_staff(actor,photo,{'owner'})
def credit_authority(actor,photo):return _scoped_staff(actor,photo,{'owner','moderator'})


def can_correct_photo(actor,photo):
    return (photo.status in {'pending','approved','rejected'} and photo.month.status in {'active','finalizing'}
        and category_available(photo.month,'scavenger') and correction_authority(actor,photo))


def can_change_photo_credit(actor,photo,action):
    # Match ordinary Book void/restoration lifecycle; no Finalizing/historical override.
    return bool(action in {'void','restore'} and photo.month.status=='active'
        and category_available(photo.month,'scavenger') and credit_authority(actor,photo))


def scope_readable_photos(actor,month,query=None):
    query=query if query is not None else ScavengerPhotoSubmission.objects.all()
    query=query.filter(month=month)
    if not actor.is_authenticated or not actor.is_active:return query.none()
    own=Q(participant__user=actor)
    from .prompt_progress import reporting_scope
    scope=reporting_scope(actor,month)
    if scope and scope[0]=='challenge':return query
    if scope:
        return query.filter(own | Q(participant__team_assignments__month=month,
            participant__team_assignments__team_id__in=scope[1],participant__team_assignments__ended_at__isnull=True,
            participant__month_enrollments__month=month,participant__month_enrollments__is_active=True)).distinct()
    return query.filter(own)


def _digest(photo):return hashlib.sha256(json.dumps(_state(photo),sort_keys=True).encode()).hexdigest()

def management_version(photo,actor,operation):
    return signing.dumps({'photo':photo.pk,'actor':actor.pk,'operation':operation,'state':_digest(photo)},salt=SALT)


def _validate(version,photo,actor,operation):
    try: data=signing.loads(version,salt=SALT,max_age=1800)
    except (signing.BadSignature,TypeError):raise ValidationError('This confirmation expired. Reload before continuing.') from None
    if data!={'photo':photo.pk,'actor':actor.pk,'operation':operation,'state':_digest(photo)}:
        raise ValidationError('This Photo or its credit changed. Reload and preview again.')


def _locked(photo_id,actor):
    require_ready()
    if not actor.is_authenticated or not actor.is_active:raise PermissionDenied('An active staff account is required.')
    actor=get_user_model().objects.get(pk=actor.pk)
    month_id=ScavengerPhotoSubmission.objects.values_list('month_id',flat=True).get(pk=photo_id)
    month=ChallengeMonth.objects.select_for_update().select_related('group').get(pk=month_id)
    photo=ScavengerPhotoSubmission.objects.select_for_update().select_related('participant__user','participation_period__enrollment','accepted_team_assignment').defer('discord_message_url').get(pk=photo_id)
    photo.month=month
    return photo,actor


def _receipt(photo,claim):
    earned=claim.earning_evidence
    if not earned or earned.get('photo_id')!=photo.pk or earned.get('acceptance')!=photo.acceptance_evidence:
        raise ValidationError('The original Photo earning receipt is missing or inconsistent.')
    from .prompt_scoring import claim_source_reference
    row=ModifierProvenance.objects.select_for_update().filter(source_type='scavenger_prompt',source_reference=claim_source_reference(claim)).first()
    if type(earned.get('amount')) is not int or earned['amount']<0:raise ValidationError('The frozen amount is invalid.')
    if earned['amount']:
        if (not row or row.month_id!=photo.month_id or row.participant_id!=photo.participant_id or row.submission_id
                or row.amount!=earned['amount'] or str(row.effective_date)!=earned.get('effective_date') or row.voided_at):
            raise ValidationError('The original prompt provenance is missing, voided, or inconsistent.')
    elif row:raise ValidationError('Zero-point credit has unexpected provenance.')
    return row


@atomic_reservations
def correct_photo(*,photo_id,actor,version,prompt_id,discord_message_url='',preview=None):
    photo,actor=_locked(photo_id,actor)
    if not can_correct_photo(actor,photo):raise PermissionDenied('Current scoped Photo correction authority is required.')
    _validate(version,photo,actor,'correct')
    validate_discord_message_url(discord_message_url)
    claim=PromptClaim.objects.select_for_update().get(photo_submission=photo)
    if prompt_id!=claim.prompt_id:
        if photo.status=='approved':
            return _apply_approved_prompt_correction(photo,actor,prompt_id,discord_message_url,preview)
        if photo.status!='pending':raise ValidationError('Only Pending prompt selections may be changed.')
        return edit_pending_photo(photo_id=photo.pk,actor=actor,version=photo.version,prompt_id=prompt_id,
            discord_message_url=discord_message_url,staff=True)
    if photo.discord_message_url==discord_message_url:return photo
    photo.discord_message_url=discord_message_url;photo._photo_correction_actor=actor;photo.save()
    _audit(photo,actor,'scavenger_photo.corrected',fields=['discord_message_url'],link_changed=True)
    return photo


def _approved_correction_receipts(photo,claim):
    if (not claim.is_current or claim.state!='qualified' or not photo.reviewer_verified
            or claim.reviewed_by_id!=photo.reviewed_by_id or claim.reviewed_at!=photo.reviewed_at):
        raise ValidationError('The current verified qualification is inconsistent. No repair was attempted.')
    row=_receipt(photo,claim)
    if row and not row.is_active:raise ValidationError('The current prompt receipt is inactive.')
    award=PromptCompletion.objects.select_for_update().filter(category=claim.category,participant=photo.participant).first()
    rows=list(ModifierProvenance.objects.select_for_update().filter(month=photo.month,participant=photo.participant,source_type='scavenger_completion'))
    if not award:
        if rows:raise ValidationError('Orphaned completion provenance requires explicit historical review.')
    else:
        if award.frozen_prompt_set!=claim.category.frozen_prompt_set or len(rows)!=(1 if award.bonus_amount_snapshot else 0):
            raise ValidationError('The frozen completion receipt is inconsistent.')
        if rows:
            bonus=rows[0]
            if (bonus.source_reference!=f'prompt_completion:{award.pk}' or bonus.submission_id
                    or bonus.amount!=award.bonus_amount_snapshot or bonus.effective_date!=award.effective_date
                    or bonus.voided_at or bonus.is_active!=award.is_active):
                raise ValidationError('The completion provenance differs from its frozen award.')
    return row


def _correction_plan(photo,actor,prompt_id,discord_message_url):
    from django.db.models import Q
    claim=photo.prompt_claim
    prompt=ChallengePrompt.objects.filter(pk=prompt_id,category=claim.category,submission_type='photo',is_active=True).first()
    conflict=PromptClaim.objects.filter(prompt_id=prompt_id,participant_id=photo.participant_id,is_current=True,
        state__in=['reserved','qualified']).exclude(pk=claim.pk).exists()
    award=PromptCompletion.objects.filter(category=claim.category,participant=photo.participant).first()
    required={p['id'] for p in claim.category.frozen_prompt_set}
    qualified=set(PromptClaim.objects.filter(category=claim.category,participant=photo.participant,is_current=True,state='qualified').filter(
        Q(submission__status='approved',submission__is_removed=False)|Q(photo_submission__status='approved')).values_list('prompt_id',flat=True))
    after=(qualified-{claim.prompt_id})|{prompt_id}
    blocked=''
    if not prompt:blocked='The destination must be an active Photo prompt in this Challenge.'
    elif conflict:blocked='Another activity holds the destination prompt reservation.'
    elif photo.status=='approved' and (claim.prompt_id not in required or prompt_id not in required):
        blocked='The association is outside the frozen required set; explicit historical recovery review is required.'
    before_complete=bool(required and qualified==required)
    after_complete=bool(required and after==required)
    if photo.status=='approved' and bool(award and award.is_active)!=before_complete:
        blocked='Stored completion and current qualifications disagree; no automatic repair is allowed.'
    old_amount=claim.earning_evidence.get('amount',0)
    before_bonus=award.bonus_amount_snapshot if award and award.is_active else 0
    after_bonus=(award.bonus_amount_snapshot if award else claim.category.completion_bonus) if after_complete else 0
    identity_change=photo.status=='approved' and prompt_id!=claim.prompt_id
    new_amount=prompt.points if prompt and identity_change else old_amount
    impact={'old_points':old_amount,'new_points':new_amount,'prompt_delta':new_amount-old_amount,
        'bonus_before':before_bonus,'bonus_after':after_bonus,'bonus_delta':after_bonus-before_bonus,
        'net':new_amount-old_amount+after_bonus-before_bonus}
    binding={'photo':photo.pk,'actor':actor.pk,'state':_digest(photo),'prompt':prompt_id,
        'url_hash':hashlib.sha256(discord_message_url.encode()).hexdigest(),'impact':impact}
    return {'current':claim.selection_evidence.get('description',claim.prompt.description),'proposed':prompt.description if prompt else 'Unavailable prompt',
        'current_number':claim.prompt.number,'destination_number':prompt.number if prompt else None,
        'awarded':old_amount,'configured':prompt.points if prompt else None,'completion':before_bonus,
        **impact,'blocked':blocked,'effect':'No changes will be applied.' if blocked else ('Replace prompt credit with the destination value; preserve the submission date and verified review.' if identity_change else 'Preserve qualification and frozen credit; only the requested metadata changes.'),
        'binding':binding}


@atomic_reservations
def correction_preview(photo,actor,*,prompt_id,discord_message_url=None):
    photo,actor=_locked(photo.pk,actor)
    if not can_correct_photo(actor,photo):raise PermissionDenied
    url=photo.discord_message_url if discord_message_url is None else discord_message_url
    validate_discord_message_url(url)
    plan=_correction_plan(photo,actor,prompt_id,url)
    if photo.status=='approved':
        participation_evidence(photo)
        _approved_correction_receipts(photo,photo.prompt_claim)
    plan['token']=signing.dumps(plan['binding'],salt=SALT+'.preview',compress=True)
    return plan


def validate_approved_claim_correction(claim,old,actor):
    from copy import deepcopy
    actor=get_user_model().objects.get(pk=actor.pk)
    photo=claim.photo_submission
    if not can_correct_photo(actor,photo) or photo.status!='approved' or old.state!='qualified' or claim.state!='qualified':
        raise PermissionDenied('Current Approved Photo correction authority is required.')
    old_receipt=deepcopy(old.earning_evidence);old_receipt.pop('history',None)
    expected_history=deepcopy(old.earning_evidence.get('history',[]))+[{'earning':old_receipt,'selection':old.selection_evidence}]
    revision=old.earning_evidence.get('correction_revision',0)+1
    new=claim.earning_evidence
    if (new.get('history')!=expected_history or new.get('correction_revision')!=revision
            or new.get('source_reference')!=f'prompt_claim:{claim.pk}:correction:{revision}'
            or new.get('amount')!=claim.prompt.points or new.get('correction_actor_id')!=actor.pk
            or any(new.get(k)!=old.earning_evidence.get(k) for k in ('photo_id','acceptance','effective_date','reviewer_id','first_qualified_at'))
            or any(getattr(claim,k)!=getattr(old,k) for k in ('reviewed_by_id','reviewed_at'))
            or claim.prompt.category_id!=claim.category_id or claim.prompt.submission_type!='photo' or not claim.prompt.is_active):
        raise ValidationError('The explicit correction must preserve frozen history and verification while freezing the destination amount.')
    if claim.selection_evidence.get('prompt_id')!=claim.prompt_id or claim.selection_evidence.get('points')!=claim.prompt.points:
        raise ValidationError('The destination selection snapshot is invalid.')
    require_ready()
    participation_evidence(photo)


def _apply_approved_prompt_correction(photo,actor,prompt_id,url,preview):
    from copy import deepcopy
    from .prompt_scoring import _source,_completion
    claim=PromptClaim.objects.select_for_update().select_related('prompt','category').get(photo_submission=photo)
    claim.category=PromptCategory.objects.select_for_update().get(pk=claim.category_id)
    prompt=ChallengePrompt.objects.select_for_update().filter(pk=prompt_id).first()
    plan=_correction_plan(photo,actor,prompt_id,url)
    if plan['blocked']:raise ValidationError(plan['blocked'])
    try:payload=signing.loads(preview,salt=SALT+'.preview',max_age=600)
    except (signing.BadSignature,TypeError):raise ValidationError('Preview this Approved correction before confirming.') from None
    if payload!=plan['binding']:raise ValidationError('This preview is stale or its requested changes differ. Preview again.')
    participation_evidence(photo)
    old_row=_approved_correction_receipts(photo,claim)
    old=deepcopy(claim.earning_evidence);old_selection=deepcopy(claim.selection_evidence)
    revision=old.get('correction_revision',0)+1
    reference=f'prompt_claim:{claim.pk}:correction:{revision}'
    if ModifierProvenance.objects.filter(source_type='scavenger_prompt',source_reference=reference).exists():
        raise ValidationError('A destination receipt already exists. No repair was attempted.')
    if old_row:old_row.is_active=False;old_row.save(update_fields=['is_active'])
    now=timezone.now();history=deepcopy(old.get('history',[]));old_without_history=deepcopy(old);old_without_history.pop('history',None)
    history.append({'earning':old_without_history,'selection':old_selection})
    new={**old,'amount':prompt.points,'description':prompt.description,'revision':prompt.revision,
        'source_reference':reference,'correction_revision':revision,'history':history,
        'awarded_at':now.isoformat(),'correction_actor_id':actor.pk,'corrected_at':now.isoformat()}
    photo.discord_message_url=url;photo._photo_correction_actor=actor;photo.save()
    claim.prompt=prompt;claim.earning_evidence=new
    claim.selection_evidence={**old_selection,'prompt_id':prompt.pk,'number':prompt.number,'description':prompt.description,
        'points':prompt.points,'revision':prompt.revision,'selected_at':now.isoformat(),'selected_by_id':actor.pk}
    claim._photo_credit_actor=actor;claim._photo_credit_action='correct';claim._photo_approved_correction_actor=actor
    claim._first_award=True;claim.save()
    photo._photo_credit_action='correct'
    _source(claim,True,actor);_completion(claim.category,photo.participant_id,actor,photo)
    _audit(photo,actor,'scavenger_photo.prompt_corrected',old_prompt_id=old_selection['prompt_id'],new_prompt_id=prompt.pk,
        old_number=old_selection['number'],new_number=prompt.number,old_award=old['amount'],new_award=prompt.points,
        impact=plan['net'],prompt_delta=plan['prompt_delta'],completion_delta=plan['bonus_delta'],corrected_at=now.isoformat())
    photo.refresh_from_db()
    return photo


@atomic_reservations
def change_photo_credit(*,photo_id,actor,version,action,reason=''):
    photo,actor=_locked(photo_id,actor)
    if not can_change_photo_credit(actor,photo,action):raise PermissionDenied('Ordinary Photo credit changes require scoped authority in an Active Challenge.')
    _validate(version,photo,actor,action)
    claim=PromptClaim.objects.select_for_update().select_related('category','prompt').get(photo_submission=photo)
    target='voided' if action=='void' else 'approved'
    row=_receipt(photo,claim)
    if not claim.is_current or claim.state!=('qualified' if photo.status=='approved' else 'released') or (row and row.is_active!=(photo.status=='approved')):
        raise ValidationError('Photo state and its original credit are inconsistent.')
    completion_rows=list(ModifierProvenance.objects.select_for_update().filter(month=photo.month,participant=photo.participant,source_type='scavenger_completion'))
    award=PromptCompletion.objects.select_for_update().filter(category=claim.category,participant=photo.participant).first()
    if not award and completion_rows:
        raise ValidationError('Orphaned completion provenance requires explicit historical review.')
    if award:
        if len(completion_rows)!=(1 if award.bonus_amount_snapshot else 0):
            raise ValidationError('Completion provenance is duplicated or missing.')
        bonus=ModifierProvenance.objects.select_for_update().filter(source_type='scavenger_completion',source_reference=f'prompt_completion:{award.pk}').first()
        if award.frozen_prompt_set!=claim.category.frozen_prompt_set:
            raise ValidationError('The frozen completion requirement set is inconsistent.')
        if award.bonus_amount_snapshot:
            if (not bonus or bonus.amount!=award.bonus_amount_snapshot or bonus.effective_date!=award.effective_date
                    or bonus.month_id!=photo.month_id or bonus.participant_id!=photo.participant_id or bonus.submission_id
                    or bonus.voided_at or bonus.is_active!=award.is_active):
                raise ValidationError('The original completion receipt is missing or inconsistent.')
        elif bonus:raise ValidationError('Zero-point completion has unexpected provenance.')
    if photo.status==target:
        if bool(row and row.is_active)!=(target=='approved' and bool(claim.earning_evidence['amount'])):
            raise ValidationError('The existing receipt is inconsistent; no repair was attempted.')
        return photo
    if photo.status!=('approved' if action=='void' else 'voided'):
        raise ValidationError('Only an Approved Photo may be voided and only an earned void may be restored.')
    if not photo.reviewer_verified or not photo.reviewed_by_id or not photo.reviewed_at:
        raise ValidationError('Original verified review evidence is required.')
    if action=='restore':
        participation_evidence(photo)
        if not claim.prompt.is_active or claim.prompt.submission_type!='photo':raise ValidationError('The original prompt is no longer eligible.')
        if PromptClaim.objects.filter(participant=photo.participant,prompt=claim.prompt,is_current=True,state__in=['reserved','qualified']).exclude(pk=claim.pk).exists():
            raise ValidationError('Another submission holds the original prompt. Nothing was restored.')
    before=photo.status
    photo.status=target;photo.removed_at=timezone.now() if action=='void' else None
    photo.removed_by=actor if action=='void' else None;photo.removal_reason=reason if action=='void' else ''
    photo._photo_credit_actor=actor;photo._photo_credit_action=action;photo.save()
    claim.state='released' if action=='void' else 'qualified';claim._photo_credit_actor=actor;claim._photo_credit_action=action;claim.save(update_fields=['state'])
    from .prompt_scoring import _source,_completion
    _source(claim,action=='restore',actor)
    _completion(claim.category,photo.participant_id,actor,photo,earned_only=action=='restore')
    safe_reason=re.sub(r'https?://\S+','[link omitted]',reason)[:1000]
    _audit(photo,actor,'scavenger_photo.'+('voided' if action=='void' else 'restored'),before=before,after=target,
        amount=claim.earning_evidence['amount'],reason=safe_reason)
    return photo
