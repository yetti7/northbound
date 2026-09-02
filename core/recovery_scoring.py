from dataclasses import replace

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (
    AuditEvent, BookSubmission, BotmCompletionAward,
    BotmCompletionAwardBook, BotmMatch, ModifierProvenance, MonthTheme,
    PersonalTBRCompletionAward, PersonalTBRCompletionAwardBook,
    PersonalTBRMatch, ThemeClaim,
)
from .recovery import (
    RecoveryImpactItem, RecoveryImpactPreview, RecoveryMutationResult,
    execute_recovery_operation,
)
from .score_aggregation import challenge_score_totals
from .scoring import refresh_submission_score


def submission_recovery_label(submission):
    return f"Submission #{submission.pk}: {submission.title} — {submission.participant.display_name}"


def _theme_reference(claim):
    return f"theme_claim:{claim.pk}"


def _theme_claim_from_provenance(provenance):
    prefix = "theme_claim:"
    if provenance.source_type != ModifierProvenance.SourceType.THEME_BONUS:
        return None
    if not provenance.source_reference.startswith(prefix):
        return None
    try:
        claim_id = int(provenance.source_reference[len(prefix):])
    except (TypeError, ValueError):
        return None
    return ThemeClaim.objects.filter(pk=claim_id).select_related("submission", "theme").first()


def _reader_score(month, participant):
    return challenge_score_totals(month=month, participant_ids=[participant.pk]).get(
        participant.pk, {"base_pages": 0, "modifier_pages": 0, "total_pages": 0}
    )


def _synchronize_reader_sources(month, participant):
    from .botm_matching import synchronize_reader as synchronize_botm_reader
    from .personal_tbr_matching import synchronize_reader as synchronize_tbr_reader

    synchronize_botm_reader(month=month, participant=participant)
    synchronize_tbr_reader(month=month, participant=participant)
    for submission in BookSubmission.objects.filter(month=month, participant=participant).order_by("pk"):
        refresh_submission_score(submission)


def set_submission_removed(*, submission, removed, recovery_request):
    def mutation():
        locked = BookSubmission.objects.select_for_update().select_related(
            "month__group", "participant__user"
        ).get(pk=submission.pk)
        before_score = _reader_score(locked.month, locked.participant)
        if removed:
            locked.is_removed = True
            locked.removed_at = timezone.now()
            locked.removed_by = recovery_request.actor
            locked.removal_reason = recovery_request.reason
        else:
            if not locked.is_removed:
                raise ValidationError("Only a soft-removed submission can be restored.")
            locked.is_removed = False
            locked.removed_at = None
            locked.removed_by = None
            locked.removal_reason = ""
        locked.save(update_fields=[
            "is_removed", "removed_at", "removed_by", "removal_reason",
        ])
        _synchronize_reader_sources(locked.month, locked.participant)
        locked.refresh_from_db(fields=["bonus_pages", "final_scored_pages"])
        after_score = _reader_score(locked.month, locked.participant)
        AuditEvent.objects.create(
            actor=recovery_request.actor, group=locked.month.group,
            action="recovery.submission_soft_removed" if removed else "recovery.submission_restored",
            object_type="BookSubmission", object_id=str(locked.pk),
            summary=f"Platform recovery {'soft removed' if removed else 'restored'} submission {locked.title}.",
        )
        return {
            "is_removed": locked.is_removed,
            "bonus_pages": locked.bonus_pages,
            "final_scored_pages": locked.final_scored_pages,
            "reader_score_before": before_score,
            "reader_score_after": after_score,
        }
    return execute_recovery_operation(recovery_request, mutation)


