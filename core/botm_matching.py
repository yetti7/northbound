from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from .models import AuditEvent, BookSubmission, BotmBook, BotmMatch, ChallengeMonth, MonthEnrollment, normalize_book_identity
from .permissions import can_operate_challenge


def _finalize_submission_matches(submission, matches):
    from .botm_rewards import synchronize_botm_book_rewards_for_submission
    synchronize_botm_book_rewards_for_submission(submission)
    from .botm_completion import synchronize_botm_completion_for_reader
    synchronize_botm_completion_for_reader(month=submission.month, participant=submission.participant)
    return matches


def _catalog_identity(catalog_book):
    if catalog_book is None:
        return ""
    return f"{catalog_book.provider}:{catalog_book.provider_book_id}"


def _submission_is_eligible(submission):
    return (
        submission.status == BookSubmission.Status.APPROVED
        and not submission.is_removed
        and submission.participant.is_active
        and not submission.participant.user.is_superuser
        and MonthEnrollment.objects.filter(
            month_id=submission.month_id,
            participant_id=submission.participant_id,
            is_active=True,
        ).exists()
    )


def _create_values(book, submission, *, method, status, evidence_summary):
    return {
        "month": submission.month,
        "participant": submission.participant,
        "method": method,
        "status": status,
        "is_qualifying": status == BotmMatch.Status.CONFIRMED,
        "botm_title_snapshot": book.title_snapshot,
        "botm_author_snapshot": book.author_snapshot,
        "botm_catalog_identity": _catalog_identity(book.catalog_book),
        "submission_title_snapshot": submission.title,
        "submission_author_snapshot": submission.author,
        "submission_catalog_identity": _catalog_identity(submission.catalog_book),
        "evidence_summary": evidence_summary,
        "decided_at": timezone.now() if status == BotmMatch.Status.CONFIRMED else None,
    }


def _confirm_candidate(book, submission, *, method, evidence_summary):
    existing = BotmMatch.objects.select_for_update().filter(
        botm_book=book,
        participant=submission.participant,
        submission=submission,
    ).first()
    if existing and existing.status == BotmMatch.Status.REJECTED:
        return existing
    competing = BotmMatch.objects.select_for_update().filter(
        status=BotmMatch.Status.CONFIRMED,
        is_qualifying=True,
    ).filter(
        models.Q(submission=submission) | models.Q(botm_book=book, participant=submission.participant)
    )
    if existing:
        competing = competing.exclude(pk=existing.pk)
    if competing.exists():
        return existing
    if existing is None:
        try:
            return BotmMatch.objects.create(
                botm_book=book,
                submission=submission,
                **_create_values(
                    book,
                    submission,
                    method=method,
                    status=BotmMatch.Status.CONFIRMED,
                    evidence_summary=evidence_summary,
                ),
            )
        except IntegrityError as exc:
            raise ValidationError("A concurrent BOTM match already satisfies this submission or Reader/book.") from exc
    existing.method = method
    existing.status = BotmMatch.Status.CONFIRMED
    existing.is_qualifying = True
    existing.decided_at = existing.decided_at or timezone.now()
    existing.evidence_summary = evidence_summary
    existing.save(update_fields=["method", "status", "is_qualifying", "decided_at", "evidence_summary", "updated_at"])
    return existing


def _pending_candidate(book, submission, evidence_summary):
    existing = BotmMatch.objects.select_for_update().filter(
        botm_book=book,
        participant=submission.participant,
        submission=submission,
    ).first()
    if existing is not None:
        return existing
    return BotmMatch.objects.create(
        botm_book=book,
        submission=submission,
        **_create_values(
            book,
            submission,
            method=BotmMatch.Method.NORMALIZED_TITLE_AUTHOR,
            status=BotmMatch.Status.PENDING_REVIEW,
            evidence_summary=evidence_summary,
        ),
    )


