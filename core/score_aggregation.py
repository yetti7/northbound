from django.core.exceptions import ValidationError
from django.db.models import F, Q, Sum

from .models import BookSubmission, ModifierProvenance


def _date_bounds(query, field, *, effective_from, effective_to):
    if effective_from is not None:
        query &= Q(**{f"{field}__gte": effective_from})
    if effective_to is not None:
        query &= Q(**{f"{field}__lte": effective_to})
    return query


def challenge_score_totals(
    *,
    month,
    participant_ids=None,
    effective_from=None,
    effective_to=None,
):
    """Return authoritative Base, Modifier, and Total pages per Reader.

    Base comes from approved, non-removed submissions. Active
    submission-linked provenance follows that submission's eligibility. All
    active provenance is date-filtered by its own effective date.
    The calculation is read-only and never uses the submission Total cache.
    """
    if effective_from is not None and effective_to is not None and effective_from > effective_to:
        raise ValidationError("Score range start cannot be after its end.")

    requested_ids = None if participant_ids is None else {int(participant_id) for participant_id in participant_ids}
    if requested_ids == set():
        return {}
    totals = {
        participant_id: {"base_pages": 0, "modifier_pages": 0, "total_pages": 0}
        for participant_id in (requested_ids or ())
    }

    submissions = BookSubmission.objects.filter(
        month=month,
        status=BookSubmission.Status.APPROVED,
        is_removed=False,
    )
    if requested_ids is not None:
        submissions = submissions.filter(participant_id__in=requested_ids)
    if effective_from is not None:
        submissions = submissions.filter(completed_on__gte=effective_from)
    if effective_to is not None:
        submissions = submissions.filter(completed_on__lte=effective_to)
    for row in submissions.values("participant_id").annotate(base_pages=Sum("approved_pages")):
        participant_id = row["participant_id"]
        totals.setdefault(participant_id, {"base_pages": 0, "modifier_pages": 0, "total_pages": 0})
        totals[participant_id]["base_pages"] = row["base_pages"] or 0

    eligible_source = Q(submission__isnull=True) | Q(
            submission__isnull=False,
            submission__month=month,
            submission__status=BookSubmission.Status.APPROVED,
            submission__is_removed=False,
            participant_id=F("submission__participant_id"),
        )
    eligible_source = _date_bounds(
        eligible_source,
        "effective_date",
        effective_from=effective_from,
        effective_to=effective_to,
    )
    provenance = ModifierProvenance.objects.filter(month=month, is_active=True).filter(eligible_source)
    if requested_ids is not None:
        provenance = provenance.filter(participant_id__in=requested_ids)
    for row in provenance.values("participant_id").annotate(modifier_pages=Sum("amount")):
        participant_id = row["participant_id"]
        totals.setdefault(participant_id, {"base_pages": 0, "modifier_pages": 0, "total_pages": 0})
        totals[participant_id]["modifier_pages"] = row["modifier_pages"] or 0

    for score in totals.values():
        score["total_pages"] = score["base_pages"] + score["modifier_pages"]
    return totals
