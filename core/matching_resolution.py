"""Conservative shared identity evaluation; awards stay in domain services."""
import hashlib
import json
import re
from importlib import import_module
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import models, transaction
from django.utils import timezone
from .matching_intent import entry_evidence
from .models import (BookSubmission, BotmBook, PersonalTBRBook, BotmMatch, PersonalTBRMatch,
                     MonthEnrollment, AuditEvent, normalize_book_identity)


def identity_stamp(submission, book):
    data = {'submission': [submission.pk, submission.month_id, submission.participant_id,
        submission.catalog_book_id, normalize_book_identity(submission.title), normalize_book_identity(submission.author)],
        'entry': [book.pk, book.catalog_book_id, book.normalized_title, book.normalized_author]}
    # Preserve existing decision fingerprints for complete, consistent work evidence.
    if any((record.catalog_edition_id and not record.catalog_book_id) or _work_identity(record)[1]
           or _abridgement(record) for record in (submission, book)):
        data['work_evidence'] = [(_work_identity(record)[0].pk if _work_identity(record)[0] else None,
                                  _work_identity(record)[1], _abridgement(record))
                                 for record in (submission, book)]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _work_identity(record):
    """An edition's stored parent is evidence; edition IDs are not work IDs."""
    work = record.catalog_book
    edition = record.catalog_edition
    if work and edition and work.pk != edition.book_id:
        return work, 'Selected edition and catalog work disagree.'
    return work or (edition.book if edition else None), ''


def same_trusted_work(left, right):
    first, first_error = _work_identity(left)
    second, second_error = _work_identity(right)
    return bool(first and second and not first_error and not second_error
                and first.provider == second.provider == 'hardcover'
                and first.provider_book_id and first.provider_book_id == second.provider_book_id)


def _abridgement(record):
    edition = record.catalog_edition
    text = normalize_book_identity(' '.join([
        getattr(record, 'title', getattr(record, 'title_snapshot', '')),
        edition.format_name if edition else '',
    ]))
    if re.search(r'\bunabridged\b', text):
        return 'unabridged'
    if re.search(r'\babridged\b', text):
        return 'abridged'
    return ''


def identity_evidence(submission, book):
    title = normalize_book_identity(submission.title)
    author = normalize_book_identity(submission.author)
    other = book.normalized_title
    left, left_error = _work_identity(submission)
    right, right_error = _work_identity(book)
    if left_error or right_error:
        return 'conflict', left_error or right_error
    def volumes(value):
        return set(re.findall(r'\b(?:volume|vol|book|band)\s+(\d+)\b', value))
    if volumes(title) and volumes(other) and volumes(title) != volumes(other):
        return 'conflict', 'Volume numbers disagree.'
    def adaptation(value):
        if re.search(r'\bmanga\b', value):
            return 'manga'
        if 'graphic novel' in value:
            return 'graphic novel'
        if re.search(r'\bnovel\b', value):
            return 'novel'
        return ''
    if adaptation(title) and adaptation(other) and adaptation(title) != adaptation(other):
        return 'conflict', 'Distinct literary adaptations disagree.'
    if left and right and (left.provider, left.provider_book_id) != (right.provider, right.provider_book_id):
        return 'conflict', 'Catalog work identities disagree; no verified cross-work relationship is stored.'
    abridgements = (_abridgement(submission), _abridgement(book))
    if 'abridged' in abridgements and abridgements[0] != abridgements[1]:
        return 'pending', 'Abridged versus complete-work equivalence requires review.'
    if left and right and left.provider == right.provider == 'hardcover' and left.provider_book_id:
        return 'work', 'Same trusted Hardcover work; edition, language and reading format do not restrict credit.'
    if title and author and title == other and author == book.normalized_author:
        return 'exact', 'Exact normalized title and author.'
    if title and other and author and author == book.normalized_author:
        short, long = sorted([title.split(), other.split()], key=len)
        if len(short) >= 3 and len(short) < len(long) and long[:len(short)] == short:
            return 'pending', 'Same author and exact title prefix; subtitle identity requires review.'
    selected = (submission.selected_botm_book_id if isinstance(book, BotmBook)
                else submission.selected_tbr_book_id)
    if selected == book.pk:
        return 'pending', 'Reader-selected target; edition or translated-work equivalence requires review.'
    if not title or not author or not other or not book.normalized_author:
        return 'pending', 'Incomplete identity evidence requires review.'
    return 'none', 'No supported identity relationship.'


