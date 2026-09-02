from dataclasses import replace

from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (
    AuditEvent, BookSubmission, BotmBook, BotmCompletionAward,
    BotmCompletionAwardBook, BotmMatch, ChallengeMonth,
    ChallengeSignupAnswer, ChallengeSignupQuestion, ChallengeStaffAssignment,
    Game, GameRewardApplication, GameRewardRecipient, Membership,
    ModifierProvenance, MonthEnrollment, MonthTheme, PersonalTBR,
    PersonalTBRBook, PersonalTBRCompletionAward,
    PersonalTBRCompletionAwardBook, PersonalTBRMatch, ProgressCheckpoint,
    ProgressCheckpointResult, Team, TeamAssignment, ThemeClaim,
)
from .participation import activate_participation, deactivate_participation, end_team_assignment
from .recovery import (
    RecoveryImpactItem, RecoveryImpactPreview, RecoveryMutationResult,
    RecoveryRequest, execute_recovery_operation,
)


def challenge_purge_impact(month):
    counts = (
        ("Signup questions", ChallengeSignupQuestion.objects.filter(month=month).count()),
        ("Enrollments", MonthEnrollment.objects.filter(month=month).count()),
        ("Signup answers", ChallengeSignupAnswer.objects.filter(enrollment__month=month).count()),
        ("Staff assignments", ChallengeStaffAssignment.objects.filter(month=month).count()),
        ("Teams", Team.objects.filter(month=month).count()),
        ("Team assignments", TeamAssignment.objects.filter(month=month).count()),
        ("Submissions", BookSubmission.objects.filter(month=month).count()),
        ("Themes", MonthTheme.objects.filter(month=month).count()),
        ("Theme claims", ThemeClaim.objects.filter(submission__month=month).count()),
        ("BOTM books", BotmBook.objects.filter(month=month).count()),
        ("BOTM matches", BotmMatch.objects.filter(month=month).count()),
        ("BOTM completion awards", BotmCompletionAward.objects.filter(month=month).count()),
        ("BOTM frozen award books", BotmCompletionAwardBook.objects.filter(award__month=month).count()),
        ("Personal TBR lists", PersonalTBR.objects.filter(enrollment__month=month).count()),
        ("Personal TBR books", PersonalTBRBook.objects.filter(personal_tbr__enrollment__month=month).count()),
        ("Personal TBR matches", PersonalTBRMatch.objects.filter(month=month).count()),
        ("Personal TBR completion awards", PersonalTBRCompletionAward.objects.filter(month=month).count()),
        ("Personal TBR frozen award books", PersonalTBRCompletionAwardBook.objects.filter(award__month=month).count()),
        ("Games", Game.objects.filter(month=month).count()),
        ("Game reward applications", GameRewardApplication.objects.filter(game__month=month).count()),
        ("Game reward recipients", GameRewardRecipient.objects.filter(application__game__month=month).count()),
        ("Modifier provenance", ModifierProvenance.objects.filter(month=month).count()),
        ("Progress checkpoints", ProgressCheckpoint.objects.filter(month=month).count()),
        ("Checkpoint results", ProgressCheckpointResult.objects.filter(checkpoint__month=month).count()),
    )
    warning = (
        "This permanently removes the Challenge and every listed competition record. "
        "Users, Group memberships, shared catalog records, and unrelated Challenges are preserved."
    )
    if month.status != ChallengeMonth.Status.DRAFT:
        warning = f"{month.get_status_display()} Challenge: " + warning
    return RecoveryImpactPreview(
        target_label=month.name,
        items=tuple(RecoveryImpactItem(label, count) for label, count in counts),
        warnings=(warning,),
    )


