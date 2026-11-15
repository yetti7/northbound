"""Read-only assessment; deliberately never invokes synchronization services."""
import json
import argparse
from core.matching_resolution import assess_entries
from core.historical_restoration import limits_automatic_credit
from core.permissions import is_active_floater

from django.core.management.base import BaseCommand, CommandError

from core.models import (
    BookSubmission, ChallengeMonth, MonthEnrollment, PersonalTBRMatch,
    PersonalTBRCompletionAward, normalize_book_identity,
)
from core.personal_tbr_matching import select_candidates


class Command(BaseCommand):
    help = "Report eligible approved submissions with no Personal TBR match; never repair data."

    def add_arguments(self, parser):
        # Reconciliation deliberately retains its historical candidate set.
        parser.add_argument("--legacy-candidates", action="store_true", help=argparse.SUPPRESS)
        parser.add_argument("--group", required=True)
        parser.add_argument("--month", required=True, type=int)

    def handle(self, *args, **options):
        try:
            month = ChallengeMonth.objects.get(pk=options["month"], group__slug=options["group"])
        except ChallengeMonth.DoesNotExist as exc:
            raise CommandError("Challenge not found for this Group.") from exc
        legacy = options["legacy_candidates"]
        rows, readers = [], []
        enrollments = MonthEnrollment.objects.filter(
            month=month, is_active=True, participant__is_active=True,
            participant__user__is_superuser=False, personal_tbr__confirmed_at__isnull=False,
        ).select_related("personal_tbr", "participant")
        if not month.tbr_enabled:
            enrollments = enrollments.none()
        for enrollment in enrollments.order_by("pk"):
            tbr = enrollment.personal_tbr
            books = list(tbr.books.select_related("catalog_book", "catalog_edition__book").order_by("pk"))
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
                if legacy:
                    method, candidates = select_candidates(
                        submission_catalog_book_id=submission.catalog_book_id,
                        normalized_title=normalize_book_identity(submission.title),
                        normalized_author=normalize_book_identity(submission.author), books=books,
                    )
                    assessed = []
                    eligible = True
                else:
                    assessed = assess_entries(submission, books, 'tbr')
                    candidates = [item[0] for item in assessed]
                    method = assessed[0][1] if len(assessed) == 1 else 'ambiguous'
                    from core.prompt_reservations import has_bingo_selection
                    eligible = (month.status in {'active', 'finalizing'} and enrollment.participant.user.is_active
                        and not enrollment.awaiting_scoring_team and not submission.late_approval_evidence
                        and not limits_automatic_credit(submission) and not has_bingo_selection(submission)
                        and not is_active_floater(month, enrollment.participant_id)
                        and all(evidence != 'conflict' for _, evidence, _ in assessed))
                if not candidates:
                    continue
                strong = legacy or (len(assessed) == 1 and assessed[0][1] in {'work', 'exact'})
                credit = eligible and strong and len(candidates) == 1 and candidates[0].pk not in projected
                bonus = month.tbr_book_bonus_pages if credit else 0
                if credit:
                    projected.add(candidates[0].pk)
                    potential_book_bonus += bonus
                rows.append({
                    "submission_id": submission.pk, "participant_id": enrollment.participant_id,
                    "tbr_book_ids": [book.pk for book in candidates], "method": method,
                    "evidence": [reason for _, _, reason in assessed],
                    "assessment": "unique_uncredited" if credit else "ineligible" if not eligible else (
                        "ambiguous_requires_review" if len(candidates) > 1 or not strong else "already_credited_or_duplicate"
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
            "read_only": True, "evaluator": "legacy_reconciliation" if legacy else "live_assess_entries", "group": options["group"], "month_id": month.pk,
            "limitations": [
                "Approved submissions without match records are assessed; eligibility and ambiguity are reported separately.",
                "Existing pending, rejected, or nonqualifying match records require separate review.",
                "Projections use submission ID order and current configuration; no awards are created.",
                "Review durable provenance, frozen award sets, effective dates and historical checkpoints before repair.",
                "This is a live read, not a consistent production snapshot; run against a read-only snapshot for final review.",
            ],
            "submissions": rows, "readers": readers,
        }, indent=2))
