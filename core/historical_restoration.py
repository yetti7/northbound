"""Exact reactivation of credit suspended by a single, recorded void."""
import hashlib
import json
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import (
    BookSubmission, BotmBook, BotmCompletionAward, BotmMatch, ImmersiveRequest,
    ModifierProvenance, MonthEnrollment, PersonalTBRCompletionAward, PersonalTBRMatch,
    TeamAssignment, ThemeClaim,
)

DOMAINS = {
    "botm": (BotmMatch, "botm_book", BotmCompletionAward, "botm_match", "botm_completion"),
    "tbr": (PersonalTBRMatch, "personal_tbr_book", PersonalTBRCompletionAward, "personal_tbr_match", "personal_tbr_completion"),
}
BOOK_FIELDS = ("title", "author", "catalog_book_id", "catalog_edition_id", "submitted_pages")
REVIEW_FIELDS = (*BOOK_FIELDS, "participant_id", "month_id", "completed_on", "book_format",
                 "status", "approved_pages", "reviewed_by_id", "reviewed_at",
                 "verification_method", "metadata_pages", "scoring_catalog_edition_id",
                 "participation_period_id", "accepted_at", "accepted_completed_on",
                 "acceptance_claims", "late_approval_evidence")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, default=str, sort_keys=True).encode()).hexdigest()


def row_stamp(row):
    return fingerprint({field.attname: getattr(row, field.attname) for field in row._meta.concrete_fields
                        if field.attname != "updated_at"})


def credited_team(submission):
    enrollment = MonthEnrollment.objects.filter(month_id=submission.month_id, participant_id=submission.participant_id).first()
    if enrollment is None:
        return None
    assignments = TeamAssignment.objects.filter(
        month_id=submission.month_id, participant_id=submission.participant_id, team__month_id=submission.month_id,
    )
    current = assignments.filter(ended_at__isnull=True).first()
    if enrollment.is_active and not enrollment.awaiting_scoring_team:
        return current.team_id if current and enrollment.credited_team_id in (None, current.team_id) else None
    latest = assignments.filter(ended_at__isnull=False).order_by("-ended_at", "-pk").first()
    if not current and latest and latest.team_id == enrollment.credited_team_id:
        return latest.team_id
    return None


def accepted_identity_is_valid(submission):
    if (not submission.has_verified_completion_period
            or submission.completed_on != submission.accepted_completed_on
            or submission.acceptance_claims.get("book") != {key: getattr(submission, key) for key in BOOK_FIELDS}):
        return False
    period = submission.participation_period
    if (period.enrollment.month_id, period.enrollment.participant_id) != (submission.month_id, submission.participant_id):
        return False
    zone = ZoneInfo(submission.acceptance_claims["group_timezone"])
    if timezone.localdate(period.started_at, zone) == submission.accepted_completed_on:
        prior_end = period.enrollment.participation_periods.exclude(pk=period.pk).filter(
            ended_at__lte=period.started_at,
        ).order_by("-ended_at").values_list("ended_at", flat=True).first()
        if prior_end and timezone.localdate(prior_end, zone) == submission.accepted_completed_on:
            return False
    return True


def _review_stamp(submission):
    return fingerprint({key: getattr(submission, key) for key in REVIEW_FIELDS})


def lock_restoration_state(submission):
    scope = _scope(submission)
    for model, filters in (
        (MonthEnrollment, {"month_id": submission.month_id, "participant_id": submission.participant_id}),
        (TeamAssignment, scope),
        (ModifierProvenance, scope),
        (BotmMatch, scope), (PersonalTBRMatch, scope),
        (BotmCompletionAward, scope), (PersonalTBRCompletionAward, scope),
        (ThemeClaim, {"submission": submission}), (ImmersiveRequest, {"submission": submission}),
    ):
        list(model.objects.select_for_update().filter(**filters).order_by("pk").values_list("pk", flat=True))


def _scope(submission):
    return {"month_id": submission.month_id, "participant_id": submission.participant_id}


def _completion_ids(award, field):
    return set(award.configured_books.values_list(f"{field}_id", flat=True))


def _match_stamp(submission, match, field):
    from .matching_resolution import identity_stamp
    book = getattr(match, field)
    return fingerprint([row_stamp(match), identity_stamp(submission, book), book.catalog_edition_id])


def _completion_stamp(award, field):
    return fingerprint([row_stamp(award), list(award.configured_books.order_by("pk").values()),
                        sorted(_completion_ids(award, field))])


def _source_rows(submission):
    return ModifierProvenance.objects.filter(**_scope(submission), submission=submission)


