import json
from dataclasses import replace

from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (
    ImmersiveRequest, AuditEvent, BookSubmission, BotmCompletionAward,
    BotmCompletionAwardBook, BotmMatch, ModifierProvenance, MonthTheme,
    PersonalTBRCompletionAward, PersonalTBRCompletionAwardBook,
    PersonalTBRMatch, ThemeClaim, ChallengeMonth, MonthEnrollment, TeamAssignment,
)
from .recovery import (
    RecoveryImpactItem, RecoveryImpactPreview, RecoveryMutationResult,
    execute_recovery_operation, validate_reason,
)
from .score_aggregation import challenge_score_totals, challenge_team_totals
from .scoring import refresh_submission_score, establish_theme_award, establish_restored_theme_awards


def submission_recovery_label(submission):
    return f"Submission #{submission.pk}: {submission.title} — {submission.participant.user.username}"


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


def _synchronize_submission_sources(submission, *, restoring=False):
    from .matching_resolution import synchronize_domain
    for domain in ("botm", "tbr"):
        synchronize_domain(submission, domain, earned_only=restoring, restore_earned=restoring)
    refresh_submission_score(submission)


def _set_locked_submission_removed(locked, *, removed, actor, reason, action):
    previous_removal = {
        "actor_id": locked.removed_by_id,
        "timestamp": locked.removed_at.isoformat() if locked.removed_at else None,
        "reason": locked.removal_reason,
    }
    from .historical_restoration import (
        apply_restoration_plan, has_void_evidence, restoration_plan, suspend_earned_credit,
    )
    before_score = _reader_score(locked.month, locked.participant)
    before_award_ids = list(ModifierProvenance.objects.filter(
        month=locked.month, participant=locked.participant, is_active=True,
    ).values_list("pk", flat=True))
    team_ids = list(locked.month.teams.values_list("pk", flat=True))
    before_teams = challenge_team_totals(month=locked.month, team_ids=team_ids)
    plan = restoration_plan(locked, lock=True) if not removed and has_void_evidence(locked) else None
    if removed:
        locked.is_removed = True
        locked.removed_at = timezone.now()
        locked.removed_by = actor
        locked.removal_reason = reason
    else:
        if not locked.is_removed:
            raise ValidationError("Only a soft-removed submission can be restored.")
        locked.is_removed = False
        locked.removed_at = None
        locked.removed_by = None
        locked.removal_reason = ""
    locked._prompt_actor = actor
    locked.save(update_fields=[
        "is_removed", "removed_at", "removed_by", "removal_reason",
    ])
    historical_recovery = action.startswith("recovery.") and (
        locked.month.status in {locked.month.Status.COMPLETED, locked.month.Status.ARCHIVED}
        or not ordinary_restore_preserves_attribution(locked)
        or MonthEnrollment.objects.filter(
            month=locked.month, participant=locked.participant, awaiting_scoring_team=True,
        ).exists()
    )
    held = []
    if removed and locked.participation_period_id:
        suspend_earned_credit(locked)
    elif plan is not None:
        held = apply_restoration_plan(locked, plan)
    elif historical_recovery:
        # Only this submission changed. Reconcile its sources and dependent
        # completion sets without re-awarding the rest of the Reader's history.
        from .matching_resolution import synchronize_domain

        for domain in ("botm", "tbr"):
            synchronize_domain(locked, domain, earned_only=True, restore_earned=not removed)
        refresh_submission_score(locked, earned_only=True, restore_earned=not removed)
    else:
        if not removed:
            establish_restored_theme_awards(locked)
        _synchronize_submission_sources(locked, restoring=not removed)
    locked.refresh_from_db(fields=["bonus_pages", "final_scored_pages"])
    after_score = _reader_score(locked.month, locked.participant)
    AuditEvent.objects.create(
        actor=actor, group=locked.month.group,
        action=action,
        object_type="BookSubmission", object_id=str(locked.pk),
        summary=json.dumps({
            "submission_id": locked.pk, "previous_removal": previous_removal,
            "previous_status": locked.status, "status": locked.status,
            "restoration_reason": reason,
            "reader_score_before": before_score, "reader_score_after": after_score,
            "held_credit": held,
        }) if action == "submission.restored" else
        f"Platform recovery {'soft removed' if removed else 'restored'} submission {locked.title}.",
    )
    return {
        "is_removed": locked.is_removed,
        "bonus_pages": locked.bonus_pages,
        "final_scored_pages": locked.final_scored_pages,
        "reader_score_before": before_score,
        "reader_score_after": after_score,
        "team_score_before": before_teams,
        "team_score_after": challenge_team_totals(month=locked.month, team_ids=team_ids),
        "held_credit": held,
        "awards": list(ModifierProvenance.objects.filter(
            month=locked.month, participant=locked.participant, is_active=True,
        ).exclude(pk__in=before_award_ids).order_by("pk").values("source_type", "source_reference", "amount")),
    }


