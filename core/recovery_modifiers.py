from dataclasses import replace
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .botm_completion import synchronize_botm_completion_for_reader
from .botm_matching import synchronize_reader as synchronize_botm_reader
from .game_rewards import apply_game_reward
from .models import (
    AuditEvent, BookSubmission, BotmBook, BotmCompletionAward,
    BotmCompletionAwardBook, BotmMatch, CatalogBook, CatalogEdition,
    Game, GameRewardApplication, GameRewardRecipient, ModifierProvenance,
    PersonalTBR, PersonalTBRBook, PersonalTBRCompletionAward,
    PersonalTBRCompletionAwardBook, PersonalTBRMatch, normalize_book_identity,
)
from .personal_tbr_completion import synchronize_personal_tbr_completion_for_reader
from .personal_tbr_matching import synchronize_reader as synchronize_tbr_reader
from .recovery import (
    RecoveryImpactItem, RecoveryImpactPreview, RecoveryMutationResult,
    execute_recovery_operation,
)
from .scoring import refresh_submission_score


def botm_match_label(match):
    return f"BOTM match #{match.pk}: {match.botm_title_snapshot} — {match.participant.display_name}"


def tbr_match_label(match):
    return f"TBR match #{match.pk}: {match.tbr_title_snapshot} — {match.participant.display_name}"


def tbr_label(tbr):
    return f"Personal TBR #{tbr.pk}: {tbr.enrollment.participant.display_name} — {tbr.enrollment.month.name}"


def game_application_label(application):
    return f"Game reward #{application.pk}: {application.game_name_snapshot} — {application.target_label}"


def _refresh_reader_submissions(month, participant):
    for submission in BookSubmission.objects.filter(month=month, participant=participant).order_by("pk"):
        refresh_submission_score(submission)


def _reconcile_botm_reader(month, participant, *, rematch=True):
    if rematch:
        synchronize_botm_reader(month=month, participant=participant)
    else:
        from .botm_rewards import synchronize_botm_book_rewards_for_reader
        synchronize_botm_book_rewards_for_reader(month=month, participant=participant)
    synchronize_botm_completion_for_reader(month=month, participant=participant)
    _refresh_reader_submissions(month, participant)


def _reconcile_tbr_reader(month, participant, *, rematch=True):
    if rematch:
        synchronize_tbr_reader(month=month, participant=participant)
    else:
        from .personal_tbr_rewards import synchronize_personal_tbr_book_rewards_for_reader
        synchronize_personal_tbr_book_rewards_for_reader(month=month, participant=participant)
    synchronize_personal_tbr_completion_for_reader(month=month, participant=participant)
    _refresh_reader_submissions(month, participant)


def _void_match_provenance(*, match, source_type, reference, actor, reason):
    provenance = ModifierProvenance.objects.select_for_update().filter(
        source_type=source_type, source_reference=reference,
    ).first()
    if provenance and provenance.is_active:
        provenance.is_active = False
        provenance.voided_by = actor
        provenance.voided_at = timezone.now()
        provenance.void_reason = reason
        provenance.save(update_fields=["is_active", "voided_by", "voided_at", "void_reason"])
    return provenance


def _repair_match_provenance(match, *, source_type, reference, context):
    candidates = ModifierProvenance.objects.select_for_update().filter(
        source_type=source_type,
    ).filter(Q(source_reference=reference) | Q(source_context=context))
    removed = 0
    for provenance in candidates:
        coherent = (
            provenance.source_reference == reference
            and provenance.month_id == match.month_id
            and provenance.participant_id == match.participant_id
            and provenance.submission_id == match.submission_id
        )
        if not coherent:
            provenance.delete()
            removed += 1
    return removed