def _suspend(row, field, *, at=None):
    changes = {field: False}
    if at is not None:
        changes["inactivated_at"] = at
    type(row).objects.filter(pk=row.pk).update(**changes)
    for key, value in changes.items():
        setattr(row, key, value)


def suspend_earned_credit(submission):
    """Capture durable evidence and suspend exactly the credit affected by this void."""
    scope = _scope(submission)
    active = {row.source_reference: row for row in _source_rows(submission).select_for_update()
              if row.is_active and row.voided_at is None}
    sources = {}
    matches = {}
    awards = {}

    def save_source(row, dependency=None):
        _suspend(row, "is_active")
        sources[str(row.pk)] = {"stamp": row_stamp(row), "dependency": dependency, "label": row.source_label}

    for domain, (model, field, award_model, match_prefix, award_prefix) in DOMAINS.items():
        earned = list(model.objects.select_for_update().filter(**scope, submission=submission,
                                                               status="confirmed", is_qualifying=True))
        for match in earned:
            book_id = getattr(match, f"{field}_id")
            _suspend(match, "is_qualifying")
            key = f"{domain}:{match.pk}"
            matches[key] = {"stamp": _match_stamp(submission, match, field), "book_id": book_id}
            reward = active.pop(f"{match_prefix}:{match.pk}", None)
            if reward:
                save_source(reward, key)
            for award in award_model.objects.select_for_update().filter(**scope, is_qualifying=True,
                                                                         **{f"configured_books__{field}_id": book_id}):
                _suspend(award, "is_qualifying", at=submission.removed_at)
                award_key = f"{domain}:{award.pk}"
                awards[award_key] = {"stamp": _completion_stamp(award, field), "match": key}
                reward = ModifierProvenance.objects.select_for_update().filter(
                    **scope, submission__isnull=True, source_type=f"{domain}_completion",
                    source_reference=f"{award_prefix}:{award.pk}", is_active=True, voided_at__isnull=True,
                ).first()
                if reward:
                    save_source(reward, f"completion:{award_key}")

    for request in submission.immersive_requests.select_for_update().filter(status="approved", is_active=True):
        _suspend(request, "is_active")
        reward = active.pop(f"immersive_request:{request.pk}", None)
        if reward:
            save_source(reward, ["immersive", request.pk, row_stamp(request)])
    for row in active.values():
        if row.source_type == ModifierProvenance.SourceType.LEGACY_MODIFIER:
            save_source(row)
        elif row.source_type == ModifierProvenance.SourceType.THEME_BONUS:
            claim = submission.theme_claims.filter(
                pk__in=[row.source_reference.removeprefix("theme_claim:")] if row.source_reference.startswith("theme_claim:")
                and row.source_reference.removeprefix("theme_claim:").isdigit() else [],
                status=ThemeClaim.Status.APPROVED,
            ).first()
            save_source(row, ["theme", claim.pk, row_stamp(claim)] if claim else ["unknown"])
        else:
            # A ledger row with no coherent source was suspended but remains held.
            save_source(row, ["unknown"])
    evidence = {"voided_at": submission.removed_at.isoformat(), "status": submission.status, "review": _review_stamp(submission),
                "matches": matches, "awards": awards, "sources": sources}
    BookSubmission.objects.filter(pk=submission.pk).update(void_earning_evidence=evidence)
    submission.void_earning_evidence = evidence
    BookSubmission.objects.filter(pk=submission.pk).update(bonus_pages=0, final_scored_pages=None)


def has_void_evidence(submission):
    return bool(submission.removed_at and submission.void_earning_evidence.get("voided_at") == submission.removed_at.isoformat())


def ordinary_historical_eligible(submission):
    return bool(
        submission.participant.is_active and submission.participant.user.is_active
        and has_void_evidence(submission)
        and submission.void_earning_evidence["review"] == _review_stamp(submission)
        and accepted_identity_is_valid(submission) and credited_team(submission)
        and (submission.status != BookSubmission.Status.APPROVED or submission.approved_pages)
    )


