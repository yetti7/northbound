from dataclasses import dataclass

from django.db.models import Sum

from .models import BookSubmission, ChallengeMonth, MonthEnrollment


@dataclass(frozen=True)
class ReaderPlanningData:
    completed_challenges: int
    average_pages: float | None
    last_challenge_pages: int | None

    @property
    def average_pages_display(self):
        if self.average_pages is None:
            return "N/A"
        return f"{self.average_pages:.1f}".removesuffix(".0")

    @property
    def last_challenge_pages_display(self):
        return "N/A" if self.last_challenge_pages is None else str(self.last_challenge_pages)


def historical_reader_planning_data(*, month, participant_ids):
    """Return Group-scoped historical planning data without per-Reader queries."""
    participant_ids = tuple(participant_ids)
    if not participant_ids:
        return {}

    participation_rows = list(
        MonthEnrollment.objects.filter(
            participant_id__in=participant_ids,
            month__group_id=month.group_id,
            month__status=ChallengeMonth.Status.COMPLETED,
        )
        .exclude(month_id=month.pk)
        .values("participant_id", "month_id", "month__starts_on")
        .order_by("participant_id", "month__starts_on", "month_id")
    )
    qualifying_month_ids = {row["month_id"] for row in participation_rows}
    page_totals = {
        (row["participant_id"], row["month_id"]): row["pages"] or 0
        for row in BookSubmission.objects.filter(
            participant_id__in=participant_ids,
            month_id__in=qualifying_month_ids,
            status=BookSubmission.Status.APPROVED,
            is_removed=False,
        )
        .values("participant_id", "month_id")
        .annotate(pages=Sum("approved_pages"))
    }

    rows_by_participant = {participant_id: [] for participant_id in participant_ids}
    for row in participation_rows:
        rows_by_participant[row["participant_id"]].append(row)

    result = {}
    for participant_id, rows in rows_by_participant.items():
        if not rows:
            result[participant_id] = ReaderPlanningData(0, None, None)
            continue
        challenge_pages = [
            page_totals.get((participant_id, row["month_id"]), 0)
            for row in rows
        ]
        result[participant_id] = ReaderPlanningData(
            completed_challenges=len(rows),
            average_pages=sum(challenge_pages) / len(challenge_pages),
            last_challenge_pages=challenge_pages[-1],
        )
    return result