def recover_botm_match(*, match, decision, target_book, recovery_request):
    if decision not in {"pending", "rejected", "confirmed"}:
        raise ValidationError("Choose Pending, Rejected, or Confirmed.")

    def mutation():
        locked = BotmMatch.objects.select_for_update().select_related(
            "month__group", "participant", "submission", "botm_book",
        ).get(pk=match.pk)
        if target_book.month_id != locked.month_id or target_book.is_retired:
            raise ValidationError("Choose an active BOTM book in the same Challenge.")
        before = {"status": locked.status, "is_qualifying": locked.is_qualifying, "botm_book_id": locked.botm_book_id}
        original = locked
        if target_book.pk != locked.botm_book_id:
            locked.status = BotmMatch.Status.REJECTED
            locked.is_qualifying = False
            locked.reviewer = recovery_request.actor
            locked.decided_at = timezone.now()
            locked.save(update_fields=["status", "is_qualifying", "reviewer", "decided_at", "updated_at"])
            _void_match_provenance(
                match=locked, source_type=ModifierProvenance.SourceType.BOTM_BOOK,
                reference=f"botm_match:{locked.pk}", actor=recovery_request.actor,
                reason="BOTM recovery replaced the match target.",
            )
            replacement = BotmMatch.objects.select_for_update().filter(
                botm_book=target_book, participant=locked.participant, submission=locked.submission,
            ).first()
            if replacement is None:
                replacement = BotmMatch.objects.create(
                    botm_book=target_book, month=locked.month, participant=locked.participant,
                    submission=locked.submission, method=BotmMatch.Method.MANUAL_REVIEW,
                    status=BotmMatch.Status.PENDING_REVIEW, is_qualifying=False,
                    botm_title_snapshot=target_book.title_snapshot,
                    botm_author_snapshot=target_book.author_snapshot,
                    botm_catalog_identity=(f"{target_book.catalog_book.provider}:{target_book.catalog_book.provider_book_id}" if target_book.catalog_book else ""),
                    submission_title_snapshot=locked.submission.title,
                    submission_author_snapshot=locked.submission.author,
                    submission_catalog_identity=(f"{locked.submission.catalog_book.provider}:{locked.submission.catalog_book.provider_book_id}" if locked.submission.catalog_book else ""),
                    evidence_summary=f"Platform recovery replaced BOTM match {original.pk}.",
                    reviewer=recovery_request.actor,
                )
            locked = replacement

        if decision == "confirmed":
            if locked.submission.status != BookSubmission.Status.APPROVED or locked.submission.is_removed:
                raise ValidationError("A removed or non-approved submission cannot qualify for BOTM.")
            competitors = BotmMatch.objects.select_for_update().filter(
                status=BotmMatch.Status.CONFIRMED, is_qualifying=True,
            ).exclude(pk=locked.pk)
            if competitors.filter(submission=locked.submission).exists() or competitors.filter(
                botm_book=locked.botm_book, participant=locked.participant,
            ).exists():
                raise ValidationError("Another qualifying BOTM match already owns this submission or Reader/book.")
            locked.status = BotmMatch.Status.CONFIRMED
            locked.is_qualifying = True
            locked.decided_at = timezone.now()
        elif decision == "rejected":
            locked.status = BotmMatch.Status.REJECTED
            locked.is_qualifying = False
            locked.decided_at = timezone.now()
        else:
            locked.status = BotmMatch.Status.PENDING_REVIEW
            locked.is_qualifying = False
            locked.decided_at = None
        locked.method = BotmMatch.Method.MANUAL_REVIEW
        locked.reviewer = recovery_request.actor
        locked.save(update_fields=["method", "status", "is_qualifying", "reviewer", "decided_at", "updated_at"])
        malformed_removed = _repair_match_provenance(
            locked, source_type=ModifierProvenance.SourceType.BOTM_BOOK,
            reference=f"botm_match:{locked.pk}", context=f"Qualifying BOTM match {locked.pk}",
        )
        _reconcile_botm_reader(locked.month, locked.participant, rematch=False)
        locked.refresh_from_db()
        return {
            "before": before, "match_id": locked.pk, "status": locked.status,
            "is_qualifying": locked.is_qualifying, "botm_book_id": locked.botm_book_id,
            "active_provenance_count": ModifierProvenance.objects.filter(
                source_type=ModifierProvenance.SourceType.BOTM_BOOK,
                source_reference=f"botm_match:{locked.pk}", is_active=True,
            ).count(),
            "malformed_provenance_removed": malformed_removed,
        }
    return execute_recovery_operation(recovery_request, mutation)


