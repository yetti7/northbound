"""Evidence checks and scoped awards for an inactive Reader's pending book."""
import json
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from .models import (
    AuditEvent, BookSubmission, BotmMatch, ModifierProvenance, MonthEnrollment, PersonalTBRMatch,
    TeamAssignment,
)
from .permissions import challenge_review_scope

BOOK_FIELDS = ("title", "author", "catalog_book_id", "catalog_edition_id", "submitted_pages")
ENTRY_FIELDS = ("id", "catalog_book_id", "normalized_title", "normalized_author")


def enrollment_for(submission, *, lock=False):
    enrollments = MonthEnrollment.objects.filter(month_id=submission.month_id, participant_id=submission.participant_id)
    if lock:
        enrollments = enrollments.select_for_update()
    return enrollments.first()


def departure_evidence(submission, enrollment):
    """Resolve the departure and its team from current, durable records."""
    if (enrollment is None or enrollment.is_active or not enrollment.inactivated_at
            or submission.is_removed or not submission.participant.is_active
            or not submission.participant.user.is_active or submission.participant.user.is_superuser
            or not submission.has_verified_completion_period):
        return None
    period = submission.participation_period
    if (period.enrollment_id != enrollment.pk or period.ended_at != enrollment.inactivated_at
            or period.end_reason != enrollment.inactive_reason
            or submission.completed_on != submission.accepted_completed_on
            or submission.acceptance_claims.get("book") != {key: getattr(submission, key) for key in BOOK_FIELDS}):
        return None
    zone = ZoneInfo(submission.acceptance_claims["group_timezone"])
    if submission.accepted_completed_on == timezone.localdate(period.started_at, zone):
        previous_end = enrollment.participation_periods.exclude(pk=period.pk).filter(
            ended_at__lte=period.started_at,
        ).order_by("-ended_at").values_list("ended_at", flat=True).first()
        if previous_end and timezone.localdate(previous_end, zone) == submission.accepted_completed_on:
            # A date alone cannot identify which of two periods on that day contains the reading.
            return None
    assignments = TeamAssignment.objects.filter(month_id=submission.month_id, participant_id=submission.participant_id)
    if assignments.filter(ended_at__isnull=True).exists():
        return None
    latest = assignments.filter(ended_at__lte=period.ended_at).order_by("-ended_at", "-pk").first()
    if (latest is None or not latest.assigned_at or latest.assigned_at > latest.ended_at
            or latest.ended_at < period.started_at or latest.team_id != enrollment.credited_team_id):
        return None
    return latest


def require_late_approval(submission, enrollment, reviewer):
    scope = challenge_review_scope(reviewer, submission.month)
    if not scope or scope[0] != "challenge":
        raise PermissionDenied("Challenge-wide review authority is required for a departed Reader.")
    if submission.status != BookSubmission.Status.PENDING:
        raise ValidationError("Only a pending submission can receive late approval.")
    assignment = departure_evidence(submission, enrollment)
    if assignment is None:
        raise ValidationError("The original participation, submission or credited team cannot be verified.")
    return assignment


def has_acceptance_day_configuration(submission):
    # An acceptance snapshot cannot establish a rate for an earlier local day.
    return submission.accepted_completed_on == timezone.localdate(
        submission.accepted_at, ZoneInfo(submission.acceptance_claims["group_timezone"]),
    )


def theme_rule(submission, claim):
    if (not has_acceptance_day_configuration(submission)
            or not claim.theme.starts_on <= submission.accepted_completed_on <= claim.theme.ends_on):
        return None
    return next((rule for rule in submission.acceptance_claims.get("themes", [])
                 if rule["id"] == claim.theme_id and rule["bonus_rule"] == claim.theme.bonus_rule), None)


def matching_configuration(submission):
    """Read evidence relevant to review staleness, including changes in other books holding credit."""
    from .models import BotmBook, PersonalTBRBook
    month = submission.month
    return {
        "rates": [month.botm_enabled, month.botm_completion_bonus_pages,
                  month.tbr_enabled, month.tbr_book_bonus_pages, month.tbr_completion_bonus_pages],
        "botm": list(BotmBook.objects.filter(month=month).order_by("pk").values(
            *ENTRY_FIELDS, "catalog_edition_id", "is_retired", "bonus_pages")),
        "tbr": list(PersonalTBRBook.objects.filter(
            personal_tbr__enrollment__month=month,
            personal_tbr__enrollment__participant=submission.participant,
        ).order_by("pk").values(*ENTRY_FIELDS, "catalog_edition_id", "personal_tbr__confirmed_at")),
        "theme_rules": list(submission.theme_claims.order_by("pk").values(
            "theme__bonus_rule", "theme__bonus_percentage", "theme__bonus_pages",
            "theme__starts_on", "theme__ends_on")),
        "credit": [list(model.objects.filter(month=month, participant=submission.participant,
                    is_qualifying=True).order_by("pk").values(
                        "id", "submission_id", "updated_at", "submission__completed_on",
                        "submission__accepted_at", "submission__submitted_at",
                    )) for model in (BotmMatch, PersonalTBRMatch)],
    }


def _entry_matches_snapshot(book, snapshot):
    return all(getattr(book, key) == snapshot.get(key) for key in ENTRY_FIELDS) and (
        "catalog_edition_id" not in snapshot or book.catalog_edition_id == snapshot["catalog_edition_id"]
    )


