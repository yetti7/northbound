"""Transactional Immersive Reading services; no provider calls or UI dependencies.

Call request_immersive() after saving a new manual/Hardcover submission inside
its existing creation transaction, or independently for an existing submission.
The caller never needs to approve the book first to record a pending request.
"""
import json
from decimal import Decimal, ROUND_HALF_UP

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (AuditEvent, BookSubmission, ChallengeMonth, ImmersiveRequest,
                     Membership, ModifierProvenance, MonthEnrollment)
from .permissions import (can_manage_months, can_operate_challenge, can_review_submission,
                          membership_for, scope_reviewable_submissions, host_scope_allows_reader, restricted_host_team_ids, is_active_floater)


def bonus_pages(base_pages, percent):
    return int((Decimal(base_pages or 0) * Decimal(percent) / 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def _actor(actor):
    if not getattr(actor, 'is_authenticated', False):
        raise PermissionDenied('An active account is required.')
    user = get_user_model().objects.filter(pk=actor.pk).first()
    if user is None or not user.is_active:
        raise PermissionDenied('An active account is required.')
    return user


def _audit(request, actor, action, **details):
    AuditEvent.objects.create(actor=actor, group=request.submission.month.group,
        action=f'immersive.{action}', object_type='ImmersiveRequest', object_id=str(request.pk),
        summary=json.dumps({'submission_id': request.submission_id,
            'captured_percent': request.captured_percent, **details}, default=str, sort_keys=True))


def _submission(submission):
    return BookSubmission.objects.select_for_update().select_related('month__group', 'participant__user').get(pk=submission.pk)


def _book_eligible(submission, *, earned=False):
    return (submission.status == BookSubmission.Status.APPROVED and not submission.is_removed
        and submission.approved_pages is not None and submission.approved_pages > 0
        and (earned or (
            submission.participant.is_active and submission.participant.user.is_active
            and MonthEnrollment.objects.filter(
                month=submission.month, participant=submission.participant, is_active=True, awaiting_scoring_team=False,
            ).exists()
        )))


def _staff(actor, submission):
    if submission.month.status not in {'active', 'finalizing', 'completed'}:
        raise PermissionDenied('Existing requests may be reviewed only in Active, Finalizing or Completed Challenges.')
    member = membership_for(actor, submission.month.group)
    oversight = bool(member and member.role == Membership.Role.OWNER)
    if not (host_scope_allows_reader(actor, submission.month, submission.participant_id)
            and (oversight or can_review_submission(actor, submission))):
        raise PermissionDenied('Submission review authority is required.')
    if actor.pk == submission.participant.user_id:
        raise PermissionDenied('An independent reviewer is required.')


@transaction.atomic
def configure_immersive(*, month, actor, enabled, percent):
    actor = _actor(actor)
    month = ChallengeMonth.objects.select_for_update().select_related('group').get(pk=month.pk)
    if not (can_operate_challenge(actor, month) or can_manage_months(actor, month.group)):
        raise PermissionDenied('Challenge configuration authority is required.')
    if month.status not in {'draft', 'upcoming', 'active'}:
        raise ValidationError('This Challenge is not configurable.')
    if type(enabled) is not bool or type(percent) is not int or not 0 <= percent <= 200:
        raise ValidationError('Use an enabled flag and an integer percentage from 0 to 200.')
    before = {'enabled': month.immersive_enabled, 'percent': month.immersive_bonus_percent}
    after = {'enabled': enabled, 'percent': percent}
    if before != after:
        month.immersive_enabled, month.immersive_bonus_percent = enabled, percent
        month.save(update_fields=['immersive_enabled', 'immersive_bonus_percent'])
        AuditEvent.objects.create(actor=actor, group=month.group, action='immersive.configured',
            object_type='ChallengeMonth', object_id=str(month.pk), summary=json.dumps({'before': before, 'after': after}))
    return month


@transaction.atomic
def request_immersive(*, submission, actor):
    actor = _actor(actor)
    submission = _submission(submission)
    month = ChallengeMonth.objects.select_for_update().get(pk=submission.month_id)
    _validate_new_request(submission, actor, month)
    request = ImmersiveRequest.objects.create(submission=submission, requester=actor,
        captured_percent=month.immersive_bonus_percent)
    _audit(request, actor, 'requested')
    return request


def _late_request_eligible(request):
    from .late_approval import departure_evidence, enrollment_for

    submission = request.submission
    if submission.month.status not in {"active", "finalizing"} or not submission.late_approval_evidence:
        return False
    if not departure_evidence(submission, enrollment_for(submission)):
        return False
    return (
        request.requester_id == submission.participant.user_id
        and submission.accepted_at <= request.requested_at < submission.participation_period.ended_at
    )


@transaction.atomic
def review_immersive(*, request, actor, decision, reason=''):
    if decision not in {'approved', 'rejected', 'revoked'}:
        raise ValidationError('Choose approval, rejection or revocation.')
    actor = _actor(actor)
    ChallengeMonth.objects.select_for_update().get(pk=request.submission.month_id)
    submission = _submission(request.submission)
    request = ImmersiveRequest.objects.select_for_update().get(pk=request.pk, submission=submission)
    request.submission = submission
    _staff(actor, submission)
    if decision == "approved" and is_active_floater(submission.month, submission.participant_id):
        raise ValidationError("Active Floaters cannot receive new Immersive credit.")
    if request.status == decision:
        return request
    if decision == 'revoked':
        if request.status != 'approved':
            raise ValidationError('Only an approved award may be revoked.')
        if not reason.strip():
            raise ValidationError('Enter a revocation reason.')
        request.revoked_by, request.revoked_at, request.revocation_reason = actor, timezone.now(), reason.strip()
        request.is_active = False
    else:
        if request.status != 'pending':
            raise ValidationError('Only pending requests may be reviewed.')
        late = _late_request_eligible(request)
        if late:
            from .permissions import challenge_review_scope
            scope = challenge_review_scope(actor, submission.month)
            if not scope or scope[0] != "challenge":
                raise PermissionDenied("Challenge-wide authority is required for a departed Reader.")
        if not _book_eligible(submission, earned=late):
            raise ValidationError('The underlying book must be approved and eligible before review.')
        request.reviewed_by, request.reviewed_at, request.review_reason = actor, timezone.now(), reason.strip()
    request.status = decision
    request.save()
    from .scoring import refresh_submission_score
    refresh_submission_score(submission)
    request.refresh_from_db()
    _audit(request, actor, decision, reason=reason.strip(), awarded_pages=request.awarded_pages)
    return request


@transaction.atomic
def withdraw_immersive(*, request, actor, reason=''):
    actor = _actor(actor)
    submission = _submission(request.submission)
    request = ImmersiveRequest.objects.select_for_update().get(pk=request.pk, submission=submission)
    request.submission = submission
    if actor.pk != request.requester_id or submission.month.status == 'archived':
        raise PermissionDenied('Only the requesting Reader may withdraw a pending request in a non-archived Challenge.')
    if request.status == 'withdrawn':
        return request
    if request.status != 'pending':
        raise ValidationError('Only a pending request may be withdrawn.')
    request.status, request.withdrawn_at, request.withdrawal_reason = 'withdrawn', timezone.now(), reason.strip()
    request.save()
    _audit(request, actor, 'withdrawn', reason=reason.strip())
    return request


@transaction.atomic
def synchronize_awards(submission, *, earned_only=False, restore_earned=False, late_only=False):
    """Called inside score refresh's transaction; does not recurse into scoring.

    Only Immersive provenance can have its rate-derived amount recalculated.
    Approval and percentage snapshots are preserved, including while suspended.
    """
    submission = _submission(submission)
    if submission.month.status == 'archived' and not earned_only:
        return
    for request in ImmersiveRequest.objects.select_for_update().filter(submission=submission).exclude(status='pending'):
        request.submission = submission
        late = not earned_only and request.status == "approved" and _late_request_eligible(request)
        if late_only and not late:
            continue
        source = f'immersive_request:{request.pk}'
        from .historical_restoration import permits_automatic_source
        if not permits_automatic_source(submission, source):
            continue
        provenance = ModifierProvenance.objects.select_for_update().filter(source_type='immersive', source_reference=source).first()
        if provenance and (provenance.submission_id != submission.pk or provenance.participant_id != submission.participant_id or provenance.month_id != submission.month_id):
            raise ValidationError('Immersive provenance does not belong to its request.')
        before = {'amount': request.awarded_pages, 'active': request.is_active}
        earned = request.is_active or (
            restore_earned and request.awarded_pages is not None
            and provenance is not None and provenance.voided_at is None
        )
        active = request.status == 'approved' and (earned or not earned_only) and _book_eligible(
            submission, earned=earned or late,
        )
        if active:
            request.awarded_pages = bonus_pages(submission.approved_pages, request.captured_percent)
        request.is_active = active
        request.save(update_fields=['awarded_pages', 'is_active'])
        if provenance and not active:
            if provenance.is_active:
                provenance.is_active = False
                provenance.save(update_fields=['is_active'])
        elif request.awarded_pages is not None and active and (provenance is not None or not earned_only):
            defaults = dict(month=submission.month, participant=submission.participant, submission=submission,
                source_label='Immersive Reading', source_context=f'{request.captured_percent}% of approved base pages; request {request.pk}',
                amount=request.awarded_pages, effective_date=submission.completed_on, applied_by=request.reviewed_by,
                applied_at=request.reviewed_at, is_system_generated=True, is_active=active)
            if provenance:
                if provenance.voided_at is not None:
                    raise ValidationError('Voided Immersive provenance requires explicit investigation.')
                changed = any(getattr(provenance, key) != value for key, value in defaults.items())
                if changed:
                    for key, value in defaults.items(): setattr(provenance, key, value)
                    provenance.save(update_fields=list(defaults))
            else:
                ModifierProvenance.objects.create(source_type='immersive', source_reference=source, **defaults)
        after = {'amount': request.awarded_pages, 'active': active}
        if before != after:
            action = ('awarded' if before['amount'] is None else
                'suspended' if before['active'] and not active else
                'restored' if not before['active'] and active else 'recalculated')
            _audit(request, None, action, before=before, after=after)


def pending_review_requests(*, month):
    """Unscoped queue candidates for private Group aggregates and reviewer scoping."""
    query = ImmersiveRequest.objects.filter(submission__month=month, status='pending',
        submission__status='approved', submission__is_removed=False, submission__approved_pages__gt=0,
        submission__participant__is_active=True, submission__participant__user__is_active=True,
        submission__participant__month_enrollments__month=month,
        submission__participant__month_enrollments__is_active=True)
    if month.status not in {"active", "finalizing", "completed"}:
        return query.none()
    return query.distinct()


def review_queue_requests(*, actor, month):
    """Separate Immersive entries for the existing queue, with no book duplicates.

    Includes selected Reader/book relations, captured_percent, and each request's
    calculated_bonus property. Completed reviews use this service without reopening
    the normal book-review queue or changing its lifecycle permissions.
    """
    query = pending_review_requests(month=month).exclude(requester_id=getattr(actor, 'pk', None))
    if not actor.is_authenticated or not actor.is_active or month.status not in {'active', 'finalizing', 'completed'}:
        return query.none()
    member = membership_for(actor, month.group)
    oversight = bool(member and member.role == Membership.Role.OWNER)
    if not oversight or restricted_host_team_ids(actor, month) is not None:
        submissions = scope_reviewable_submissions(actor, month, month.submissions.all())
        query = query.filter(submission__in=submissions)
    return query.select_related('submission__participant__user', 'submission__month__group').distinct()


@transaction.atomic
def create_with_immersive(*, actor, create_submission):
    """Compose an existing submission-creation callable and a pending request.

    The callable performs the normal creation, Theme/TBR/BOTM synchronization and
    auditing. Failure to request rolls its writes back too. No approval is inferred
    from manual versus Hardcover verification. No frontend field is added.
    """
    submission = create_submission()
    return submission, request_immersive(submission=submission, actor=actor)


@transaction.atomic
def synchronize_reader_awards(*, month, participant):
    """Participation transitions suspend/restore only this Reader's awards."""
    from .scoring import refresh_submission_score
    for submission in BookSubmission.objects.filter(month=month, participant=participant,
            immersive_requests__status='approved').distinct().order_by('pk'):
        refresh_submission_score(submission)


def _validate_new_request(submission, actor, month):
    if actor.pk != submission.participant.user_id or not submission.participant.is_active:
        raise PermissionDenied('Only the submitting Reader may request Immersive Reading.')
    if submission.late_approval_evidence:
        raise ValidationError('Late-approved submissions require an Immersive request made before departure.')
    if (month.status != 'active' or not month.immersive_enabled or submission.is_removed
        or not MonthEnrollment.objects.filter(month=month, participant=submission.participant, is_active=True, awaiting_scoring_team=False).exists()):
        raise ValidationError('New Immersive requests require an enabled, Active Challenge and active participation.')
    if (not submission.completed_on or not month.starts_on or not month.ends_on
        or not month.starts_on <= submission.completed_on <= month.ends_on):
        raise ValidationError('The submission completion date must be inside the Challenge schedule.')
    if ImmersiveRequest.objects.filter(submission=submission, status__in=['pending', 'approved']).exists():
        raise ValidationError('A pending or approved Immersive request already exists.')


def can_request_immersive(*, submission, actor):
    if not actor.is_authenticated or not actor.is_active:
        return False
    try:
        _validate_new_request(submission, actor, submission.month)
    except (PermissionDenied, ValidationError):
        return False
    return True


def can_review_immersive(*, submission, actor):
    if not actor.is_authenticated or not actor.is_active:
        return False
    try:
        _staff(actor, submission)
    except PermissionDenied:
        return False
    return True
