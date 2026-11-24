import json

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum

from .models import AuditEvent, BookSubmission, ModifierProvenance, ThemeClaim, MonthTheme


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


def establish_theme_award(claim, *, correction=False, historical_rule=None):
    """Called only by approval/recovery or intentional review page correction.

    The claim owns calculation evidence; provenance remains the awarded-page ledger.
    Refresh/preview/reporting never establish or recalculate these snapshots.
    """
    if (historical_rule is None and claim.submission.late_approval_evidence
            and not claim.award_finalized and not ModifierProvenance.objects.filter(
                source_type=ModifierProvenance.SourceType.THEME_BONUS,
                source_reference=_theme_source_reference(claim),
            ).exists()):
        return 0
    from .historical_restoration import permits_automatic_source
    if not permits_automatic_source(claim.submission, _theme_source_reference(claim)):
        return 0
    if claim.theme.bonus_rule == MonthTheme.BonusRule.PERCENTAGE:
        if not claim.award_finalized and (
            claim.submission.status != BookSubmission.Status.APPROVED or claim.submission.is_removed
        ):
            return 0
        if not claim.award_finalized or correction:
            # Existing snapshots retain their rate; the configured rate is locked.
            rate = claim.award_percentage if claim.award_finalized else (
                historical_rule["bonus_percentage"] if historical_rule else claim.theme.bonus_percentage
            )
            pages = claim.submission.approved_pages
            claim.award_bonus_pages = percentage_bonus_pages(pages, rate)
            claim.award_percentage = rate
            claim.award_basis_pages = pages
            claim.award_rule = MonthTheme.BonusRule.PERCENTAGE
            claim.award_version = 1
            claim.award_finalized = True
        return claim.award_bonus_pages
    if historical_rule is not None:
        return historical_rule["bonus_pages"]
    return theme_approval_snapshot(claim)


def _synchronize_locked_theme_provenance(submission, claim, *, earned_only=False):
    from .historical_restoration import permits_automatic_source
    source_reference = _theme_source_reference(claim)
    if not permits_automatic_source(submission, source_reference):
        return None
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

    if earned_only and ((provenance is None and not claim.award_finalized)
                        or (provenance is not None and provenance.voided_at)):
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
def decide_pending_themes(submission, reviewed_claims, reviewer, reviewed_at, *, late_assignment=None, theme_reasons=None):
    """Initial Theme decisions only; caller has locked/revalidated book authority."""
    locked = BookSubmission.objects.select_for_update().get(pk=submission.pk)
    claims = {claim.pk: claim for claim in ThemeClaim.objects.select_for_update().select_related('theme').filter(submission=locked)}
    changed = []
    seen = set()
    for proposed in reviewed_claims:
        claim = claims.get(proposed.pk)
        if claim is None or proposed.pk in seen:
            raise ValidationError('Select distinct Theme claims belonging to this submission.')
        seen.add(proposed.pk)
        if proposed.status == claim.status:
            continue
        source = _theme_source_reference(claim)
        if (claim.status != ThemeClaim.Status.PENDING or claim.award_finalized
                or claim.approved_bonus_pages or ModifierProvenance.objects.filter(
                    source_type=ModifierProvenance.SourceType.THEME_BONUS, source_reference=source).exists()):
            raise ValidationError('Finalized Theme decisions require the signed Edit Book correction preview.')
        if proposed.status not in {ThemeClaim.Status.APPROVED, ThemeClaim.Status.REJECTED}:
            raise ValidationError('Choose approval or rejection for a Pending Theme claim.')
        historical_rule = None
        if proposed.status == ThemeClaim.Status.APPROVED:
            if claim.pk in locked.late_approval_evidence.get('held_theme_claim_ids', ()):
                raise ValidationError('A held historical Theme claim requires review of its original configuration.')
            from .historical_restoration import permits_automatic_source
            if not permits_automatic_source(locked, source):
                raise ValidationError('Protected Theme history requires authorized correction review.')
            if late_assignment is not None or locked.late_approval_evidence:
                from .late_approval import theme_rule
                historical_rule = theme_rule(locked, claim)
                if historical_rule is None:
                    raise ValidationError('A held historical Theme claim requires review of its original configuration.')
        from .reader_rejections import validate_reason,record
        reason=validate_reason((theme_reasons or {}).get(claim.pk,getattr(proposed,'_rejection_reason',''))) if proposed.status=='rejected' else ''
        claim.submission = locked
        claim.status = proposed.status
        claim.reviewed_by = reviewer
        claim.reviewed_at = reviewed_at
        claim.full_clean()
        claim.approved_bonus_pages = establish_theme_award(claim, historical_rule=historical_rule) if claim.status == ThemeClaim.Status.APPROVED else 0
        claim.full_clean()
        claim.save()
        AuditEvent.objects.create(actor=reviewer, group=locked.month.group,
            object_type='BookSubmission', object_id=str(locked.pk),
            action='submission.theme_'+claim.status,
            summary=json.dumps({'theme_claim_id':claim.pk,'theme_id':claim.theme_id,
                'previous_status':'pending','status':claim.status,'awarded_pages':claim.approved_bonus_pages}))
        if claim.status=='rejected':
            record(category='theme',target=claim,book=locked,actor=reviewer,reason=reason,label=f'{locked.title} · Theme: {claim.theme.name}')
        changed.append(claim.pk)
    if changed:
        refresh_submission_score(locked, theme_claim_ids=changed)
    locked._theme_decisions_count = len(changed)
    return locked


