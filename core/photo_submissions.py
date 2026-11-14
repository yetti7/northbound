"""Phase A Photo ownership, participation and shared reservation foundations.

No endpoints, approval, qualification, provenance or score-cache writes.
"""
import json
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone
from .models import (AuditEvent, ChallengeMonth, ChallengePrompt, MonthEnrollment,
                     ParticipationPeriod, PromptClaim, ScavengerPhotoSubmission, TeamAssignment)
from .permissions import (challenge_review_scope, can_view_challenge, membership_for,
                          is_active_floater)
from .prompt_reservations import (atomic_reservations, require_ready, category_available,
                                  _reader_eligible, HOLDING)
from .photo_validation import validate_discord_message_url


def can_review_photo(actor, photo):
    # Same scope as ordinary book review, including ordinary self-approval.
    if not actor.is_authenticated or not actor.is_active or not can_view_challenge(actor, photo.month):
        return False
    scope = challenge_review_scope(actor, photo.month)
    if not scope or photo.month.status not in {'active', 'finalizing'}:
        return False
    if scope[0] == 'challenge':
        return True
    return TeamAssignment.objects.filter(month=photo.month, participant=photo.participant,
        team_id__in=scope[1], ended_at__isnull=True,
        participant__month_enrollments__month=photo.month,
        participant__month_enrollments__is_active=True).exists()


def can_edit_pending_photo(actor, photo):
    return bool(actor.is_authenticated and actor.is_active and photo.status == 'pending'
        and photo.month.status in {'active','finalizing'}
        and photo.participant.user_id == actor.pk and photo.participant.is_active
        and not is_active_floater(photo.month, photo.participant_id)
        and MonthEnrollment.objects.filter(month=photo.month, participant_id=photo.participant_id, is_active=True, awaiting_scoring_team=False).exists()
        and can_view_challenge(actor, photo.month))


def _audit(photo, actor, action, **detail):
    AuditEvent.objects.create(actor=actor, group=photo.month.group, action=action,
        object_type='ScavengerPhotoSubmission', object_id=str(photo.pk),
        summary=json.dumps({'challenge_id':photo.month_id, 'reader_id':photo.participant_id, **detail}, sort_keys=True))


def available_photo_prompts(month, participant_id, *, photo_id=None):
    if not category_available(month, 'scavenger'):
        return ChallengePrompt.objects.none()
    held = PromptClaim.objects.filter(participant_id=participant_id, is_current=True, state__in=HOLDING)
    if photo_id:
        held = held.exclude(photo_submission_id=photo_id)
    return ChallengePrompt.objects.filter(category__month=month, category__kind='scavenger',
        category__enabled=True, is_active=True, submission_type='photo').exclude(
        pk__in=held.values('prompt_id')).order_by('number')


def _prompt(month, participant_id, prompt_id, *, photo_id=None):
    if type(prompt_id) is not int or prompt_id <= 0:
        raise ValidationError('Choose a valid Photo prompt.')
    prompt = available_photo_prompts(month, participant_id, photo_id=photo_id).select_for_update().filter(pk=prompt_id).first()
    if prompt is None:
        raise ValidationError('This Photo prompt is unavailable, already reserved, or outside this Challenge.')
    return prompt


