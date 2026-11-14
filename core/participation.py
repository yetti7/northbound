from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import AuditEvent, ChallengeMonth, ChallengeStaffAssignment, MonthEnrollment, ParticipationPeriod, TeamAssignment
from .permissions import can_manage_team_assignments, can_operate_challenge, host_scope_allows_reader, is_challenge_host, restricted_host_team_ids


def _lock_month(month_id):
    return ChallengeMonth.objects.select_for_update().get(pk=month_id)


def ensure_participation_period(enrollment, *, reason, at):
    """Record eligibility while the caller holds the Challenge and enrollment locks."""
    if not enrollment.is_active or enrollment.awaiting_scoring_team:
        raise ValidationError("Current Reader eligibility is required.")
    period, _ = enrollment.participation_periods.get_or_create(
        ended_at__isnull=True, defaults={"started_at": at, "start_reason": reason},
    )
    return period


def _check_host_roster_change(actor, month, participant, *, destination=None):
    teams = restricted_host_team_ids(actor, month)
    if teams is None:
        return
    if (participant.user_id == actor.pk
            or not host_scope_allows_reader(actor, month, participant.pk)
            or (destination is not None and destination not in teams)):
        raise PermissionDenied("This roster change is outside your Host team authority.")


def _validate_participant(month, participant):
    if participant.group_id != month.group_id:
        raise ValidationError("The participant must belong to the same reading group.")
    if not participant.is_active or participant.user.is_superuser:
        raise ValidationError("Challenge participation requires an active normal Group membership.")
    if month.staff_assignments.filter(
        membership=participant,
        role=ChallengeStaffAssignment.Role.FLOATER,
        ended_at__isnull=True,
    ).exists():
        raise ValidationError("End this member's active Floater assignment before activating them as a Reader.")


@transaction.atomic
def activate_participation(*, month, participant, actor, origin, agreement_accepted=False, agreement_version=''):
    month = _lock_month(month.pk)
    _validate_participant(month, participant)
    if origin == MonthEnrollment.Origin.STAFF and not can_operate_challenge(actor, month):
        raise PermissionDenied("Current Challenge roster authority is required.")
    if origin == MonthEnrollment.Origin.STAFF and restricted_host_team_ids(actor, month) is not None:
        if not MonthEnrollment.objects.filter(month=month, participant=participant, is_active=True).exists():
            raise PermissionDenied("A team-scoped Host cannot add unassigned Readers.")
        _check_host_roster_change(actor, month, participant)
    new_agreement = month.registration_agreement_enabled and not MonthEnrollment.objects.filter(month=month, participant=participant).exists()
    if new_agreement:
        if (getattr(actor, 'pk', None) != participant.user_id or origin != MonthEnrollment.Origin.SELF
            or not getattr(actor, 'is_active', False)):
            raise ValidationError('This Reader must self-register and accept the Challenge Agreement before being added to the roster or a team.')
        if not month.registration_is_open or month.status not in {'upcoming', 'active'}:
            raise ValidationError('Registration is not currently available for this Challenge.')
        from .registration_agreements import validate_acceptance
        validate_acceptance(month=month, actor=actor, accepted=agreement_accepted, version=agreement_version)
    enrollment, created = MonthEnrollment.objects.select_for_update().get_or_create(
        month=month,
        participant=participant,
        defaults={"enrolled_by": actor, "origin": origin, "is_active": True},
    )
    if created and new_agreement:
        from .registration_agreements import record_acceptance
        record_acceptance(enrollment=enrollment, actor=actor, accepted=agreement_accepted, version=agreement_version)
    reactivated = not created and not enrollment.is_active
    if reactivated:
        enrollment.is_active = True
        enrollment.inactive_reason = ""
        enrollment.inactivated_at = None
        enrollment.inactivated_by = None
        enrollment.awaiting_scoring_team = True
        enrollment.host_scope_restored = False
        enrollment.save(
            update_fields=["is_active", "inactive_reason", "inactivated_at", "inactivated_by", "host_scope_restored", "awaiting_scoring_team"]
        )

    if created:
        ensure_participation_period(
            enrollment, reason=ParticipationPeriod.StartReason.ENROLLMENT, at=enrollment.enrolled_at,
        )
    if created or reactivated:
        self_service = origin == MonthEnrollment.Origin.SELF
        if reactivated:
            action = "participation.self_reactivated" if self_service else "participation.staff_reactivated"
            verb = "Reactivated"
        else:
            action = "participation.self_registered" if self_service else "participation.staff_created"
            verb = "Registered" if self_service else "Added"
        AuditEvent.objects.create(
            actor=actor,
            group=month.group,
            action=action,
            object_type="MonthEnrollment",
            object_id=str(enrollment.pk),
            summary=f"{verb} {participant.user.username} for {month.name}.",
        )
        from .botm_matching import synchronize_reader
        synchronize_reader(month=month, participant=participant)
        from .personal_tbr_matching import synchronize_reader as synchronize_personal_tbr_reader
        synchronize_personal_tbr_reader(month=month, participant=participant)
        from .immersive import synchronize_reader_awards
        synchronize_reader_awards(month=month, participant=participant)
    return enrollment, created, reactivated


