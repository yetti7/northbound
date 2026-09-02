from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum

from .models import BookSubmission, ModifierProvenance, ThemeClaim


def _theme_source_reference(claim):
    return f"theme_claim:{claim.pk}"


def theme_approval_snapshot(claim):
    """Return the durable amount to use when approving a Theme claim."""
    existing_amount = ModifierProvenance.objects.filter(
        source_type=ModifierProvenance.SourceType.THEME_BONUS,
        source_reference=_theme_source_reference(claim),
    ).values_list("amount", flat=True).first()
    if existing_amount is not None:
        return existing_amount
    if claim.approved_bonus_pages > 0:
        return claim.approved_bonus_pages
    return claim.theme.bonus_pages


def _synchronize_locked_theme_provenance(submission, claim):
    source_reference = _theme_source_reference(claim)
    provenance = ModifierProvenance.objects.select_for_update().filter(
        source_type=ModifierProvenance.SourceType.THEME_BONUS,
        source_reference=source_reference,
    ).first()
    if provenance is not None and (
        provenance.month_id != submission.month_id
        or provenance.participant_id != submission.participant_id
        or provenance.submission_id != submission.pk
    ):
        raise ValidationError(
            f"Theme modifier source {source_reference} does not belong to its claim's Challenge, Reader, and submission."
        )

    contributes = (
        submission.status == BookSubmission.Status.APPROVED
        and not submission.is_removed
        and claim.status == ThemeClaim.Status.APPROVED
        and claim.approved_bonus_pages > 0
    )

    if not contributes:
        if provenance and provenance.is_active:
            provenance.is_active = False
            provenance.save(update_fields=["is_active"])
        return provenance

    create_values = {
        "month": submission.month,
        "participant": submission.participant,
        "submission": submission,
        "source_label": claim.theme.name,
        "source_context": f"Approved Theme claim {claim.pk}",
        "amount": claim.approved_bonus_pages,
        "effective_date": submission.completed_on,
        "applied_by": claim.reviewed_by,
        "applied_at": claim.reviewed_at,
        "is_active": True,
    }
    if provenance is None:
        return ModifierProvenance.objects.create(
            source_type=ModifierProvenance.SourceType.THEME_BONUS,
            source_reference=source_reference,
            **create_values,
        )

    changed_fields = []
    for field, value in {
        "amount": claim.approved_bonus_pages,
        "is_active": True,
    }.items():
        if getattr(provenance, field) != value:
            setattr(provenance, field, value)
            changed_fields.append(field)
    if changed_fields:
        provenance.save(update_fields=changed_fields)
    return provenance


@transaction.atomic
def synchronize_theme_provenance(claim):
    """Synchronize one Theme source while serializing on its submission."""
    submission = BookSubmission.objects.select_for_update().get(pk=claim.submission_id)
    locked_claim = ThemeClaim.objects.select_for_update().select_related("theme", "reviewed_by").get(pk=claim.pk)
    return _synchronize_locked_theme_provenance(submission, locked_claim)


def active_submission_modifier_total(submission):
    return submission.modifier_provenance.filter(
        is_active=True,
    ).aggregate(total=Sum("amount"))["total"] or 0


def preview_submission_score(submission):
    """Calculate without writes for the legacy recalculate_score(save=False) API."""
    if submission.status != BookSubmission.Status.APPROVED or submission.is_removed or not submission.approved_pages:
        return 0, None
    non_theme_total = submission.modifier_provenance.filter(
        is_active=True,
    ).exclude(
        source_type=ModifierProvenance.SourceType.THEME_BONUS,
    ).aggregate(total=Sum("amount"))["total"] or 0
    theme_total = sum(
        theme_approval_snapshot(claim)
        for claim in submission.theme_claims.select_related("theme").filter(
            status=ThemeClaim.Status.APPROVED,
        )
        if claim.approved_bonus_pages > 0
    )
    modifier_total = non_theme_total + theme_total
    return modifier_total, submission.approved_pages + modifier_total


