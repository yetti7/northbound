from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import ChallengeMonth
from .progress_checkpoints import process_due_progress_checkpoints


def due_challenge_ids(*, now):
    registration_open_due = Q(
        auto_open_registration=True,
        registration_opens_at__lte=now,
    ) & (
        Q(status=ChallengeMonth.Status.DRAFT)
        | Q(
            status__in=(ChallengeMonth.Status.UPCOMING, ChallengeMonth.Status.ACTIVE),
            registration_is_open=False,
        )
    )
    registration_close_due = Q(
        auto_close_registration=True,
        registration_closes_at__lte=now,
        registration_is_open=True,
    )
    challenge_start_due = Q(
        auto_start_challenge=True,
        starts_at__lte=now,
        status=ChallengeMonth.Status.UPCOMING,
    )
    challenge_end_due = Q(
        auto_end_challenge=True,
        ends_at__lte=now,
        status=ChallengeMonth.Status.ACTIVE,
    )
    final_announcement_due = Q(
        auto_complete_challenge=True,
        final_announcement_at__lte=now,
        status=ChallengeMonth.Status.FINALIZING,
    )
    return list(
        ChallengeMonth.objects.filter(
            registration_open_due
            | registration_close_due
            | challenge_start_due
            | challenge_end_due
            | final_announcement_due
        ).values_list("pk", flat=True)
    )


def process_due_challenge_schedules(*, now=None):
    current_time = now or timezone.now()
    processed = []
    for challenge_id in due_challenge_ids(now=current_time):
        with transaction.atomic():
            challenge = (
                ChallengeMonth.objects.select_for_update()
                .select_related("group")
                .filter(pk=challenge_id)
                .first()
            )
            if challenge is None:
                continue
            actions = challenge.apply_scheduled_actions(now=current_time)
        if actions:
            processed.append((challenge_id, actions))
    for challenge_id, checkpoint_id, outcome, result_count in process_due_progress_checkpoints(now=current_time):
        processed.append((
            challenge_id,
            [f"checkpoint_{outcome}:{checkpoint_id}:{result_count}"],
        ))
    return processed