def competing(match_model, submission, book, field, exclude=None):
    query = match_model.objects.filter(status='confirmed', is_qualifying=True).filter(
        models.Q(submission=submission) | models.Q(**{field: book, 'participant_id': submission.participant_id}))
    return query.exclude(pk=exclude).exists() if exclude else query.exists()


def _legacy_stamp_matches(match, submission):
    current_catalog = (f'{submission.catalog_book.provider}:{submission.catalog_book.provider_book_id}'
                       if submission.catalog_book_id else '')
    return (normalize_book_identity(match.submission_title_snapshot) == normalize_book_identity(submission.title)
            and normalize_book_identity(match.submission_author_snapshot) == normalize_book_identity(submission.author)
            and match.submission_catalog_identity == current_catalog)


def assess_entries(submission, books, domain):
    """Shared, read-only candidate evaluation for synchronization and review UI."""
    selected = submission.selected_botm_book_id if domain == 'botm' else submission.selected_tbr_book_id
    assessed = [(b, *identity_evidence(submission, b)) for b in books]
    if selected:
        assessed = [item for item in assessed if item[0].pk == selected]
    else:
        strong = [item for item in assessed if item[1] == 'work']
        exact = [item for item in assessed if item[1] == 'exact']
        if strong or exact:
            assessed = strong or exact
        else:
            # Never generate arbitrary candidates for every unrelated catalog work.
            def related_titles(book):
                title = normalize_book_identity(submission.title)
                other = book.normalized_title
                def base(value):
                    value = re.sub(r'\b(?:volume|vol|book)\s+\d+\b', '', value)
                    value = re.sub(r'\b(?:manga|light novel|graphic novel)\b', '', value)
                    return ' '.join(value.split())
                short, long = sorted([title.split(), other.split()], key=len)
                return (title == other or (base(title) == base(other) and len(base(title).split()) >= 3)
                        or (len(short) >= 3 and long[:len(short)] == short))
            assessed = [item for item in assessed if
                (item[1] == 'pending' and same_trusted_work(submission, item[0])) or
                (submission.author and item[0].normalized_author == normalize_book_identity(submission.author)
                 and (item[1] == 'pending' or (item[1] == 'conflict' and related_titles(item[0]))))]
    return assessed


