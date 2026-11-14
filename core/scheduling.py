from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import ChallengeMonth
from .progress_checkpoints import process_due_progress_checkpoints


def due_challenge_ids(*, now):
    registration_open_due = Q(
        auto_open_registration=True,
        registration_open_handled=False,
        registration_opens_at__lte=now,
        status__in=(ChallengeMonth.Status.DRAFT, ChallengeMonth.Status.UPCOMING, ChallengeMonth.Status.ACTIVE),
    )
    registration_close_due = Q(
        auto_close_registration=True,
        registration_close_handled=False,
        registration_closes_at__lte=now,
    ) & ~Q(status__in=(ChallengeMonth.Status.COMPLETED, ChallengeMonth.Status.ARCHIVED))
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


REGISTRATION_EVENTS = {
    "open": ("auto_open_registration", "registration_opens_at", "registration_open_handled"),
    "close": ("auto_close_registration", "registration_closes_at", "registration_close_handled"),
}
LIFECYCLE_MILESTONES = {
    ChallengeMonth.Status.ACTIVE: ("auto_start_challenge", "starts_at", "Automatically start challenge"),
    ChallengeMonth.Status.FINALIZING: ("auto_end_challenge", "ends_at", "Automatically end submissions"),
    ChallengeMonth.Status.COMPLETED: ("auto_complete_challenge", "final_announcement_at", "Automatically finalize results"),
}
REGISTRATION_STAGES = {ChallengeMonth.Status.UPCOMING, ChallengeMonth.Status.ACTIVE}


def milestone_overdue(month, target, now):
    toggle, timestamp, _ = LIFECYCLE_MILESTONES[target]
    scheduled = getattr(month, timestamp)
    return getattr(month, toggle) and scheduled is not None and scheduled <= now


def lifecycle_due_target(month, now, *, status=None):
    order = month.lifecycle_order()
    index = order.index(status or month.status)
    if index + 1 < len(order):
        target = order[index + 1]
        if (target in LIFECYCLE_MILESTONES and target not in month.held_schedule_milestones
                and milestone_overdue(month, target, now)):
            return target
    return None


def backward_schedule_conflict(month, target, now):
    if lifecycle_due_target(month, now, status=target) == month.status:
        label = LIFECYCLE_MILESTONES[month.status][2]
        reason = {
            month.Status.ACTIVE: "This challenge is set to start automatically, and its start date has already passed.",
            month.Status.FINALIZING: "Submissions are set to close automatically, and the closing date has already passed.",
            month.Status.COMPLETED: "Results are set to become final automatically, and the announcement date has already passed.",
        }[month.status]
    elif (
        target == month.Status.DRAFT and month.auto_open_registration
        and not month.registration_open_handled and month.registration_opens_at
        and month.registration_opens_at <= now
    ):
        label = "Automatically open registration"
        reason = "Registration is set to open automatically, and the opening date has already passed."
    else:
        return ""
    return (
        f"Can’t move this challenge back yet. {reason} "
        f"Turn off “{label}” or choose a future date, then try again."
    )


def manual_registration_action(month):
    if month.status not in REGISTRATION_STAGES:
        return None
    if month.registration_is_open:
        return None if month.auto_close_registration else "close"
    return None if month.auto_open_registration else "open"


@transaction.atomic
def set_manual_registration(*, month_id, actor, action):
    from django.core.exceptions import PermissionDenied, ValidationError

    from .models import AuditEvent
    from .permissions import can_transition_challenge, challenge_history_locked

    month = ChallengeMonth.objects.select_for_update().select_related("group").get(pk=month_id)
    if not can_transition_challenge(actor, month):
        raise PermissionDenied
    if challenge_history_locked(month) or action not in REGISTRATION_EVENTS:
        raise ValidationError("This registration action is unavailable.")
    toggle, _, _ = REGISTRATION_EVENTS[action]
    if getattr(month, toggle) or month.status not in REGISTRATION_STAGES:
        raise ValidationError("This registration action is unavailable with the current schedule or Challenge status.")
    desired = action == "open"
    if month.registration_is_open == desired:
        return False
    month.registration_is_open = desired
    month.save(update_fields=["registration_is_open"])
    AuditEvent.objects.create(
        actor=actor, group=month.group, action=f"challenge.registration_{'opened' if desired else 'closed'}",
        object_type="ChallengeMonth", object_id=str(month.pk),
        summary=f"Manually {'opened' if desired else 'closed'} registration for {month.name}.",
    )
    return True


def prepare_schedule_save(month, changed_fields, now):
    """Prepare a locked form instance for a save and identify any immediate transition."""
    registration_changed = False
    for toggle, timestamp, handled in REGISTRATION_EVENTS.values():
        if {toggle, timestamp}.intersection(changed_fields):
            setattr(month, handled, False)
            registration_changed = True

    changed_milestones = {
        target for target, (toggle, timestamp, _) in LIFECYCLE_MILESTONES.items()
        if {toggle, timestamp}.intersection(changed_fields)
    }
    holds = set(month.held_schedule_milestones) - changed_milestones
    if changed_milestones or registration_changed:
        order = month.lifecycle_order()
        upcoming_index = order.index(month.status) + 1
        publishing = (
            registration_changed and month.status == month.Status.DRAFT
            and month.auto_open_registration and not month.registration_open_handled
            and month.registration_opens_at and month.registration_opens_at <= now
        )
        # If a save can move the Challenge, defer other overdue milestones.
        # Also defer a newly scheduled overdue milestone when its stage is not yet eligible.
        immediate = None
        if changed_milestones and not publishing:
            month.held_schedule_milestones = sorted(holds)
            immediate = lifecycle_due_target(month, now)
        for target in order[upcoming_index:]:
            if target == immediate or target not in LIFECYCLE_MILESTONES:
                continue
            if milestone_overdue(month, target, now) and (immediate or publishing or target in changed_milestones):
                holds.add(target)
        month.held_schedule_milestones = sorted(holds)
        return registration_changed, immediate
    return False, None


def reconcile_saved_registration(month, changed_fields, now):
    """Reevaluate a newly saved registration window without rearming unrelated events."""
    if month.status in REGISTRATION_STAGES:
        opens = month.registration_opens_at if month.auto_open_registration else None
        closes = month.registration_closes_at if month.auto_close_registration else None
        opening_edited = {"auto_open_registration", "registration_opens_at"}.intersection(changed_fields)
        closing_edited = {"auto_close_registration", "registration_closes_at"}.intersection(changed_fields)
        if (opens and opens > now and opening_edited) or (closes and closes <= now):
            month.registration_is_open = False
        elif opens and opens <= now and closes and closes > now and closing_edited:
            month.registration_is_open = True
        month.save(update_fields=["registration_is_open"])
    actions = month.apply_scheduled_actions(now=now, lifecycle=False)
    # A new opening cannot make an already expired automatic window available.
    if (
        month.registration_is_open and month.auto_close_registration
        and month.registration_closes_at and month.registration_closes_at <= now
    ):
        month.registration_is_open = False
        month.save(update_fields=["registration_is_open"])
    return actions