def purge_challenge(*, month, recovery_request, fail_after_step=None):
    if recovery_request.tier != 3:
        raise ValidationError("Challenge purge requires Tier 3 recovery.")
    request = replace(recovery_request, challenge=None, group=month.group)

    def mutation():
        locked = ChallengeMonth.objects.select_for_update().select_related("group").get(pk=month.pk)
        impact = challenge_purge_impact(locked)

        def remove(step, queryset):
            queryset.delete()
            if fail_after_step == step:
                raise RuntimeError(f"Injected Challenge purge failure after {step}.")

        remove("checkpoint_results", ProgressCheckpointResult.objects.filter(checkpoint__month=locked))
        remove("checkpoints", ProgressCheckpoint.objects.filter(month=locked))
        remove("game_recipients", GameRewardRecipient.objects.filter(application__game__month=locked))
        remove("game_applications", GameRewardApplication.objects.filter(game__month=locked))
        remove("modifier_provenance", ModifierProvenance.objects.filter(month=locked))
        remove("tbr_award_books", PersonalTBRCompletionAwardBook.objects.filter(award__month=locked))
        remove("tbr_awards", PersonalTBRCompletionAward.objects.filter(month=locked))
        remove("tbr_matches", PersonalTBRMatch.objects.filter(month=locked))
        remove("botm_award_books", BotmCompletionAwardBook.objects.filter(award__month=locked))
        remove("botm_awards", BotmCompletionAward.objects.filter(month=locked))
        remove("botm_matches", BotmMatch.objects.filter(month=locked))
        remove("theme_claims", ThemeClaim.objects.filter(submission__month=locked))
        remove("games", Game.objects.filter(month=locked))
        remove("personal_tbrs", PersonalTBR.objects.filter(enrollment__month=locked))
        remove("botm_books", BotmBook.objects.filter(month=locked))
        remove("submissions", BookSubmission.objects.filter(month=locked))
        remove("signup_answers", ChallengeSignupAnswer.objects.filter(enrollment__month=locked))
        remove("signup_questions", ChallengeSignupQuestion.objects.filter(month=locked))
        remove("staffing", ChallengeStaffAssignment.objects.filter(month=locked))
        remove("team_assignments", TeamAssignment.objects.filter(month=locked))
        remove("teams", Team.objects.filter(month=locked))
        remove("enrollments", MonthEnrollment.objects.filter(month=locked))
        remove("themes", MonthTheme.objects.filter(month=locked))
        group = locked.group
        target_name = locked.name
        target_id = locked.pk
        locked.delete()
        if fail_after_step == "challenge":
            raise RuntimeError("Injected Challenge purge failure after challenge.")
        AuditEvent.objects.create(
            actor=request.actor, group=group, action="recovery.challenge_purged",
            object_type="ChallengeMonth", object_id=str(target_id),
            summary=f"Platform recovery permanently purged Challenge {target_name}.",
        )
        return RecoveryMutationResult(
            after_state={"exists": False, "challenge_name": target_name},
            impact=impact,
        )

    return execute_recovery_operation(request, mutation)


def set_group_active(*, group, active, recovery_request):
    def mutation():
        locked = type(group).objects.select_for_update().get(pk=group.pk)
        before = locked.is_active
        locked.is_active = active
        locked.save(update_fields=["is_active"])
        AuditEvent.objects.create(
            actor=recovery_request.actor, group=locked,
            action="recovery.group_reactivated" if active else "recovery.group_deactivated",
            object_type="ReadingGroup", object_id=str(locked.pk),
            summary=f"Platform recovery {'reactivated' if active else 'deactivated'} Group {locked.name}.",
        )
        return {"is_active": locked.is_active, "previous_is_active": before}
    return execute_recovery_operation(recovery_request, mutation)


def transfer_group_ownership(*, group, target_membership, recovery_request):
    def mutation():
        locked_group = type(group).objects.select_for_update().get(pk=group.pk)
        memberships = list(Membership.objects.select_for_update().filter(group=locked_group, is_active=True).select_related("user"))
        target = next((item for item in memberships if item.pk == target_membership.pk), None)
        if target is None or target.user.is_superuser:
            raise ValidationError("Choose an active normal membership in this Group.")
        previous_owners = [item for item in memberships if item.role == Membership.Role.OWNER]
        for membership in previous_owners:
            if membership.pk != target.pk:
                membership.role = Membership.Role.MEMBER
                membership.save(update_fields=["role"])
        if target.role != Membership.Role.OWNER:
            target.role = Membership.Role.OWNER
            target.save(update_fields=["role"])
        active_owner_ids = list(Membership.objects.filter(group=locked_group, is_active=True, role=Membership.Role.OWNER).values_list("pk", flat=True))
        if active_owner_ids != [target.pk]:
            raise ValidationError("Ownership transfer must leave exactly one active Group Owner.")
        return {
            "owner_membership_id": target.pk,
            "owner_label": target.display_name,
            "previous_owner_ids": [item.pk for item in previous_owners],
        }
    return execute_recovery_operation(recovery_request, mutation)


def _delete_user_sessions(user):
    for session in Session.objects.all().iterator():
        try:
            session_user_id = session.get_decoded().get("_auth_user_id")
        except Exception:
            continue
        if str(session_user_id) == str(user.pk):
            session.delete()


def set_user_active(*, user, active, recovery_request):
    def mutation():
        User = get_user_model()
        locked_users = list(User.objects.select_for_update().filter(is_superuser=True)) if user.is_superuser else []
        locked = User.objects.select_for_update().get(pk=user.pk)
        if locked.pk == recovery_request.actor.pk:
            raise ValidationError("You cannot deactivate or reactivate your own Platform Owner account through recovery.")
        if locked.is_superuser and not active and sum(owner.is_active for owner in locked_users) <= 1:
            raise ValidationError("Northbound must retain at least one active Platform Owner.")
        before = locked.is_active
        locked.is_active = active
        locked.save(update_fields=["is_active"])
        if not active:
            _delete_user_sessions(locked)
        return {"is_active": locked.is_active, "previous_is_active": before, "is_platform_owner": locked.is_superuser}
    return execute_recovery_operation(recovery_request, mutation)