def restoration_plan(submission, *, lock=False):
    from .matching_resolution import competing, identity_evidence, identity_stamp, _legacy_stamp_matches

    def query(model):
        return model.objects.select_for_update() if lock else model.objects

    evidence = submission.void_earning_evidence
    scope = _scope(submission)
    matches, awards, sources, immersive, held = {}, {}, {}, {}, []
    coherent = (has_void_evidence(submission) and evidence["review"] == _review_stamp(submission)
                and submission.status == BookSubmission.Status.APPROVED)
    for key, saved in evidence["matches"].items():
        domain, pk = key.split(":")
        model, field, _, _, _ = DOMAINS[domain]
        match = query(model).filter(**scope, pk=pk, submission=submission).first()
        if coherent and match and _match_stamp(submission, match, field) == saved["stamp"]:
            book = getattr(match, field)
            available = (submission.month.botm_enabled and not book.is_retired if domain == "botm"
                         else submission.month.tbr_enabled and book.personal_tbr.confirmed_at
                         and book.personal_tbr.enrollment_id == submission.participation_period.enrollment_id)
            if match.method == "manual_review":
                valid = (match.decision_evidence.get("fingerprint") == identity_stamp(submission, book)
                         if match.decision_evidence else _legacy_stamp_matches(match, submission))
            else:
                valid = identity_evidence(submission, book)[0] in {"work", "exact"}
            if available and valid and not competing(model, submission, book, field, match.pk):
                matches[key] = match
                continue
        held.append(f"{domain.upper()} match #{pk}")
    for key, saved in evidence["awards"].items():
        domain, pk = key.split(":")
        model, field, award_model, _, _ = DOMAINS[domain]
        award = query(award_model).filter(**scope, pk=pk).first()
        if coherent and award and saved["match"] in matches and _completion_stamp(award, field) == saved["stamp"]:
            frozen = _completion_ids(award, field)
            current = set(BotmBook.objects.filter(month=submission.month, is_retired=False).values_list("pk", flat=True)
                          if domain == "botm" else award.personal_tbr.books.values_list("pk", flat=True))
            qualifying = set(model.objects.filter(**scope, status="confirmed", is_qualifying=True,
                                                    submission__status="approved", submission__is_removed=False)
                             .values_list(f"{field}_id", flat=True))
            qualifying.add(getattr(matches[saved["match"]], f"{field}_id"))
            if frozen == current and frozen <= qualifying:
                awards[key] = award
                continue
        held.append(f"{domain.upper()} completion award #{pk}")
    for pk, saved in evidence["sources"].items():
        row = query(ModifierProvenance).filter(**scope, pk=pk, voided_at__isnull=True).first()
        dependency = saved["dependency"]
        valid = dependency is None
        if isinstance(dependency, str):
            valid = (dependency.removeprefix("completion:") in awards
                     if dependency.startswith("completion:") else dependency in matches)
        elif dependency and dependency[0] in {"theme", "immersive"}:
            kind, target_id, stamp = dependency
            target = query(ThemeClaim if kind == "theme" else ImmersiveRequest).filter(
                pk=target_id, submission=submission, status="approved",
            ).first()
            valid = target is not None and row_stamp(target) == stamp
            if valid and coherent and kind == "immersive" and row and row_stamp(row) == saved["stamp"]:
                immersive[target_id] = target
        if coherent and row and valid and row_stamp(row) == saved["stamp"]:
            sources[pk] = row
        else:
            held.append(saved["label"])
    if _source_rows(submission).filter(is_active=True).exclude(pk__in=sources).exists():
        raise ValidationError("Active credit changed independently while this submission was voided. Review it before restoring.")
    return matches, awards, sources, immersive, held


def apply_restoration_plan(submission, plan):
    matches, awards, sources, immersive, held = plan
    for rows, field in ((matches, "is_qualifying"), (awards, "is_qualifying"),
                        (sources, "is_active"), (immersive, "is_active")):
        for row in rows.values():
            changes = {field: True}
            if isinstance(row, (BotmCompletionAward, PersonalTBRCompletionAward)):
                changes["inactivated_at"] = None
            type(row).objects.filter(pk=row.pk).update(**changes)
    total = sum(row.amount for row in sources.values() if row.submission_id == submission.pk)
    BookSubmission.objects.filter(pk=submission.pk).update(
        bonus_pages=total, final_scored_pages=(submission.approved_pages + total
                                             if submission.status == BookSubmission.Status.APPROVED else None),
    )
    evidence = submission.void_earning_evidence | {
        "restored_source_references": sorted(row.source_reference for row in sources.values()),
    }
    BookSubmission.objects.filter(pk=submission.pk).update(void_earning_evidence=evidence)
    submission.void_earning_evidence = evidence
    return held


def limits_automatic_credit(submission):
    return bool(
        submission.void_earning_evidence.get("status") == BookSubmission.Status.APPROVED
        and "restored_source_references" in submission.void_earning_evidence
    )


def permits_automatic_source(submission, source_reference):
    return not limits_automatic_credit(submission) or source_reference in submission.void_earning_evidence["restored_source_references"]
