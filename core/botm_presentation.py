from .models import (
    BookSubmission,
    BotmCompletionAward,
    BotmMatch,
    MonthEnrollment,
    normalize_book_identity,
)


def _reader_is_participating(month, participant):
    return bool(
        participant
        and participant.is_active
        and not participant.user.is_superuser
        and MonthEnrollment.objects.filter(
            month=month,
            participant=participant,
            is_active=True,
        ).exists()
    )


def _pending_submission_book_ids(*, month, participant, books):
    pending_ids = set()
    submissions = BookSubmission.objects.filter(
        month=month,
        participant=participant,
        status=BookSubmission.Status.PENDING,
        is_removed=False,
    ).select_related("catalog_book")
    for submission in submissions:
        normalized_title = normalize_book_identity(submission.title)
        normalized_author = normalize_book_identity(submission.author)
        for book in books:
            if submission.catalog_book_id:
                plausible = (
                    book.catalog_book_id == submission.catalog_book_id
                    or (
                        book.catalog_book_id is None
                        and book.normalized_title == normalized_title
                        and book.normalized_author == normalized_author
                    )
                )
            else:
                plausible = (
                    book.normalized_title == normalized_title
                    and book.normalized_author == normalized_author
                )
            if plausible:
                pending_ids.add(book.pk)
    return pending_ids


def build_botm_reader_presentation(*, month, participant, books):
    """Annotate active BOTM books with one authenticated Reader's display state."""
    books = list(books)
    result = {
        "has_reader_state": False,
        "completed_count": 0,
        "configured_count": len(books),
        "completion_award": None,
    }
    for book in books:
        book.reader_state = "not_completed"

    if not _reader_is_participating(month, participant):
        return result

    book_ids = {book.pk for book in books}
    completed_ids = set(
        BotmMatch.objects.filter(
            month=month,
            participant=participant,
            botm_book_id__in=book_ids,
            status=BotmMatch.Status.CONFIRMED,
            is_qualifying=True,
        ).values_list("botm_book_id", flat=True)
    )
    pending_ids = set(
        BotmMatch.objects.filter(
            month=month,
            participant=participant,
            botm_book_id__in=book_ids,
            status=BotmMatch.Status.PENDING_REVIEW,
            submission__is_removed=False,
        ).values_list("botm_book_id", flat=True)
    )
    pending_ids.update(
        _pending_submission_book_ids(month=month, participant=participant, books=books)
    )
    pending_ids.difference_update(completed_ids)

    for book in books:
        if book.pk in completed_ids:
            book.reader_state = "completed"
        elif book.pk in pending_ids:
            book.reader_state = "pending"

    completion_award = next(
        (
            award
            for award in BotmCompletionAward.objects.filter(
                month=month,
                participant=participant,
                is_qualifying=True,
                configured_book_count=len(books),
            ).prefetch_related("configured_books")
            if {snapshot.botm_book_id for snapshot in award.configured_books.all()} == book_ids
        ),
        None,
    )
    result.update(
        has_reader_state=True,
        completed_count=len(completed_ids),
        completion_award=completion_award,
    )
    return result