@atomic_reservations
def create_photo_submission(*, month_id, actor, prompt_id, reader_confirmed, discord_message_url=''):
    require_ready()
    if not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied('An active authenticated Reader is required.')
    actor = get_user_model().objects.get(pk=actor.pk)
    month = ChallengeMonth.objects.select_for_update().select_related('group').get(pk=month_id)
    member = membership_for(actor, month.group)
    if not actor.is_active or member is None or actor.is_superuser or not can_view_challenge(actor, month):
        raise PermissionDenied('Only an eligible Reader may submit their own Photo activity.')
    if month.status != 'active' or not category_available(month, 'scavenger'):
        raise ValidationError('New Photo submissions require an enabled Active Challenge.')
    if reader_confirmed is not True:
        raise ValidationError('Confirm that you posted your photo in the designated Discord channel.')
    validate_discord_message_url(discord_message_url)
    enrollment = MonthEnrollment.objects.select_for_update().filter(month=month, participant=member, is_active=True, awaiting_scoring_team=False).first()
    if not enrollment:
        raise ValidationError('Current Reader participation is required.')
    now = timezone.now()
    photo = ScavengerPhotoSubmission(month=month, participant=member, reader_confirmed=True,
                                     discord_message_url=discord_message_url, submitted_at=now)
    _reader_eligible(photo)
    from .participation import ensure_participation_period
    period = ensure_participation_period(enrollment, reason=ParticipationPeriod.StartReason.OBSERVED, at=now)
    assignments = list(TeamAssignment.objects.select_for_update().filter(month=month, participant=member, ended_at__isnull=True))
    if len(assignments) > 1:
        raise ValidationError('Current Team attribution is ambiguous.')
    assignment = assignments[0] if assignments else None
    if enrollment.credited_team_id and (not assignment or enrollment.credited_team_id != assignment.team_id):
        raise ValidationError('Current Team attribution is inconsistent.')
    photo.participation_period = period
    photo.accepted_team_assignment = assignment
    photo.acceptance_evidence = {'enrollment_id': enrollment.pk, 'period_id': period.pk,
        'team_assignment_id': assignment.pk if assignment else None,
        'team_id': assignment.team_id if assignment else None, 'accepted_at': now.isoformat(),
        'group_timezone': month.group.timezone, 'reader_confirmed': True}
    prompt = _prompt(month, member.pk, prompt_id)
    photo.save()
    # Freeze before capturing the set so Photo and Book evidence have the same meaning.
    prompt.category.freeze_structure()
    PromptClaim.objects.create(category=prompt.category, prompt=prompt, photo_submission=photo,
        participant=member, state='reserved', selection_evidence={
            'prompt_id':prompt.pk, 'number':prompt.number, 'description':prompt.description,
            'points':prompt.points, 'revision':prompt.revision, 'submission_type':'photo',
            'completion_bonus':prompt.category.completion_bonus,
            'required_prompt_set':prompt.category.frozen_prompt_set,
            'selected_at':now.isoformat(), 'selected_by_id':actor.pk})
    _audit(photo, actor, 'scavenger_photo.created', prompt_id=prompt.pk)
    return photo


@atomic_reservations
def transition_photo_reservation(*, photo_id, actor, version, status, reason=''):
    """Pending reservation release/reacquisition only; earned credit is deliberately unavailable."""
    require_ready()
    if not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied('An active authenticated actor is required.')
    initial = ScavengerPhotoSubmission.objects.only('month_id').get(pk=photo_id)
    ChallengeMonth.objects.select_for_update().get(pk=initial.month_id)
    photo = ScavengerPhotoSubmission.objects.select_for_update().select_related('month__group','participant__user').get(pk=photo_id)
    actor = get_user_model().objects.get(pk=actor.pk)
    claim = PromptClaim.objects.select_for_update().select_related('prompt','category').get(photo_submission=photo)
    if type(version) is not int or photo.version != version:
        raise ValidationError('This Photo activity changed. Reload before continuing.')
    if photo.status == 'approved' or claim.state == 'qualified' or claim.earning_evidence:
        raise ValidationError('Earned Photo correction and restoration are not implemented yet.')
    if status == 'withdrawn':
        allowed = can_edit_pending_photo(actor, photo)
    elif status in {'rejected','voided','pending'}:
        allowed = can_review_photo(actor, photo)
        if status == 'pending':
            allowed = allowed and photo.month.status == 'active'
    else:
        raise ValidationError('Photo approval is not implemented yet.')
    if not allowed:
        raise PermissionDenied('Current ownership or scoped staff authority is required.')
    before = photo.status
    if status == 'rejected' and before == 'pending':
        from .photo_review import review_photo_submission,photo_review_version
        return review_photo_submission(photo_id=photo.pk,actor=actor,version=photo_review_version(photo,actor),decision='rejected')
    if status == 'pending' and photo.reviewed_at:
        raise ValidationError('Reviewed Photo restoration belongs to the historical correction workflow. Submit a new attempt instead.')
    if before == status:
        return photo
    if status == 'pending':
        _reader_eligible(photo)
        _prompt(photo.month, photo.participant_id, claim.prompt_id, photo_id=photo.pk)
    photo.status = status
    photo.removed_at = timezone.now() if status in {'withdrawn','voided'} else None
    photo.removed_by = actor if photo.removed_at else None
    photo.removal_reason = reason if photo.removed_at else ''
    photo.save()
    _audit(photo, actor, 'scavenger_photo.reservation_changed', before=before, after=status, claim_id=claim.pk)
    return photo