@transaction.atomic
def end_team_assignment(*, assignment, actor, reason, audit=True, ordinary_staff_change=True):
    # Ordinary assignment edits are the default. Existing withdrawal/recovery
    # workflows explicitly retain their separate authorization paths.
    month = _lock_month(assignment.month_id)
    assignment = TeamAssignment.objects.select_for_update().select_related(
        "month__group", "participant", "team"
    ).get(pk=assignment.pk)
    if ordinary_staff_change:
        if not can_manage_team_assignments(actor, month, assignment.participant):
            raise PermissionDenied("Current team assignment authority is required.")
    else:
        _check_host_roster_change(actor, month, assignment.participant)
    if assignment.ended_at is not None:
        return assignment, False
    from .models import end_active_team_leader_assignments

    end_active_team_leader_assignments(
        month=assignment.month,
        participant=assignment.participant,
        team=assignment.team,
        actor=actor,
        reason=reason,
    )
    assignment.ended_at = timezone.now()
    assignment.ended_by = actor
    assignment.save(update_fields=["ended_at", "ended_by"])
    if month.status not in {month.Status.COMPLETED, month.Status.ARCHIVED}:
        MonthEnrollment.objects.filter(
            month=month, participant=assignment.participant, is_active=True,
        ).update(credited_team_id=assignment.team_id)
    if audit:
        AuditEvent.objects.create(
            actor=actor,
            group=assignment.month.group,
            action="team_assignment.ended",
            object_type="TeamAssignment",
            object_id=str(assignment.pk),
            summary=(
                f"Ended {assignment.participant.user.username}'s assignment to {assignment.team.name} "
                f"for {assignment.month.name}. Reason: {reason}."
            ),
        )
    return assignment, True


@transaction.atomic
def deactivate_participation(*, enrollment, actor, reason, note="", ordinary_staff_removal=False):
    month = _lock_month(enrollment.month_id)
    enrollment = MonthEnrollment.objects.select_for_update().select_related(
        "month__group", "participant"
    ).get(pk=enrollment.pk)
    target_is_host = is_challenge_host(enrollment.participant.user, month)
    if target_is_host and actor.pk == enrollment.participant.user_id:
        raise PermissionDenied("Hosts cannot remove their own Reader enrollment.")
    _check_host_roster_change(actor, month, enrollment.participant)
    if reason == MonthEnrollment.InactiveReason.REMOVED and (
        not getattr(actor, "is_active", False) or not can_operate_challenge(actor, month)
    ):
        raise PermissionDenied("Current Challenge roster authority is required.")
    if ordinary_staff_removal and (
        reason != MonthEnrollment.InactiveReason.REMOVED
        or not actor.is_active or not can_operate_challenge(actor, month)
        or month.status not in {month.Status.DRAFT, month.Status.UPCOMING, month.Status.ACTIVE}
    ):
        raise PermissionDenied("Current Challenge roster authority is required.")
    if not enrollment.is_active:
        return enrollment, False
    current_assignments = list(
        TeamAssignment.objects.select_for_update().filter(
            month=enrollment.month,
            participant=enrollment.participant,
            ended_at__isnull=True,
        )
    )
    for assignment in current_assignments:
        end_team_assignment(
            assignment=assignment,
            actor=actor,
            reason="the Reader withdrew" if reason == MonthEnrollment.InactiveReason.WITHDRAWN else "staff removed the Reader",
            ordinary_staff_change=False,
        )
    enrollment.is_active = False
    enrollment.inactive_reason = reason
    enrollment.inactivated_at = timezone.now()
    enrollment.inactivated_by = actor
    enrollment.participation_periods.filter(ended_at__isnull=True).update(
        ended_at=enrollment.inactivated_at, end_reason=reason,
    )
    enrollment.host_scope_restored = ordinary_staff_removal and target_is_host
    enrollment.save(update_fields=["is_active", "inactive_reason", "inactivated_at", "inactivated_by", "host_scope_restored"])
    self_withdrawal = reason == MonthEnrollment.InactiveReason.WITHDRAWN
    summary = (
        f"{enrollment.participant.user.username} withdrew from {enrollment.month.name}."
        if self_withdrawal
        else f"Removed {enrollment.participant.user.username} from active participation in {enrollment.month.name}."
    )
    if enrollment.host_scope_restored:
        summary += " Challenge-wide Host authority was restored."
    if note:
        summary += f" Reason: {note}"
    AuditEvent.objects.create(
        actor=actor,
        group=enrollment.month.group,
        action="participation.self_withdrew" if self_withdrawal else "participation.staff_removed",
        object_type="MonthEnrollment",
        object_id=str(enrollment.pk),
        summary=summary,
    )
    from .botm_matching import synchronize_reader
    synchronize_reader(month=enrollment.month, participant=enrollment.participant)
    from .personal_tbr_matching import synchronize_reader as synchronize_personal_tbr_reader
    synchronize_personal_tbr_reader(month=enrollment.month, participant=enrollment.participant)
    from .immersive import synchronize_reader_awards
    synchronize_reader_awards(month=enrollment.month, participant=enrollment.participant)
    return enrollment, True


