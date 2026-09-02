from datetime import timedelta
import logging
import hashlib
import fcntl
import tempfile
from functools import wraps
from pathlib import Path

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .integrations.hardcover import HardcoverConnectionError, search_books
from .integrations.hardcover_library import (
    create_read_occurrence, create_read_user_book, read_library_state,
    read_occurrence, update_read_occurrence, update_user_book_to_read,
)
from .models import BookSubmission, HardcoverSyncOutbox, HardcoverSyncProvenance, ReaderHardcoverConnection, ReaderHardcoverSyncPreference, normalize_book_identity, safe_audit_summary
from .reader_hardcover import ReaderHardcoverUnavailable, get_reader_hardcover_credential
from .integrations.credentials import CredentialOwner

WRITE_SCOPE = "write:library"
CLAIM_LEASE = timedelta(minutes=5)
MAX_FAILED_ATTEMPTS = 6
logger = logging.getLogger(__name__)


def single_sync_worker(function):
    """One in-flight batch across processes in the supported single container."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        with (Path(tempfile.gettempdir()) / "northbound-hardcover-worker.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return []
            try:
                return function(*args, **kwargs)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    return wrapped


def _verify_write_consent(outbox, *, dates=False):
    preference = ReaderHardcoverSyncPreference.objects.filter(user=outbox.user).first()
    connection = ReaderHardcoverConnection.objects.filter(user=outbox.user).first()
    capable, _ = reader_sync_capability(connection)
    if connection and getattr(outbox, "credential_fingerprint", "") and hashlib.sha256(connection.encrypted_token.encode()).hexdigest() != outbox.credential_fingerprint:
        raise ReaderHardcoverUnavailable("The personal connection changed during synchronization. Try again.")
    if not capable or not preference or not preference.sync_completed_books or (dates and not preference.sync_completion_dates):
        raise ReaderHardcoverUnavailable("Synchronization consent or the personal connection changed. Review My Account before retrying.")
    return preference


def reader_sync_capability(connection):
    if connection is None:
        return False, "Connect your personal Hardcover account before enabling synchronization."
    if not connection.is_valid or connection.reconnect_required:
        return False, "Reconnect your personal Hardcover account before enabling synchronization."
    if connection.connection_method == ReaderHardcoverConnection.ConnectionMethod.OAUTH and WRITE_SCOPE not in connection.granted_scopes:
        return False, "Reconnect with Hardcover and approve library-write permission before enabling synchronization."
    return True, ""


def submission_event_key(submission, action):
    return f"book_submission:{submission.pk}:{action}:v1"


def enqueue_submission_sync(submission, action):
    if not submission.pk:
        raise ValueError("The originating submission must be saved before it can be queued.")
    if action not in HardcoverSyncOutbox.Action.values:
        raise ValueError("Unsupported Hardcover synchronization action.")
    with transaction.atomic():
        return HardcoverSyncOutbox.objects.get_or_create(
            event_key=submission_event_key(submission, action),
            defaults={"user": submission.participant.user, "source_type": "BookSubmission", "source_id": str(submission.pk), "action": action, "effective_date": submission.completed_on},
        )


def enqueue_eligible_approved_submission(submission_id):
    """Post-commit producer for one new approval; never scans historical submissions."""
    submission = BookSubmission.objects.select_related("participant__user", "catalog_book").filter(pk=submission_id, status=BookSubmission.Status.APPROVED, is_removed=False).first()
    if submission is None:
        return None
    user = submission.participant.user
    preference = ReaderHardcoverSyncPreference.objects.filter(user=user).first()
    connection = ReaderHardcoverConnection.objects.filter(user=user).first()
    capable, _ = reader_sync_capability(connection)
    if not preference or not preference.sync_completed_books or not capable:
        return None
    trusted = submission.catalog_book and submission.catalog_book.provider == "hardcover" and submission.catalog_book.provider_book_id
    if not trusted and not (normalize_book_identity(submission.title) and normalize_book_identity(submission.author)):
        return None
    return enqueue_submission_sync(submission, HardcoverSyncOutbox.Action.COMPLETED_BOOK)[0]


def enqueue_eligible_approved_submission_safely(submission_id):
    """Keep an already-committed Northbound approval successful if enqueueing fails."""
    try:
        return enqueue_eligible_approved_submission(submission_id)
    except Exception:
        logger.error("Could not enqueue the committed Hardcover sync event for BookSubmission %s.", submission_id)
        return None


def record_sync_attempt(*, outbox, outcome, provider_identifier="", provider_book_id="", provider_read_id="", result_detail="", library_result_detail="", occurrence_result_detail="", error_classification="", error_message="", next_attempt_at=None):
    safe_error = safe_audit_summary(error_message)[:300]
    with transaction.atomic():
        locked = HardcoverSyncOutbox.objects.select_for_update().get(pk=outbox.pk)
        safe_provider_identifier = safe_audit_summary(str(provider_identifier))[:255] if provider_identifier not in (None, "") else locked.provider_user_book_id
        provider_book_value = str(provider_book_id)[:100] if provider_book_id not in (None, "") else locked.provider_book_id
        provider_read_value = str(provider_read_id)[:100] if provider_read_id not in (None, "") else locked.provider_read_id
        library_detail_value = library_result_detail[:40] or locked.library_result_detail
        occurrence_detail_value = occurrence_result_detail[:40] or locked.occurrence_result_detail
        attempt_number = locked.attempt_count + 1
        provenance = HardcoverSyncProvenance.objects.create(
            outbox=locked, user=locked.user, attempt_number=attempt_number,
            source_type=locked.source_type, source_id=locked.source_id, action=locked.action,
            effective_date=locked.effective_date, outcome=outcome,
            provider_identifier=safe_provider_identifier, provider_book_id=provider_book_value,
            provider_read_id=provider_read_value, library_result_detail=library_detail_value,
            occurrence_result_detail=occurrence_detail_value, result_detail=result_detail[:40],
            error_classification=error_classification[:80], error_message=safe_error,
        )
        locked.attempt_count = attempt_number
        locked.last_attempt_at = provenance.created_at
        locked.next_attempt_at = next_attempt_at
        locked.provider_book_id = provider_book_value
        locked.provider_user_book_id = safe_provider_identifier[:100]
        locked.provider_read_id = provider_read_value
        locked.library_result_detail = library_detail_value
        locked.occurrence_result_detail = occurrence_detail_value
        locked.result_detail = result_detail[:40]
        locked.error_classification = error_classification[:80]
        locked.error_message = safe_error
        locked.status = {
            HardcoverSyncProvenance.Outcome.SUCCEEDED: HardcoverSyncOutbox.Status.SUCCEEDED,
            HardcoverSyncProvenance.Outcome.RETRYABLE_FAILURE: HardcoverSyncOutbox.Status.RETRYABLE,
            HardcoverSyncProvenance.Outcome.BLOCKED: HardcoverSyncOutbox.Status.BLOCKED,
            HardcoverSyncProvenance.Outcome.SKIPPED: HardcoverSyncOutbox.Status.SKIPPED,
            HardcoverSyncProvenance.Outcome.FAILED_PERMANENT: HardcoverSyncOutbox.Status.FAILED_PERMANENT,
        }[outcome]
        locked.save(update_fields=["attempt_count", "last_attempt_at", "next_attempt_at", "provider_book_id", "provider_user_book_id", "provider_read_id", "library_result_detail", "occurrence_result_detail", "result_detail", "error_classification", "error_message", "status", "updated_at"])
    return provenance


def _claim_one(now, *, exclude_ids=()):
    eligible = Q(status=HardcoverSyncOutbox.Status.PENDING) | Q(status=HardcoverSyncOutbox.Status.RETRYABLE, next_attempt_at__lte=now) | Q(status=HardcoverSyncOutbox.Status.PROCESSING, next_attempt_at__lte=now)
    with transaction.atomic():
        item = HardcoverSyncOutbox.objects.select_for_update().filter(eligible, action=HardcoverSyncOutbox.Action.COMPLETED_BOOK).exclude(pk__in=exclude_ids).order_by("created_at", "pk").first()
        if item is None:
            return None
        item.status = HardcoverSyncOutbox.Status.PROCESSING
        item.next_attempt_at = now + CLAIM_LEASE
        item.save(update_fields=["status", "next_attempt_at", "updated_at"])
        return item.pk


def _trusted_match(submission):
    book = submission.catalog_book
    if not book or book.provider != "hardcover":
        return None
    try:
        return str(int(book.provider_book_id))
    except (TypeError, ValueError):
        return None


def _fallback_match(token, submission):
    title, author = normalize_book_identity(submission.title), normalize_book_identity(submission.author)
    if not title or not author:
        return None, "insufficient_metadata"
    results, _ = search_books(token, f"{submission.title} {submission.author}", per_page=10)
    matches = {str(item.get("book_id")) for item in results if normalize_book_identity(item.get("title")) == title and normalize_book_identity(item.get("author")) == author and str(item.get("book_id", "")).isdigit()}
    if not matches:
        return None, "no_match"
    if len(matches) != 1:
        return None, "ambiguous_match"
    return matches.pop(), ""


def _edition_id(submission, book_id):
    edition = submission.catalog_edition
    if not edition or edition.provider != "hardcover" or str(edition.book.provider_book_id) != str(book_id):
        return None
    try:
        return int(edition.provider_edition_id)
    except (TypeError, ValueError):
        return None


def _retry_time(outbox):
    return timezone.now() + timedelta(seconds=min(60 * (2 ** min(outbox.attempt_count, 6)), 3600))


def reader_sync_health(user):
    events = HardcoverSyncOutbox.objects.filter(user=user)
    last_success = HardcoverSyncProvenance.objects.filter(user=user, outcome=HardcoverSyncProvenance.Outcome.SUCCEEDED).order_by("-created_at", "-pk").first()
    last_failure = HardcoverSyncProvenance.objects.filter(user=user).exclude(outcome=HardcoverSyncProvenance.Outcome.SUCCEEDED).order_by("-created_at", "-pk").first()
    return {
        "pending": events.filter(status=HardcoverSyncOutbox.Status.PENDING).count(),
        "retry_scheduled": events.filter(status=HardcoverSyncOutbox.Status.RETRYABLE).count(),
        "reconnect_required": events.filter(status=HardcoverSyncOutbox.Status.BLOCKED, error_classification__in=["reconnect_required", "credential_rejected", "insufficient_permission"]).count(),
        "reconciliation_required": events.filter(status=HardcoverSyncOutbox.Status.BLOCKED, error_classification="reconciliation_required").count(),
        "recovery_required": events.filter(status=HardcoverSyncOutbox.Status.BLOCKED, error_classification__in=["retry_exhausted", "restore_reconciliation_required"]).count(),
        "last_success": last_success,
        "last_failure": last_failure,
    }


def make_existing_reader_events_due(user):
    """Wake only this Reader's existing safe work; never creates or discovers events."""
    preference = ReaderHardcoverSyncPreference.objects.filter(user=user).first()
    connection = ReaderHardcoverConnection.objects.filter(user=user).first()
    capable, reason = reader_sync_capability(connection)
    if not preference or not preference.sync_completed_books:
        return 0, "Enable completed-book synchronization before processing existing queued work."
    if not capable:
        return 0, reason
    now = timezone.now()
    with transaction.atomic():
        eligible = HardcoverSyncOutbox.objects.select_for_update().filter(user=user).filter(
            Q(status__in=[HardcoverSyncOutbox.Status.PENDING, HardcoverSyncOutbox.Status.RETRYABLE])
            | Q(status=HardcoverSyncOutbox.Status.BLOCKED, error_classification__in=["reconnect_required", "credential_rejected", "insufficient_permission"])
        )
        count = eligible.count()
        eligible.update(status=HardcoverSyncOutbox.Status.PENDING, next_attempt_at=now, error_classification="", error_message="", updated_at=now)
    return count, ""


