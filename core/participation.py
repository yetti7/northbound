from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import AuditEvent, ChallengeStaffAssignment, MonthEnrollment, TeamAssignment


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
def activate_participation(*, month, participant, actor, origin):
    _validate_participant(month, participant)
    enrollment, created = MonthEnrollment.objects.select_for_update().get_or_create(
        month=month,
        participant=participant,
        defaults={"enrolled_by": actor, "origin": origin, "is_active": True},
    )
    reactivated = not created and not enrollment.is_active
    if reactivated:
        enrollment.is_active = True
        enrollment.inactive_reason = ""
        enrollment.inactivated_at = None
        enrollment.inactivated_by = None
        enrollment.save(
            update_fields=["is_active", "inactive_reason", "inactivated_at", "inactivated_by"]
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
            summary=f"{verb} {participant.display_name} for {month.name}.",
        )
        from .botm_matching import synchronize_reader
        synchronize_reader(month=month, participant=participant)
        from .personal_tbr_matching import synchronize_reader as synchronize_personal_tbr_reader
        synchronize_personal_tbr_reader(month=month, participant=participant)
    return enrollment, created, reactivated


@transaction.atomic
def end_team_assignment(*, assignment, actor, reason, audit=True):
    assignment = TeamAssignment.objects.select_for_update().select_related(
        "month__group", "participant", "team"
    ).get(pk=assignment.pk)
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
    if audit:
        AuditEvent.objects.create(
            actor=actor,
            group=assignment.month.group,
            action="team_assignment.ended",
            object_type="TeamAssignment",
            object_id=str(assignment.pk),
            summary=(
                f"Ended {assignment.participant.display_name}'s assignment to {assignment.team.name} "
                f"for {assignment.month.name}. Reason: {reason}."
            ),
        )
    return assignment, True


@transaction.atomic
def deactivate_participation(*, enrollment, actor, reason, note=""):
    enrollment = MonthEnrollment.objects.select_for_update().select_related(
        "month__group", "participant"
    ).get(pk=enrollment.pk)
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
        )
    enrollment.is_active = False
    enrollment.inactive_reason = reason
    enrollment.inactivated_at = timezone.now()
    enrollment.inactivated_by = actor
    enrollment.save(update_fields=["is_active", "inactive_reason", "inactivated_at", "inactivated_by"])
    self_withdrawal = reason == MonthEnrollment.InactiveReason.WITHDRAWN
    summary = (
        f"{enrollment.participant.display_name} withdrew from {enrollment.month.name}."
        if self_withdrawal
        else f"Removed {enrollment.participant.display_name} from active participation in {enrollment.month.name}."
    )
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
    return enrollment, True


@transaction.atomic
def assign_participant_to_team(*, month, participant, team, actor):
    if team.month_id != month.pk:
        raise ValidationError("The team must belong to the selected challenge month.")
    enrollment, created, reactivated = activate_participation(
        month=month,
        participant=participant,
        actor=actor,
        origin=MonthEnrollment.Origin.STAFF,
    )
    current = TeamAssignment.objects.select_for_update().filter(
        month=month,
        participant=participant,
        ended_at__isnull=True,
    ).first()
    if current and current.team_id == team.pk:
        return current, enrollment, False
    previous_team = current.team if current else None
    if current:
        end_team_assignment(
            assignment=current,
            actor=actor,
            reason=f"the Reader moved to {team.name}",
        )
    assignment = TeamAssignment.objects.create(
        month=month,
        participant=participant,
        team=team,
        assigned_by=actor,
    )
    action = "team_assignment.moved" if previous_team else "team_assignment.created"
    summary = (
        f"Moved {participant.display_name} from {previous_team.name} to {team.name} for {month.name}."
        if previous_team
        else f"Assigned {participant.display_name} to {team.name} for {month.name}."
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