@transaction.atomic
def assign_participant_to_team(*, month, participant, team, actor, allow_enrollment=False):
    month = _lock_month(month.pk)
    if not can_manage_team_assignments(actor, month, participant):
        raise PermissionDenied("Current team assignment authority is required.")
    _validate_participant(month, participant)
    if not month.teams.filter(pk=team.pk, is_archived=False).exists():
        raise ValidationError("Select an active team in this Challenge.")
    created = reactivated = False
    if allow_enrollment:
        # Enrollment is available only through the existing staff enrollment flow.
        enrollment, created, reactivated = activate_participation(
            month=month, participant=participant, actor=actor, origin=MonthEnrollment.Origin.STAFF,
        )
    else:
        enrollment = MonthEnrollment.objects.select_for_update().filter(
            month=month, participant=participant, is_active=True,
        ).first()
        if enrollment is None:
            raise ValidationError("This Reader must already have an active Challenge enrollment.")
    current = TeamAssignment.objects.select_for_update().filter(
        month=month,
        participant=participant,
        ended_at__isnull=True,
    ).first()
    if current and current.team_id == team.pk:
        record_scoring_team(enrollment, team)
        return current, enrollment, False
    previous_team = current.team if current else None
    if current:
        end_team_assignment(
            assignment=current,
            actor=actor,
            reason=f"the Reader moved to {team.name}",
            ordinary_staff_change=True,
        )
    assignment = TeamAssignment.objects.create(
        month=month,
        participant=participant,
        team=team,
        assigned_by=actor,
    )
    record_scoring_team(enrollment, team)
    action = "team_assignment.moved" if previous_team else "team_assignment.created"
    summary = (
        f"Moved {participant.user.username} from {previous_team.name} to {team.name} for {month.name}."
        if previous_team
        else f"Assigned {participant.user.username} to {team.name} for {month.name}."
    )
    AuditEvent.objects.create(
        actor=actor,
        group=month.group,
        action=action,
        object_type="TeamAssignment",
        object_id=str(assignment.pk),
        summary=summary,
    )
    return assignment, enrollment, created or reactivated


def record_scoring_team(enrollment, team):
    """Update the locked enrollment after a successful team assignment."""
    reason = (ParticipationPeriod.StartReason.TEAM_ASSIGNMENT if enrollment.awaiting_scoring_team
              else ParticipationPeriod.StartReason.OBSERVED)
    enrollment.credited_team = team
    enrollment.awaiting_scoring_team = False
    enrollment.save(update_fields=["credited_team", "awaiting_scoring_team"])
    ensure_participation_period(enrollment, reason=reason, at=timezone.now())