@transaction.atomic
def apply_submission_review(submission, reviewed_claims, reviewer, reviewed_at):
    """Persist one submission/Theme review through the scoring boundary."""
    locked_submission = BookSubmission.objects.select_for_update().get(pk=submission.pk)
    newly_approved = locked_submission.status != BookSubmission.Status.APPROVED and submission.status == BookSubmission.Status.APPROVED
    for field in ("approved_pages", "status", "verification_url", "review_notes"):
        setattr(locked_submission, field, getattr(submission, field))
    locked_submission.reviewed_by = reviewer
    locked_submission.reviewed_at = reviewed_at
    locked_submission.full_clean()
    locked_submission.save()

    locked_claims = {
        claim.pk: claim
        for claim in ThemeClaim.objects.select_for_update().select_related("theme").filter(
            submission=locked_submission,
        )
    }
    for reviewed_claim in reviewed_claims:
        claim = locked_claims[reviewed_claim.pk]
        claim.status = reviewed_claim.status
        claim.reviewed_by = reviewer
        claim.reviewed_at = reviewed_at
        if (
            locked_submission.status == BookSubmission.Status.APPROVED
            and claim.status == ThemeClaim.Status.APPROVED
        ):
            claim.approved_bonus_pages = theme_approval_snapshot(claim)
        else:
            claim.approved_bonus_pages = 0
        claim.full_clean()
        claim.save()

    if locked_submission.status != BookSubmission.Status.APPROVED:
        ThemeClaim.objects.filter(submission=locked_submission).update(
            status=ThemeClaim.Status.REJECTED,
            approved_bonus_pages=0,
            reviewed_by=reviewer,
            reviewed_at=reviewed_at,
        )

    refresh_submission_score(locked_submission)
    from .botm_matching import synchronize_submission
    synchronize_submission(locked_submission)
    from .personal_tbr_matching import synchronize_submission as synchronize_personal_tbr_submission
    synchronize_personal_tbr_submission(locked_submission)
    locked_submission.refresh_from_db(fields=["bonus_pages", "final_scored_pages"])
    submission.bonus_pages = locked_submission.bonus_pages
    submission.final_scored_pages = locked_submission.final_scored_pages
    if newly_approved:
        from .hardcover_sync import enqueue_eligible_approved_submission_safely
        from .models import ReaderHardcoverSyncPreference
        # Consent must already exist at approval, not first appear before the
        # post-commit callback runs. The callback also checks current consent.
        if ReaderHardcoverSyncPreference.objects.filter(user=locked_submission.participant.user, sync_completed_books=True).exists():
            transaction.on_commit(lambda submission_id=locked_submission.pk: enqueue_eligible_approved_submission_safely(submission_id))
    return locked_submission


@transaction.atomic
def refresh_submission_score(submission):
    """Synchronize Theme provenance and persist the submission score caches."""
    locked_submission = BookSubmission.objects.select_for_update().get(pk=submission.pk)
    claims = ThemeClaim.objects.select_for_update().select_related("theme", "reviewed_by").filter(
        submission=locked_submission,
    )
    for claim in claims:
        _synchronize_locked_theme_provenance(locked_submission, claim)

    if (
        locked_submission.status == BookSubmission.Status.APPROVED
        and not locked_submission.is_removed
        and locked_submission.approved_pages
    ):
        modifier_total = active_submission_modifier_total(locked_submission)
        final_total = locked_submission.approved_pages + modifier_total
    else:
        modifier_total = 0
        final_total = None
    BookSubmission.objects.filter(pk=locked_submission.pk).update(
        bonus_pages=modifier_total,
        final_scored_pages=final_total,
    )
    locked_submission.bonus_pages = modifier_total
    locked_submission.final_scored_pages = final_total
    submission.bonus_pages = modifier_total
    submission.final_scored_pages = final_total
    return final_total