@transaction.atomic
def synchronize_submission(submission):
    """Synchronize one submission into durable BOTM decisions without changing score."""
    submission = BookSubmission.objects.select_for_update().select_related(
        "month", "participant", "catalog_book"
    ).get(pk=submission.pk)
    existing_matches = BotmMatch.objects.select_for_update().filter(submission=submission)
    existing_matches.filter(is_qualifying=True).update(is_qualifying=False, updated_at=timezone.now())
    if not _submission_is_eligible(submission) or not submission.month.botm_enabled:
        return _finalize_submission_matches(submission, list(existing_matches))

    books = list(
        BotmBook.objects.select_for_update().select_related("catalog_book").filter(
            month=submission.month,
            is_retired=False,
        )
    )
    if submission.catalog_book_id:
        canonical = [book for book in books if book.catalog_book_id == submission.catalog_book_id]
        if canonical:
            match = _confirm_candidate(
                canonical[0],
                submission,
                method=BotmMatch.Method.CATALOG_WORK,
                evidence_summary="The configured BOTM and submission share the same canonical catalog work.",
            )
            return _finalize_submission_matches(submission, [match] if match else [])
        fallback_pool = [book for book in books if book.catalog_book_id is None]
    else:
        fallback_pool = books

    normalized_title = normalize_book_identity(submission.title)
    normalized_author = normalize_book_identity(submission.author)
    candidates = [
        book for book in fallback_pool
        if book.normalized_title == normalized_title and book.normalized_author == normalized_author
    ]
    if len(candidates) == 1:
        match = _confirm_candidate(
            candidates[0],
            submission,
            method=BotmMatch.Method.NORMALIZED_TITLE_AUTHOR,
            evidence_summary="Unique deterministic normalized title and author match.",
        )
        return _finalize_submission_matches(submission, [match] if match else [])
    if len(candidates) > 1:
        reviewed_confirmation = existing_matches.filter(
            botm_book__in=candidates,
            status=BotmMatch.Status.CONFIRMED,
            method=BotmMatch.Method.MANUAL_REVIEW,
        ).first()
        if reviewed_confirmation is not None:
            reviewed_confirmation.is_qualifying = True
            reviewed_confirmation.save(update_fields=["is_qualifying", "updated_at"])
            return _finalize_submission_matches(submission, [reviewed_confirmation])
        matches = [
            _pending_candidate(
                book,
                submission,
                "Multiple active BOTM books share the submission's normalized title and author.",
            )
            for book in candidates
        ]
        return _finalize_submission_matches(submission, matches)
    return _finalize_submission_matches(submission, list(existing_matches))


@transaction.atomic
def synchronize_reader(*, month, participant):
    submissions = BookSubmission.objects.filter(month=month, participant=participant).order_by("pk")
    results = []
    for submission in submissions:
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
    match = BotmMatch.objects.select_for_update().select_related(
        "month__group", "botm_book", "submission", "participant"
    ).get(pk=match.pk)
    if not can_operate_challenge(actor, match.month):
        raise ValidationError("Only a current Host or Platform Owner may adjudicate BOTM matches.")
    if match.status != BotmMatch.Status.PENDING_REVIEW:
        raise ValidationError("Only pending BOTM matches can be adjudicated.")
    if decision not in {BotmMatch.Status.CONFIRMED, BotmMatch.Status.REJECTED}:
        raise ValidationError("Choose Confirmed or Rejected.")
    if decision == BotmMatch.Status.CONFIRMED:
        if not _submission_is_eligible(match.submission) or match.botm_book.is_retired:
            raise ValidationError("This candidate is no longer eligible for confirmation.")
        competing = BotmMatch.objects.select_for_update().filter(
            status=BotmMatch.Status.CONFIRMED,
            is_qualifying=True,
        ).filter(
            models.Q(submission=match.submission)
            | models.Q(botm_book=match.botm_book, participant=match.participant)
        ).exclude(pk=match.pk)
        if competing.exists():
            raise ValidationError("This submission or Reader/book already has an active confirmed BOTM match.")
    match.method = BotmMatch.Method.MANUAL_REVIEW
    match.status = decision
    match.is_qualifying = decision == BotmMatch.Status.CONFIRMED
    match.reviewer = actor
    match.decided_at = timezone.now()
    match.save(update_fields=["method", "status", "is_qualifying", "reviewer", "decided_at", "updated_at"])
    from .botm_rewards import synchronize_botm_book_reward
    synchronize_botm_book_reward(match)
    from .botm_completion import synchronize_botm_completion_for_reader
    synchronize_botm_completion_for_reader(month=match.month, participant=match.participant)
    action = "botm.match_confirmed" if decision == BotmMatch.Status.CONFIRMED else "botm.match_rejected"
    AuditEvent.objects.create(
        actor=actor,
        group=match.month.group,
        action=action,
        object_type="BotmMatch",
        object_id=str(match.pk),
        summary=f"{match.get_status_display()} BOTM candidate {match.botm_book.title_snapshot} for {match.participant.display_name}'s submission {match.submission.title}.",
    )
    return match
