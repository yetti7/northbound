from django.core.exceptions import ValidationError
from django.db import transaction

from .models import BookSubmission, ModifierProvenance, PersonalTBRMatch


def personal_tbr_book_reward_source_reference(match):
    return f"personal_tbr_match:{match.pk}"


def _reward_is_eligible(match):
    tbr = match.personal_tbr_book.personal_tbr
    return (
        match.status == PersonalTBRMatch.Status.CONFIRMED
        and match.is_qualifying
        and match.month.tbr_enabled
        and match.month.tbr_book_bonus_pages > 0
        and tbr.confirmed_at is not None
        and tbr.enrollment.month_id == match.month_id
        and tbr.enrollment.participant_id == match.participant_id
        and match.submission.month_id == match.month_id
        and match.submission.participant_id == match.participant_id
        and match.submission.status == BookSubmission.Status.APPROVED
        and not match.submission.is_removed
    )


def _synchronize_locked_reward(match):
    source_reference = personal_tbr_book_reward_source_reference(match)
    provenance = ModifierProvenance.objects.select_for_update().filter(
        source_type=ModifierProvenance.SourceType.TBR_BOOK,
        source_reference=source_reference,
    ).first()
    if provenance is not None and (
        provenance.month_id != match.month_id
        or provenance.participant_id != match.participant_id
        or provenance.submission_id != match.submission_id
    ):
        raise ValidationError(
            "Personal TBR reward provenance does not belong to its match's Challenge, Reader, and submission."
        )

    if not _reward_is_eligible(match):
        if provenance is not None and provenance.is_active:
            provenance.is_active = False
            provenance.save(update_fields=["is_active"])
            return provenance, True
        return provenance, False

    if provenance is None:
        provenance = ModifierProvenance.objects.create(
            month=match.month,
            participant=match.participant,
            submission=match.submission,
            source_type=ModifierProvenance.SourceType.TBR_BOOK,
            source_reference=source_reference,
            source_label=match.tbr_title_snapshot,
            source_context=f"Qualifying Personal TBR match {match.pk}",
            amount=match.month.tbr_book_bonus_pages,
            effective_date=match.submission.completed_on,
            applied_by=match.reviewer,
            applied_at=match.decided_at,
            is_system_generated=True,
            is_active=True,
        )
        return provenance, True

    # Amount and effective date are historical snapshots. Configuration edits
    # never rewrite an established reward.
    if not provenance.is_active:
        provenance.is_active = True
        provenance.save(update_fields=["is_active"])
        return provenance, True
    return provenance, False


@transaction.atomic
def synchronize_personal_tbr_book_reward(match):
    match = PersonalTBRMatch.objects.select_for_update().select_related(
        "personal_tbr_book__personal_tbr__enrollment", "month", "participant", "submission", "reviewer",
    ).get(pk=match.pk)
    provenance, changed = _synchronize_locked_reward(match)
    if changed:
        from .scoring import refresh_submission_score
        refresh_submission_score(match.submission)
    return provenance


@transaction.atomic
def synchronize_personal_tbr_book_rewards_for_submission(submission):
    submission = BookSubmission.objects.select_for_update().get(pk=submission.pk)
    matches = PersonalTBRMatch.objects.select_for_update().select_related(
        "personal_tbr_book__personal_tbr__enrollment", "month", "participant", "submission", "reviewer",
    ).filter(submission=submission).order_by("pk")
    changed = False
    provenances = []
    for match in matches:
        provenance, reward_changed = _synchronize_locked_reward(match)
        changed = changed or reward_changed
        if provenance is not None:
            provenances.append(provenance)
    if changed:
        from .scoring import refresh_submission_score
        refresh_submission_score(submission)
    return provenances


@transaction.atomic
def synchronize_personal_tbr_book_rewards_for_reader(*, month, participant):
    provenances = []
    for submission in BookSubmission.objects.filter(month=month, participant=participant).order_by("pk"):
        provenances.extend(synchronize_personal_tbr_book_rewards_for_submission(submission))
    return provenances


@transaction.atomic
def synchronize_personal_tbr_book_rewards_for_challenge(month):
    provenances = []
    for submission in BookSubmission.objects.filter(month=month).order_by("pk"):
        provenances.extend(synchronize_personal_tbr_book_rewards_for_submission(submission))
    return provenances
