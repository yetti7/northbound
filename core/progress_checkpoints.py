from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import (
    BookSubmission,
    ChallengeMonth,
    ProgressCheckpoint,
    ProgressCheckpointResult,
)
from .reader_planning import historical_reader_planning_data


PAST_COMPETITION_STATUSES = {
    ChallengeMonth.Status.FINALIZING,
    ChallengeMonth.Status.COMPLETED,
    ChallengeMonth.Status.ARCHIVED,
}


def due_progress_checkpoint_ids(*, now):
    return list(
        ProgressCheckpoint.objects.filter(
            evaluation_state=ProgressCheckpoint.EvaluationState.PENDING,
            scheduled_at__lte=now,
            month__status__in=(ChallengeMonth.Status.ACTIVE, *PAST_COMPETITION_STATUSES),
        ).values_list("pk", flat=True)
    )


def evaluate_progress_checkpoint(checkpoint, *, now):
    enrollments = list(
        checkpoint.month.enrollments.filter(is_active=True).select_related("participant")
    )
    participant_ids = [enrollment.participant_id for enrollment in enrollments]
    progress_field = (
        "approved_pages"
        if checkpoint.progress_basis == ProgressCheckpoint.ProgressBasis.BASE
        else "final_scored_pages"
    )
    progress_by_participant = {
        row["participant_id"]: row["pages"] or 0
        for row in BookSubmission.objects.filter(
            month=checkpoint.month,
            participant_id__in=participant_ids,
            status=BookSubmission.Status.APPROVED,
            is_removed=False,
        )
        .values("participant_id")
        .annotate(pages=Sum(progress_field))
    }
    planning = {}
    if checkpoint.target_basis == ProgressCheckpoint.TargetBasis.PREVIOUS_AVERAGE:
        planning = historical_reader_planning_data(
            month=checkpoint.month,
            participant_ids=participant_ids,
        )

    results = []
    threshold = Decimal(checkpoint.threshold_percentage) / Decimal(100)
    for enrollment in enrollments:
        progress_pages = progress_by_participant.get(enrollment.participant_id, 0)
        if checkpoint.target_basis == ProgressCheckpoint.TargetBasis.FIXED:
            target_pages = Decimal(checkpoint.fixed_target_pages)
        else:
            average = planning[enrollment.participant_id].average_pages
            target_pages = Decimal(str(average)) if average is not None else None
        required_pages = target_pages * threshold if target_pages is not None else None
        if target_pages is None:
            outcome = ProgressCheckpointResult.Outcome.NOT_EVALUATED
        elif Decimal(progress_pages) >= required_pages:
            outcome = ProgressCheckpointResult.Outcome.MET
        else:
            outcome = ProgressCheckpointResult.Outcome.BELOW
        results.append(ProgressCheckpointResult(
            checkpoint=checkpoint,
            participant=enrollment.participant,
            evaluated_at=now,
            threshold_percentage=checkpoint.threshold_percentage,
            progress_basis=checkpoint.progress_basis,
            target_basis=checkpoint.target_basis,
            target_pages=target_pages,
            required_pages=required_pages,
            progress_pages=progress_pages,
            outcome=outcome,
        ))
    ProgressCheckpointResult.objects.bulk_create(results)
    ProgressCheckpoint.objects.filter(pk=checkpoint.pk).update(
        evaluation_state=ProgressCheckpoint.EvaluationState.EVALUATED,
        evaluated_at=now,
    )
    return len(results)


def process_due_progress_checkpoints(*, now=None):
    current_time = now or timezone.now()
    processed = []
    for checkpoint_id in due_progress_checkpoint_ids(now=current_time):
        with transaction.atomic():
            checkpoint = (
                ProgressCheckpoint.objects.select_for_update()
                .select_related("month__group")
                .filter(pk=checkpoint_id, evaluation_state=ProgressCheckpoint.EvaluationState.PENDING)
                .first()
            )
            if checkpoint is None:
                continue
            if checkpoint.month.status in PAST_COMPETITION_STATUSES:
                ProgressCheckpoint.objects.filter(pk=checkpoint.pk).update(
                    evaluation_state=ProgressCheckpoint.EvaluationState.SKIPPED,
                    evaluated_at=current_time,
                )
                processed.append((checkpoint.month_id, checkpoint.pk, "skipped", 0))
                continue
            result_count = evaluate_progress_checkpoint(checkpoint, now=current_time)
            processed.append((checkpoint.month_id, checkpoint.pk, "evaluated", result_count))
    return processed
