from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from .models import AuditEvent, BookSubmission, BotmBook, BotmMatch, ChallengeMonth, MonthEnrollment, normalize_book_identity
from .permissions import can_operate_challenge, challenge_history_locked


def _finalize_submission_matches(submission, matches, *, earned_only=False, restore_earned=False):
    from .botm_rewards import synchronize_botm_book_rewards_for_submission
    synchronize_botm_book_rewards_for_submission(
        submission, earned_only=earned_only, restore_earned=restore_earned,
    )
    from .botm_completion import synchronize_botm_completion_for_reader
    synchronize_botm_completion_for_reader(
        month=submission.month, participant=submission.participant,
        earned_only=earned_only, restore_earned=restore_earned,
    )
    return matches


def _catalog_identity(catalog_book):
    if catalog_book is None:
        return ""
    return f"{catalog_book.provider}:{catalog_book.provider_book_id}"


def _submission_is_eligible(submission):
    return (
        submission.status == BookSubmission.Status.APPROVED
        and not submission.is_removed
        and not submission.late_approval_evidence
        and submission.participant.is_active
        and not submission.participant.user.is_superuser
        and MonthEnrollment.objects.filter(
            month_id=submission.month_id,
            participant_id=submission.participant_id,
            is_active=True, awaiting_scoring_team=False,
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
    from .matching_resolution import synchronize_domain
    return synchronize_domain(submission, 'botm')


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
    if challenge_history_locked(month):
        raise ValidationError("Completed and Archived Challenges are read-only.")
    return _synchronize_challenge_matches(month)


def _synchronize_challenge_matches(month):
    """Rebuild matches inside the caller's ordinary or audited recovery transaction."""
    results = []
    for submission in BookSubmission.objects.filter(month=month).order_by("pk"):
        results.extend(synchronize_submission(submission))
    return results


@transaction.atomic
def adjudicate_match(*, match, actor, decision):
    from .matching_resolution import confirm_suggestion
    return confirm_suggestion(match=match, actor=actor, decision=decision)
