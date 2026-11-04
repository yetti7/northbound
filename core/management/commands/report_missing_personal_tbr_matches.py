"""Read-only assessment; deliberately never invokes synchronization services."""
import json

from django.core.management.base import BaseCommand, CommandError

from core.models import (
    BookSubmission, ChallengeMonth, MonthEnrollment, PersonalTBRMatch,
    PersonalTBRCompletionAward, normalize_book_identity,
)
from core.personal_tbr_matching import select_candidates


class Command(BaseCommand):
    help = "Report eligible approved submissions with no Personal TBR match; never repair data."

    def add_arguments(self, parser):
        parser.add_argument("--group", required=True)
        parser.add_argument("--month", required=True, type=int)

    def handle(self, *args, **options):
        try:
            month = ChallengeMonth.objects.get(pk=options["month"], group__slug=options["group"])
        except ChallengeMonth.DoesNotExist as exc:
            raise CommandError("Challenge not found for this Group.") from exc
        rows, readers = [], []
        enrollments = MonthEnrollment.objects.filter(
            month=month, is_active=True, participant__is_active=True,
            participant__user__is_superuser=False, personal_tbr__confirmed_at__isnull=False,
        ).select_related("personal_tbr", "participant")
        if not month.tbr_enabled:
            enrollments = enrollments.none()
        for enrollment in enrollments.order_by("pk"):
            tbr = enrollment.personal_tbr
            books = list(tbr.books.order_by("pk"))
            completed = set(PersonalTBRMatch.objects.filter(
                personal_tbr_book__personal_tbr=tbr, status="confirmed", is_qualifying=True,
            ).values_list("personal_tbr_book_id", flat=True))
            projected = set(completed)
            potential_book_bonus = 0
            submissions = BookSubmission.objects.filter(
                month=month, participant=enrollment.participant, status="approved", is_removed=False,
                personal_tbr_matches__isnull=True,
            ).order_by("pk")
            for submission in submissions:
                method, candidates = select_candidates(
                    submission_catalog_book_id=submission.catalog_book_id,
                    normalized_title=normalize_book_identity(submission.title),
                    normalized_author=normalize_book_identity(submission.author), books=books,
                )
                if not candidates:
                    continue
                credit = len(candidates) == 1 and candidates[0].pk not in projected
                bonus = month.tbr_book_bonus_pages if credit else 0
                if credit:
                    projected.add(candidates[0].pk)
                    potential_book_bonus += bonus
                rows.append({
                    "submission_id": submission.pk, "participant_id": enrollment.participant_id,
                    "tbr_book_ids": [book.pk for book in candidates], "method": method,
                    "assessment": "unique_uncredited" if credit else (
                        "ambiguous_requires_review" if len(candidates) > 1 else "already_credited_or_duplicate"
                    ),
                    "potential_new_book_bonus_pages": bonus,
                })
            if projected != completed:
                award = PersonalTBRCompletionAward.objects.filter(personal_tbr=tbr).first()
                complete = len(books) == 9 and projected == {book.pk for book in books}
                readers.append({
                    "participant_id": enrollment.participant_id,
                    "current_completed": len(completed), "projected_completed": len(projected),
                    "potential_new_book_bonus_pages": potential_book_bonus,
                    "could_qualify_full_completion": complete,
                    "existing_completion_award_id": award.pk if award else None,
                    "potential_completion_bonus_pages": (
                        (award.bonus_amount_snapshot if award else month.tbr_completion_bonus_pages)
                        if complete and (award is None or not award.is_qualifying) else 0
                    ),
                })
        self.stdout.write(json.dumps({
            "read_only": True, "group": options["group"], "month_id": month.pk,
            "limitations": [
                "Only eligible approved submissions with no match records and deterministic candidates are listed.",
                "Existing pending, rejected, or nonqualifying match records require separate review.",
                "Projections use submission ID order and current configuration; no awards are created.",
                "Review durable provenance, frozen award sets, effective dates and historical checkpoints before repair.",
                "This is a live read, not a consistent production snapshot; run against a read-only snapshot for final review.",
            ],
            "submissions": rows, "readers": readers,
        }, indent=2))
