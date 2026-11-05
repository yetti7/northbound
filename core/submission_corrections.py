"""Atomic, explicitly authorized submission corrections. No editing interface."""
import hashlib
import json
from datetime import date, datetime

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.debug import sensitive_variables

from .integrations.hardcover import fetch_edition_metadata, resolve_scoring_edition
from .models import (
    AuditEvent, BookSubmission, CatalogEdition, ThemeClaim, MonthTheme,
    PersonalTBRMatch, BotmMatch, ModifierProvenance, PersonalTBRCompletionAward,
    BotmCompletionAward, PersonalTBRBook, BotmBook, ImmersiveRequest,
)
from .permissions import can_edit_submission
from .scoring import establish_theme_award, refresh_submission_score
from .personal_tbr_matching import synchronize_submission as synchronize_tbr
from .botm_matching import synchronize_submission as synchronize_botm

SALT = 'northbound.submission-correction.v1'
FIELDS = ('title', 'author', 'catalog_book_id', 'catalog_edition_id', 'scoring_catalog_edition_id',
          'submitted_pages', 'metadata_pages', 'approved_pages', 'completed_on', 'book_format',
          'status', 'verification_method', 'verification_url', 'bonus_pages', 'final_scored_pages',
          'reviewed_by_id', 'reviewed_at')
CLAIM_FIELDS = ('id', 'theme_id', 'status', 'approved_bonus_pages', 'award_finalized', 'award_rule',
                'award_version', 'award_percentage', 'award_basis_pages', 'award_bonus_pages')
ALLOWED = {'title', 'author', 'catalog_edition_id', 'page_count', 'completed_on', 'theme_ids', 'theme_responses', 'association_mode'}
PER_BOOK = (ModifierProvenance.SourceType.THEME_BONUS, ModifierProvenance.SourceType.TBR_BOOK,
            ModifierProvenance.SourceType.BOTM_BOOK)


def _json(value):
    return json.dumps(value, cls=DjangoJSONEncoder, sort_keys=True)


def _load(pk):
    try:
        return BookSubmission.objects.select_related('month__group', 'participant__user',
            'catalog_book', 'catalog_edition', 'scoring_catalog_edition').get(pk=pk)
    except BookSubmission.DoesNotExist:
        raise ValidationError('The submission is unavailable.') from None


def _authorize(actor, submission):
    if not can_edit_submission(actor, submission):
        raise PermissionDenied('You do not have correction authority for this submission.')


def _rows(queryset, fields=None):
    return list(queryset.order_by('pk').values(*(fields or ())))


def _reader_state(submission):
    scope = dict(month_id=submission.month_id, participant_id=submission.participant_id)
    state = {'submission': {name: getattr(submission, name) for name in FIELDS},
             'claims': _rows(submission.theme_claims.all(), CLAIM_FIELDS),
             'reader_submissions': _rows(BookSubmission.objects.filter(**scope), ('id',) + FIELDS)}
    for model, fields in (
        (PersonalTBRMatch, ('id', 'submission_id', 'personal_tbr_book_id', 'method', 'status', 'is_qualifying')),
        (BotmMatch, ('id', 'submission_id', 'botm_book_id', 'method', 'status', 'is_qualifying')),
        (ModifierProvenance, ('id', 'submission_id', 'source_type', 'source_reference', 'amount', 'effective_date', 'is_active')),
        (PersonalTBRCompletionAward, ('id', 'is_qualifying', 'bonus_amount_snapshot', 'effective_date', 'completion_set_fingerprint')),
        (BotmCompletionAward, ('id', 'is_qualifying', 'bonus_amount_snapshot', 'effective_date', 'completion_set_fingerprint')),
    ):
        state[model.__name__] = _rows(model.objects.filter(**scope), fields)
    state['ImmersiveRequest'] = _rows(ImmersiveRequest.objects.filter(submission=submission),
        ('id', 'status', 'captured_percent', 'awarded_pages', 'is_active'))
    return state


