"""Reader-only presentation and read-only feedback for challenge book intent."""
from django.core import signing
from django.core.exceptions import ValidationError
from .models import BookSubmission, CatalogEdition, PersonalTBR, MonthEnrollment
from .matching_intent import selection_token, ReaderIntentForm
from .matching_resolution import identity_evidence, assess_entries
from .models import BotmMatch, PersonalTBRMatch, normalize_book_identity
from difflib import SequenceMatcher
import re
from .botm_presentation import build_botm_reader_presentation
from .personal_tbr_presentation import build_personal_tbr_reader_presentation


def picker_groups(*, month, participant, actor, data=None, allow_finalizing=False):
    from .permissions import is_active_floater
    if (actor.is_superuser or not actor.is_active or not participant or not participant.is_active
            or participant.user_id != actor.pk or participant.group_id != month.group_id
            or month.status not in ({'active','finalizing'} if allow_finalizing else {'active'}) or is_active_floater(month, participant.pk)
            or not MonthEnrollment.objects.filter(month=month, participant=participant, is_active=True, awaiting_scoring_team=False).exists()):
        return []
    botm = list(month.botm_books.filter(is_retired=False).select_related('catalog_book', 'catalog_edition')) if month.botm_enabled else []
    build_botm_reader_presentation(month=month, participant=participant, books=botm)
    tbr = PersonalTBR.objects.filter(enrollment__month=month, enrollment__participant=participant,
                                   confirmed_at__isnull=False).first() if month.tbr_enabled else None
    books = build_personal_tbr_reader_presentation(personal_tbr=tbr)['books'] if tbr else []
    groups = []
    for domain, title, entries in [('botm', 'Book of the Month', botm), ('tbr', 'My Personal TBR', books)]:
        if not entries:
            continue
        model, field = (BotmMatch, 'botm_book') if domain == 'botm' else (PersonalTBRMatch, 'personal_tbr_book')
        rejected = set(model.objects.filter(month=month, participant=participant, status='rejected').values_list(field + '_id', flat=True))
        for book in entries:
            book.selection_unavailable = book.reader_state in {'completed', 'pending'} or book.pk in rejected
            book.selection_rejected = book.pk in rejected
            book.selection_selected = bool(data and (_choice_id(data.get(domain + '_choice')) == book.pk
                or (domain + '_choice' not in data and str(data.get('selected_' + domain, '')) == str(book.pk))))
            book.selection_token = selection_token(book, actor)
            bonus = book.bonus_pages if domain == 'botm' else month.tbr_book_bonus_pages
            book.selection_summary = (f'Potential bonus: +{bonus} pages after approval.' if domain == 'botm'
                else 'Potential TBR credit: 1 book after approval.' + (f' Potential bonus: +{bonus} pages.' if bonus else ''))
        groups.append({'domain':domain, 'title':title, 'books':entries})
    return groups