def submission_purge_impact(submission):
    theme_claims = ThemeClaim.objects.filter(submission=submission)
    botm_matches = BotmMatch.objects.filter(submission=submission)
    tbr_matches = PersonalTBRMatch.objects.filter(submission=submission)
    botm_awards = BotmCompletionAward.objects.filter(
        month=submission.month, participant=submission.participant,
    ) if botm_matches.filter(is_qualifying=True).exists() else BotmCompletionAward.objects.none()
    tbr_awards = PersonalTBRCompletionAward.objects.filter(
        month=submission.month, participant=submission.participant,
    ) if tbr_matches.filter(is_qualifying=True).exists() else PersonalTBRCompletionAward.objects.none()
    counts = (
        ("Affected Reader", 1),
        ("Affected Challenge", 1),
        ("Theme claims", theme_claims.count()),
        ("Theme modifier provenance", ModifierProvenance.objects.filter(
            submission=submission, source_type=ModifierProvenance.SourceType.THEME_BONUS,
        ).count()),
        ("BOTM matches", botm_matches.count()),
        ("BOTM per-book provenance", ModifierProvenance.objects.filter(
            submission=submission, source_type=ModifierProvenance.SourceType.BOTM_BOOK,
        ).count()),
        ("Personal TBR matches", tbr_matches.count()),
        ("Personal TBR per-book provenance", ModifierProvenance.objects.filter(
            submission=submission, source_type=ModifierProvenance.SourceType.TBR_BOOK,
        ).count()),
        ("Completion awards potentially affected", botm_awards.count() + tbr_awards.count()),
        ("Completion frozen-book references potentially affected",
         BotmCompletionAwardBook.objects.filter(award__in=botm_awards).count()
         + PersonalTBRCompletionAwardBook.objects.filter(award__in=tbr_awards).count()),
        ("Other submission-linked modifier provenance", ModifierProvenance.objects.filter(
            submission=submission,
        ).exclude(source_type__in=(
            ModifierProvenance.SourceType.THEME_BONUS,
            ModifierProvenance.SourceType.BOTM_BOOK,
            ModifierProvenance.SourceType.TBR_BOOK,
        )).count()),
        ("Audit/history records retained", AuditEvent.objects.filter(
            object_type="BookSubmission", object_id=str(submission.pk),
        ).count()),
    )
    return RecoveryImpactPreview(
        target_label=submission_recovery_label(submission),
        items=tuple(RecoveryImpactItem(label, count) for label, count in counts),
        warnings=(
            "The submission and its directly linked Theme, BOTM, TBR, and modifier relationships are permanently removed. Shared Reader, Challenge, catalog, completion-award history, and unrelated submissions remain.",
        ),
    )


def purge_submission(*, submission, recovery_request, fail_after_step=None):
    if recovery_request.tier != 3:
        raise ValidationError("Submission purge requires Tier 3 recovery.")
    request = replace(recovery_request, challenge=submission.month, group=submission.month.group)

    def mutation():
        locked = BookSubmission.objects.select_for_update().select_related(
            "month__group", "participant__user", "catalog_book", "catalog_edition",
        ).get(pk=submission.pk)
        impact = submission_purge_impact(locked)
        before_score = _reader_score(locked.month, locked.participant)
        target_id = locked.pk
        target_title = locked.title
        month = locked.month
        participant = locked.participant
        group = month.group

        locked.is_removed = True
        locked.removed_at = timezone.now()
        locked.removed_by = request.actor
        locked.removal_reason = request.reason
        locked.save(update_fields=["is_removed", "removed_at", "removed_by", "removal_reason"])
        _synchronize_reader_sources(month, participant)
        if fail_after_step == "reconciliation":
            raise RuntimeError("Injected submission purge failure after reconciliation.")

        ModifierProvenance.objects.filter(submission=locked).delete()
        if fail_after_step == "provenance":
            raise RuntimeError("Injected submission purge failure after provenance.")
        BotmMatch.objects.filter(submission=locked).delete()
        PersonalTBRMatch.objects.filter(submission=locked).delete()
        if fail_after_step == "matches":
            raise RuntimeError("Injected submission purge failure after matches.")
        ThemeClaim.objects.filter(submission=locked).delete()
        if fail_after_step == "claims":
            raise RuntimeError("Injected submission purge failure after Theme claims.")
        BookSubmission.objects.filter(pk=locked.pk).delete()
        if fail_after_step == "submission":
            raise RuntimeError("Injected submission purge failure after submission.")

        _synchronize_reader_sources(month, participant)
        after_score = _reader_score(month, participant)
        AuditEvent.objects.create(
            actor=request.actor, group=group, action="recovery.submission_purged",
            object_type="BookSubmission", object_id=str(target_id),
            summary=f"Platform recovery permanently purged submission {target_title}.",
        )
        return RecoveryMutationResult(
            after_state={
                "exists": False,
                "reader_score_before": before_score,
                "reader_score_after": after_score,
            },
            impact=impact,
        )
    return execute_recovery_operation(request, mutation)


def set_theme_active(*, theme, active, recovery_request):
    def mutation():
        locked = MonthTheme.objects.select_for_update().select_related("month__group").get(pk=theme.pk)
        before = locked.is_active
        locked.is_active = active
        locked.save(update_fields=["is_active"])
        return {"is_active": active, "previous_is_active": before, "claim_count": locked.claims.count()}
    return execute_recovery_operation(recovery_request, mutation)