def purge_botm_match(*, match, recovery_request, fail_after_step=None):
    if recovery_request.tier != 3:
        raise ValidationError("BOTM match purge requires Tier 3 recovery.")

    def mutation():
        locked = BotmMatch.objects.select_for_update().select_related("month__group", "participant", "submission").get(pk=match.pk)
        month, participant = locked.month, locked.participant
        provenance = ModifierProvenance.objects.filter(
            source_type=ModifierProvenance.SourceType.BOTM_BOOK,
            source_reference=f"botm_match:{locked.pk}",
        )
        impact = RecoveryImpactPreview(
            target_label=botm_match_label(locked),
            items=(RecoveryImpactItem("Match", 1), RecoveryImpactItem("Per-book provenance", provenance.count())),
            warnings=("The malformed match relationship is permanently removed after reward and completion reconciliation.",),
        )
        locked.is_qualifying = False
        locked.status = BotmMatch.Status.REJECTED
        locked.save(update_fields=["status", "is_qualifying", "updated_at"])
        _reconcile_botm_reader(month, participant)
        provenance.delete()
        if fail_after_step == "provenance":
            raise RuntimeError("Injected BOTM match purge failure.")
        BotmMatch.objects.filter(pk=locked.pk).delete()
        _reconcile_botm_reader(month, participant)
        return RecoveryMutationResult(after_state={"exists": False}, impact=impact)
    return execute_recovery_operation(recovery_request, mutation)


def set_botm_book_retired(*, book, retired, recovery_request):
    def mutation():
        locked = BotmBook.objects.select_for_update().select_related("month__group").get(pk=book.pk)
        before = locked.is_retired
        locked.is_retired = retired
        locked.save(update_fields=["is_retired"])
        from .botm_matching import synchronize_challenge
        synchronize_challenge(locked.month)
        return {"is_retired": retired, "previous_is_retired": before, "match_count": locked.matches.count()}
    return execute_recovery_operation(recovery_request, mutation)


def correct_botm_book(*, book, values, recovery_request, allow_used=False):
    identity_fields = {
        "catalog_book", "catalog_edition", "title_snapshot", "author_snapshot",
        "page_count_snapshot", "cover_url_snapshot", "source_url_snapshot",
    }
    if set(values) != identity_fields:
        raise ValidationError("Provide the complete supported BOTM identity snapshot.")

    def mutation():
        locked = BotmBook.objects.select_for_update().select_related("month__group").get(pk=book.pk)
        used = locked.matches.exists()
        if used and not allow_used:
            raise ValidationError("Matched BOTM identity repair requires Tier 3 recovery.")
        if used and recovery_request.tier != 3:
            raise ValidationError("Matched BOTM identity repair requires Tier 3 recovery.")
        before = {field: getattr(locked, f"{field}_id", None) if field in {"catalog_book", "catalog_edition"} else getattr(locked, field) for field in identity_fields}
        catalog_book = values["catalog_book"]
        catalog_edition = values["catalog_edition"]
        if catalog_edition and (not catalog_book or catalog_edition.book_id != catalog_book.pk):
            raise ValidationError("The catalog edition must belong to the selected catalog book.")
        normalized_title = normalize_book_identity(values["title_snapshot"])
        normalized_author = normalize_book_identity(values["author_snapshot"])
        if not normalized_title or not normalized_author or values["page_count_snapshot"] <= 0:
            raise ValidationError("BOTM title, author, and positive page count are required.")
        BotmBook.objects.filter(pk=locked.pk).update(
            **values, normalized_title=normalized_title, normalized_author=normalized_author,
        )
        BotmMatch.objects.filter(botm_book=locked).update(
            botm_title_snapshot=values["title_snapshot"],
            botm_author_snapshot=values["author_snapshot"],
            botm_catalog_identity=(f"{catalog_book.provider}:{catalog_book.provider_book_id}" if catalog_book else ""),
            status=BotmMatch.Status.PENDING_REVIEW,
            is_qualifying=False,
            updated_at=timezone.now(),
        )
        from .botm_matching import synchronize_challenge
        synchronize_challenge(locked.month)
        return {"before": before, "used": used, "match_count": locked.matches.count()}
    return execute_recovery_operation(recovery_request, mutation)


