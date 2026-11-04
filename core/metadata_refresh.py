"""Individual display refresh; evidence and scoring services are deliberately absent."""
import hashlib
import json
from datetime import timedelta

from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.core.validators import URLValidator
from django.db import connection, transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .integrations.hardcover import fetch_edition_metadata
from .models import AuditEvent, BookSubmission, BotmBook, BotmMatch, PersonalTBRBook, PersonalTBRMatch, normalize_book_identity
from .permissions import can_refresh_botm_metadata, can_refresh_tbr_metadata

SALT = 'northbound.individual-metadata-refresh'
DISPLAY = ('display_title', 'display_author', 'display_cover_url', 'display_page_count', 'display_refreshed_at')


def load_selection(model, pk):
    related = ['catalog_book', 'catalog_edition']
    related += ['month__group'] if model is BotmBook else ['personal_tbr__enrollment__month__group', 'personal_tbr__enrollment__participant__user']
    try:
        return model.objects.select_related(*related).get(pk=pk)
    except model.DoesNotExist:
        raise ValidationError('This selection changed or was removed. Reload before refreshing.') from None


def selection_month(book):
    return book.month if isinstance(book, BotmBook) else book.personal_tbr.enrollment.month


def can_refresh(user, book):
    return can_refresh_botm_metadata(user, book) if isinstance(book, BotmBook) else can_refresh_tbr_metadata(user, book)


def _identity(book):
    if not book.catalog_book_id or not book.catalog_edition_id:
        raise ValidationError('Manual entries cannot be refreshed from Hardcover.')
    work, edition = book.catalog_book, book.catalog_edition
    if work.provider != 'hardcover' or edition.provider != 'hardcover' or edition.book_id != work.pk:
        raise ValidationError('The saved Hardcover edition/work identity is inconsistent.')
    return (work.provider, work.provider_book_id, edition.provider_edition_id)


def _fingerprint(book):
    names = ('position', 'title_snapshot', 'author_snapshot', 'page_count_snapshot',
             'cover_url_snapshot', 'source_url_snapshot', 'normalized_title', 'normalized_author',
             'catalog_book_id', 'catalog_edition_id') + DISPLAY
    values = {name: getattr(book, name) for name in names}
    values['identity'] = _identity(book)
    values['container'] = book.month_id if isinstance(book, BotmBook) else (
        book.personal_tbr_id, book.personal_tbr.enrollment_id,
        book.personal_tbr.enrollment.month_id, book.personal_tbr.enrollment.participant_id,
    )
    return hashlib.sha256(json.dumps(values, sort_keys=True, cls=DjangoJSONEncoder).encode()).hexdigest()


def refresh_version(book, actor):
    return signing.dumps({'model': book._meta.label_lower, 'pk': book.pk, 'actor': actor.pk,
                          'fingerprint': _fingerprint(book)}, salt=SALT)


def cover_only(book):
    """Use frozen identity evidence, including pending submissions without matches."""
    month = selection_month(book)
    is_botm = isinstance(book, BotmBook)
    matches = book.matches.filter(submission__is_removed=False)
    if matches.filter(status='confirmed', is_qualifying=True).exists() or matches.filter(status='pending_review').exists():
        return True
    pending = BookSubmission.objects.filter(month=month, status='pending', is_removed=False)
    if not is_botm:
        pending = pending.filter(participant_id=book.personal_tbr.enrollment.participant_id)
    for submission in pending:
        canonical = submission.catalog_book_id
        if canonical:
            relevant = canonical == book.catalog_book_id
            if not relevant and book.catalog_book_id is None:
                relevant = (normalize_book_identity(submission.title) == book.normalized_title
                            and normalize_book_identity(submission.author) == book.normalized_author)
        else:
            relevant = (normalize_book_identity(submission.title) == book.normalized_title
                        and normalize_book_identity(submission.author) == book.normalized_author)
        if relevant:
            return True
    return False


def _validated(metadata):
    result = {}
    for name in ('title', 'author'):
        value = metadata.get(name)
        if isinstance(value, str) and 0 < len(value.strip()) <= 300:
            result['display_' + name] = value.strip()
    cover = metadata.get('cover_url')
    if isinstance(cover, str) and 0 < len(cover.strip()) <= 1000:
        try:
            URLValidator(schemes=['http', 'https'])(cover.strip())
        except ValidationError:
            pass
        else:
            result['display_cover_url'] = cover.strip()
    pages = metadata.get('page_count')
    if isinstance(pages, int) and not isinstance(pages, bool) and 0 < pages <= 2147483647:
        result['display_page_count'] = pages
    return result


@sensitive_variables("token")
def refresh_selection(*, selection, actor, token, version):
    if connection.in_atomic_block:
        raise ValidationError('Metadata refresh must begin outside a write transaction.')
    model = type(selection)
    if model not in (PersonalTBRBook, BotmBook):
        raise ValidationError('Unsupported selection.')
    before = load_selection(model, selection.pk)
    if not can_refresh(actor, before):
        raise PermissionDenied('You may not refresh metadata for this selection.')
    identity = _identity(before)
    try:
        expected = signing.loads(version, salt=SALT, max_age=1800)
    except (signing.BadSignature, TypeError) as exc:
        raise ValidationError('Refresh form expired. Reload before trying again.') from exc
    state = {'model': before._meta.label_lower, 'pk': before.pk, 'actor': actor.pk, 'fingerprint': _fingerprint(before)}
    if expected != state:
        raise ValidationError('This selection changed. Reload before refreshing.')
    # The only provider call runs before acquiring SQLite's write transaction.
    metadata = _validated(fetch_edition_metadata(token, edition_id=identity[2], book_id=identity[1]))
    with transaction.atomic():
        from django.contrib.auth import get_user_model
        try:
            actor = get_user_model().objects.get(pk=actor.pk)
        except get_user_model().DoesNotExist:
            raise PermissionDenied('Refresh authority is no longer available.') from None
        current = load_selection(model, before.pk)
        # SQLite uses IMMEDIATE transactions; row locks also protect other backends.
        model.objects.select_for_update().get(pk=current.pk)
        current = load_selection(model, current.pk)
        if not can_refresh(actor, current):
            raise PermissionDenied('Refresh authority changed. The selection was preserved.')
        if _identity(current) != identity or _fingerprint(current) != state['fingerprint']:
            raise ValidationError('This selection changed during refresh. Reload before trying again.')
        artwork_only = cover_only(current)
        permitted = {name: value for name, value in metadata.items()
                     if not artwork_only or name == 'display_cover_url'}
        changes = {name: {'before': getattr(current, name), 'after': value}
                   for name, value in permitted.items() if getattr(current, name) != value}
        update = {name: item['after'] for name, item in changes.items()}
        now = timezone.now()
        update['display_refreshed_at'] = max(now, current.display_refreshed_at + timedelta(microseconds=1)) if current.display_refreshed_at else now
        # Explicit whitelist bypasses blanket evidence-save locks only for the
        # separate Phase 2 display columns. No evidence/model save is performed.
        model.objects.filter(pk=current.pk).update(**update)
        AuditEvent.objects.create(actor=actor, group=selection_month(current).group,
            action='book.display_metadata_refreshed', object_type=model.__name__, object_id=str(current.pk),
            summary=json.dumps({'edition_id': identity[2], 'book_id': identity[1],
                'mode': 'cover_only' if artwork_only else 'full_display', 'changes': changes}, cls=DjangoJSONEncoder))
        return {'cover_only': artwork_only, 'changed_fields': list(changes)}