def set_submission_removed(*, submission, removed, recovery_request, preview_token=None):
    if not removed and recovery_request.tier < 2:
        raise ValidationError("Submission restoration requires Tier 2 Advanced Recovery.")
    def mutation():
        ChallengeMonth.objects.select_for_update().get(pk=submission.month_id)
        locked = BookSubmission.objects.select_for_update().select_related(
            "month__group", "participant__user"
        ).get(pk=submission.pk)
        from .historical_restoration import lock_restoration_state
        lock_restoration_state(locked)
        if not removed:
            from .recovery import validate_typed_confirmation
            validate_typed_confirmation(required=submission_recovery_label(locked),
                                        supplied=recovery_request.supplied_confirmation)
            if preview_token is None:
                raise ValidationError("Review the current restoration score preview before confirming.")
            if preview_token is not None:
                _validate_restore_preview(locked, recovery_request.actor, preview_token)
        result = _set_locked_submission_removed(
            locked, removed=removed, actor=recovery_request.actor, reason=recovery_request.reason,
            action="recovery.submission_soft_removed" if removed else "recovery.submission_restored",
        )
        if not removed and preview_token is not None and json.loads(json.dumps(result, default=str)) != expected_restore_result(preview_token):
            raise ValidationError("The executed score changes differ from the approved preview.")
        return RecoveryMutationResult(after_state=result, impact=result)
    return execute_recovery_operation(recovery_request, mutation)


RESTORE_CONFIRMATION_SALT = "northbound.staff-submission-restore"


def _void_confirmation_state(submission, actor):
    return [actor.pk, submission.pk, submission.is_removed,
            submission.removed_at.isoformat() if submission.removed_at else None,
            submission.removed_by_id, submission.removal_reason, submission.status,
            AuditEvent.objects.filter(
                group_id=submission.month.group_id, object_type="BookSubmission", object_id=str(submission.pk),
                action__in=["submission.removed", "recovery.submission_soft_removed"],
            ).order_by("-pk").values_list("pk", flat=True).first(),
            restoration_state(submission)]


def staff_restore_confirmation(submission, actor):
    return signing.dumps(_void_confirmation_state(submission, actor), salt=RESTORE_CONFIRMATION_SALT)


def ordinary_restore_preserves_attribution(submission):
    if submission.participation_period_id:
        from .historical_restoration import ordinary_historical_eligible
        return ordinary_historical_eligible(submission)
    enrollment = MonthEnrollment.objects.filter(month_id=submission.month_id, participant_id=submission.participant_id)
    if not (submission.participant.is_active and submission.participant.user.is_active
            and enrollment.filter(is_active=True).exists()):
        return False
    if TeamAssignment.objects.filter(
        month_id=submission.month_id, participant_id=submission.participant_id, ended_at__isnull=False,
    ).exists():
        return False
    # A Reader can have left and reenrolled without ever having a team.
    return not AuditEvent.objects.filter(
        group_id=submission.month.group_id, object_type="MonthEnrollment",
        object_id__in=[str(pk) for pk in enrollment.values_list("pk", flat=True)],
        action__in=["participation.self_reactivated", "participation.staff_reactivated",
                    "participation.self_withdrew", "participation.staff_removed"],
    ).exists()


