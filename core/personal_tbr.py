import json

from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import AuditEvent, ChallengeMonth, MonthEnrollment, PersonalTBR, PersonalTBRBook, normalize_book_identity


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
    def signature(book):
        return tuple(getattr(book, name) for name in (
            "position", "catalog_book_id", "catalog_edition_id", "title_snapshot",
            "author_snapshot", "page_count_snapshot", "cover_url_snapshot", "source_url_snapshot",
        ))
    existing = {signature(book): book for book in personal_tbr.books.select_for_update()}
    retained_ids = [existing[signature(book)].pk for book in candidates if signature(book) in existing]
    personal_tbr.books.exclude(pk__in=retained_ids).delete()
    identities = set()
    catalog_ids = set()
    for candidate in candidates:
        retained = existing.get(signature(candidate))
        if retained is not None:
            candidate = retained
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
        if signature(candidate) not in existing:
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
    from .permissions import can_manage_challenge_tbr, challenge_history_locked
    enrollment = MonthEnrollment.objects.select_for_update().select_related(
        "month__group", "participant__user"
    ).get(pk=enrollment.pk)
    if not can_manage_challenge_tbr(actor, enrollment):
        raise ValidationError("You may not manage this Reader's Challenge TBR.")
    month = enrollment.month
    if challenge_history_locked(month):
        raise ValidationError("Completed and Archived Challenges are read-only.")
    if not month.tbr_enabled:
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
        if signature(book) not in proposed and (book.matches.exists() or book.completion_award_snapshots.exists() or book.reader_selected_submissions.exists()):
            raise ValidationError(
                f'“{book.title_snapshot}” has match, award or Reader selection history. Keep its selection and position unchanged; '
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


def staff_tbr_book_lock_reason(book):
    if book.matches.exists() or book.completion_award_snapshots.exists() or book.reader_selected_submissions.exists():
        return "This book has match, award, or Reader selection history and its title and author are locked."
    return ""


def _staff_tbr_book_state(book):
    return {
        "list": book.personal_tbr_id, "entry": book.pk,
        "list_updated": book.personal_tbr.updated_at.isoformat(),
        "values": [getattr(book, field) for field in (
            "position", "title_snapshot", "author_snapshot", "display_title",
            "display_author", "catalog_book_id", "catalog_edition_id",
        )],
    }


def staff_tbr_book_version(book):
    return signing.dumps(_staff_tbr_book_state(book), salt="staff-tbr-book-edit")


@transaction.atomic
def edit_staff_tbr_book(*, enrollment, book_id, actor, title, author, version):
    from django.core.exceptions import PermissionDenied
    from .permissions import can_manage_challenge_tbr, challenge_history_locked

    month = ChallengeMonth.objects.select_for_update().get(pk=enrollment.month_id)
    enrollment = MonthEnrollment.objects.select_for_update().get(pk=enrollment.pk, month=month)
    if not actor.is_active or not can_manage_challenge_tbr(actor, enrollment):
        raise PermissionDenied("Challenge TBR staff authority is required.")
    if challenge_history_locked(month) or not month.tbr_enabled:
        raise ValidationError("This Challenge TBR cannot be edited.")
    tbr = PersonalTBR.objects.select_for_update().filter(enrollment=enrollment).first()
    book = PersonalTBRBook.objects.select_for_update().filter(pk=book_id, personal_tbr=tbr).first() if tbr else None
    try:
        expected = signing.loads(version, salt="staff-tbr-book-edit")
    except signing.BadSignature:
        expected = None
    if book is None or expected != _staff_tbr_book_state(book):
        raise ValidationError("This book changed. Reopen Edit Book before saving.")
    if reason := staff_tbr_book_lock_reason(book):
        raise ValidationError(reason)

    title, author = title.strip(), author.strip()
    normalized_title, normalized_author = map(normalize_book_identity, (title, author))
    errors = {}
    for field, value, normalized in (("title", title, normalized_title), ("author", author, normalized_author)):
        if not normalized or len(value) > 300 or len(normalized) > 300:
            errors[field] = f"Enter a valid book {field}."
    if errors:
        raise ValidationError(errors)
    before = {"title": book.title_snapshot, "author": book.author_snapshot}
    after = {"title": title, "author": author}
    if before == after:
        return False

    if book.catalog_book_id:
        for field, value in after.items():
            if value != before[field] and normalize_book_identity(value) != normalize_book_identity(getattr(book.catalog_book, field)):
                errors[field] = f"This {field} conflicts with the linked catalog book."
    if tbr.books.exclude(pk=book.pk).filter(normalized_title=normalized_title, normalized_author=normalized_author).exists():
        errors["title"] = "This title and author are already on the Personal TBR."
    if errors:
        raise ValidationError(errors)

    updates = {}
    for field, normalized in (("title", normalized_title), ("author", normalized_author)):
        if before[field] != after[field]:
            updates.update({f"{field}_snapshot": after[field], f"normalized_{field}": normalized, f"display_{field}": after[field]})
    # The regular model save protects confirmed lists. This exception changes only book text.
    PersonalTBRBook.objects.filter(pk=book.pk).update(**updates)
    tbr.save(update_fields=["updated_at"])
    AuditEvent.objects.create(
        actor=actor, group=month.group, action="challenge_tbr.book_edited",
        object_type="PersonalTBRBook", object_id=str(book.pk),
        summary=json.dumps({"challenge_id": month.pk, "personal_tbr_id": tbr.pk, "before": before, "after": after}),
    )
    return True
