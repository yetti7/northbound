from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import AuditEvent, MonthEnrollment, PersonalTBR, PersonalTBRBook


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
    personal_tbr.save(update_fields=["updated_at"])
    _record_tbr_change(personal_tbr, enrollment.participant.user, staff=False)
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
    _record_tbr_change(personal_tbr, enrollment.participant.user, staff=False)
    return personal_tbr, True


def _record_tbr_change(tbr, actor, *, staff):
    AuditEvent.objects.create(
        actor=actor, group=tbr.enrollment.month.group,
        action="challenge_tbr.staff_updated" if staff else "challenge_tbr.reader_updated",
        object_type="PersonalTBR", object_id=str(tbr.pk),
        summary="Staff-assisted Challenge TBR change." if staff else "Reader-managed Challenge TBR change.",
    )


def staff_tbr_version(tbr):
    from django.core import signing
    return signing.dumps({"id": tbr.pk, "updated": tbr.updated_at.isoformat()}, salt="staff-tbr-version") if tbr else ""


@transaction.atomic
def save_staff_challenge_tbr(*, enrollment, actor, books, complete=False, version=""):
    """Explicit, permission-checked exception; no registration, score or sync writes."""
    from django.core import signing
    from .permissions import can_manage_challenge_tbr
    enrollment = MonthEnrollment.objects.select_for_update().select_related(
        "month__group", "participant__user"
    ).get(pk=enrollment.pk)
    if not can_manage_challenge_tbr(actor, enrollment):
        raise ValidationError("You may not manage this Reader's Challenge TBR.")
    if not enrollment.month.tbr_enabled:
        raise ValidationError("Challenge TBR is disabled for this Challenge.")
    tbr = PersonalTBR.objects.select_for_update().filter(enrollment=enrollment).first()
    try:
        current = signing.loads(version, salt="staff-tbr-version") if version else None
    except signing.BadSignature:
        raise ValidationError("This TBR has changed. Reload before saving.")
    expected = {"id": tbr.pk, "updated": tbr.updated_at.isoformat()} if tbr else None
    if current != expected:
        raise ValidationError("This TBR has changed. Reload before saving.")
    if len(books) > 9 or [b["position"] for b in books] != list(range(1, len(books) + 1)):
        raise ValidationError("Select at most nine books in consecutive positions.")
    # Registration permits optional 0-9 lists. Staff completion requires all nine.
    if complete and tbr and tbr.confirmed_at:
        raise ValidationError("This Challenge TBR is already complete.")
    if complete and len(books) != 9:
        raise ValidationError("Save & Complete TBR requires nine valid book selections.")
    if tbr is None:
        tbr = PersonalTBR.objects.create(enrollment=enrollment)
    old = list(tbr.books.select_for_update())
    confirmed_at = tbr.confirmed_at
    # Only this in-memory parent is draft during validation; the persisted lock
    # never changes. The public model and Reader paths retain their lock guards.
    tbr.confirmed_at = None
    candidates = [PersonalTBRBook(personal_tbr=tbr, **_book_values(book)) for book in books]
    identities, catalog_ids = set(), set()
    for candidate in candidates:
        candidate.personal_tbr = PersonalTBR(enrollment=enrollment)
        candidate.full_clean(exclude=["personal_tbr"], validate_constraints=False)
        candidate.personal_tbr = tbr
        identity = (candidate.normalized_title, candidate.normalized_author)
        if identity in identities or (candidate.catalog_book_id and candidate.catalog_book_id in catalog_ids):
            raise ValidationError("The same book cannot appear twice on the Challenge TBR.")
        identities.add(identity)
        if candidate.catalog_book_id:
            catalog_ids.add(candidate.catalog_book_id)
    def signature(book):
        return tuple(getattr(book, name) for name in (
            "position", "catalog_book_id", "catalog_edition_id", "title_snapshot",
            "author_snapshot", "page_count_snapshot", "cover_url_snapshot", "source_url_snapshot",
        ))
    unchanged = {signature(book): book for book in old}
    proposed = {signature(book) for book in candidates}
    for book in old:
        if signature(book) not in proposed and (book.matches.exists() or book.completion_award_snapshots.exists()):
            raise ValidationError(
                f'“{book.title_snapshot}” has match or award history. Keep its selection and position unchanged; '
                "this correction would alter preserved scoring history."
            )
    retained_ids = [book.pk for key, book in unchanged.items() if key in proposed]
    tbr.books.exclude(pk__in=retained_ids).delete()
    for candidate in candidates:
        if signature(candidate) not in unchanged:
            candidate.save()
    tbr.confirmed_at = confirmed_at or (timezone.now() if complete else None)
    tbr.save(update_fields=["confirmed_at", "updated_at"])
    _record_tbr_change(tbr, actor, staff=True)
    return tbr
