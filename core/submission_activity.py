"""Submission activity from existing audit records, with explicit scope and safe labels."""
import json

from django.db.models import CharField, Exists, OuterRef, Q
from django.db.models.functions import Cast

from .models import AuditEvent, BookSubmission, BotmMatch, Membership, PersonalTBRMatch
from .permissions import can_view_challenge, membership_for, restricted_host_team_ids, scope_reviewable_submissions

LABELS = {
    "submission.created": "Submitted",
    "submission.restored": "Restored",
    "submission.approved": "Approved",
    "submission.rejected": "Rejected",
    "submission.pending": "Returned to review",
    "submission.review_changed": "Review updated",
    "submission.corrected": "Submission edited",
    "submission.credit_assign": "Challenge credit assigned",
    "submission.credit_reverse": "Challenge credit reversed",
    "submission.credit_restore": "Challenge credit restored",
    "personal_tbr.match_confirmed": "TBR match confirmed",
    "personal_tbr.match_rejected": "TBR match rejected",
    "botm.match_confirmed": "Book of the Month match confirmed",
    "botm.match_rejected": "Book of the Month match rejected",
}
READER_ACTIONS = {
    "submission.created", "submission.approved", "submission.rejected",
    "submission.pending", "submission.review_changed", "submission.corrected", "submission.restored",
}
REVIEW_ACTIONS = {"submission.approved", "submission.rejected", "submission.pending", "submission.review_changed"}
MATCH_MODELS = {model.__name__: model for model in (PersonalTBRMatch, BotmMatch)}
CORRECTION_FIELDS = {
    "title": "Title", "author": "Author", "completed_on": "Completion date",
    "catalog_book_id": "Linked book", "catalog_edition_id": "Edition",
    "submitted_pages": "Page count", "approved_pages": "Approved pages",
    "final_scored_pages": "Scored pages", "verification_method": "Page verification",
}


def can_view_staff_activity(actor, month=None):
    from .permissions import challenge_review_scope

    if not actor.is_authenticated or not actor.is_active:
        return False
    if actor.is_superuser:
        return True
    if month is None or not can_view_challenge(actor, month):
        return False
    scope = challenge_review_scope(actor, month)
    return bool(scope and (scope[0] == "challenge" or scope[1]))



def activity_scope(actor, month=None, *, include_own_voided=False):
    empty = BookSubmission.objects.none()
    if not actor.is_authenticated or not actor.is_active:
        return empty, empty
    if month is None:
        return (BookSubmission.objects.all(), empty) if actor.is_superuser else (empty, empty)
    if not can_view_challenge(actor, month):
        return empty, empty
    submissions = BookSubmission.objects.filter(month=month)
    if actor.is_superuser:
        return submissions, empty
    member = membership_for(actor, month.group)
    if member is None:
        return empty, empty
    if member.role == Membership.Role.OWNER and restricted_host_team_ids(actor, month) is None:
        staff = submissions
    else:
        staff = scope_reviewable_submissions(actor, month, submissions)
    own = submissions.filter(participant=member)
    if not include_own_voided:
        own = own.filter(is_removed=False)
    return staff, own


def _targets(query, group_field):
    return Exists(query.annotate(activity_id=Cast("pk", CharField())).filter(
        activity_id=OuterRef("object_id"), **{group_field: OuterRef("group_id")},
    ))


def activity_events(staff, own):
    staff_events = Q(
        object_type="BookSubmission", action__in=LABELS,
    ) & _targets(staff, "month__group_id")
    reader_events = Q(
        object_type="BookSubmission", action__in=READER_ACTIONS,
    ) & _targets(own, "month__group_id")
    for name, model in MATCH_MODELS.items():
        prefix = "botm" if model is BotmMatch else "personal_tbr"
        staff_events |= Q(
            object_type=name, action__in=[f"{prefix}.match_confirmed", f"{prefix}.match_rejected"],
        ) & _targets(model.objects.filter(submission__in=staff), "submission__month__group_id")
    return AuditEvent.objects.filter(staff_events | reader_events).select_related("actor").order_by("-created_at", "-pk")


def _safe_details(event):
    try:
        data = json.loads(event.summary)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    if event.action == "submission.corrected":
        before, after = data.get("before"), data.get("after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            return []
        old, new = before.get("submission"), after.get("submission")
        if not isinstance(old, dict) or not isinstance(new, dict):
            return []
        labels = [label for key, label in CORRECTION_FIELDS.items() if key in old and key in new and old[key] != new[key]]
        if before.get("claims") != after.get("claims"):
            labels.append("Themes")
        return ["Changed " + ", ".join(labels) + "."] if labels else []
    if event.action in REVIEW_ACTIONS:
        statuses = dict(BookSubmission.Status.choices)
        old, new = data.get("previous_status"), data.get("status")
        if isinstance(old, str) and isinstance(new, str) and old in statuses and new in statuses and old != new:
            return [f"{statuses[old]} → {statuses[new]}"]
    return []


def _restoration_history_details(event):
    try:
        removal = json.loads(event.summary)["previous_removal"]
        reason = removal["reason"]
    except (TypeError, ValueError, KeyError):
        return []
    return [f"Previously voided. Removal reason: {reason or 'No reason recorded'}"] if isinstance(reason, str) else []



def activity_cards(events, staff):
    events = list(events)
    targets = {}
    for name, model in MATCH_MODELS.items():
        ids = [int(e.object_id) for e in events if e.object_type == name]
        targets[name] = dict(model.objects.filter(pk__in=ids).values_list("pk", "submission_id"))
    pairs = [(event, int(event.object_id) if event.object_type == "BookSubmission"
              else targets[event.object_type].get(int(event.object_id))) for event in events]
    ids = {pk for _, pk in pairs if pk is not None}
    submissions = BookSubmission.objects.filter(pk__in=ids).select_related("month__group", "participant__user").in_bulk()
    staff_ids = set(staff.filter(pk__in=ids).values_list("pk", flat=True))
    return [{
        "submission": submissions[pk], "label": LABELS[event.action], "timestamp": event.created_at,
        "actor": event.actor.get_username() if event.actor and pk in staff_ids else "",
        "details": (_restoration_history_details(event) if event.action == "submission.restored"
                    else _safe_details(event) if pk in staff_ids else []),
    } for event, pk in pairs if pk in submissions and (pk in staff_ids or event.action in READER_ACTIONS)]


def submission_timeline(actor, submission, page_number=1):
    from django.core.paginator import Paginator

    staff, own = (scope.filter(pk=submission.pk) for scope in activity_scope(actor, submission.month, include_own_voided=True))
    staff_access = staff.exists()
    if not staff_access and not own.exists():
        return None
    events = activity_events(staff, own)
    edited = events.filter(object_type="BookSubmission", action="submission.corrected").first()
    reviewed = events.filter(object_type="BookSubmission", action__in=REVIEW_ACTIONS).first()
    page = Paginator(events, 25).get_page(page_number)
    return {
        "page": page, "cards": activity_cards(page.object_list, staff),
        "edited": edited, "reviewed": reviewed,
        "voided": submission.is_removed,
        "removed_at": submission.removed_at,
        "removal_reason": submission.removal_reason,
        "removed_actor": submission.removed_by.get_username() if submission.is_removed and staff_access and submission.removed_by else "",

        "edited_actor": edited.actor.get_username() if staff_access and edited and edited.actor else "",
    }