def set_membership_active(*, membership, active, recovery_request):
    def mutation():
        locked = Membership.objects.select_for_update().select_related("group", "user").get(pk=membership.pk)
        if not active:
            if locked.role == Membership.Role.OWNER and not Membership.objects.filter(group=locked.group, is_active=True, role=Membership.Role.OWNER).exclude(pk=locked.pk).exists():
                raise ValidationError("Transfer Group ownership before deactivating its final active Owner.")
            if ChallengeStaffAssignment.objects.filter(membership=locked, ended_at__isnull=True).exists():
                raise ValidationError("End this membership's active Challenge staffing before deactivation.")
            if MonthEnrollment.objects.filter(participant=locked, is_active=True).exists():
                raise ValidationError("Withdraw this membership from active Challenges before deactivation.")
        if active and not locked.user.is_active:
            raise ValidationError("Reactivate the User account before reactivating this Membership.")
        before = locked.is_active
        locked.is_active = active
        locked.save(update_fields=["is_active"])
        return {"is_active": locked.is_active, "previous_is_active": before}
    return execute_recovery_operation(recovery_request, mutation)


def correct_membership_role(*, membership, role, recovery_request):
    if role not in Membership.Role.values:
        raise ValidationError("Choose a valid Group role.")
    def mutation():
        locked = Membership.objects.select_for_update().select_related("group").get(pk=membership.pk)
        if locked.role == Membership.Role.OWNER and role != Membership.Role.OWNER:
            if not Membership.objects.filter(group=locked.group, is_active=True, role=Membership.Role.OWNER).exclude(pk=locked.pk).exists():
                raise ValidationError("Transfer ownership before demoting the final active Group Owner.")
        before = locked.role
        locked.role = role
        locked.save(update_fields=["role"])
        return {"role": locked.role, "previous_role": before}
    return execute_recovery_operation(recovery_request, mutation)


def set_staffing_active(*, assignment, active, recovery_request):
    def mutation():
        locked = ChallengeStaffAssignment.objects.select_for_update().select_related("month__group", "membership", "team").get(pk=assignment.pk)
        before = locked.ended_at is None
        if active:
            if locked.ended_at is None:
                return {"is_active": True, "previous_is_active": True}
            locked.ended_at = None
            locked.ended_by = None
            locked.full_clean()
            locked.save(update_fields=["ended_at", "ended_by"])
        else:
            if locked.ended_at is not None:
                return {"is_active": False, "previous_is_active": False}
            locked.ended_at = timezone.now()
            locked.ended_by = recovery_request.actor
            locked.save(update_fields=["ended_at", "ended_by"])
        return {"is_active": locked.ended_at is None, "previous_is_active": before}
    return execute_recovery_operation(recovery_request, mutation)


def correct_staffing_role(*, assignment, role, team, recovery_request):
    if role not in ChallengeStaffAssignment.Role.values:
        raise ValidationError("Choose a valid Challenge staffing role.")
    def mutation():
        locked = ChallengeStaffAssignment.objects.select_for_update().select_related("month__group", "membership", "team").get(pk=assignment.pk)
        if locked.ended_at is not None:
            raise ValidationError("Only a current staffing assignment can be corrected. Restore it first if appropriate.")
        if locked.role == role and locked.team_id == getattr(team, "pk", None):
            raise ValidationError("Choose a different coherent staffing role or Team.")
        locked.ended_at = timezone.now()
        locked.ended_by = recovery_request.actor
        locked.save(update_fields=["ended_at", "ended_by"])
        replacement = ChallengeStaffAssignment(
            month=locked.month, membership=locked.membership, role=role,
            team=team if role == ChallengeStaffAssignment.Role.TEAM_LEADER else None,
            assigned_by=recovery_request.actor,
        )
        replacement.save()
        return {"ended_assignment_id": locked.pk, "replacement_assignment_id": replacement.pk, "role": replacement.role, "team_id": replacement.team_id}
    return execute_recovery_operation(recovery_request, mutation)


def _has_required_registration_answers(enrollment):
    required_ids = set(enrollment.month.signup_questions.filter(is_required=True).values_list("pk", flat=True))
    if not required_ids:
        return True
    answers = {
        answer.question_id: answer.value
        for answer in enrollment.signup_answers.filter(question_id__in=required_ids)
    }
    return all(question_id in answers and answers[question_id] not in (None, "", [], {}) for question_id in required_ids)