def _fingerprint(submission):
    state = _reader_state(submission)
    state['month'] = (submission.month.status, submission.month.starts_on, submission.month.ends_on,
                      submission.month.tbr_enabled, submission.month.botm_enabled)
    # Matching inputs only: a display refresh must not invalidate a correction.
    state['tbr_evidence'] = _rows(PersonalTBRBook.objects.filter(
        personal_tbr__enrollment__month_id=submission.month_id,
        personal_tbr__enrollment__participant_id=submission.participant_id),
        ('id', 'personal_tbr_id', 'catalog_book_id', 'catalog_edition_id', 'normalized_title',
         'normalized_author', 'title_snapshot', 'author_snapshot', 'page_count_snapshot'))
    state['botm_evidence'] = _rows(BotmBook.objects.filter(month_id=submission.month_id),
        ('id', 'catalog_book_id', 'catalog_edition_id', 'normalized_title', 'normalized_author',
         'title_snapshot', 'author_snapshot', 'page_count_snapshot', 'bonus_pages', 'is_retired'))
    state['immersive'] = _rows(ImmersiveRequest.objects.filter(submission=submission))
    state['correction_revision'] = AuditEvent.objects.filter(action='submission.corrected',
        object_type='BookSubmission', object_id=str(submission.pk)).order_by('-pk').values_list('pk', flat=True).first()
    state['themes'] = _rows(MonthTheme.objects.filter(month_id=submission.month_id))
    return hashlib.sha256(_json(state).encode()).hexdigest()


def correction_version(submission, actor):
    current = _load(submission.pk)
    _authorize(actor, current)
    return signing.dumps({'submission': current.pk, 'actor': actor.pk,
                          'fingerprint': _fingerprint(current)}, salt=SALT)


def _check_version(submission, actor, version):
    try:
        expected = signing.loads(version, salt=SALT, max_age=1800)
    except (signing.BadSignature, TypeError):
        raise ValidationError('Correction form expired. Reload before trying again.') from None
    actual = {'submission': submission.pk, 'actor': actor.pk, 'fingerprint': _fingerprint(submission)}
    if expected != actual:
        raise ValidationError('The submission or its scoring evidence changed. Reload before correcting.')


@sensitive_variables("token")
def _prepare_edition(submission, changes, token):
    if 'catalog_edition_id' not in changes or changes['catalog_edition_id'] == submission.catalog_edition_id:
        return None
    key = changes['catalog_edition_id']
    if key is None:
        return {'clear': True}
    if isinstance(key, bool) or not isinstance(key, int):
        raise ValidationError('Select a valid saved Hardcover edition.')
    try:
        edition = CatalogEdition.objects.select_related('book').get(pk=key, provider='hardcover', book__provider='hardcover')
    except CatalogEdition.DoesNotExist:
        raise ValidationError('Select a valid saved Hardcover edition.') from None
    if not token:
        raise ValidationError('A Hardcover credential is required to verify the corrected edition.')
    metadata = fetch_edition_metadata(token, edition_id=edition.provider_edition_id, book_id=edition.book.provider_book_id)
    scoring = edition
    method = 'hardcover'
    if changes.get('association_mode', 'update') != 'only':
        selected = {'edition_id': edition.provider_edition_id, 'book_id': edition.book.provider_book_id,
                    'format': edition.format_name, 'audio_seconds': edition.audio_seconds,
                    'pages': metadata.get('page_count')}
        resolved, method = resolve_scoring_edition(token, selected)
        if resolved:
            try:
                scoring = CatalogEdition.objects.select_related('book').get(
                    provider='hardcover', provider_edition_id=resolved['edition_id'])
            except CatalogEdition.DoesNotExist:
                raise ValidationError('The scoring edition is unavailable. Select it again.') from None
            if scoring.book_id != edition.book_id:
                raise ValidationError('The scoring edition must belong to the selected Hardcover work.')
            if scoring.pk != edition.pk:
                scoring_metadata = fetch_edition_metadata(token, edition_id=scoring.provider_edition_id,
                                                         book_id=edition.book.provider_book_id)
                metadata['page_count'] = scoring_metadata.get('page_count')
        else:
            scoring = None
            metadata['page_count'] = None
    return {'id': edition.pk, 'book_id': edition.book_id, 'provider_id': edition.provider_edition_id,
            'work_id': edition.book.provider_book_id, 'metadata': metadata,
            'scoring_id': scoring.pk if scoring else None, 'method': method}



