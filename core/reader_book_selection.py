"""Reader-only presentation and read-only feedback for challenge book intent."""
from django.core import signing
from django.core.exceptions import ValidationError
from .models import BookSubmission, CatalogEdition, PersonalTBR
from .matching_intent import selection_token, ReaderIntentForm
from .matching_resolution import identity_evidence
from .botm_presentation import build_botm_reader_presentation
from .personal_tbr_presentation import build_personal_tbr_reader_presentation


def picker_groups(*, month, participant, actor):
    if actor.is_superuser:
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
        for book in entries:
            book.selection_token = selection_token(book, actor)
            bonus = book.bonus_pages if domain == 'botm' else month.tbr_book_bonus_pages
            book.selection_summary = (f'Potential bonus: +{bonus} pages after approval.' if domain == 'botm'
                else 'Potential TBR credit: 1 book after approval.' + (f' Potential bonus: +{bonus} pages.' if bonus else ''))
        groups.append({'domain':domain, 'title':title, 'books':entries})
    return groups


def preview_selection(*, data, month, participant, actor):
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
    for domain in ['botm', 'tbr']:
        book = getattr(submission, 'selected_'+domain+'_book')
        if book:
            strength, _ = identity_evidence(submission, book)
            if strength in {'conflict', 'none'}:
                message = 'This book appears different from your selection. You can change the selection or submit it for review; challenge credit is not guaranteed.'
            elif strength == 'pending':
                message = 'Your selection is saved. A reviewer will need to check whether this book qualifies.'
            else:
                message = 'This book appears to match your selection. Credit depends on approval and challenge rules.'
            feedback.append({'domain':domain, 'message':message})
    return feedback