def can_submit_photo(actor, month):
    if not actor.is_authenticated or not actor.is_active or actor.is_superuser or month.status != 'active':
        return False
    if not can_view_challenge(actor, month) or not category_available(month, 'scavenger'):
        return False
    member = membership_for(actor, month.group)
    if not member or is_active_floater(month, member.pk):
        return False
    return (MonthEnrollment.objects.filter(month=month, participant=member, is_active=True, awaiting_scoring_team=False).exists()
            and available_photo_prompts(month, member.pk).exists())


def validate_pending_photo_selection_edit(claim, actor):
    """Narrow model boundary exception: unearned, current, owner-edited Photo only."""
    actor = get_user_model().objects.get(pk=actor.pk)
    photo = ScavengerPhotoSubmission.objects.select_related('month__group','participant__user').get(pk=claim.photo_submission_id)
    from .photo_actions import can_correct_photo
    if (not (can_edit_pending_photo(actor, photo) or can_correct_photo(actor,photo)) or claim.state != 'reserved' or not claim.is_current
            or claim.earning_evidence or claim.reviewed_at or claim.reviewed_by_id):
        raise PermissionDenied('Only the owner may change an unearned Pending Photo selection.')
    require_ready()
    _reader_eligible(photo)
    _prompt(photo.month, photo.participant_id, claim.prompt_id, photo_id=photo.pk)


@atomic_reservations
def edit_pending_photo(*, photo_id, actor, version, prompt_id, discord_message_url='', staff=False):
    require_ready()
    if not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied('An active authenticated Reader is required.')
    initial = ScavengerPhotoSubmission.objects.only('month_id').get(pk=photo_id)
    ChallengeMonth.objects.select_for_update().get(pk=initial.month_id)
    photo = ScavengerPhotoSubmission.objects.select_for_update().select_related('month__group','participant__user').get(pk=photo_id)
    actor = get_user_model().objects.get(pk=actor.pk)
    from .photo_actions import can_correct_photo
    if not (can_edit_pending_photo(actor, photo) if not staff else photo.status=='pending' and can_correct_photo(actor,photo)):
        raise PermissionDenied('Current Pending Photo edit authority is required.')
    if type(version) is not int or photo.version != version:
        raise ValidationError('This Photo submission changed. Reload before saving.')
    validate_discord_message_url(discord_message_url)
    _reader_eligible(photo)
    claim = PromptClaim.objects.select_for_update().select_related('prompt','category').get(photo_submission=photo)
    prompt = _prompt(photo.month, photo.participant_id, prompt_id, photo_id=photo.pk)
    before = {'prompt_id':claim.prompt_id, 'selection':claim.selection_evidence}
    link_changed = photo.discord_message_url != discord_message_url
    prompt_changed = claim.prompt_id != prompt.pk
    if not prompt_changed and not link_changed:
        return photo
    photo.discord_message_url = discord_message_url
    photo.save()
    if prompt_changed:
        claim.prompt = prompt
        claim.selection_evidence = {'prompt_id':prompt.pk,'number':prompt.number,'description':prompt.description,
            'points':prompt.points,'revision':prompt.revision,'submission_type':'photo',
            'completion_bonus':prompt.category.completion_bonus,'required_prompt_set':prompt.category.frozen_prompt_set,
            'selected_at':timezone.now().isoformat(),'selected_by_id':actor.pk}
        claim._photo_selection_edit_actor = actor
        claim.save(update_fields=['prompt','selection_evidence'])
    _audit(photo, actor, 'scavenger_photo.edited', before=before,
        after={'prompt_id':claim.prompt_id,'selection':claim.selection_evidence}, link_changed=link_changed)
    return photo