@transaction.atomic
def apply_submission_review(submission, reviewed_claims, reviewer, reviewed_at, *, late_assignment=None, rejection_reason=None, theme_reasons=None):
    """Persist one submission/Theme review through the scoring boundary."""
    locked_submission = BookSubmission.objects.select_for_update().get(pk=submission.pk)
    locked_submission._prompt_actor = reviewer
    previous_status = locked_submission.status
    from .reader_rejections import validate_reason,record
    rejecting_book=submission.status=='rejected' and previous_status!='rejected'
    book_reason=validate_reason(rejection_reason if rejection_reason is not None else getattr(submission,'_rejection_reason','')) if rejecting_book else ''
    rejected_themes=[]
    if submission.status=='approved':
        originals=dict(locked_submission.theme_claims.values_list('pk','status'))
        for proposed in reviewed_claims:
            if proposed.status=='rejected' and originals.get(proposed.pk)!='rejected':
                reason=validate_reason((theme_reasons or {}).get(proposed.pk,getattr(proposed,'_rejection_reason','')))
                rejected_themes.append((proposed.pk,reason))
    first_review = locked_submission.reviewed_by_id is None
    review_changed = any(
        getattr(locked_submission, field) != getattr(submission, field)
        for field in ("approved_pages", "status", "verification_url", "review_notes")
    )
    pages_corrected = (
        submission.approved_pages is not None and submission.approved_pages > 0
        and locked_submission.approved_pages != submission.approved_pages
    )
    newly_approved = locked_submission.status != BookSubmission.Status.APPROVED and submission.status == BookSubmission.Status.APPROVED
    for field in ("approved_pages", "status", "verification_url", "review_notes"):
        setattr(locked_submission, field, getattr(submission, field))
    locked_claims = {
        claim.pk: claim
        for claim in ThemeClaim.objects.select_for_update().select_related("theme").filter(
            submission=locked_submission,
        )
    }
    decisions = {claim.pk: claim.status for claim in reviewed_claims}
    review_changed = review_changed or any(
        claim.status != (decisions.get(claim.pk, claim.status) if submission.status == BookSubmission.Status.APPROVED
                         else ThemeClaim.Status.REJECTED)
        for claim in locked_claims.values()
    )
    if review_changed or first_review:
        locked_submission.reviewed_by = reviewer
        locked_submission.reviewed_at = reviewed_at
    locked_submission.full_clean()
    locked_submission.save()
    if review_changed or first_review:
        status = locked_submission.status
        decision = status if status != previous_status or first_review else "review_changed"
        AuditEvent.objects.create(
            actor=reviewer, group=locked_submission.month.group,
            object_type="BookSubmission", object_id=str(locked_submission.pk),
            action=f"submission.{decision}",
            summary=json.dumps({"challenge_id": locked_submission.month_id,
                                "previous_status": previous_status, "status": status}),
        )
    holds = []
    late_theme_claim_ids = set()
    held_theme_claim_ids = []
    previously_held = set(locked_submission.late_approval_evidence.get("held_theme_claim_ids", ()))
    for claim in locked_claims.values():
        claim.submission = locked_submission
        historical_rule = None
        if claim.pk in previously_held and decisions.get(claim.pk) == ThemeClaim.Status.APPROVED:
            raise ValidationError("A held historical Theme claim requires review of its original configuration.")
        if late_assignment is not None:
            from .late_approval import theme_rule
            if claim.award_finalized or ModifierProvenance.objects.filter(
                source_type=ModifierProvenance.SourceType.THEME_BONUS,
                source_reference=_theme_source_reference(claim),
            ).exists():
                continue
            decision = decisions.get(claim.pk, claim.status)
            if decision != ThemeClaim.Status.REJECTED:
                historical_rule = theme_rule(locked_submission, claim)
                if historical_rule is None or decision != ThemeClaim.Status.APPROVED:
                    holds.append(f"Theme {claim.theme.name} needs historical configuration review.")
                    held_theme_claim_ids.append(claim.pk)
                    continue
        percentage = claim.theme.bonus_rule == MonthTheme.BonusRule.PERCENTAGE
        percentage_correction = pages_corrected and percentage and claim.award_finalized
        first_eligible_award = (
            percentage and not claim.award_finalized
            and decisions.get(claim.pk, claim.status) == ThemeClaim.Status.APPROVED
            and locked_submission.status == BookSubmission.Status.APPROVED
            and not locked_submission.is_removed
        )
        if claim.pk not in decisions and not percentage_correction and not first_eligible_award and historical_rule is None:
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
            claim.approved_bonus_pages = establish_theme_award(claim, historical_rule=historical_rule)
        else:
            claim.approved_bonus_pages = 0
        claim.full_clean()
        claim.save()
        if late_assignment is not None and historical_rule is not None:
            late_theme_claim_ids.add(claim.pk)

    if locked_submission.status != BookSubmission.Status.APPROVED:
        ThemeClaim.objects.filter(submission=locked_submission).update(
            status=ThemeClaim.Status.REJECTED,
            approved_bonus_pages=0,
            reviewed_by=reviewer,
            reviewed_at=reviewed_at,
        )

    if late_assignment is not None:
        from .late_approval import award_matches, record_late_approval
        award_matches(locked_submission, reviewed_at=reviewed_at, holds=holds)
        record_late_approval(locked_submission, reviewer, late_assignment, holds, held_theme_claim_ids)
        refresh_submission_score(locked_submission, late_theme_claim_ids=late_theme_claim_ids)
        locked_submission.late_approval_holds = holds
    else:
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
    if previous_status=='rejected' and locked_submission.status=='approved':
        from .reader_rejections import resolve_target_correction
        resolve_target_correction(reviewer,'book',locked_submission,locked_submission)
    if rejecting_book:
        record(category='book',target=locked_submission,book=locked_submission,actor=reviewer,reason=book_reason,label=locked_submission.title)
    for pk,reason in rejected_themes:
        claim=ThemeClaim.objects.select_related('theme').get(pk=pk)
        record(category='theme',target=claim,book=locked_submission,actor=reviewer,reason=reason,label=f'{locked_submission.title} · Theme: {claim.theme.name}')
    return locked_submission