def preview_selection(*, data, month, participant, actor, allow_finalizing=False):
    """No saves, catalog requests, matching records or awards."""
    submission = BookSubmission(month=month, participant=participant,
                                title=data.get('title', ''), author=data.get('author', ''))
    intent = ReaderIntentForm(data, submission=submission, actor=actor)
    if not intent.is_valid():
        raise ValidationError('Your Challenge book selection changed or expired. Select it again.')
    token = data.get('catalog_selection')
    if token:
        try:
            selected = signing.loads(token, salt='northbound.catalog-selection', max_age=86400)
            edition = CatalogEdition.objects.select_related('book').get(pk=selected['selected'])
        except (signing.BadSignature, CatalogEdition.DoesNotExist, KeyError, TypeError, ValueError):
            raise ValidationError('Choose the Hardcover edition again or use manual entry.') from None
        submission.catalog_book = edition.book
        submission.catalog_edition = edition
        submission.title = edition.book.title
        submission.author = edition.book.author
    feedback = []
    for group in picker_groups(month=month, participant=participant, actor=actor, allow_finalizing=allow_finalizing):
        domain = group['domain']
        selected = getattr(submission, 'selected_' + domain + '_book_id')
        assessed = assess_entries(submission, group['books'], domain)
        suggestions = []
        message = 'No matching Challenge book was found.'
        state = 'none'
        for book, strength, reason in assessed:
            if selected and book.pk == selected:
                state, message = 'pending', 'Qualification request awaiting review after submission; selection does not guarantee credit.'
                if strength == 'conflict':
                    state, message = 'conflict', 'Selected book identity does not match; ordinary qualification review cannot approve conflicting identity.'
                elif strength in {'work', 'exact'}:
                    state, message = 'detected', 'Qualification detected. Credit depends on approval and existing eligibility rules.'
                if book.selection_unavailable:
                    state, message = entry_state(book)
                break
            if strength in {'work', 'exact'}:
                state, message = ('detected', 'Qualification detected. Credit depends on approval and existing eligibility rules.')
                if book.selection_unavailable:
                    state, message = entry_state(book)
        if not selected and state == 'detected' and len(assessed) > 1:
            state, message = 'pending', 'Multiple matching entries were found; select one for review. Credit is not automatic.'
        if not selected and state == 'none':
            for book in group['books']:
                reason = suggestion_reason(submission, book)
                if reason and not book.selection_unavailable:
                    suggestions.append({'title': book.title_snapshot, 'author': book.author_snapshot,
                                        'token': book.selection_token, 'entry': book.pk, 'message': reason})
            # Ambiguity is explicit; large candidate sets are intentionally suppressed.
            if 0 < len(suggestions) <= 3:
                state, message = 'suggested', 'Possible match; identity requires review. You may continue without selecting.'
            else:
                suggestions = []
        feedback.append({'domain': domain, 'state': state, 'message': message, 'suggestions': suggestions})
    return feedback


def _choice_id(token):
    try:
        return signing.loads(token, salt='northbound.challenge-book-intent.v1', max_age=1800)['entry']['id']
    except (signing.BadSignature, TypeError, KeyError):
        return None


def entry_state(book):
    if book.reader_state == 'completed':
        return 'qualified', 'This entry already has qualifying credit.'
    if book.reader_state == 'pending':
        return 'pending', 'Qualification request awaiting review.'
    return 'rejected', 'A previous qualification request was rejected; no new suggestion is offered.'


def suggestion_reason(submission, book):
    """Presentation only: never used to award or synchronize qualification."""
    strength, reason = identity_evidence(submission, book)
    if strength == 'conflict':
        return ''
    title, author = normalize_book_identity(submission.title), normalize_book_identity(submission.author)
    other, other_author = book.normalized_title, book.normalized_author
    if not all((title, author, other, other_author)):
        return ''
    # Never suggest across a volume, adaptation or abridgement distinction,
    # including one-sided labels where equivalence is unknown.
    signature = lambda value: re.findall(r'\b(?:volume|vol|book|band)\s+\d+\b|\b(?:manga|graphic novel|light novel|abridged|unabridged)\b', value)
    if signature(title) != signature(other):
        return ''
    if title == other and author != other_author and len(title.split()) >= 2:
        if min(len(author), len(other_author)) >= 5 and SequenceMatcher(None, author, other_author, autojunk=False).ratio() >= .8:
            return 'The title matches a book on your Challenge list, but the author information differs.'
    if author == other_author:
        if strength == 'pending':
            return 'Similar title found; subtitle or edition identity requires review.'
        raw = getattr(book, 'title_snapshot', '')
        bases = [normalize_book_identity(value.split(':', 1)[0]) for value in (submission.title, raw)]
        if bases[0] == bases[1] and len(bases[0].split()) >= 2 and title != other:
            return 'Similar title found; subtitle identity requires review.'
    return ''


def saved_feedback(submission):
    """Durable outcomes only, scoped to the newly saved Reader submission."""
    result = []
    for enabled, label, manager in [(submission.month.botm_enabled, 'BOTM', submission.botm_matches),
                                   (submission.month.tbr_enabled, 'Personal TBR', submission.personal_tbr_matches)]:
        if not enabled:
            continue
        matches = list(manager.all())
        if any(match.is_qualifying for match in matches):
            text = 'Automatically qualified.'
        elif any(match.status == 'rejected' for match in matches):
            text = 'Qualification rejected.'
        elif any(match.status == 'pending_review' for match in matches):
            text = 'Qualification request awaiting review.'
        else:
            text = 'No qualifying relationship was recorded. Eligibility and identity rules still apply.'
        result.append(label + ': ' + text)
    return result