def recover_tbr_match(*, match, decision, target_book, recovery_request):
    if decision not in {"pending", "rejected", "confirmed"}:
        raise ValidationError("Choose Pending, Rejected, or Confirmed.")

    def mutation():
        locked = PersonalTBRMatch.objects.select_for_update().select_related(
            "month__group", "participant", "submission", "personal_tbr_book__personal_tbr__enrollment",
        ).get(pk=match.pk)
        if target_book.personal_tbr_id != locked.personal_tbr_book.personal_tbr_id:
            raise ValidationError("Choose a book from the same locked Personal TBR.")
        before = {"status": locked.status, "is_qualifying": locked.is_qualifying, "personal_tbr_book_id": locked.personal_tbr_book_id}
        if target_book.pk != locked.personal_tbr_book_id:
            locked.status = PersonalTBRMatch.Status.REJECTED
            locked.is_qualifying = False
            locked.reviewer = recovery_request.actor
            locked.decided_at = timezone.now()
            locked.save(update_fields=["status", "is_qualifying", "reviewer", "decided_at", "updated_at"])
            _void_match_provenance(
                match=locked, source_type=ModifierProvenance.SourceType.TBR_BOOK,
                reference=f"personal_tbr_match:{locked.pk}", actor=recovery_request.actor,
                reason="Personal TBR recovery replaced the match target.",
            )
            replacement = PersonalTBRMatch.objects.select_for_update().filter(
                personal_tbr_book=target_book, submission=locked.submission,
            ).first()
            if replacement is None:
                replacement = PersonalTBRMatch.objects.create(
                    personal_tbr_book=target_book, month=locked.month,
                    participant=locked.participant, submission=locked.submission,
                    method=PersonalTBRMatch.Method.MANUAL_REVIEW,
                    status=PersonalTBRMatch.Status.PENDING_REVIEW, is_qualifying=False,
                    tbr_title_snapshot=target_book.title_snapshot,
                    tbr_author_snapshot=target_book.author_snapshot,
                    tbr_catalog_identity=(f"{target_book.catalog_book.provider}:{target_book.catalog_book.provider_book_id}" if target_book.catalog_book else ""),
                    submission_title_snapshot=locked.submission.title,
                    submission_author_snapshot=locked.submission.author,
                    submission_catalog_identity=(f"{locked.submission.catalog_book.provider}:{locked.submission.catalog_book.provider_book_id}" if locked.submission.catalog_book else ""),
                    normalized_title_evidence=normalize_book_identity(locked.submission.title),
                    normalized_author_evidence=normalize_book_identity(locked.submission.author),
                    evidence_summary=f"Platform recovery replaced Personal TBR match {locked.pk}.",
                    reviewer=recovery_request.actor,
                )
            locked = replacement
        if decision == "confirmed":
            if locked.submission.status != BookSubmission.Status.APPROVED or locked.submission.is_removed:
                raise ValidationError("A removed or non-approved submission cannot qualify for Personal TBR.")
            competitors = PersonalTBRMatch.objects.select_for_update().filter(
                status=PersonalTBRMatch.Status.CONFIRMED, is_qualifying=True,
            ).exclude(pk=locked.pk)
            if competitors.filter(submission=locked.submission).exists() or competitors.filter(
                personal_tbr_book=locked.personal_tbr_book,
            ).exists():
                raise ValidationError("Another qualifying TBR match already owns this submission or book.")
            locked.status = PersonalTBRMatch.Status.CONFIRMED
            locked.is_qualifying = True
            locked.decided_at = timezone.now()
        elif decision == "rejected":
            locked.status = PersonalTBRMatch.Status.REJECTED
            locked.is_qualifying = False
            locked.decided_at = timezone.now()
        else:
            locked.status = PersonalTBRMatch.Status.PENDING_REVIEW
            locked.is_qualifying = False
            locked.decided_at = None
        locked.method = PersonalTBRMatch.Method.MANUAL_REVIEW
        locked.reviewer = recovery_request.actor
        locked.save(update_fields=["method", "status", "is_qualifying", "reviewer", "decided_at", "updated_at"])
        malformed_removed = _repair_match_provenance(
            locked, source_type=ModifierProvenance.SourceType.TBR_BOOK,
            reference=f"personal_tbr_match:{locked.pk}",
            context=f"Qualifying Personal TBR match {locked.pk}",
        )
        _reconcile_tbr_reader(locked.month, locked.participant, rematch=False)
        locked.refresh_from_db()
        return {"before": before, "match_id": locked.pk, "status": locked.status, "is_qualifying": locked.is_qualifying, "personal_tbr_book_id": locked.personal_tbr_book_id, "malformed_provenance_removed": malformed_removed}
    return execute_recovery_operation(recovery_request, mutation)