def correct_unused_theme(*, theme, values, recovery_request):
    allowed = {"name", "description", "starts_on", "ends_on", "bonus_pages", "allow_stacking", "prompt", "is_visible"}
    if set(values) - allowed:
        raise ValidationError("Unsupported Theme recovery field.")

    def mutation():
        locked = MonthTheme.objects.select_for_update().select_related("month__group").get(pk=theme.pk)
        if locked.claims.exists():
            raise ValidationError("Only a Theme with no claim history can use configuration correction.")
        before = {field: getattr(locked, field) for field in allowed}
        for field, value in values.items():
            setattr(locked, field, value)
        locked.full_clean()
        locked.save(update_fields=sorted(values))
        return {"before": before, "after": {field: getattr(locked, field) for field in allowed}}
    return execute_recovery_operation(recovery_request, mutation)


def _reconcile_theme_claim_locked(claim, *, actor, force_rebuild=False):
    submission = BookSubmission.objects.select_for_update().get(pk=claim.submission_id)
    canonical_reference = _theme_reference(claim)
    candidates = ModifierProvenance.objects.select_for_update().filter(
        submission=submission, source_type=ModifierProvenance.SourceType.THEME_BONUS,
    )
    malformed = candidates.filter(source_context=f"Approved Theme claim {claim.pk}").exclude(
        source_reference=canonical_reference,
    )
    removed_malformed = malformed.count()
    malformed.delete()
    canonical = candidates.filter(source_reference=canonical_reference).first()
    contributes = (
        submission.status == BookSubmission.Status.APPROVED
        and not submission.is_removed
        and claim.status == ThemeClaim.Status.APPROVED
        and claim.approved_bonus_pages > 0
    )
    recreated = False
    if canonical is not None and contributes:
        coherent = (
            canonical.month_id == submission.month_id
            and canonical.participant_id == submission.participant_id
            and canonical.submission_id == submission.pk
            and canonical.amount == claim.approved_bonus_pages
            and canonical.effective_date == submission.completed_on
        )
        if force_rebuild or not coherent:
            canonical.delete()
            canonical = None
            recreated = True
    if not contributes:
        if canonical is not None and canonical.is_active:
            canonical.is_active = False
            canonical.voided_by = actor
            canonical.voided_at = timezone.now()
            canonical.void_reason = "Theme claim no longer qualifies after Platform recovery."
            canonical.save(update_fields=["is_active", "voided_by", "voided_at", "void_reason"])
    elif canonical is None:
        canonical = ModifierProvenance.objects.create(
            month=submission.month, participant=submission.participant, submission=submission,
            source_type=ModifierProvenance.SourceType.THEME_BONUS,
            source_reference=canonical_reference, source_label=claim.theme.name,
            source_context=f"Approved Theme claim {claim.pk}", amount=claim.approved_bonus_pages,
            effective_date=submission.completed_on, applied_by=claim.reviewed_by,
            applied_at=claim.reviewed_at, is_system_generated=True, is_active=True,
        )
        recreated = True
    elif not canonical.is_active:
        canonical.is_active = True
        canonical.voided_by = None
        canonical.voided_at = None
        canonical.void_reason = ""
        canonical.save(update_fields=["is_active", "voided_by", "voided_at", "void_reason"])
    refresh_submission_score(submission)
    return canonical, removed_malformed, recreated


def recover_theme_claim(*, claim, status, recovery_request, force_rebuild=False):
    if status not in ThemeClaim.Status.values:
        raise ValidationError("Choose a valid Theme claim state.")

    def mutation():
        locked = ThemeClaim.objects.select_for_update().select_related(
            "submission__month__group", "submission__participant", "theme",
        ).get(pk=claim.pk)
        before = {
            "status": locked.status,
            "approved_bonus_pages": locked.approved_bonus_pages,
        }
        locked.status = status
        locked.reviewed_by = recovery_request.actor
        locked.reviewed_at = timezone.now()
        if status == ThemeClaim.Status.APPROVED:
            historical_amount = ModifierProvenance.objects.filter(
                source_type=ModifierProvenance.SourceType.THEME_BONUS,
                source_reference=_theme_reference(locked),
            ).values_list("amount", flat=True).first()
            locked.approved_bonus_pages = (
                locked.approved_bonus_pages or historical_amount or locked.theme.bonus_pages
            )
        else:
            locked.approved_bonus_pages = 0
        locked.full_clean()
        locked.save(update_fields=["status", "approved_bonus_pages", "reviewed_by", "reviewed_at"])
        provenance, removed_malformed, recreated = _reconcile_theme_claim_locked(
            locked, actor=recovery_request.actor, force_rebuild=force_rebuild,
        )
        return {
            "before": before,
            "status": locked.status,
            "approved_bonus_pages": locked.approved_bonus_pages,
            "active_provenance_id": provenance.pk if provenance and provenance.is_active else None,
            "malformed_duplicates_removed": removed_malformed,
            "provenance_recreated": recreated,
        }
    return execute_recovery_operation(recovery_request, mutation)


