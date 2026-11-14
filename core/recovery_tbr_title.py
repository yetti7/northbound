import hashlib

from django.core import serializers, signing
from django.core.exceptions import ValidationError

from .models import PersonalTBR, PersonalTBRBook, normalize_book_identity
from .recovery import RecoveryRequest, execute_recovery_operation, require_platform_owner

SALT = "personal-tbr-title-recovery"


def confirmation_label(book):
    return f"Correct TBR title #{book.pk}"


def _snapshot(book):
    confirmed = book.personal_tbr.confirmed_at
    return {
        "entry": book.pk,
        "title_snapshot": book.title_snapshot,
        "display_title": book.display_title,
        "normalized_title": book.normalized_title,
        "confirmed_at": confirmed.isoformat() if confirmed else None,
        "fingerprint": hashlib.sha256(serializers.serialize("json", [book]).encode()).hexdigest(),
    }


def _title_values(book, title):
    title = title.strip()
    normalized = normalize_book_identity(title)
    if not normalized or len(title) > 300 or len(normalized) > 300 or title == book.title_snapshot:
        raise ValidationError("Enter a different, valid book title.")
    if not book.personal_tbr.confirmed_at:
        raise ValidationError("This action requires a confirmed Personal TBR.")
    if book.matches.exists() or book.completion_award_snapshots.exists() or book.reader_selected_submissions.exists():
        raise ValidationError("This entry has match, award, or Reader selection history and cannot be corrected here.")
    if book.personal_tbr.books.exclude(pk=book.pk).filter(
        normalized_title=normalized, normalized_author=book.normalized_author,
    ).exists():
        raise ValidationError("This title and author are already on the Personal TBR.")
    return {"title_snapshot": title, "normalized_title": normalized, "display_title": title}


def preview_tbr_title(*, book, actor, title):
    require_platform_owner(actor)
    values = _title_values(book, title)
    return signing.dumps({"actor": actor.pk, "before": _snapshot(book), "title": values["title_snapshot"]}, salt=SALT)


def apply_tbr_title(*, book, actor, token, reason, confirmation, current_password):
    require_platform_owner(actor)
    try:
        preview = signing.loads(token, salt=SALT, max_age=900)
    except signing.BadSignature:
        raise ValidationError("The title preview is invalid or expired. Generate a fresh preview.")
    if preview["actor"] != actor.pk or preview["before"]["entry"] != book.pk:
        raise ValidationError("The title preview does not belong to this entry or operator.")
    label = confirmation_label(book)
    month = book.personal_tbr.enrollment.month
    request = RecoveryRequest(
        actor=actor, tier=3, action="personal_tbr.correct_title", target_type="PersonalTBRBook",
        target_id=str(book.pk), target_label=label, required_confirmation=label,
        supplied_confirmation=confirmation, confirmation_method="exact target label plus current password",
        current_password=current_password, reason=reason, group=month.group, challenge=month,
        before_state={field: preview["before"][field] for field in ("title_snapshot", "normalized_title", "display_title")},
    )

    def mutation():
        tbr = PersonalTBR.objects.select_for_update().get(pk=book.personal_tbr_id)
        locked = PersonalTBRBook.objects.select_for_update().filter(pk=book.pk, personal_tbr=tbr).first()
        if locked is None or _snapshot(locked) != preview["before"]:
            raise ValidationError("This entry changed after the preview. Generate a fresh preview.")
        values = _title_values(locked, preview["title"])
        # The ordinary model save intentionally rejects edits to confirmed lists.
        PersonalTBRBook.objects.filter(pk=locked.pk).update(**values)
        return values

    return execute_recovery_operation(request, mutation)
