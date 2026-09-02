from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from .models import AuditEvent, BotmBook, ChallengeMonth
from .permissions import can_operate_challenge


CONFIGURABLE_STATUSES = {
    ChallengeMonth.Status.DRAFT,
    ChallengeMonth.Status.UPCOMING,
    ChallengeMonth.Status.ACTIVE,
}


def _locked_month(month, actor):
    locked = ChallengeMonth.objects.select_for_update().select_related("group").get(pk=month.pk)
    if not can_operate_challenge(actor, locked):
        raise ValidationError("Only a current Host or Platform Owner may configure Book of the Month.")
    if locked.status not in CONFIGURABLE_STATUSES:
        raise ValidationError(f"{locked.get_status_display()} Challenges are read-only.")
    return locked


def _save(book):
    try:
        book.save()
    except IntegrityError as exc:
        raise ValidationError("That active position or Book of the Month work is already configured.") from exc
    return book


def _owned_book(book, month):
    try:
        return BotmBook.objects.select_for_update().get(pk=book.pk, month=month)
    except BotmBook.DoesNotExist as exc:
        raise ValidationError("That Book of the Month title does not belong to this Challenge.") from exc


def _audit(actor, month, action, book, summary):
    AuditEvent.objects.create(
        actor=actor,
        group=month.group,
        action=action,
        object_type="BotmBook",
        object_id=str(book.pk),
        summary=summary,
    )


@transaction.atomic
def add_botm_book(*, month, actor, values):
    month = _locked_month(month, actor)
    book = BotmBook(month=month, **values)
    _save(book)
    _audit(actor, month, "botm.book_created", book, f"Added BOTM book {book.title_snapshot} at position {book.position} for {month.name}.")
    from .botm_matching import synchronize_challenge
    synchronize_challenge(month)
    return book


@transaction.atomic
def update_botm_book(*, month, book, actor, values):
    month = _locked_month(month, actor)
    book = _owned_book(book, month)
    old_title, old_position = book.title_snapshot, book.position
    target_position = values.get("position", book.position)
    displaced = None
    if not book.is_retired and target_position != old_position:
        displaced = BotmBook.objects.select_for_update().filter(
            month=month, position=target_position, is_retired=False
        ).exclude(pk=book.pk).first()
        if displaced:
            displaced.is_retired = True
            displaced.save(update_fields=["is_retired"])
    for field, value in values.items():
        setattr(book, field, value)
    _save(book)
    if displaced:
        displaced.position = old_position
        displaced.is_retired = False
        _save(displaced)
    _audit(actor, month, "botm.book_updated", book, f"Updated BOTM book {old_title} for {month.name}; position {old_position} to {book.position}.")
    from .botm_matching import synchronize_challenge
    synchronize_challenge(month)
    return book


@transaction.atomic
def retire_botm_book(*, month, book, actor):
    month = _locked_month(month, actor)
    book = _owned_book(book, month)
    if book.is_retired:
        raise ValidationError("This Book of the Month title is already retired.")
    book.is_retired = True
    _save(book)
    _audit(actor, month, "botm.book_retired", book, f"Retired BOTM book {book.title_snapshot} from position {book.position} in {month.name}.")
    from .botm_matching import synchronize_challenge
    synchronize_challenge(month)
    return book


@transaction.atomic
def reactivate_botm_book(*, month, book, actor, position):
    month = _locked_month(month, actor)
    book = _owned_book(book, month)
    if not book.is_retired:
        raise ValidationError("This Book of the Month title is already active.")
    book.position = position
    book.is_retired = False
    _save(book)
    _audit(actor, month, "botm.book_reactivated", book, f"Reactivated BOTM book {book.title_snapshot} at position {book.position} in {month.name}.")
    from .botm_matching import synchronize_challenge
    synchronize_challenge(month)
    return book


@transaction.atomic
def delete_unused_botm_book(*, month, book, actor):
    month = _locked_month(month, actor)
    book = _owned_book(book, month)
    title, position, object_id = book.title_snapshot, book.position, book.pk
    if book.matches.exists():
        raise ValidationError("BOTM books with match history must be retired rather than deleted.")
    book.delete()
    AuditEvent.objects.create(
        actor=actor,
        group=month.group,
        action="botm.book_deleted",
        object_type="BotmBook",
        object_id=str(object_id),
        summary=f"Deleted unused BOTM book {title} from position {position} in {month.name}.",
    )
