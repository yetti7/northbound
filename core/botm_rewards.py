from django.core.exceptions import ValidationError
from django.db import transaction

from .models import BookSubmission, BotmMatch, ModifierProvenance


def botm_book_reward_source_reference(match):
    return f"botm_match:{match.pk}"


def _reward_amount(match):
    return match.decision_evidence.get("late_approval_bonus_pages", match.botm_book.bonus_pages)


def _reward_is_eligible(match):
    return (
        match.status == BotmMatch.Status.CONFIRMED
        and match.is_qualifying
        and (_reward_amount(match) or 0) > 0
        and match.botm_book.month_id == match.month_id
        and match.submission.month_id == match.month_id
        and match.submission.participant_id == match.participant_id
        and match.submission.status == BookSubmission.Status.APPROVED
        and not match.submission.is_removed
    )


def _synchronize_locked_reward(match, *, earned_only=False, restore_earned=False, late_approval=False):
    from .historical_restoration import permits_automatic_source
    source_reference = botm_book_reward_source_reference(match)
    if not permits_automatic_source(match.submission, source_reference):
        return None, False
    provenance = ModifierProvenance.objects.select_for_update().filter(
        source_type=ModifierProvenance.SourceType.BOTM_BOOK,
        source_reference=source_reference,
    ).first()
    if provenance is not None and (
        provenance.month_id != match.month_id
        or provenance.participant_id != match.participant_id
        or provenance.submission_id != match.submission_id
    ):
        raise ValidationError("BOTM reward provenance does not belong to its match's Challenge, Reader, and submission.")

    eligible = _reward_is_eligible(match)
    if not eligible:
        if provenance is not None and provenance.is_active:
            provenance.is_active = False
            provenance.save(update_fields=["is_active"])
            return provenance, True
        return provenance, False

    from .botm_matching import _submission_is_eligible
    if earned_only and (provenance is None or provenance.voided_at
                        or (not provenance.is_active and not restore_earned)):
        return provenance, False
    if (provenance is None or not provenance.is_active) and not (earned_only or late_approval) and not _submission_is_eligible(match.submission):
        return provenance, False

    if provenance is None:
        provenance = ModifierProvenance.objects.create(
            month=match.month,
            participant=match.participant,
            submission=match.submission,
            source_type=ModifierProvenance.SourceType.BOTM_BOOK,
            source_reference=source_reference,
            source_label=match.botm_title_snapshot,
            source_context=f"Qualifying BOTM match {match.pk}",
            amount=_reward_amount(match),
            effective_date=match.submission.completed_on,
            applied_by=match.reviewer,
            applied_at=match.decided_at,
            is_system_generated=True,
            is_active=True,
        )
        return provenance, True

    if provenance.amount != _reward_amount(match):
        raise ValidationError("The locked BOTM bonus no longer matches its durable reward provenance.")
    if not provenance.is_active:
        provenance.is_active = True
        provenance.save(update_fields=["is_active"])
        return provenance, True
    return provenance, False


@transaction.atomic
def synchronize_botm_book_reward(match):
    match = BotmMatch.objects.select_for_update().select_related(
        "botm_book", "month", "participant", "submission", "reviewer"
    ).get(pk=match.pk)
    provenance, changed = _synchronize_locked_reward(match)
    if changed:
        from .scoring import refresh_submission_score
        refresh_submission_score(match.submission)
    return provenance


@transaction.atomic
def synchronize_botm_book_rewards_for_submission(submission, *, earned_only=False, restore_earned=False):
    submission = BookSubmission.objects.select_for_update().get(pk=submission.pk)
    matches = BotmMatch.objects.select_for_update().select_related(
        "botm_book", "month", "participant", "submission", "reviewer"
    ).filter(submission=submission).order_by("pk")
    changed = False
    provenances = []
    for match in matches:
        provenance, reward_changed = _synchronize_locked_reward(
            match, earned_only=earned_only, restore_earned=restore_earned,
        )
        changed = changed or reward_changed
        if provenance is not None:
            provenances.append(provenance)
    if changed and not earned_only:
        from .scoring import refresh_submission_score
        refresh_submission_score(submission)
    return provenances


@transaction.atomic
def synchronize_botm_book_rewards_for_reader(*, month, participant):
    provenances = []
    for submission in BookSubmission.objects.filter(month=month, participant=participant).order_by("pk"):
        provenances.extend(synchronize_botm_book_rewards_for_submission(submission))
    return provenances


@transaction.atomic
def synchronize_botm_book_rewards_for_challenge(month):
    provenances = []
    for submission in BookSubmission.objects.filter(month=month).order_by("pk"):
        provenances.extend(synchronize_botm_book_rewards_for_submission(submission))
    return provenances