def award_matches(submission, *, reviewed_at, holds):
    from . import botm_completion, botm_matching, botm_rewards
    from . import personal_tbr_completion, personal_tbr_matching, personal_tbr_rewards
    from .matching_resolution import assess_entries, competing, identity_stamp
    from .models import BotmBook, PersonalTBRBook

    month = submission.month
    known_rate = has_acceptance_day_configuration(submission)
    for domain, match_model, field, matcher, rewards, completion, books in (
        ("botm", BotmMatch, "botm_book", botm_matching, botm_rewards, botm_completion,
         BotmBook.objects.filter(month=month, is_retired=False)),
        ("tbr", PersonalTBRMatch, "personal_tbr_book", personal_tbr_matching, personal_tbr_rewards, personal_tbr_completion,
         PersonalTBRBook.objects.filter(personal_tbr__enrollment__month=month,
             personal_tbr__enrollment__participant=submission.participant,
             personal_tbr__confirmed_at__lte=submission.accepted_at)),
    ):
        from .prompt_reservations import has_bingo_selection, assert_bingo_allowed
        if domain == 'tbr' and has_bingo_selection(submission):
            assert_bingo_allowed(submission)
            continue
        label = "BOTM" if domain == "botm" else "Personal TBR"
        config = submission.acceptance_claims.get(domain, {})
        if not config.get("enabled"):
            continue
        books = list(books.select_for_update())
        candidates = assess_entries(submission, books, domain)
        if not candidates:
            if (getattr(submission, "selected_botm_book_id" if domain == "botm" else "selected_tbr_book_id")
                    or match_model.objects.filter(submission=submission).exists()):
                holds.append(f"{label} qualification needs review.")
            continue
        if len(candidates) != 1:
            holds.append(f"{label} qualification needs review.")
            continue
        book, strength, reason = candidates[0]
        existing = match_model.objects.filter(submission=submission)
        snapshots = {item["id"]: item for item in config.get("books", [])}
        snapshot = snapshots.get(book.pk)
        if snapshot is None:
            intent = submission.reader_intent.get(domain)
            if intent and intent["id"] == book.pk:
                snapshot = intent
        prior = existing.filter(**{field: book}).first()
        source_ref = (rewards.botm_book_reward_source_reference if domain == "botm"
                      else rewards.personal_tbr_book_reward_source_reference)
        if prior and prior.is_qualifying:
            continue
        if (not getattr(month, f"{domain}_enabled") or not snapshot or not _entry_matches_snapshot(book, snapshot)
                or strength not in {"work", "exact"}
                or existing.filter(method="manual_review").exists()
                or (prior and (prior.status != "pending_review" or ModifierProvenance.objects.filter(
                    submission=submission, source_reference=source_ref(prior),
                ).exists()))
                or competing(match_model, submission, book, field)):
            holds.append(f"{label} qualification needs review.")
            continue
        match = matcher._confirm_candidate(
            book, submission, method="catalog_work" if strength == "work" else "normalized_title_author",
            evidence_summary=reason,
        )
        if match is None or not match.is_qualifying:
            holds.append(f"{label} qualification needs review.")
            continue
        rate = (snapshot.get("bonus_pages") if domain == "botm" else config.get("book_bonus_pages")) if known_rate else None
        match.decision_evidence = {
            "fingerprint": identity_stamp(submission, book), "late_approval_bonus_pages": rate,
            "accepted_at": submission.accepted_at.isoformat(), "participation_period_id": submission.participation_period_id,
        }
        match.save(update_fields=["decision_evidence", "updated_at"])
        if rate is None:
            holds.append(f"{label} book bonus needs historical configuration review.")
        else:
            rewards._synchronize_locked_reward(match, late_approval=True)
        matched = match_model.objects.filter(month=month, participant=submission.participant,
                                              status="confirmed", is_qualifying=True)
        current_ids = {entry.pk for entry in books}
        if current_ids != set(matched.values_list(f"{field}_id", flat=True)) or (domain == "tbr" and len(current_ids) != 9):
            continue
        if (not known_rate or current_ids != snapshots.keys()
                or not all(_entry_matches_snapshot(entry, snapshots[entry.pk]) for entry in books)
                or any((row.submission.accepted_at or row.submission.submitted_at) > submission.accepted_at
                       or row.submission.completed_on > submission.accepted_completed_on
                       for row in matched.select_related("submission"))):
            holds.append(f"{label} completion bonus needs historical configuration review.")
            match.decision_evidence["late_approval_completion_held"] = True
            match.save(update_fields=["decision_evidence", "updated_at"])
            continue
        synchronize = (completion.synchronize_botm_completion_for_reader if domain == "botm"
                       else completion.synchronize_personal_tbr_completion_for_reader)
        synchronize(month=month, participant=submission.participant, now=reviewed_at,
                    late_award=(config["completion_bonus_pages"], submission.accepted_completed_on))


def record_late_approval(submission, reviewer, assignment, holds, held_theme_claim_ids):
    evidence = {
        "challenge_id": submission.month_id, "participation_period": submission.participation_period_id,
        "assignment_id": assignment.pk, "team_id": assignment.team_id, "approved_pages": submission.approved_pages,
        "approved_at": submission.reviewed_at.isoformat(), "reviewer_id": reviewer.pk,
        "held_for_review": holds, "held_theme_claim_ids": held_theme_claim_ids,
    }
    BookSubmission.objects.filter(pk=submission.pk).update(late_approval_evidence=evidence)
    submission.late_approval_evidence = evidence
    AuditEvent.objects.create(
        actor=reviewer, group=submission.month.group, action="submission.late_approved",
        object_type="BookSubmission", object_id=str(submission.pk),
        summary=json.dumps(evidence),
    )
