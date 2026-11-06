from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from .models import (
    AuditEvent, BookSubmission, ChallengeMonth, MonthEnrollment, PersonalTBRBook,
    PersonalTBRMatch, normalize_book_identity,
)
from .permissions import can_review_submission


def _finalize_submission_matches(submission, matches):
    from .personal_tbr_rewards import synchronize_personal_tbr_book_rewards_for_submission
    synchronize_personal_tbr_book_rewards_for_submission(submission)
    from .personal_tbr_completion import synchronize_personal_tbr_completion_for_reader
    synchronize_personal_tbr_completion_for_reader(
        month=submission.month, participant=submission.participant,
    )
    return matches


def _catalog_identity(catalog_book):
    return "" if catalog_book is None else f"{catalog_book.provider}:{catalog_book.provider_book_id}"


def _submission_is_eligible(submission):
    return (
        submission.status == BookSubmission.Status.APPROVED
        and not submission.is_removed
        and submission.participant.is_active
        and not submission.participant.user.is_superuser
        and MonthEnrollment.objects.filter(
            month_id=submission.month_id, participant_id=submission.participant_id, is_active=True,
        ).exists()
    )


def _values(book, submission, *, method, status, evidence_summary):
    return {
        "month": submission.month,
        "participant": submission.participant,
        "submission": submission,
        "method": method,
        "status": status,
        "is_qualifying": False,
        "tbr_title_snapshot": book.title_snapshot,
        "tbr_author_snapshot": book.author_snapshot,
        "tbr_catalog_identity": _catalog_identity(book.catalog_book),
        "submission_title_snapshot": submission.title,
        "submission_author_snapshot": submission.author,
        "submission_catalog_identity": _catalog_identity(submission.catalog_book),
        "normalized_title_evidence": normalize_book_identity(submission.title),
        "normalized_author_evidence": normalize_book_identity(submission.author),
        "evidence_summary": evidence_summary,
        "decided_at": timezone.now() if status == PersonalTBRMatch.Status.CONFIRMED else None,
    }


def _can_qualify(book, submission, existing=None):
    competing = PersonalTBRMatch.objects.select_for_update().filter(
        status=PersonalTBRMatch.Status.CONFIRMED, is_qualifying=True,
    ).filter(models.Q(submission=submission) | models.Q(personal_tbr_book=book))
    if existing is not None:
        competing = competing.exclude(pk=existing.pk)
    return not competing.exists()


def _confirm_candidate(book, submission, *, method, evidence_summary):
    existing = PersonalTBRMatch.objects.select_for_update().filter(
        personal_tbr_book=book, submission=submission,
    ).first()
    if existing and existing.status == PersonalTBRMatch.Status.REJECTED:
        return existing
    qualifying = _can_qualify(book, submission, existing)
    if existing is None:
        try:
            return PersonalTBRMatch.objects.create(
                personal_tbr_book=book,
                **{
                    **_values(book, submission, method=method, status=PersonalTBRMatch.Status.CONFIRMED,
                              evidence_summary=evidence_summary),
                    "is_qualifying": qualifying,
                },
            )
        except IntegrityError as exc:
            raise ValidationError("A concurrent Personal TBR match already satisfies this submission or book.") from exc
    existing.method = method if existing.method != PersonalTBRMatch.Method.MANUAL_REVIEW else existing.method
    existing.status = PersonalTBRMatch.Status.CONFIRMED
    existing.is_qualifying = qualifying
    existing.decided_at = existing.decided_at or timezone.now()
    existing.evidence_summary = evidence_summary
    existing.save(update_fields=["method", "status", "is_qualifying", "decided_at", "evidence_summary", "updated_at"])
    return existing


def _pending_candidate(book, submission, evidence_summary):
    existing = PersonalTBRMatch.objects.select_for_update().filter(
        personal_tbr_book=book, submission=submission,
    ).first()
    if existing is not None:
        return existing
    return PersonalTBRMatch.objects.create(
        personal_tbr_book=book,
        **_values(book, submission, method=PersonalTBRMatch.Method.NORMALIZED_TITLE_AUTHOR,
                  status=PersonalTBRMatch.Status.PENDING_REVIEW, evidence_summary=evidence_summary),
    )


def select_candidates(*, submission_catalog_book_id, normalized_title, normalized_author, books):
    """Return deterministic identity candidates without fuzzy or title-only matching."""
    if submission_catalog_book_id:
        canonical = [book for book in books if book.catalog_book_id == submission_catalog_book_id]
        if canonical:
            return PersonalTBRMatch.Method.CATALOG_WORK, canonical
        books = [book for book in books if book.catalog_book_id is None]
    return PersonalTBRMatch.Method.NORMALIZED_TITLE_AUTHOR, [
        book for book in books
        if book.normalized_title == normalized_title and book.normalized_author == normalized_author
    ]


@transaction.atomic
def synchronize_submission(submission):
    from .matching_resolution import synchronize_domain
    return synchronize_domain(submission, 'tbr')


@transaction.atomic
def synchronize_reader(*, month, participant):
    results = []
    for submission in BookSubmission.objects.filter(month=month, participant=participant).order_by("pk"):
        results.extend(synchronize_submission(submission))
    return results


@transaction.atomic
def synchronize_challenge(month):
    month = ChallengeMonth.objects.select_for_update().get(pk=month.pk)
    results = []
    for submission in BookSubmission.objects.filter(month=month).order_by("pk"):
        results.extend(synchronize_submission(submission))
    return results


@transaction.atomic
def adjudicate_match(*, match, actor, decision):
    from .matching_resolution import confirm_suggestion
    return confirm_suggestion(match=match, actor=actor, decision=decision)