def _apply_fields(submission, changes, edition_data):
    if changes.get('association_mode', 'update') not in {'update', 'only'}:
        raise ValidationError('Choose a valid association mode.')
    old_status = submission.status
    submission.status = 'approved' if old_status == 'approved' else 'pending'
    if edition_data is not None:
        if edition_data.get('clear'):
            submission.catalog_book = submission.catalog_edition = submission.scoring_catalog_edition = None
            submission.metadata_pages = None
            submission.verification_method = 'manual'
            submission.verification_url = ''
        else:
            try:
                selected = CatalogEdition.objects.select_related('book').get(pk=edition_data['id'])
            except CatalogEdition.DoesNotExist:
                raise ValidationError('The selected edition was removed. Reload before correcting.') from None
            if (selected.book_id, selected.provider_edition_id, selected.book.provider_book_id,
                selected.provider, selected.book.provider) != (
                    edition_data['book_id'], edition_data['provider_id'], edition_data['work_id'], 'hardcover', 'hardcover'):
                raise ValidationError('The selected Hardcover identity changed during lookup.')
            if changes.get('association_mode') == 'only':
                # No title, page, claim, provenance, approval, or match changes.
                allowed = {'catalog_edition_id', 'association_mode'}
                if any(key not in allowed for key in changes):
                    raise ValidationError('Associate Only cannot be combined with other corrections.')
                submission.catalog_book = selected.book
                submission.catalog_edition = selected
                if submission.scoring_catalog_edition_id and submission.scoring_catalog_edition.book_id != selected.book_id:
                    submission.scoring_catalog_edition = None
                    submission.metadata_pages = None
                    submission.verification_method = 'manual'
                    submission.verification_url = ''
                submission.status = old_status
                submission.full_clean()
                return
            metadata = edition_data['metadata']
            submission.catalog_book = selected.book
            submission.catalog_edition = selected
            for field in ('title', 'author'):
                value = metadata.get(field)
                limit = 200 if field == 'author' else 300
                value = value.strip() if isinstance(value, str) and 0 < len(value.strip()) <= limit else getattr(selected.book, field)
                setattr(submission, field, value)
            pages = metadata.get('page_count')
            valid_pages = isinstance(pages, int) and not isinstance(pages, bool) and 0 < pages <= 2147483647
            if not valid_pages and 'page_count' not in changes:
                raise ValidationError('This edition has no valid page count. Supply an explicit manual page override.')
            fmt = selected.format_name.casefold()
            submission.book_format = ('audio' if 'audio' in fmt or selected.audio_seconds else
                'paperback' if 'paperback' in fmt else 'hardcover' if 'hardcover' in fmt or 'hardback' in fmt else
                'ebook' if any(term in fmt for term in ('ebook', 'e-book', 'kindle', 'digital')) else 'other')
            submission.scoring_catalog_edition_id = edition_data['scoring_id'] if valid_pages else None
            submission.metadata_pages = pages if valid_pages else None
            submission.verification_url = (submission.scoring_catalog_edition.source_url
                                           if submission.scoring_catalog_edition_id else selected.source_url)
            submission.verification_method = edition_data['method'] if valid_pages else 'manual'
            if valid_pages:
                submission.submitted_pages = pages
                if submission.status == 'approved': submission.approved_pages = pages
    for name in ('title', 'author'):
        if name in changes:
            if not isinstance(changes[name], str): raise ValidationError(f'Enter a valid {name}.')
            setattr(submission, name, changes[name].strip())
    if 'completed_on' in changes:
        value = changes['completed_on']
        if isinstance(value, str):
            try: value = parse_date(value)
            except ValueError: value = None
        if not isinstance(value, date) or isinstance(value, datetime):
            raise ValidationError('Enter a valid completion date.')
        submission.completed_on = value
    if 'page_count' in changes:
        pages = changes['page_count']
        if not isinstance(pages, int) or isinstance(pages, bool) or not 0 < pages <= 2147483647:
            raise ValidationError('Enter a positive whole-number page count.')
        submission.submitted_pages = pages
        submission.approved_pages = pages if submission.status == 'approved' else None
        # Retain catalog associations and metadata_pages as provider evidence.
        submission.verification_method = 'manual'
    if submission.status != 'approved':
        submission.approved_pages = None
        submission.verification_method = 'manual'
    submission.full_clean()