def void_provenance(*, provenance, recovery_request):
    def mutation():
        locked = ModifierProvenance.objects.select_for_update().select_related("submission").get(pk=provenance.pk)
        if locked.source_type not in {
            ModifierProvenance.SourceType.THEME_BONUS,
            ModifierProvenance.SourceType.LEGACY_MODIFIER,
        }:
            raise ValidationError(
                "This source belongs to a later recovery domain and cannot be voided independently in Phase 3F.4."
            )
        claim = _theme_claim_from_provenance(locked)
        if claim is not None and (
            claim.status == ThemeClaim.Status.APPROVED
            and claim.submission.status == BookSubmission.Status.APPROVED
            and not claim.submission.is_removed
            and claim.approved_bonus_pages > 0
        ):
            raise ValidationError("This canonical Theme source still qualifies. Correct the Theme claim instead of voiding its reward.")
        locked.is_active = False
        locked.voided_by = recovery_request.actor
        locked.voided_at = timezone.now()
        locked.void_reason = recovery_request.reason
        locked.save(update_fields=["is_active", "voided_by", "voided_at", "void_reason"])
        if locked.submission_id:
            refresh_submission_score(locked.submission)
        return {"is_active": False, "source_type": locked.source_type, "amount": locked.amount}
    return execute_recovery_operation(recovery_request, mutation)


def rebuild_provenance(*, provenance, recovery_request):
    def mutation():
        locked = ModifierProvenance.objects.select_for_update().select_related("submission").get(pk=provenance.pk)
        if locked.source_type == ModifierProvenance.SourceType.THEME_BONUS:
            claim = _theme_claim_from_provenance(locked)
            if claim is None or claim.submission_id != locked.submission_id:
                raise ValidationError("The Theme provenance has no coherent canonical claim source. Purge the malformed provenance instead.")
            canonical, removed_malformed, recreated = _reconcile_theme_claim_locked(
                ThemeClaim.objects.select_for_update().select_related("submission", "theme").get(pk=claim.pk),
                actor=recovery_request.actor, force_rebuild=True,
            )
            return {
                "canonical_provenance_id": canonical.pk if canonical else None,
                "is_active": bool(canonical and canonical.is_active),
                "malformed_duplicates_removed": removed_malformed,
                "recreated": recreated,
            }
        if locked.source_type == ModifierProvenance.SourceType.LEGACY_MODIFIER:
            locked.full_clean()
            locked.is_active = True
            locked.voided_by = None
            locked.voided_at = None
            locked.void_reason = ""
            locked.save(update_fields=["is_active", "voided_by", "voided_at", "void_reason"])
            if locked.submission_id:
                refresh_submission_score(locked.submission)
            return {"canonical_provenance_id": locked.pk, "is_active": True, "recreated": False}
        raise ValidationError(
            "This source belongs to a later recovery domain. Its canonical rebuild is intentionally unavailable in Phase 3F.4."
        )
    return execute_recovery_operation(recovery_request, mutation)


def purge_malformed_provenance(*, provenance, recovery_request):
    if recovery_request.tier != 3:
        raise ValidationError("Malformed provenance purge requires Tier 3 recovery.")

    def mutation():
        locked = ModifierProvenance.objects.select_for_update().select_related("submission").get(pk=provenance.pk)
        if locked.source_type != ModifierProvenance.SourceType.THEME_BONUS:
            raise ValidationError("Only malformed Theme provenance is purgeable in Phase 3F.4.")
        claim = _theme_claim_from_provenance(locked)
        if claim is not None and claim.submission_id == locked.submission_id:
            raise ValidationError("Coherent Theme provenance must be rebuilt or reconciled, not purged.")
        submission = locked.submission
        target_id = locked.pk
        locked.delete()
        if submission is not None:
            refresh_submission_score(submission)
        return {"exists": False, "purged_provenance_id": target_id}
    return execute_recovery_operation(recovery_request, mutation)