@transaction.atomic
def synchronize_domain(submission, domain):
    if domain not in {'botm', 'tbr'}:
        raise ValidationError('Unknown matching domain.')
    module = import_module('.botm_matching' if domain == 'botm' else '.personal_tbr_matching', __package__)
    match_model, field = (BotmMatch, 'botm_book') if domain == 'botm' else (PersonalTBRMatch, 'personal_tbr_book')
    submission = BookSubmission.objects.select_for_update().select_related(
        'month', 'participant__user', 'catalog_book').get(pk=submission.pk)
    existing = list(match_model.objects.select_for_update().filter(submission=submission).select_related(field+'__catalog_book'))
    enrollment = MonthEnrollment.objects.filter(month=submission.month, participant=submission.participant, is_active=True).exists()
    participating = (submission.participant.is_active and submission.participant.user.is_active
        and not submission.participant.user.is_superuser and enrollment and not submission.is_removed)
    enabled = submission.month.botm_enabled if domain == 'botm' else submission.month.tbr_enabled
    if domain == 'botm':
        books = list(BotmBook.objects.filter(month=submission.month, is_retired=False).select_related('catalog_book'))
    else:
        books = list(PersonalTBRBook.objects.filter(personal_tbr__enrollment__month=submission.month,
            personal_tbr__enrollment__participant=submission.participant,
            personal_tbr__confirmed_at__isnull=False).select_related('catalog_book'))
    valid_ids = {b.pk for b in books}
    approved = participating and enabled and submission.status == 'approved'
    for match in existing:
        if match.is_qualifying:
            match.is_qualifying = False
            match.save(update_fields=['is_qualifying', 'updated_at'])
    decisions = [m for m in existing if m.method == 'manual_review']
    administrative = [m for m in decisions if m.decision_evidence.get('authority') == 'administrator']
    if administrative:
        decisions = administrative
    confirmed = [m for m in decisions if m.status == 'confirmed']
    if decisions:
        if len(confirmed) == 1:
            match = confirmed[0];book = getattr(match, field)
            unchanged = (match.decision_evidence.get('fingerprint') == identity_stamp(submission, book)
                         if match.decision_evidence else _legacy_stamp_matches(match, submission))
            blocked = not unchanged or book.pk not in valid_ids
            match.conflict_reason = 'Reviewed identity changed or entry unavailable; explicit review is required.' if blocked else ''
            match.is_qualifying = approved and not blocked and not competing(match_model, submission, book, field, match.pk)
            match.save(update_fields=['is_qualifying', 'conflict_reason', 'updated_at'])
        return module._finalize_submission_matches(submission, existing)
    if not participating or not enabled or submission.status not in {'pending', 'approved'}:
        return module._finalize_submission_matches(submission, existing)
    selected = submission.selected_botm_book_id if domain == 'botm' else submission.selected_tbr_book_id
    if selected and selected not in valid_ids:
        return module._finalize_submission_matches(submission, existing)
    assessed = assess_entries(submission, books, domain)
    results = []
    for book, evidence, reason in assessed:
        previous = next((m for m in existing if getattr(m, field+'_id') == book.pk), None)
        if previous and previous.status == 'rejected':
            results.append(previous);continue
        conflict = evidence == 'conflict' or evidence == 'none'
        duplicate = competing(match_model, submission, book, field, previous.pk if previous else None)
        if approved and evidence in {'work', 'exact'} and len(assessed) == 1 and not duplicate:
            method = 'catalog_work' if evidence == 'work' else 'normalized_title_author'
            match = module._confirm_candidate(book, submission, method=method, evidence_summary=reason)
        else:
            match = module._pending_candidate(book, submission, reason)
            if match.status != 'rejected':
                match.status = 'pending_review';match.is_qualifying = False;match.method = 'suggested'
                match.conflict_reason = (reason if conflict else 'Qualification is already held by another relationship.' if duplicate else '')
                match.evidence_summary = reason
                match.suggestion_fingerprint = identity_stamp(submission, book)
                match.save(update_fields=['status', 'is_qualifying', 'method', 'conflict_reason', 'evidence_summary', 'suggestion_fingerprint', 'updated_at'])
        if match is not None:
            results.append(match)
    return module._finalize_submission_matches(submission, results)


@transaction.atomic
def confirm_suggestion(*, match, actor, decision, expected_fingerprint=None):
    from django.contrib.auth import get_user_model
    from .permissions import can_review_submission
    actor = get_user_model().objects.get(pk=actor.pk)
    model = type(match)
    if model not in {BotmMatch, PersonalTBRMatch}:
        raise ValidationError('Use an existing BOTM or TBR suggestion.')
    field = 'botm_book' if model is BotmMatch else 'personal_tbr_book'
    original_fingerprint = expected_fingerprint if expected_fingerprint is not None else match.suggestion_fingerprint
    match = model.objects.select_for_update().select_related(
        field+'__catalog_book', 'submission__catalog_book', 'submission__month', 'submission__participant__user').get(pk=match.pk)
    s = match.submission;book = getattr(match, field)
    match.full_clean()
    if not actor.is_active or not can_review_submission(actor, s):
        raise ValidationError('Submission review scope is required for suggested-match confirmation.')
    if s.month.status not in {'active', 'finalizing'} or match.status != 'pending_review':
        raise ValidationError('Only pending suggestions in Active or Finalizing Challenges can be reviewed.')
    if decision not in {'confirmed', 'rejected'}:
        raise ValidationError('Choose Confirmed or Rejected.')
    stamp = identity_stamp(s, book)
    if original_fingerprint and original_fingerprint != stamp:
        raise ValidationError('The suggested identity changed. Reload before reviewing.')
    if match.suggestion_fingerprint and match.suggestion_fingerprint != stamp:
        raise ValidationError('The suggested identity changed. Reload before reviewing.')
    if not match.suggestion_fingerprint and not _legacy_stamp_matches(match, s):
        raise ValidationError('Historical candidate evidence changed. Reload and investigate.')
    if decision == 'confirmed':
        module = import_module('.botm_matching' if model is BotmMatch else '.personal_tbr_matching', __package__)
        evidence, reason = identity_evidence(s, book)
        selected = s.selected_botm_book_id if model is BotmMatch else s.selected_tbr_book_id
        plausible = evidence in {'work','exact','pending'} and (evidence != 'pending' or bool(selected == book.pk) or
            same_trusted_work(s, book) or
            (normalize_book_identity(s.author) == book.normalized_author and bool(s.author)))
        if not plausible or match.conflict_reason:
            raise ValidationError('Conflicting evidence requires an administrative override, not suggested-match confirmation.')
        if not module._submission_is_eligible(s) or (model is BotmMatch and (not s.month.botm_enabled or book.is_retired)):
            raise ValidationError('This candidate is no longer eligible.')
        if model is PersonalTBRMatch and (not s.month.tbr_enabled or not book.personal_tbr.confirmed_at):
            raise ValidationError('This candidate is no longer eligible.')
        if competing(model, s, book, field, match.pk):
            raise ValidationError('Qualification is already held by another relationship.')
    match.method='manual_review';match.status=decision;match.is_qualifying=decision=='confirmed'
    match.reviewer=actor;match.decided_at=timezone.now()
    match.decision_evidence={'fingerprint':stamp,'decision':decision,'submission_id':s.pk,'entry_id':book.pk,
                            'evidence':match.evidence_summary,
                            'submission_identity':{'title':s.title,'author':s.author,'catalog_book_id':s.catalog_book_id,
                                                   'catalog_edition_id':s.catalog_edition_id},
                            'entry_identity':entry_evidence(book),
                            'actor_id':actor.pk,'decided_at':match.decided_at.isoformat()}
    match.save(update_fields=['method','status','is_qualifying','reviewer','decided_at','decision_evidence','updated_at'])
    module = import_module('.botm_matching' if model is BotmMatch else '.personal_tbr_matching', __package__)
    module._finalize_submission_matches(s, list(model.objects.filter(submission=s)))
    AuditEvent.objects.create(actor=actor,group=s.month.group,
        action=('botm.' if model is BotmMatch else 'personal_tbr.')+('match_confirmed' if decision=='confirmed' else 'match_rejected'),
        object_type=model.__name__,object_id=str(match.pk),summary=json.dumps(match.decision_evidence,sort_keys=True))
    return match