def _theme_plan(submission, changes):
    claims = {claim.theme_id: claim for claim in submission.theme_claims.select_related('theme')}
    selected = changes.get('theme_ids', [key for key, claim in claims.items() if claim.status != 'rejected'])
    if not isinstance(selected, (list, tuple, set)) or any(type(key) is not int for key in selected) or len(set(selected)) != len(selected):
        raise ValidationError('Select valid, distinct Theme IDs.')
    themes = {theme.pk: theme for theme in MonthTheme.objects.select_for_update().filter(pk__in=selected, month=submission.month)}
    if len(themes) != len(selected): raise ValidationError('Themes must belong to this Challenge.')
    eligible = [theme for theme in themes.values() if theme.starts_on <= submission.completed_on <= theme.ends_on]
    if any(not theme.allow_stacking for theme in eligible) and len(eligible) > 1:
        raise ValidationError('A selected Theme cannot be stacked.')
    responses = changes.get('theme_responses', {})
    if not isinstance(responses, dict): raise ValidationError('Enter valid Theme responses.')
    for theme in themes.values():
        if theme.pk not in claims:
            if not theme.is_active or not theme.is_visible:
                raise ValidationError('New Theme selections must be active and visible.')
            response = responses.get(theme.pk, '')
            if not isinstance(response, str) or (theme.prompt and not response.strip()):
                raise ValidationError('Answer the Theme prompt before selecting it.')
            if not theme.starts_on <= submission.completed_on <= theme.ends_on:
                raise ValidationError('A new Theme selection must cover the corrected completion date.')
    return claims, themes, {theme.pk for theme in eligible}, responses


def _themes(submission, actor, plan):
    claims, themes, eligible_ids, responses = plan
    for theme in themes.values():
        if theme.pk not in claims:
            claims[theme.pk] = ThemeClaim.objects.create(submission=submission, theme=theme,
                response=responses.get(theme.pk, '').strip())
    for key, claim in claims.items():
        claim.submission = submission
        qualifies = key in eligible_ids
        claim.status = ('approved' if submission.status == 'approved' else 'pending') if qualifies else 'rejected'
        claim.reviewed_by = actor
        claim.reviewed_at = timezone.now()
        if claim.status == 'approved':
            claim.approved_bonus_pages = establish_theme_award(claim, correction=claim.award_finalized)
        else:
            claim.approved_bonus_pages = 0
        # Removed/out-of-window claims retain their original evidence. Do not
        # run cross-date validation against these intentionally inactive records.
        if qualifies: claim.full_clean()
        claim.save()


@sensitive_variables('hardcover_token')
def correct_submission(*, submission, actor, changes, version, hardcover_token=None, _preview=False, _expected_impact=None):
    if connection.in_atomic_block:
        raise ValidationError('Begin correction outside an existing write transaction.')
    if not isinstance(changes, dict) or set(changes) - ALLOWED:
        raise ValidationError('Unsupported correction fields.')
    if changes.get('association_mode') == 'only' and (
            set(changes) != {'catalog_edition_id', 'association_mode'} or type(changes['catalog_edition_id']) is not int):
        raise ValidationError('Associate Only requires a selected edition and no other corrections.')
    current = _load(submission.pk)
    _authorize(actor, current)
    _check_version(current, actor, version)
    if changes.get('association_mode') == 'only' and changes['catalog_edition_id'] == current.catalog_edition_id:
        raise ValidationError('Choose a different edition for Associate Only, or keep the current association.')
    edition_data = _prepare_edition(current, changes, hardcover_token)
    with transaction.atomic():
        try: actor = get_user_model().objects.get(pk=actor.pk)
        except get_user_model().DoesNotExist: raise PermissionDenied('Correction authority is unavailable.') from None
        try:
            BookSubmission.objects.select_for_update().get(pk=submission.pk)
        except BookSubmission.DoesNotExist:
            raise ValidationError('The submission was removed. Reload before correcting.') from None
        current = _load(submission.pk)
        _authorize(actor, current)
        _check_version(current, actor, version)
        before = _reader_state(current)
        _apply_fields(current, changes, edition_data)
        if changes.get('association_mode') == 'only':
            current.save()
        else:
            theme_plan = _theme_plan(current, changes)
            current.reviewed_by = actor
            current.reviewed_at = timezone.now()
            current.save()
            _themes(current, actor, theme_plan)
            refresh_submission_score(current)
            # Corrected row first releases obsolete credit; existing qualifying
            # alternatives keep priority while other approved occurrences are retried.
            synchronize_tbr(current)
            synchronize_botm(current)
            for other in BookSubmission.objects.filter(month=current.month, participant=current.participant,
                    status='approved', is_removed=False).exclude(pk=current.pk).order_by('pk'):
                synchronize_tbr(other)
                synchronize_botm(other)
            if before['submission']['completed_on'] != current.completed_on:
                ModifierProvenance.objects.filter(submission=current, source_type__in=PER_BOOK).update(effective_date=current.completed_on)
            refresh_submission_score(current)
        current.refresh_from_db()
        after = _reader_state(current)
        impact = _correction_impact(current)
        if _expected_impact is not None and impact != _expected_impact:
            raise ValidationError('The correction preview changed. Preview again before confirming.')
        if _preview:
            result = {'before': before, 'after': after, 'impact': impact,
                      'selected': str(current.catalog_edition) if current.catalog_edition_id else '',
                      'scoring': str(current.scoring_catalog_edition) if current.scoring_catalog_edition_id else ''}
            transaction.set_rollback(True)
            return result
        AuditEvent.objects.create(actor=actor, group=current.month.group, action='submission.corrected',
            object_type='BookSubmission', object_id=str(current.pk), summary=_json({
                'manual_page_override': 'page_count' in changes,
                'association_mode': changes.get('association_mode', 'update'),
                'before': before, 'after': after,
            }))
        return current


