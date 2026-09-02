from django.core.exceptions import ValidationError
from django.db.models import Count, Q

from .models import ProgressCheckpoint, ProgressCheckpointResult
from .recovery import (
    RecoveryImpactItem,
    RecoveryImpactPreview,
    RecoveryMutationResult,
    execute_recovery_operation,
)


def checkpoint_recovery_label(checkpoint):
    return (
        f"Checkpoint #{checkpoint.pk}: {checkpoint.month.name} — "
        f"position {checkpoint.position}"
    )


def checkpoint_configuration_snapshot(checkpoint):
    return {
        "position": checkpoint.position,
        "scheduled_at": checkpoint.scheduled_at.isoformat(),
        "threshold_percentage": checkpoint.threshold_percentage,
        "progress_basis": checkpoint.progress_basis,
        "target_basis": checkpoint.target_basis,
        "fixed_target_pages": checkpoint.fixed_target_pages,
    }


def checkpoint_result_summary(checkpoint):
    summary = checkpoint.results.aggregate(
        result_count=Count("pk"),
        affected_reader_count=Count("participant_id", distinct=True),
        met_count=Count("pk", filter=Q(outcome=ProgressCheckpointResult.Outcome.MET)),
        below_count=Count("pk", filter=Q(outcome=ProgressCheckpointResult.Outcome.BELOW)),
        not_evaluated_count=Count(
            "pk", filter=Q(outcome=ProgressCheckpointResult.Outcome.NOT_EVALUATED),
        ),
    )
    return {key: value or 0 for key, value in summary.items()}


def checkpoint_reset_impact(checkpoint):
    summary = checkpoint_result_summary(checkpoint)
    return RecoveryImpactPreview(
        target_label=checkpoint_recovery_label(checkpoint),
        items=(
            RecoveryImpactItem("Results removed", summary["result_count"]),
            RecoveryImpactItem("Affected Readers", summary["affected_reader_count"]),
            RecoveryImpactItem("Met", summary["met_count"]),
            RecoveryImpactItem("Below", summary["below_count"]),
            RecoveryImpactItem("Not Evaluated", summary["not_evaluated_count"]),
            RecoveryImpactItem(
                "Needs Attention entries removed", summary["below_count"],
            ),
        ),
        warnings=(
            "All immutable Reader results for this evaluation will be removed together.",
            "The checkpoint will remain under Recovery Hold until a Platform Owner explicitly releases it.",
        ),
    )


def reset_checkpoint_evaluation(*, checkpoint, recovery_request, fail_after_step=None):
    if recovery_request.tier != 3:
        raise ValidationError("Checkpoint evaluation reset requires Tier 3 recovery.")

    def mutation():
        locked = ProgressCheckpoint.objects.select_for_update().select_related(
            "month__group",
        ).get(pk=checkpoint.pk)
        if locked.evaluation_state != ProgressCheckpoint.EvaluationState.EVALUATED:
            raise ValidationError("Only an evaluated checkpoint can be reset.")
        results = locked.results.select_for_update()
        list(results.values_list("pk", flat=True))
        summary = checkpoint_result_summary(locked)
        before = {
            "configuration": checkpoint_configuration_snapshot(locked),
            "evaluation_state": locked.evaluation_state,
            "evaluated_at": locked.evaluated_at.isoformat() if locked.evaluated_at else None,
            **summary,
        }
        results.delete()
        if fail_after_step == "results":
            raise RuntimeError("Injected checkpoint reset failure after result deletion.")
        ProgressCheckpoint.objects.filter(pk=locked.pk).update(
            evaluation_state=ProgressCheckpoint.EvaluationState.PENDING,
            evaluated_at=None,
            recovery_hold=True,
        )
        if fail_after_step == "state":
            raise RuntimeError("Injected checkpoint reset failure after state update.")
        return RecoveryMutationResult(
            after_state={
                "previous_evaluation": before,
                "evaluation_state": ProgressCheckpoint.EvaluationState.PENDING,
                "evaluated_at": None,
                "recovery_hold": True,
                "remaining_result_count": 0,
            },
            impact=checkpoint_reset_impact_from_summary(locked, summary),
        )

    return execute_recovery_operation(recovery_request, mutation)


def checkpoint_reset_impact_from_summary(checkpoint, summary):
    return RecoveryImpactPreview(
        target_label=checkpoint_recovery_label(checkpoint),
        items=(
            RecoveryImpactItem("Results removed", summary["result_count"]),
            RecoveryImpactItem("Affected Readers", summary["affected_reader_count"]),
            RecoveryImpactItem("Met", summary["met_count"]),
            RecoveryImpactItem("Below", summary["below_count"]),
            RecoveryImpactItem("Not Evaluated", summary["not_evaluated_count"]),
            RecoveryImpactItem("Needs Attention entries removed", summary["below_count"]),
        ),
        warnings=(
            "All immutable Reader results for this evaluation were removed together.",
            "Automatic evaluation remains paused until explicit Platform Owner release.",
        ),
    )


def release_checkpoint_evaluation(*, checkpoint, recovery_request, fail_after_step=None):
    if recovery_request.tier != 2:
        raise ValidationError("Checkpoint evaluation release requires Tier 2 recovery.")

    def mutation():
        locked = ProgressCheckpoint.objects.select_for_update().select_related(
            "month__group",
        ).get(pk=checkpoint.pk)
        if not locked.recovery_hold:
            raise ValidationError("This checkpoint is not under Recovery Hold.")
        if locked.evaluation_state != ProgressCheckpoint.EvaluationState.PENDING:
            raise ValidationError("A held checkpoint must be Pending before release.")
        if locked.results.exists():
            raise ValidationError("A held checkpoint with result history cannot be released.")
        before = {
            "configuration": checkpoint_configuration_snapshot(locked),
            "evaluation_state": locked.evaluation_state,
            "recovery_hold": True,
        }
        ProgressCheckpoint.objects.filter(pk=locked.pk).update(recovery_hold=False)
        if fail_after_step == "state":
            raise RuntimeError("Injected checkpoint release failure.")
        return {
            "before": before,
            "configuration_at_release": checkpoint_configuration_snapshot(locked),
            "evaluation_state": ProgressCheckpoint.EvaluationState.PENDING,
            "recovery_hold": False,
            "result_count": 0,
        }

    return execute_recovery_operation(recovery_request, mutation)
