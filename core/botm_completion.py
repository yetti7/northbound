import hashlib
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    BotmBook, BotmCompletionAward, BotmCompletionAwardBook, BotmMatch,
    ChallengeMonth, Membership, ModifierProvenance, MonthEnrollment,
)


def completion_set_fingerprint(books):
    identity = ",".join(str(book.pk) for book in sorted(books, key=lambda book: book.pk))
    return hashlib.sha256(identity.encode("ascii")).hexdigest()


def botm_completion_source_reference(award):
    return f"botm_completion:{award.pk}"


def _participant_is_eligible(month, participant):
    return (
        month.botm_enabled
        and participant.group_id == month.group_id
        and participant.is_active
        and not participant.user.is_superuser
        and MonthEnrollment.objects.filter(month=month, participant=participant, is_active=True).exists()
    )


def _synchronize_award_provenance(award):
    source_reference = botm_completion_source_reference(award)
    provenance = ModifierProvenance.objects.select_for_update().filter(
        source_type=ModifierProvenance.SourceType.BOTM_COMPLETION,
        source_reference=source_reference,
    ).first()
    if provenance is not None and (
        provenance.month_id != award.month_id
        or provenance.participant_id != award.participant_id
        or provenance.submission_id is not None
    ):
        raise ValidationError("BOTM completion provenance does not belong to its award's Challenge and Reader.")

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
            source_type=ModifierProvenance.SourceType.BOTM_COMPLETION,
            source_reference=source_reference,
            source_label="Book of the Month completion",
            source_context=f"Completed frozen {award.configured_book_count}-book BOTM set {award.completion_set_fingerprint}",
            amount=award.bonus_amount_snapshot,
            effective_date=award.effective_date,
            applied_at=award.qualified_at,
            is_system_generated=True,
            is_active=True,
        )
    if provenance.amount != award.bonus_amount_snapshot or provenance.effective_date != award.effective_date:
        raise ValidationError("The frozen BOTM completion award no longer matches its durable provenance.")
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
def synchronize_botm_completion_for_reader(*, month, participant, now=None):
    month = ChallengeMonth.objects.select_for_update().select_related("group").get(pk=month.pk)
    participant = Membership.objects.select_for_update().select_related("user").get(pk=participant.pk)
    now = now or timezone.now()
    active_books = list(BotmBook.objects.select_for_update().filter(month=month, is_retired=False).order_by("pk"))
    fingerprint = completion_set_fingerprint(active_books) if active_books else None
    awards = list(
        BotmCompletionAward.objects.select_for_update().filter(month=month, participant=participant).order_by("pk")
    )

    qualifying_book_ids = set(
        BotmMatch.objects.filter(
            month=month,
            participant=participant,
            status=BotmMatch.Status.CONFIRMED,
            is_qualifying=True,
            botm_book__is_retired=False,
        ).values_list("botm_book_id", flat=True)
    )
    configured_book_ids = {book.pk for book in active_books}
    complete = (
        bool(active_books)
        and _participant_is_eligible(month, participant)
        and qualifying_book_ids == configured_book_ids
    )

    current_award = next((award for award in awards if award.completion_set_fingerprint == fingerprint), None)
    for award in awards:
        if not complete or award is not current_award:
            _deactivate_award(award, now)
    if not complete:
        return current_award

    if current_award is None:
        effective_date = timezone.localtime(now, ZoneInfo(month.group.timezone)).date()
        try:
            with transaction.atomic():
                current_award = BotmCompletionAward.objects.create(
                    month=month,
                    participant=participant,
                    completion_set_fingerprint=fingerprint,
                    configured_book_count=len(active_books),
                    bonus_amount_snapshot=month.botm_completion_bonus_pages,
                    qualified_at=now,
                    last_qualified_at=now,
                    effective_date=effective_date,
                    is_qualifying=True,
                )
        except IntegrityError:
            current_award = BotmCompletionAward.objects.select_for_update().get(
                month=month,
                participant=participant,
                completion_set_fingerprint=fingerprint,
            )
        if not current_award.configured_books.exists():
            BotmCompletionAwardBook.objects.bulk_create([
                BotmCompletionAwardBook(
                    award=current_award,
                    botm_book=book,
                    position_snapshot=book.position,
                    title_snapshot=book.title_snapshot,
                    author_snapshot=book.author_snapshot,
                )
                for book in active_books
            ])
    else:
        frozen_book_ids = set(current_award.configured_books.values_list("botm_book_id", flat=True))
        if frozen_book_ids != configured_book_ids or current_award.configured_book_count != len(configured_book_ids):
            raise ValidationError("The frozen BOTM completion set does not match its stable identity.")
        if not current_award.is_qualifying:
            current_award.is_qualifying = True
            current_award.inactivated_at = None
        current_award.last_qualified_at = now
        current_award.save(update_fields=["is_qualifying", "inactivated_at", "last_qualified_at", "updated_at"])

    _synchronize_award_provenance(current_award)
    return current_award


@transaction.atomic
def synchronize_botm_completions_for_challenge(month, *, now=None):
    participant_ids = set(MonthEnrollment.objects.filter(month=month).values_list("participant_id", flat=True))
    participant_ids.update(month.botm_completion_awards.values_list("participant_id", flat=True))
    return [
        synchronize_botm_completion_for_reader(month=month, participant=participant, now=now)
        for participant in Membership.objects.filter(pk__in=participant_ids).order_by("pk")
    ]