def purge_tbr_match(*, match, recovery_request, fail_after_step=None):
    if recovery_request.tier != 3:
        raise ValidationError("Personal TBR match purge requires Tier 3 recovery.")

    def mutation():
        locked = PersonalTBRMatch.objects.select_for_update().select_related("month__group", "participant").get(pk=match.pk)
        month, participant = locked.month, locked.participant
        provenance = ModifierProvenance.objects.filter(
            source_type=ModifierProvenance.SourceType.TBR_BOOK,
            source_reference=f"personal_tbr_match:{locked.pk}",
        )
        locked.status = PersonalTBRMatch.Status.REJECTED
        locked.is_qualifying = False
        locked.save(update_fields=["status", "is_qualifying", "updated_at"])
        _reconcile_tbr_reader(month, participant)
        provenance.delete()
        if fail_after_step == "provenance":
            raise RuntimeError("Injected Personal TBR match purge failure.")
        PersonalTBRMatch.objects.filter(pk=locked.pk).delete()
        _reconcile_tbr_reader(month, participant)
        return {"exists": False}
    return execute_recovery_operation(recovery_request, mutation)


def _normalize_tbr_values(books):
    if len(books) > 9:
        raise ValidationError("A Personal TBR may contain at most nine books.")
    positions, identities, catalog_ids, normalized = set(), set(), set(), []
    for values in books:
        position = int(values["position"])
        if position < 1 or position > 9 or position in positions:
            raise ValidationError("Personal TBR positions must be unique values from 1 through 9.")
        positions.add(position)
        title = (values.get("title_snapshot") or "").strip()
        author = (values.get("author_snapshot") or "").strip()
        identity = (normalize_book_identity(title), normalize_book_identity(author))
        if not all(identity) or identity in identities:
            raise ValidationError("Each Personal TBR title/author identity must be present and unique.")
        identities.add(identity)
        catalog_book = values.get("catalog_book")
        catalog_edition = values.get("catalog_edition")
        if catalog_edition and (not catalog_book or catalog_edition.book_id != catalog_book.pk):
            raise ValidationError("Each catalog edition must belong to its selected catalog book.")
        if catalog_book:
            if catalog_book.pk in catalog_ids:
                raise ValidationError("The same catalog work cannot appear twice.")
            catalog_ids.add(catalog_book.pk)
        page_count = values.get("page_count_snapshot")
        if page_count is not None and page_count <= 0:
            raise ValidationError("Page counts must be positive when provided.")
        normalized.append({
            **values, "position": position, "title_snapshot": title,
            "author_snapshot": author, "normalized_title": identity[0],
            "normalized_author": identity[1],
        })
    return sorted(normalized, key=lambda item: item["position"])


