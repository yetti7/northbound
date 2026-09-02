from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    AuditEvent,
    ChallengeMonth,
    ChallengeStaffAssignment,
    Game,
    GameRewardApplication,
    GameRewardRecipient,
    Membership,
    ModifierProvenance,
    MonthEnrollment,
    TeamAssignment,
)
from .permissions import can_operate_challenge


APPLY_LIFECYCLE_STATES = {
    ChallengeMonth.Status.ACTIVE,
    ChallengeMonth.Status.FINALIZING,
}


def _validate_operator(actor, month):
    if not can_operate_challenge(actor, month):
        raise ValidationError("Only a current Host or Platform Owner may operate Challenge Games.")


def game_reward_application_unavailable_reason(game):
    if game.month.status not in APPLY_LIFECYCLE_STATES:
        return "Game rewards may be applied only while the Challenge is Active or Finalizing."
    if not game.month.games_enabled:
        return "Games are not enabled for this Challenge."
    if not game.is_active:
        return "Rewards cannot be applied from a retired Game."
    return ""


def _validate_apply_state(game):
    reason = game_reward_application_unavailable_reason(game)
    if reason:
        raise ValidationError(reason)


def _eligible_readers(month):
    enrollment_participant_ids = list(
        MonthEnrollment.objects.select_for_update().filter(
            month=month,
            is_active=True,
            participant__is_active=True,
            participant__user__is_superuser=False,
        ).values_list("participant_id", flat=True)
    )
    return Membership.objects.select_for_update().filter(
        pk__in=enrollment_participant_ids,
        group=month.group,
        is_active=True,
        user__is_superuser=False,
    ).exclude(
        challenge_staff_assignments__month=month,
        challenge_staff_assignments__role=ChallengeStaffAssignment.Role.FLOATER,
        challenge_staff_assignments__ended_at__isnull=True,
    )


def _resolve_recipients(*, month, target_type, target_participant, target_team):
    eligible = _eligible_readers(month)
    if target_type == GameRewardApplication.TargetType.READER:
        if target_participant is None or target_team is not None:
            raise ValidationError("Reader rewards require exactly one Reader target.")
        if target_participant.group_id != month.group_id:
            raise ValidationError("The target Reader does not belong to the Game's Group.")
        recipients = list(eligible.filter(pk=target_participant.pk).order_by("pk"))
        target_label = target_participant.display_name
    elif target_type == GameRewardApplication.TargetType.TEAM:
        if target_team is None or target_participant is not None:
            raise ValidationError("Team rewards require exactly one Team target.")
        if target_team.month_id != month.pk:
            raise ValidationError("The target Team does not belong to the Game's Challenge.")
        assignment_participant_ids = list(
            TeamAssignment.objects.select_for_update().filter(
                month=month,
                team=target_team,
                ended_at__isnull=True,
            ).values_list("participant_id", flat=True)
        )
        recipients = list(eligible.filter(pk__in=assignment_participant_ids).distinct().order_by("pk"))
        target_label = target_team.name
    elif target_type == GameRewardApplication.TargetType.CHALLENGE:
        if target_participant is not None or target_team is not None:
            raise ValidationError("Challenge-wide rewards cannot store a Reader or Team target.")
        recipients = list(eligible.distinct().order_by("pk"))
        target_label = month.name
    else:
        raise ValidationError("Select a valid Game reward target type.")
    if not recipients:
        raise ValidationError("The selected target has no eligible Readers.")
    return recipients, target_label


@transaction.atomic
def preview_game_reward(*, game, actor, target_type, target_participant=None, target_team=None):
    """Resolve a read-only recipient preview through the application rules."""
    game = Game.objects.select_for_update().select_related("month__group").get(pk=game.pk)
    _validate_operator(actor, game.month)
    _validate_apply_state(game)
    return _resolve_recipients(
        month=game.month,
        target_type=target_type,
        target_participant=target_participant,
        target_team=target_team,
    )


def _payload_matches(application, *, game, target_type, target_participant, target_team, amount, reason):
    return (
        application.game_id == game.pk
        and application.target_type == target_type
        and application.target_participant_id == (target_participant.pk if target_participant else None)
        and application.target_team_id == (target_team.pk if target_team else None)
        and application.amount == amount
        and application.reason == reason
    )


def _reuse_or_reject(application, **payload):
    if not _payload_matches(application, **payload):
        raise ValidationError("This idempotency key is already associated with a different Game reward payload.")
    return application, False