@transaction.atomic
def review_submission_with_matches(submission, reviewed_claims, reviewer, reviewed_at, *, suggestions=None):
    """Normal approval plus explicit suggested IDs; no arbitrary target selection."""
    from .permissions import can_review_submission
    from .scoring import apply_submission_review
    from django.contrib.auth import get_user_model
    reviewer = get_user_model().objects.get(pk=reviewer.pk)
    current = BookSubmission.objects.select_for_update().select_related('month').get(pk=submission.pk)
    if not reviewer.is_active or not can_review_submission(reviewer,current) or current.month.status not in {'active','finalizing'}:
        raise PermissionDenied('Current submission review scope and lifecycle are required.')
    if suggestions is not None and not isinstance(suggestions, dict):
        raise ValidationError('Suggested decisions must be keyed by matching domain.')
    prepared=[]
    for domain, selection in (suggestions or {}).items():
        if domain not in {'botm','tbr'} or not isinstance(selection,dict) or set(selection)!={'match_id','fingerprint'}:
            raise ValidationError('Use an existing suggested match and its evidence fingerprint.')
        if type(selection['match_id']) is not int or not isinstance(selection['fingerprint'], str):
            raise ValidationError('Invalid suggestion identifier or fingerprint.')
        model = BotmMatch if domain=='botm' else PersonalTBRMatch
        match = model.objects.filter(pk=selection['match_id'],submission=current,status='pending_review').first()
        if not match or not selection['fingerprint'] or selection['fingerprint'] != match.suggestion_fingerprint:
            raise ValidationError('The suggestion is unavailable or stale.')
        prepared.append((match,selection['fingerprint']))
    result=apply_submission_review(submission,reviewed_claims,reviewer,reviewed_at)
    for match,stamp in prepared:
        match.refresh_from_db()
        if match.status=='confirmed' and match.method!='manual_review':
            # A strong match became automatically eligible during ordinary approval.
            if stamp != identity_stamp(result,getattr(match,'botm_book' if isinstance(match,BotmMatch) else 'personal_tbr_book')):
                raise ValidationError('Suggestion identity changed.')
            continue
        confirm_suggestion(match=match,actor=reviewer,decision='confirmed',expected_fingerprint=stamp)
    result.refresh_from_db();return result