def repair_tbr_entry(*, book, values, recovery_request, fail_after_step=None):
    if recovery_request.tier != 3:
        raise ValidationError("Locked Personal TBR entry repair requires Tier 3 recovery.")

    def mutation():
        locked = PersonalTBRBook.objects.select_for_update().select_related(
            "personal_tbr__enrollment__month__group", "personal_tbr__enrollment__participant",
        ).get(pk=book.pk)
        if not locked.personal_tbr.confirmed_at:
            raise ValidationError("This workflow is only for a confirmed locked Personal TBR.")
        candidate = _normalize_tbr_values([{**values, "position": locked.position}])[0]
        siblings = locked.personal_tbr.books.exclude(pk=locked.pk)
        if siblings.filter(normalized_title=candidate["normalized_title"], normalized_author=candidate["normalized_author"]).exists():
            raise ValidationError("The corrected identity duplicates another locked TBR book.")
        if candidate.get("catalog_book") and siblings.filter(catalog_book=candidate["catalog_book"]).exists():
            raise ValidationError("The corrected catalog work duplicates another locked TBR book.")
        before = {
            "position": locked.position, "title_snapshot": locked.title_snapshot,
            "author_snapshot": locked.author_snapshot, "catalog_book_id": locked.catalog_book_id,
            "catalog_edition_id": locked.catalog_edition_id, "page_count_snapshot": locked.page_count_snapshot,
        }
        update = {key: value for key, value in candidate.items() if key != "position"}
        PersonalTBRBook.objects.filter(pk=locked.pk).update(**update)
        PersonalTBRCompletionAwardBook.objects.filter(personal_tbr_book=locked).update(
            title_snapshot=candidate["title_snapshot"], author_snapshot=candidate["author_snapshot"],
        )
        PersonalTBRMatch.objects.filter(personal_tbr_book=locked).update(
            tbr_title_snapshot=candidate["title_snapshot"],
            tbr_author_snapshot=candidate["author_snapshot"],
            tbr_catalog_identity=(f"{candidate['catalog_book'].provider}:{candidate['catalog_book'].provider_book_id}" if candidate.get("catalog_book") else ""),
            status=PersonalTBRMatch.Status.PENDING_REVIEW, is_qualifying=False,
            updated_at=timezone.now(),
        )
        if fail_after_step == "entry":
            raise RuntimeError("Injected locked TBR entry repair failure.")
        _reconcile_tbr_reader(locked.personal_tbr.enrollment.month, locked.personal_tbr.enrollment.participant)
        return {"before": before, "position": locked.position, "title_snapshot": candidate["title_snapshot"], "author_snapshot": candidate["author_snapshot"]}
    return execute_recovery_operation(recovery_request, mutation)


