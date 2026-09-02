from .models import PersonalTBRCompletionAward, PersonalTBRMatch


def build_personal_tbr_reader_presentation(*, personal_tbr):
    """Annotate one Reader-owned locked TBR using durable match truth only."""
    books = list(personal_tbr.books.select_related("catalog_book", "catalog_edition").order_by("position", "pk"))
    book_ids = {book.pk for book in books}
    completed_ids = set(PersonalTBRMatch.objects.filter(
        personal_tbr_book_id__in=book_ids,
        status=PersonalTBRMatch.Status.CONFIRMED,
        is_qualifying=True,
    ).values_list("personal_tbr_book_id", flat=True))
    pending_ids = set(PersonalTBRMatch.objects.filter(
        personal_tbr_book_id__in=book_ids,
        status=PersonalTBRMatch.Status.PENDING_REVIEW,
        is_qualifying=False,
        submission__is_removed=False,
    ).values_list("personal_tbr_book_id", flat=True))
    pending_ids.difference_update(completed_ids)
    for book in books:
        if book.pk in completed_ids:
            book.reader_state = "completed"
        elif book.pk in pending_ids:
            book.reader_state = "pending"
        else:
            book.reader_state = "not_completed"
    completion_award = PersonalTBRCompletionAward.objects.filter(
        personal_tbr=personal_tbr,
        is_qualifying=True,
    ).first()
    return {
        "books": books,
        "completed_count": len(completed_ids),
        "registered_count": len(books),
        "completion_award": completion_award,
        "full_completion_eligible": len(books) == 9,
    }