PREVIEW_SALT = 'northbound.correction-preview.v1'


def _correction_impact(submission):
    from .score_aggregation import challenge_score_totals
    # Stable values: do not compare generated IDs or review timestamps across rollback.
    data = {field: getattr(submission, field) for field in FIELDS
            if field not in {'reviewed_at', 'reviewed_by_id'}}
    data['reader_score'] = challenge_score_totals(month=submission.month,
                                                participant_ids=[submission.participant_id])[submission.participant_id]
    for model, fields in ((PersonalTBRMatch, ('submission_id', 'personal_tbr_book_id', 'method', 'status', 'is_qualifying')),
                          (BotmMatch, ('submission_id', 'botm_book_id', 'method', 'status', 'is_qualifying')),
                          (ModifierProvenance, ('submission_id', 'source_type', 'source_label', 'amount', 'effective_date', 'is_active'))):
        rows = list(model.objects.filter(month=submission.month, participant=submission.participant).values_list(*fields))
        data[model.__name__] = sorted(rows, key=_json)
    data['themes'] = list(submission.theme_claims.order_by('theme_id').values_list(
        'theme_id', 'status', 'approved_bonus_pages', 'award_percentage'))
    data['immersive'] = list(ImmersiveRequest.objects.filter(submission=submission).order_by('pk').values_list(
        'id', 'status', 'captured_percent', 'awarded_pages', 'is_active'))
    # JSON normalization makes date/tuple values stable after signing.
    return json.loads(_json(data))


def preview_correction(*, submission, actor, changes, version, hardcover_token=None):
    result = correct_submission(submission=submission, actor=actor, changes=changes,
                                version=version, hardcover_token=hardcover_token, _preview=True)
    current = _load(submission.pk)
    selected_id = changes.get('catalog_edition_id')
    selected = CatalogEdition.objects.select_related('book').get(pk=selected_id) if selected_id else None
    result['work_changed'] = bool(selected and current.catalog_book_id != selected.book_id)
    result['approved_confirmation'] = current.status == 'approved'
    payload = {'submission': current.pk, 'actor': actor.pk, 'version': version,
               'changes': _json(changes), 'impact': result['impact'],
               'work_changed': result['work_changed'], 'approved_confirmation': result['approved_confirmation']}
    result['token'] = signing.dumps(payload, salt=PREVIEW_SALT, compress=True)
    return result


def apply_previewed_correction(*, submission, actor, changes, version, preview_token,
                               confirm_approved=False, confirm_work=False, hardcover_token=None):
    try:
        expected = signing.loads(preview_token, salt=PREVIEW_SALT, max_age=600)
    except (signing.BadSignature, TypeError):
        raise ValidationError('Correction preview expired. Preview again.') from None
    if (expected.get('submission'), expected.get('actor'), expected.get('version'), expected.get('changes')) != (
            submission.pk, actor.pk, version, _json(changes)):
        raise ValidationError('Correction preview is stale or the inputs changed. Preview again.')
    if expected['approved_confirmation'] and not confirm_approved:
        raise ValidationError('Confirm the approved submission changes before applying.')
    if expected['work_changed'] and not confirm_work:
        raise ValidationError('Confirm the different Hardcover work before applying.')
    return correct_submission(submission=submission, actor=actor, changes=changes, version=version,
                              hardcover_token=hardcover_token, _expected_impact=expected['impact'])