def rebuild_locked_tbr(*, tbr, books, recovery_request, fail_after_step=None):
    if recovery_request.tier != 3:
        raise ValidationError("Whole locked Personal TBR rebuild requires Tier 3 recovery.")
    candidates = _normalize_tbr_values(books)

    def mutation():
        locked = PersonalTBR.objects.select_for_update().select_related(
            "enrollment__month__group", "enrollment__participant",
        ).get(pk=tbr.pk)
        if not locked.confirmed_at:
            raise ValidationError("This workflow is only for a confirmed locked Personal TBR.")
        month, participant = locked.enrollment.month, locked.enrollment.participant
        old_books = list(locked.books.select_for_update().order_by("position"))
        before = [{
            "position": item.position, "title_snapshot": item.title_snapshot,
            "author_snapshot": item.author_snapshot, "catalog_book_id": item.catalog_book_id,
            "catalog_edition_id": item.catalog_edition_id, "page_count_snapshot": item.page_count_snapshot,
        } for item in old_books]
        PersonalTBRMatch.objects.filter(personal_tbr_book__personal_tbr=locked).update(
            status=PersonalTBRMatch.Status.REJECTED, is_qualifying=False, updated_at=timezone.now(),
        )
        _reconcile_tbr_reader(month, participant)
        ModifierProvenance.objects.filter(
            participant=participant, month=month,
            source_type__in=(ModifierProvenance.SourceType.TBR_BOOK, ModifierProvenance.SourceType.TBR_COMPLETION),
        ).delete()
        PersonalTBRMatch.objects.filter(personal_tbr_book__personal_tbr=locked).delete()
        awards = PersonalTBRCompletionAward.objects.filter(personal_tbr=locked)
        PersonalTBRCompletionAwardBook.objects.filter(award__in=awards).delete()
        awards.delete()
        PersonalTBRBook.objects.filter(personal_tbr=locked).delete()
        if fail_after_step == "cleared":
            raise RuntimeError("Injected whole locked TBR rebuild failure.")
        PersonalTBRBook.objects.bulk_create([
            PersonalTBRBook(personal_tbr=locked, **candidate) for candidate in candidates
        ])
        if fail_after_step == "created":
            raise RuntimeError("Injected whole locked TBR rebuild failure after creation.")
        _reconcile_tbr_reader(month, participant)
        return {
            "previous_locked_list": before,
            "resulting_count": len(candidates),
            "resulting_positions": [candidate["position"] for candidate in candidates],
            "qualifying_match_count": PersonalTBRMatch.objects.filter(
                personal_tbr_book__personal_tbr=locked, is_qualifying=True,
            ).count(),
            "completion_qualifying": PersonalTBRCompletionAward.objects.filter(
                personal_tbr=locked, is_qualifying=True,
            ).exists(),
        }
    return execute_recovery_operation(recovery_request, mutation)


def set_game_active(*, game, active, recovery_request):
    def mutation():
        locked = Game.objects.select_for_update().select_related("month__group").get(pk=game.pk)
        before = locked.is_active
        locked.is_active = active
        locked.save(update_fields=["is_active", "updated_at"])
        return {"is_active": active, "previous_is_active": before, "application_count": locked.reward_applications.count()}
    return execute_recovery_operation(recovery_request, mutation)


def _canonical_game_provenance(recipient, application, actor):
    expected = f"game_reward_recipient:{recipient.pk}"
    duplicates = ModifierProvenance.objects.select_for_update().filter(
        source_type=ModifierProvenance.SourceType.GAME_REWARD,
        month=application.game.month, participant=recipient.participant,
        submission__isnull=True, applied_at=application.applied_at,
    ).exclude(source_reference=expected)
    for duplicate in duplicates:
        if not hasattr(duplicate, "game_reward_recipient"):
            duplicate.delete()
    linked = recipient.provenance
    canonical = ModifierProvenance.objects.select_for_update().filter(
        source_type=ModifierProvenance.SourceType.GAME_REWARD, source_reference=expected,
    ).first()
    coherent = canonical is not None and (
        canonical.month_id == application.game.month_id
        and canonical.participant_id == recipient.participant_id
        and canonical.submission_id is None
        and canonical.amount == application.amount
    )
    if linked is not None and linked.pk != getattr(canonical, "pk", None):
        GameRewardRecipient.objects.filter(pk=recipient.pk).update(provenance=None)
        if not hasattr(linked, "game_reward_recipient"):
            linked.delete()
        linked = None
    if canonical is not None and not coherent:
        GameRewardRecipient.objects.filter(provenance=canonical).update(provenance=None)
        canonical.delete()
        canonical = None
    if canonical is None:
        canonical = ModifierProvenance.objects.create(
            month=application.game.month, participant=recipient.participant,
            submission=None, source_type=ModifierProvenance.SourceType.GAME_REWARD,
            source_reference=expected, source_label=application.game_name_snapshot,
            source_context=f"{application.target_label}: {application.reason}",
            amount=application.amount,
            effective_date=timezone.localtime(application.applied_at, ZoneInfo(application.game.month.group.timezone)).date(),
            applied_by=application.applied_by, applied_at=application.applied_at,
            is_system_generated=True, is_active=not application.is_voided,
        )
    canonical.is_active = not application.is_voided
    canonical.voided_by = application.voided_by if application.is_voided else None
    canonical.voided_at = application.voided_at if application.is_voided else None
    canonical.void_reason = application.void_reason if application.is_voided else ""
    canonical.save(update_fields=["is_active", "voided_by", "voided_at", "void_reason"])
    GameRewardRecipient.objects.filter(pk=recipient.pk).update(provenance=canonical)
    return canonical