def set_enrollment_active(*, enrollment, active, recovery_request):
    def mutation():
        locked = MonthEnrollment.objects.select_for_update().select_related("month__group", "participant__user").get(pk=enrollment.pk)
        before = locked.is_active
        if active:
            if not _has_required_registration_answers(locked):
                raise ValidationError("Required registration answers are missing; reactivation was refused without fabricating data.")
            activate_participation(month=locked.month, participant=locked.participant, actor=recovery_request.actor, origin=locked.origin)
        else:
            deactivate_participation(
                enrollment=locked, actor=recovery_request.actor,
                reason=MonthEnrollment.InactiveReason.REMOVED,
                note=recovery_request.reason,
            )
        locked.refresh_from_db()
        return {"is_active": locked.is_active, "previous_is_active": before, "origin": locked.origin}
    return execute_recovery_operation(recovery_request, mutation)


def correct_enrollment_origin(*, enrollment, origin, recovery_request):
    if origin not in MonthEnrollment.Origin.values:
        raise ValidationError("Choose a valid enrollment origin.")
    def mutation():
        locked = MonthEnrollment.objects.select_for_update().get(pk=enrollment.pk)
        before = locked.origin
        locked.origin = origin
        locked.save(update_fields=["origin"])
        return {"origin": locked.origin, "previous_origin": before, "is_active": locked.is_active}
    return execute_recovery_operation(recovery_request, mutation)


def set_team_archived(*, team, archived, recovery_request):
    def mutation():
        locked = Team.objects.select_for_update().select_related("month__group").get(pk=team.pk)
        before = locked.is_archived
        if not archived:
            locked.full_clean()
        locked.is_archived = archived
        locked.save(update_fields=["is_archived"])
        return {"is_archived": locked.is_archived, "previous_is_archived": before}
    return execute_recovery_operation(recovery_request, mutation)


def delete_unused_team(*, team, recovery_request):
    def mutation():
        locked = Team.objects.select_for_update().select_related("month__group").get(pk=team.pk)
        if locked.month.status != ChallengeMonth.Status.DRAFT:
            raise ValidationError("Only an unused Team in a Draft Challenge can be deleted.")
        if locked.assignments.exists() or locked.staff_assignments.exists() or locked.game_reward_applications.exists():
            raise ValidationError("Historically used Teams cannot be deleted; archive the Team instead.")
        label = locked.name
        locked.delete()
        return {"exists": False, "team_name": label}
    return execute_recovery_operation(recovery_request, mutation)


def set_team_assignment_active(*, assignment, active, recovery_request):
    def mutation():
        locked = TeamAssignment.objects.select_for_update().select_related("month__group", "participant", "team").get(pk=assignment.pk)
        before = locked.ended_at is None
        if active:
            if locked.ended_at is None:
                return {"is_active": True, "previous_is_active": True}
            if locked.team.is_archived:
                raise ValidationError("Reactivate the Team before restoring this assignment.")
            if TeamAssignment.objects.filter(month=locked.month, participant=locked.participant, ended_at__isnull=True).exclude(pk=locked.pk).exists():
                raise ValidationError("The Reader already has a current Team assignment.")
            locked.ended_at = None
            locked.ended_by = None
            locked.full_clean()
            locked.save(update_fields=["ended_at", "ended_by"])
        else:
            end_team_assignment(assignment=locked, actor=recovery_request.actor, reason=recovery_request.reason)
        locked.refresh_from_db()
        return {"is_active": locked.ended_at is None, "previous_is_active": before, "team_id": locked.team_id}
    return execute_recovery_operation(recovery_request, mutation)


def reassign_team_assignment(*, assignment, team, recovery_request):
    def mutation():
        locked = TeamAssignment.objects.select_for_update().select_related("month__group", "participant", "team").get(pk=assignment.pk)
        target = Team.objects.select_for_update().get(pk=team.pk)
        if locked.ended_at is not None:
            raise ValidationError("Only a current Team assignment can be reassigned.")
        if target.month_id != locked.month_id or target.is_archived:
            raise ValidationError("Choose an active Team in the same Challenge.")
        if target.pk == locked.team_id:
            raise ValidationError("Choose a different Team.")
        old_team_id = locked.team_id
        end_team_assignment(assignment=locked, actor=recovery_request.actor, reason=f"Platform recovery reassigned the Reader to {target.name}")
        replacement = TeamAssignment.objects.create(
            month=locked.month, team=target, participant=locked.participant,
            assigned_by=recovery_request.actor,
        )
        return {"ended_assignment_id": locked.pk, "replacement_assignment_id": replacement.pk, "previous_team_id": old_team_id, "team_id": target.pk}
    return execute_recovery_operation(recovery_request, mutation)
