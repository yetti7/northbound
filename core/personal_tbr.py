from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import PersonalTBR, PersonalTBRBook


def _book_values(book):
    return {
        "position": book["position"],
        "catalog_book": book.get("catalog_book"),
        "catalog_edition": book.get("catalog_edition"),
        "title_snapshot": book["title_snapshot"],
        "author_snapshot": book["author_snapshot"],
        "page_count_snapshot": book.get("page_count_snapshot"),
        "cover_url_snapshot": book.get("cover_url_snapshot", ""),
        "source_url_snapshot": book.get("source_url_snapshot", ""),
    }


@transaction.atomic
def replace_draft_personal_tbr(*, enrollment, books):
    """Replace a draft selection atomically; confirmed selections fail closed."""
    if len(books) > 9:
        raise ValidationError("A Personal TBR may contain at most nine books.")
    personal_tbr, _ = PersonalTBR.objects.select_for_update().get_or_create(enrollment=enrollment)
    if personal_tbr.confirmed_at:
        raise ValidationError("A confirmed Personal TBR cannot be changed.")

    candidates = [PersonalTBRBook(personal_tbr=personal_tbr, **_book_values(book)) for book in books]
    positions = [candidate.position for candidate in candidates]
    if len(positions) != len(set(positions)):
        raise ValidationError("Personal TBR book positions must be unique.")
    personal_tbr.books.all().delete()
    identities = set()
    catalog_ids = set()
    for candidate in candidates:
        candidate.full_clean(validate_constraints=False)
        identity = (candidate.normalized_title, candidate.normalized_author)
        if identity in identities:
            raise ValidationError("The same title and author cannot appear twice on a Personal TBR.")
        identities.add(identity)
        if candidate.catalog_book_id:
            if candidate.catalog_book_id in catalog_ids:
                raise ValidationError("The same catalog book cannot appear twice on a Personal TBR.")
            catalog_ids.add(candidate.catalog_book_id)

    for candidate in candidates:
        candidate.save()
    return personal_tbr


@transaction.atomic
def confirm_personal_tbr(*, enrollment):
    """Permanently lock the enrollment's current 0–9 book selection."""
    personal_tbr, _ = PersonalTBR.objects.select_for_update().get_or_create(enrollment=enrollment)
    if personal_tbr.confirmed_at:
        return personal_tbr, False
    books = list(personal_tbr.books.select_for_update())
    if len(books) > 9:
        raise ValidationError("A Personal TBR may contain at most nine books.")
    for book in books:
        book.full_clean()
    personal_tbr.confirmed_at = timezone.now()
    personal_tbr.save(update_fields=["confirmed_at", "updated_at"])
    return personal_tbr, True