def recover_game_application(*, application, mode, recovery_request, fail_after_step=None):
    if mode not in {"void", "restore", "repair"}:
        raise ValidationError("Choose void, restore, or repair.")

    def mutation():
        locked = GameRewardApplication.objects.select_for_update().select_related("game__month__group").get(pk=application.pk)
        recipients = list(GameRewardRecipient.objects.select_for_update().select_related("participant", "provenance").filter(application=locked).order_by("pk"))
        if not recipients:
            raise ValidationError("The frozen Game reward has no recipients to recover.")
        if any(item.participant.group_id != locked.game.month.group_id or item.participant.user.is_superuser for item in recipients):
            raise ValidationError("A frozen recipient is not a coherent historical Reader in the Game Group.")
        before = {"is_voided": locked.is_voided, "amount": locked.amount, "recipient_ids": [item.participant_id for item in recipients]}
        if mode == "restore":
            if not locked.is_voided:
                raise ValidationError("Only a coherently voided Game reward can be restored.")
            locked.is_voided = False
            locked.voided_by = None
            locked.voided_at = None
            locked.void_reason = ""
        elif mode == "void":
            if locked.is_voided:
                raise ValidationError("This Game reward is already voided.")
            locked.is_voided = True
            locked.voided_by = recovery_request.actor
            locked.voided_at = timezone.now()
            locked.void_reason = recovery_request.reason
        locked.full_clean()
        locked.save(update_fields=["is_voided", "voided_by", "voided_at", "void_reason"])
        provenance_ids = []
        for recipient in recipients:
            recipient.application = locked
            provenance_ids.append(_canonical_game_provenance(recipient, locked, recovery_request.actor).pk)
            if fail_after_step == f"recipient:{recipient.pk}":
                raise RuntimeError("Injected Game recovery failure.")
        return {
            "before": before, "is_voided": locked.is_voided,
            "amount": locked.amount, "recipient_ids": [item.participant_id for item in recipients],
            "provenance_ids": provenance_ids,
        }
    return execute_recovery_operation(recovery_request, mutation)


def recreate_game_application(*, application, amount, recovery_request):
    def mutation():
        locked = GameRewardApplication.objects.select_for_update().select_related("game__month__group", "target_participant", "target_team").get(pk=application.pk)
        if not locked.is_voided:
            raise ValidationError("Void the incorrect Game reward before creating its replacement.")
        replacement, created = apply_game_reward(
            game=locked.game, actor=recovery_request.actor,
            target_type=locked.target_type, target_participant=locked.target_participant,
            target_team=locked.target_team, amount=amount,
            reason=f"Replacement for voided Game reward {locked.pk}: {recovery_request.reason}",
            idempotency_key=uuid4(),
        )
        if not created:
            raise ValidationError("A replacement Game reward was not created.")
        return {"voided_application_id": locked.pk, "replacement_application_id": replacement.pk, "replacement_amount": replacement.amount}
    return execute_recovery_operation(recovery_request, mutation)