@transaction.atomic
def refresh_submission_score(submission, *, earned_only=False, restore_earned=False, late_theme_claim_ids=None, theme_claim_ids=None):
    """Synchronize Theme/Immersive provenance and persist submission score caches."""
    locked_submission = BookSubmission.objects.select_for_update().get(pk=submission.pk)
    from .immersive import synchronize_awards
    # Theme-only decisions synchronize only their own sources and the score cache.
    if theme_claim_ids is None:
        if late_theme_claim_ids is None:
            synchronize_awards(locked_submission, earned_only=earned_only, restore_earned=restore_earned)
        else:
            synchronize_awards(locked_submission, late_only=True)
    claims = ThemeClaim.objects.select_for_update(of=("self",)).select_related("theme", "reviewed_by").filter(
        submission=locked_submission,
    )
    if theme_claim_ids is not None:
        claims = claims.filter(pk__in=theme_claim_ids)
    for claim in claims:
        if late_theme_claim_ids is not None and claim.pk not in late_theme_claim_ids:
            continue
        _synchronize_locked_theme_provenance(locked_submission, claim, earned_only=earned_only)

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
    held = locked.late_approval_evidence.get("held_theme_claim_ids", ())
    for claim in claims:
        if claim.pk in held:
            continue
        claim.submission = locked
        claim.approved_bonus_pages = establish_theme_award(claim)
        claim.full_clean()
        claim.save()