@transaction.atomic
def restore_submission_as_staff(*, submission, actor, confirmation, reason=""):
    from .permissions import can_restore_submission

    month = ChallengeMonth.objects.select_for_update().get(pk=submission.month_id)
    locked = BookSubmission.objects.select_for_update().select_related("participant__user").get(
        pk=submission.pk, month=month,
    )
    locked.month = month
    from .historical_restoration import lock_restoration_state
    lock_restoration_state(locked)
    if not locked.is_removed:
        raise ValidationError("This submission is already restored.")
    if not can_restore_submission(actor, locked):
        raise PermissionDenied("Submission restoration permission is required.")
    try:
        confirmed = signing.loads(confirmation if isinstance(confirmation, str) else "", salt=RESTORE_CONFIRMATION_SALT, max_age=900)
    except signing.BadSignature:
        confirmed = None
    if confirmed != _void_confirmation_state(locked, actor):
        raise ValidationError("This confirmation is stale or invalid. Reload and confirm the current void.")
    if not ordinary_restore_preserves_attribution(locked):
        raise ValidationError("Ordinary restoration is unavailable for this Reader’s enrollment or team history.")
    return _set_locked_submission_removed(
        locked, removed=False, actor=actor, reason=validate_reason(reason, required=False),
        action="submission.restored",
    )


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
        ("Blocking Immersive requests", ImmersiveRequest.objects.filter(submission=submission).count()),
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
    warnings = (
        ('Purge blocked: protected Immersive requests must be preserved. No records will be deleted.',)
        if ImmersiveRequest.objects.filter(submission=submission).exists() else
        ('The submission and its directly linked Theme, BOTM, TBR, and modifier relationships are permanently removed. Shared Reader, Challenge, catalog, completion-award history, and unrelated submissions remain.',)
    )
    return RecoveryImpactPreview(
        target_label=submission_recovery_label(submission),
        items=tuple(RecoveryImpactItem(label, count) for label, count in counts),
        warnings=warnings,
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
        if ImmersiveRequest.objects.filter(submission=locked).exists():
            raise ValidationError('Purge blocked: protected Immersive requests must be preserved. No records were deleted.')
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
    allowed = {"name", "description", "starts_on", "ends_on", "bonus_pages", "bonus_percentage", "allow_stacking", "prompt", "is_visible"}
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
            if locked.theme.bonus_rule == MonthTheme.BonusRule.PERCENTAGE:
                locked.approved_bonus_pages = establish_theme_award(locked)
            else:
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
        locked.save()
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
    from .prompt_scoring import SOURCE_TYPES
    if provenance.source_type in SOURCE_TYPES:
        raise ValidationError('Prompt provenance requires a previewed prompt qualification correction; generic provenance recovery cannot rewrite frozen receipts.')
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
    from .prompt_scoring import SOURCE_TYPES
    if provenance.source_type in SOURCE_TYPES:
        raise ValidationError('Prompt provenance requires a previewed prompt qualification correction; generic provenance recovery cannot rewrite frozen receipts.')
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
    from .prompt_scoring import SOURCE_TYPES
    if provenance.source_type in SOURCE_TYPES:
        raise ValidationError('Prompt provenance requires a previewed prompt qualification correction; generic provenance recovery cannot rewrite frozen receipts.')
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


RESTORE_PREVIEW_SALT = "northbound.restore-score-preview"


def restoration_state(submission):
    from .historical_restoration import fingerprint
    from .submission_review_matching import review_state
    return fingerprint({
        "review": review_state(submission),
        "group_timezone": submission.month.group.timezone,
        "provenance": list(ModifierProvenance.objects.filter(
            month=submission.month, participant=submission.participant,
        ).order_by("pk").values()),
        "immersive": list(submission.immersive_requests.order_by("pk").values()),
        "botm_completion": list(BotmCompletionAward.objects.filter(
            month=submission.month, participant=submission.participant,
        ).order_by("pk").values()),
        "botm_frozen": list(BotmCompletionAwardBook.objects.filter(
            award__month=submission.month, award__participant=submission.participant,
        ).order_by("pk").values()),
        "tbr_completion": list(PersonalTBRCompletionAward.objects.filter(
            month=submission.month, participant=submission.participant,
        ).order_by("pk").values()),
        "tbr_frozen": list(PersonalTBRCompletionAwardBook.objects.filter(
            award__month=submission.month, award__participant=submission.participant,
        ).order_by("pk").values()),
    })


def expected_restore_result(token):
    try:
        return signing.loads(token, salt=RESTORE_PREVIEW_SALT, max_age=900)["result"]
    except (signing.BadSignature, KeyError, TypeError):
        raise ValidationError("The restoration score preview expired. Reload and review it again.") from None


def _validate_restore_preview(submission, actor, token):
    try:
        payload = signing.loads(token, salt=RESTORE_PREVIEW_SALT, max_age=900)
    except (signing.BadSignature, TypeError):
        raise ValidationError("The restoration score preview expired. Reload and review it again.") from None
    if (payload.get("actor"), payload.get("submission"), payload.get("state")) != (
        actor.pk, submission.pk, restoration_state(submission),
    ):
        raise ValidationError("The restoration evidence changed. Review the current score preview again.")


@transaction.atomic
def preview_submission_restoration(*, submission, actor):
    from .recovery import require_platform_owner
    require_platform_owner(actor)
    ChallengeMonth.objects.select_for_update().get(pk=submission.month_id)
    locked = BookSubmission.objects.select_for_update().get(pk=submission.pk)
    if not locked.is_removed:
        raise ValidationError("Only a voided submission can be previewed.")
    from .historical_restoration import lock_restoration_state
    lock_restoration_state(locked)
    state = restoration_state(locked)
    # Match the established correction-preview pattern. All database writes and
    # on-commit callbacks in this savepoint are rolled back before returning.
    with transaction.atomic():
        result = _set_locked_submission_removed(
            locked, removed=False, actor=actor, reason="Restoration preview", action="recovery.submission_restored",
        )
        transaction.set_rollback(True)
    result = json.loads(json.dumps(result, default=str))
    token = signing.dumps({"actor": actor.pk, "submission": submission.pk, "state": state,
                           "result": result}, salt=RESTORE_PREVIEW_SALT, serializer=signing.JSONSerializer)
    return result, token
