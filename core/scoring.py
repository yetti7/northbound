from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum

from .models import BookSubmission, ModifierProvenance, ThemeClaim, MonthTheme


def _theme_source_reference(claim):
    return f"theme_claim:{claim.pk}"


def theme_approval_snapshot(claim):
    """Return the durable amount to use when approving a Theme claim."""
    if claim.theme.bonus_rule == MonthTheme.BonusRule.PERCENTAGE:
        if claim.award_finalized:
            return claim.award_bonus_pages
        return percentage_bonus_pages(claim.submission.approved_pages, claim.theme.bonus_percentage)
    existing_amount = ModifierProvenance.objects.filter(
        source_type=ModifierProvenance.SourceType.THEME_BONUS,
        source_reference=_theme_source_reference(claim),
    ).values_list("amount", flat=True).first()
    if existing_amount is not None:
        return existing_amount
    if claim.approved_bonus_pages > 0:
        return claim.approved_bonus_pages
    return claim.theme.bonus_pages


def percentage_bonus_pages(approved_pages, percentage):
    """Exact nonnegative round-half-up; each Theme uses base pages independently."""
    if not isinstance(approved_pages, int) or isinstance(approved_pages, bool) or approved_pages <= 0:
        raise ValidationError("Approved Pages must be positive before awarding a Theme bonus.")
    if not isinstance(percentage, int) or isinstance(percentage, bool) or not 0 <= percentage <= 100:
        raise ValidationError("Enter a whole-number percentage from 0 through 100.")
    return (approved_pages * percentage + 50) // 100


def establish_theme_award(claim, *, correction=False):
    """Called only by approval/recovery or intentional review page correction.

    The claim owns calculation evidence; provenance remains the awarded-page ledger.
    Refresh/preview/reporting never establish or recalculate these snapshots.
    """
    if claim.theme.bonus_rule == MonthTheme.BonusRule.PERCENTAGE:
        if not claim.award_finalized and (
            claim.submission.status != BookSubmission.Status.APPROVED or claim.submission.is_removed
        ):
            return 0
        if not claim.award_finalized or correction:
            # Existing snapshots retain their rate; the configured rate is locked.
            rate = claim.award_percentage if claim.award_finalized else claim.theme.bonus_percentage
            pages = claim.submission.approved_pages
            claim.award_bonus_pages = percentage_bonus_pages(pages, rate)
            claim.award_percentage = rate
            claim.award_basis_pages = pages
            claim.award_rule = MonthTheme.BonusRule.PERCENTAGE
            claim.award_version = 1
            claim.award_finalized = True
        return claim.award_bonus_pages
    return theme_approval_snapshot(claim)


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
        if provenance:
            changed_fields = []
            if provenance.is_active:
                provenance.is_active = False
                changed_fields.append("is_active")
            # A corrected, temporarily rejected award keeps its canonical source.
            # Zero has no ledger amount: retain the inactive positive history.
            if (claim.theme.bonus_rule == MonthTheme.BonusRule.PERCENTAGE
                    and claim.award_finalized and claim.award_bonus_pages > 0
                    and provenance.amount != claim.award_bonus_pages):
                provenance.amount = claim.award_bonus_pages
                changed_fields.append("amount")
            if changed_fields:
                provenance.save(update_fields=changed_fields)
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
    pages_corrected = (
        submission.approved_pages is not None and submission.approved_pages > 0
        and locked_submission.approved_pages != submission.approved_pages
    )
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
    decisions = {claim.pk: claim.status for claim in reviewed_claims}
    for claim in locked_claims.values():
        claim.submission = locked_submission
        percentage = claim.theme.bonus_rule == MonthTheme.BonusRule.PERCENTAGE
        percentage_correction = pages_corrected and percentage and claim.award_finalized
        first_eligible_award = (
            percentage and not claim.award_finalized
            and decisions.get(claim.pk, claim.status) == ThemeClaim.Status.APPROVED
            and locked_submission.status == BookSubmission.Status.APPROVED
            and not locked_submission.is_removed
        )
        if claim.pk not in decisions and not percentage_correction and not first_eligible_award:
            continue
        if claim.pk in decisions:
            claim.status = decisions[claim.pk]
            claim.reviewed_by = reviewer
            claim.reviewed_at = reviewed_at
        if percentage_correction:
            establish_theme_award(claim, correction=True)
        if (
            locked_submission.status == BookSubmission.Status.APPROVED
            and claim.status == ThemeClaim.Status.APPROVED
        ):
            claim.approved_bonus_pages = establish_theme_award(claim)
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


@transaction.atomic
def establish_restored_theme_awards(submission):
    """Initialize only missing awards during explicit, authorized restoration.

    Reports and ordinary cache refresh never call this eligibility transition.
    Established snapshots (including zero) and legacy rules remain untouched.
    """
    locked = BookSubmission.objects.select_for_update().get(pk=submission.pk)
    if locked.status != BookSubmission.Status.APPROVED or locked.is_removed:
        return
    claims = ThemeClaim.objects.select_for_update().select_related("theme").filter(
        submission=locked, status=ThemeClaim.Status.APPROVED,
        theme__bonus_rule=MonthTheme.BonusRule.PERCENTAGE, award_finalized=False,
    )
    for claim in claims:
        claim.submission = locked
        claim.approved_bonus_pages = establish_theme_award(claim)
        claim.full_clean()
        claim.save()