def _mark_occurrence_create_started(outbox, *, provider_book_id, provider_user_book_id, library_result_detail):
    """Durably prevent a blind second create after a crash or ambiguous response."""
    with transaction.atomic():
        locked = HardcoverSyncOutbox.objects.select_for_update().get(pk=outbox.pk)
        locked.provider_book_id = str(provider_book_id)[:100]
        locked.provider_user_book_id = str(provider_user_book_id)[:100]
        locked.library_result_detail = library_result_detail[:40]
        locked.occurrence_result_detail = "create_started"
        locked.save(update_fields=["provider_book_id", "provider_user_book_id", "library_result_detail", "occurrence_result_detail", "updated_at"])


def _occurrence_outcome(token, outbox, submission, *, book_id, user_book_id, library_detail):
    if outbox.provider_read_id:
        occurrence = read_occurrence(token, outbox.provider_read_id)
        if not occurrence or str(occurrence.get("user_book_id")) != str(user_book_id):
            raise HardcoverConnectionError("The mapped Hardcover read occurrence could not be verified.", classification="reconciliation_required", reconnect_required=True)
        if str(occurrence.get("finished_at") or "") == submission.completed_on.isoformat():
            return outbox.provider_read_id, "already_satisfied"
        _verify_write_consent(outbox, dates=True)
        return update_read_occurrence(token, read_id=outbox.provider_read_id, finished_at=submission.completed_on), "updated"
    if outbox.occurrence_result_detail in {"create_started", "reconciliation_required"}:
        raise HardcoverConnectionError("A previous Hardcover read-occurrence create has an unknown result.", classification="reconciliation_required", reconnect_required=True)
    _verify_write_consent(outbox, dates=True)
    _mark_occurrence_create_started(outbox, provider_book_id=book_id, provider_user_book_id=user_book_id, library_result_detail=library_detail)
    try:
        read_id = create_read_occurrence(token, user_book_id=user_book_id, finished_at=submission.completed_on, edition_id=_edition_id(submission, book_id))
    except HardcoverConnectionError as exc:
        if exc.retryable:
            raise HardcoverConnectionError(
                "The Hardcover read-occurrence create result is unknown and requires reconciliation.",
                classification="reconciliation_required", reconnect_required=True,
            ) from exc
        raise
    return read_id, "created"


