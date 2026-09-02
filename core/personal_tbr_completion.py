import hashlib
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    ChallengeMonth, Membership, ModifierProvenance, MonthEnrollment, PersonalTBR,
    PersonalTBRCompletionAward, PersonalTBRCompletionAwardBook, PersonalTBRMatch,
)


def personal_tbr_completion_set_fingerprint(books):
    identity = ",".join(str(book.pk) for book in sorted(books, key=lambda book: book.pk))
    return hashlib.sha256(identity.encode("ascii")).hexdigest()


def personal_tbr_completion_source_reference(award):
    return f"personal_tbr_completion:{award.pk}"


def _participant_is_eligible(month, participant, enrollment):
    return (
        month.tbr_enabled
        and participant.group_id == month.group_id
        and participant.is_active
        and not participant.user.is_superuser
        and enrollment.is_active
    )


def _synchronize_award_provenance(award):
    source_reference = personal_tbr_completion_source_reference(award)
    provenance = ModifierProvenance.objects.select_for_update().filter(
        source_type=ModifierProvenance.SourceType.TBR_COMPLETION,
        source_reference=source_reference,
    ).first()
    if provenance is not None and (
        provenance.month_id != award.month_id
        or provenance.participant_id != award.participant_id
        or provenance.submission_id is not None
    ):
        raise ValidationError(
            "Personal TBR completion provenance does not belong to its award's Challenge and Reader."
        )
    contributes = award.is_qualifying and award.bonus_amount_snapshot > 0
    if not contributes:
        if provenance is not None and provenance.is_active:
            provenance.is_active = False
            provenance.save(update_fields=["is_active"])
        return provenance
    if provenance is None:
        return ModifierProvenance.objects.create(
            month=award.month,
            participant=award.participant,
            submission=None,
            source_type=ModifierProvenance.SourceType.TBR_COMPLETION,
            source_reference=source_reference,
            source_label="Personal TBR completion",
            source_context=(
                f"Completed frozen nine-book Personal TBR set {award.completion_set_fingerprint}"
            ),
            amount=award.bonus_amount_snapshot,
            effective_date=award.effective_date,
            applied_at=award.qualified_at,
            is_system_generated=True,
            is_active=True,
        )
    if provenance.amount != award.bonus_amount_snapshot or provenance.effective_date != award.effective_date:
        raise ValidationError(
            "The frozen Personal TBR completion award no longer matches its durable provenance."
        )
    if not provenance.is_active:
        provenance.is_active = True
        provenance.save(update_fields=["is_active"])
    return provenance


def _deactivate_award(award, now):
    if award.is_qualifying:
        award.is_qualifying = False
        award.inactivated_at = now
        award.save(update_fields=["is_qualifying", "inactivated_at", "updated_at"])
    _synchronize_award_provenance(award)


@transaction.atomic
def synchronize_personal_tbr_completion_for_reader(*, month, participant, now=None):
    month = ChallengeMonth.objects.select_for_update().select_related("group").get(pk=month.pk)
    participant = Membership.objects.select_for_update().select_related("user").get(pk=participant.pk)
    now = now or timezone.now()
    enrollment = MonthEnrollment.objects.select_for_update().filter(
        month=month, participant=participant,
    ).first()
    tbr = None
    if enrollment is not None:
        tbr = PersonalTBR.objects.select_for_update().filter(enrollment=enrollment).first()
    award = PersonalTBRCompletionAward.objects.select_for_update().filter(
        personal_tbr=tbr,
    ).first() if tbr is not None else None

    books = list(tbr.books.select_for_update().order_by("pk")) if tbr and tbr.confirmed_at else []
    configured_book_ids = {book.pk for book in books}
    qualifying_book_ids = set()
    if tbr is not None:
        qualifying_book_ids = set(PersonalTBRMatch.objects.filter(
            personal_tbr_book__personal_tbr=tbr,
            month=month,
            participant=participant,
            status=PersonalTBRMatch.Status.CONFIRMED,
            is_qualifying=True,
        ).values_list("personal_tbr_book_id", flat=True))
    complete = (
        tbr is not None
        and tbr.confirmed_at is not None
        and len(books) == 9
        and len(configured_book_ids) == 9
        and qualifying_book_ids == configured_book_ids
        and _participant_is_eligible(month, participant, enrollment)
    )

    if not complete:
        if award is not None:
            _deactivate_award(award, now)
        return award

    fingerprint = personal_tbr_completion_set_fingerprint(books)
    if award is None:
        effective_date = timezone.localtime(now, ZoneInfo(month.group.timezone)).date()
        try:
            with transaction.atomic():
                award = PersonalTBRCompletionAward.objects.create(
                    personal_tbr=tbr,
                    month=month,
                    participant=participant,
                    completion_set_fingerprint=fingerprint,
                    configured_book_count=9,
                    bonus_amount_snapshot=month.tbr_completion_bonus_pages,
                    qualified_at=now,
                    last_qualified_at=now,
                    effective_date=effective_date,
                    is_qualifying=True,
                )
        except IntegrityError:
            award = PersonalTBRCompletionAward.objects.select_for_update().get(personal_tbr=tbr)
        if not award.configured_books.exists():
            PersonalTBRCompletionAwardBook.objects.bulk_create([
                PersonalTBRCompletionAwardBook(
                    award=award,
                    personal_tbr_book=book,
                    position_snapshot=book.position,
                    title_snapshot=book.title_snapshot,
                    author_snapshot=book.author_snapshot,
                )
                for book in books
            ])
    else:
        frozen_book_ids = set(award.configured_books.values_list("personal_tbr_book_id", flat=True))
        if (
            award.completion_set_fingerprint != fingerprint
            or frozen_book_ids != configured_book_ids
            or award.configured_book_count != 9
        ):
            raise ValidationError("The frozen Personal TBR completion set does not match its locked TBR.")
        if not award.is_qualifying:
            award.is_qualifying = True
            award.inactivated_at = None
        award.last_qualified_at = now
        award.save(update_fields=["is_qualifying", "inactivated_at", "last_qualified_at", "updated_at"])

    _synchronize_award_provenance(award)
    return award


@transaction.atomic
def synchronize_personal_tbr_completions_for_challenge(month, *, now=None):
    participant_ids = set(MonthEnrollment.objects.filter(month=month).values_list("participant_id", flat=True))
    participant_ids.update(month.personal_tbr_completion_awards.values_list("participant_id", flat=True))
    return [
        synchronize_personal_tbr_completion_for_reader(month=month, participant=participant, now=now)
        for participant in Membership.objects.filter(pk__in=participant_ids).order_by("pk")
    ]