@transaction.atomic
def apply_game_reward(
    *,
    game,
    actor,
    target_type,
    amount,
    reason,
    idempotency_key,
    target_participant=None,
    target_team=None,
):
    """Apply and freeze one manual Game reward without updating score aggregates."""
    reason = (reason or "").strip()
    game = Game.objects.select_for_update().select_related("month__group").get(pk=game.pk)
    _validate_operator(actor, game.month)
    _validate_apply_state(game)

    payload = {
        "game": game,
        "target_type": target_type,
        "target_participant": target_participant,
        "target_team": target_team,
        "amount": amount,
        "reason": reason,
    }
    existing = GameRewardApplication.objects.select_for_update().filter(
        idempotency_key=idempotency_key
    ).first()
    if existing:
        return _reuse_or_reject(existing, **payload)

    recipients, target_label = _resolve_recipients(
        month=game.month,
        target_type=target_type,
        target_participant=target_participant,
        target_team=target_team,
    )
    applied_at = timezone.now()
    application = GameRewardApplication(
        game=game,
        amount=amount,
        target_type=target_type,
        target_participant=target_participant,
        target_team=target_team,
        target_label=target_label,
        game_name_snapshot=game.name,
        advertised_bonus_pages_snapshot=game.advertised_bonus_pages,
        reason=reason,
        applied_by=actor,
        applied_at=applied_at,
        idempotency_key=idempotency_key,
    )
    application.full_clean(validate_unique=False)
    try:
        with transaction.atomic():
            application.save(force_insert=True)
    except IntegrityError:
        concurrent = GameRewardApplication.objects.select_for_update().get(idempotency_key=idempotency_key)
        return _reuse_or_reject(concurrent, **payload)

    effective_date = timezone.localtime(applied_at, ZoneInfo(game.month.group.timezone)).date()
    for participant in recipients:
        recipient = GameRewardRecipient.objects.create(application=application, participant=participant)
        provenance = ModifierProvenance.objects.create(
            month=game.month,
            participant=participant,
            submission=None,
            source_type=ModifierProvenance.SourceType.GAME_REWARD,
            source_reference=f"game_reward_recipient:{recipient.pk}",
            source_label=application.game_name_snapshot,
            source_context=f"{application.target_label}: {application.reason}",
            amount=application.amount,
            effective_date=effective_date,
            applied_by=application.applied_by,
            applied_at=application.applied_at,
            is_active=True,
        )
        recipient.provenance = provenance
        recipient.full_clean()
        recipient.save(update_fields=["provenance"])

    AuditEvent.objects.create(
        actor=actor,
        group=game.month.group,
        action="game.reward_applied",
        object_type="GameRewardApplication",
        object_id=str(application.pk),
        summary=(
            f"Applied {application.amount} pages from {application.game_name_snapshot} "
            f"to {application.target_label} ({len(recipients)} recipient(s))."
        ),
    )
    return application, True


@transaction.atomic
def void_game_reward(*, application, actor, reason):
    """Void one complete Game reward application while preserving all history."""
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Enter a reason for voiding this Game reward.")
    application = GameRewardApplication.objects.select_for_update().select_related(
        "game__month__group"
    ).get(pk=application.pk)
    _validate_operator(actor, application.game.month)
    if application.is_voided:
        raise ValidationError("This Game reward application has already been voided.")

    recipients = list(
        GameRewardRecipient.objects.select_for_update()
        .select_related("provenance")
        .filter(application=application)
        .order_by("pk")
    )
    if not recipients or any(recipient.provenance_id is None for recipient in recipients):
        raise ValidationError("This Game reward application has incomplete scoring provenance.")
    provenance_ids = [recipient.provenance_id for recipient in recipients]
    provenances = list(ModifierProvenance.objects.select_for_update().filter(pk__in=provenance_ids))
    if len(provenances) != len(recipients):
        raise ValidationError("This Game reward application has incomplete scoring provenance.")

    voided_at = timezone.now()
    application.is_voided = True
    application.voided_by = actor
    application.voided_at = voided_at
    application.void_reason = reason
    application.full_clean()
    application.save(update_fields=["is_voided", "voided_by", "voided_at", "void_reason"])
    ModifierProvenance.objects.filter(pk__in=provenance_ids).update(
        is_active=False,
        voided_by=actor,
        voided_at=voided_at,
        void_reason=reason,
    )
    AuditEvent.objects.create(
        actor=actor,
        group=application.game.month.group,
        action="game.reward_voided",
        object_type="GameRewardApplication",
        object_id=str(application.pk),
        summary=(
            f"Voided {application.amount} pages from {application.game_name_snapshot} "
            f"for {application.target_label} ({len(recipients)} recipient(s))."
        ),
    )
    return application