def _process_one(outbox_id):
    outbox = HardcoverSyncOutbox.objects.select_related("user").get(pk=outbox_id)
    preference = ReaderHardcoverSyncPreference.objects.filter(user=outbox.user).first()
    if not preference or not preference.sync_completed_books:
        return record_sync_attempt(outbox=outbox, outcome=HardcoverSyncProvenance.Outcome.SKIPPED, result_detail="consent_withdrawn", error_classification="consent_withdrawn")
    connection = ReaderHardcoverConnection.objects.filter(user=outbox.user).first()
    capable, reason = reader_sync_capability(connection)
    if not capable:
        return record_sync_attempt(outbox=outbox, outcome=HardcoverSyncProvenance.Outcome.BLOCKED, result_detail="reconnect_required", error_classification="reconnect_required", error_message=reason)
    submission = BookSubmission.objects.select_related("catalog_book", "catalog_edition__book").filter(pk=outbox.source_id, participant__user=outbox.user, status=BookSubmission.Status.APPROVED, is_removed=False).first()
    if submission is None:
        return record_sync_attempt(outbox=outbox, outcome=HardcoverSyncProvenance.Outcome.SKIPPED, result_detail="source_unavailable", error_classification="source_unavailable")
    book_id = outbox.provider_book_id
    user_book_id = outbox.provider_user_book_id
    read_id = outbox.provider_read_id
    library_detail = outbox.library_result_detail
    occurrence_detail = outbox.occurrence_result_detail
    try:
        credential = get_reader_hardcover_credential(outbox.user)
        outbox.credential_fingerprint = getattr(credential, "connection_fingerprint", "")
        if credential.owner != CredentialOwner.READER:
            raise ReaderHardcoverUnavailable("A Reader-owned Hardcover credential is required.")
        token = credential.bearer_token
        book_id = book_id or _trusted_match(submission)
        if book_id is None:
            book_id, reason = _fallback_match(token, submission)
            if book_id is None:
                return record_sync_attempt(outbox=outbox, outcome=HardcoverSyncProvenance.Outcome.SKIPPED, result_detail=reason, error_classification=reason)
        state = read_library_state(token, book_id)
        preference = _verify_write_consent(outbox)
        existing = state.user_book
        if existing and ((existing.get("user_book_status") or {}).get("slug") == "read" or existing.get("status_id") == state.read_status_id):
            user_book_id = str(existing.get("id", ""))
            library_detail = "already_satisfied"
        elif existing:
            user_book_id = update_user_book_to_read(token, user_book_id=existing["id"], read_status_id=state.read_status_id)
            library_detail = "updated"
        else:
            user_book_id = create_read_user_book(token, book_id=book_id, read_status_id=state.read_status_id, edition_id=_edition_id(submission, book_id))
            library_detail = "created"
        if not outbox.provider_read_id and occurrence_detail in {"create_started", "reconciliation_required"}:
            raise HardcoverConnectionError("A previous Hardcover read-occurrence create has an unknown result.", classification="reconciliation_required", reconnect_required=True)
        preference = _verify_write_consent(outbox)
        if not preference.sync_completion_dates:
            occurrence_detail = "consent_off"
        else:
            read_id, occurrence_detail = _occurrence_outcome(
                token, outbox, submission, book_id=book_id,
                user_book_id=user_book_id, library_detail=library_detail,
            )
        detail = occurrence_detail if preference.sync_completion_dates else library_detail
        return record_sync_attempt(
            outbox=outbox, outcome=HardcoverSyncProvenance.Outcome.SUCCEEDED,
            provider_book_id=book_id, provider_identifier=user_book_id, provider_read_id=read_id,
            library_result_detail=library_detail, occurrence_result_detail=occurrence_detail,
            result_detail=detail,
        )
    except ReaderHardcoverUnavailable as exc:
        return record_sync_attempt(outbox=outbox, outcome=HardcoverSyncProvenance.Outcome.BLOCKED, provider_book_id=book_id, provider_identifier=user_book_id, provider_read_id=read_id, result_detail="reconnect_required", library_result_detail=library_detail, occurrence_result_detail=occurrence_detail, error_classification="reconnect_required", error_message=str(exc))
    except HardcoverConnectionError as exc:
        outbox.refresh_from_db(fields=["provider_book_id", "provider_user_book_id", "provider_read_id", "library_result_detail", "occurrence_result_detail"])
        book_id = outbox.provider_book_id or book_id
        user_book_id = outbox.provider_user_book_id or user_book_id
        read_id = outbox.provider_read_id or read_id
        library_detail = outbox.library_result_detail or library_detail
        occurrence_detail = outbox.occurrence_result_detail or occurrence_detail
        if exc.reconnect_required:
            outcome, detail, next_attempt = HardcoverSyncProvenance.Outcome.BLOCKED, "reconnect_required", None
        elif exc.retryable:
            if outbox.attempt_count + 1 >= MAX_FAILED_ATTEMPTS:
                outcome, detail, next_attempt = HardcoverSyncProvenance.Outcome.BLOCKED, "retry_exhausted", None
                exc.classification = "retry_exhausted"
            else:
                outcome, detail, next_attempt = HardcoverSyncProvenance.Outcome.RETRYABLE_FAILURE, "failed_retryable", _retry_time(outbox)
        else:
            outcome, detail, next_attempt = HardcoverSyncProvenance.Outcome.FAILED_PERMANENT, "failed_permanent", None
        if exc.classification == "reconciliation_required":
            detail = "reconciliation_required"
            occurrence_detail = detail
        return record_sync_attempt(outbox=outbox, outcome=outcome, provider_book_id=book_id, provider_identifier=user_book_id, provider_read_id=read_id, result_detail=detail, library_result_detail=library_detail, occurrence_result_detail=occurrence_detail, error_classification=exc.classification, error_message=str(exc), next_attempt_at=next_attempt)


@single_sync_worker
def process_hardcover_sync_batch(*, batch_size=20, now=None):
    now = now or timezone.now()
    processed = []
    for _ in range(max(1, min(int(batch_size), 100))):
        outbox_id = _claim_one(now, exclude_ids=processed)
        if outbox_id is None:
            break
        _process_one(outbox_id)
        processed.append(outbox_id)
    return processed
