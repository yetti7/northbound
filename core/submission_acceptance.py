from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import MonthTheme, ParticipationPeriod, PersonalTBRBook
from .participation import ensure_participation_period


def record_acceptance(submission, *, enrollment, themes, immersive_requested):
    """Capture evidence under the Challenge and enrollment locks before saving."""
    if not submission._state.adding or (submission.month_id, submission.participant_id) != (
        enrollment.month_id, enrollment.participant_id,
    ):
        raise ValidationError("Acceptance requires a new submission for the locked enrollment.")
    accepted_at = timezone.now()
    period = ensure_participation_period(
        enrollment, reason=ParticipationPeriod.StartReason.OBSERVED, at=accepted_at,
    )
    month = submission.month
    # This records configuration observed at acceptance. It does not establish
    # which configuration was in effect on an earlier completion date.
    submission.acceptance_claims = {
        "group_timezone": month.group.timezone,
        "book": {
            "title": submission.title,
            "author": submission.author,
            "catalog_book_id": submission.catalog_book_id,
            "catalog_edition_id": submission.catalog_edition_id,
            "submitted_pages": submission.submitted_pages,
        },
        "themes": list(MonthTheme.objects.select_for_update().filter(month=month, pk__in=[theme.pk for theme in themes])
                       .order_by("pk").values("id", "bonus_rule", "bonus_percentage", "bonus_pages")),
        "immersive_requested": immersive_requested,
        "botm": {
            "enabled": month.botm_enabled,
            "completion_bonus_pages": month.botm_completion_bonus_pages,
            "books": list(month.botm_books.filter(is_retired=False).order_by("pk").values(
                "id", "catalog_book_id", "catalog_edition_id", "normalized_title", "normalized_author", "bonus_pages",
            )),
        },
        "tbr": {
            "enabled": month.tbr_enabled,
            "book_bonus_pages": month.tbr_book_bonus_pages,
            "completion_bonus_pages": month.tbr_completion_bonus_pages,
            "books": list(PersonalTBRBook.objects.filter(
                personal_tbr__enrollment=enrollment, personal_tbr__confirmed_at__lte=accepted_at,
            ).order_by("pk").values("id", "catalog_book_id", "catalog_edition_id", "normalized_title", "normalized_author")),
        },
    }
    submission.participation_period = period
    submission.accepted_at = accepted_at
    submission.accepted_completed_on = submission.completed_on
