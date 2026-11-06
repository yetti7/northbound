"""Immutable Reader intent, independent of book identity and awarded credit."""
from django.core import signing
from django import forms
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction, connection
from .models import BotmBook, PersonalTBRBook, MonthEnrollment, ChallengeMonth, Membership

SALT = 'northbound.challenge-book-intent.v1'


def entry_evidence(book):
    return {key: getattr(book, key) for key in (
        'id', 'catalog_book_id', 'catalog_edition_id', 'title_snapshot', 'author_snapshot',
        'normalized_title', 'normalized_author')} | {'kind': book._meta.model_name}


def selection_token(book, actor):
    """Future picker may issue this only after checking the scoped entry queryset."""
    return signing.dumps({'actor': actor.pk, 'entry': entry_evidence(book)}, salt=SALT)


def eligible_entry(submission, domain, pk):
    if domain == 'botm':
        if not submission.month.botm_enabled:
            raise ValidationError('BOTM is disabled.')
        query = BotmBook.objects.filter(pk=pk, month_id=submission.month_id, is_retired=False)
        book = (query.select_for_update() if connection.in_atomic_block else query).first()
    else:
        if not submission.month.tbr_enabled:
            raise ValidationError('Personal TBR is disabled.')
        query = PersonalTBRBook.objects.filter(pk=pk,
            personal_tbr__enrollment__month_id=submission.month_id,
            personal_tbr__enrollment__participant_id=submission.participant_id,
            personal_tbr__confirmed_at__isnull=False)
        book = (query.select_for_update() if connection.in_atomic_block else query).first()
    if book is None:
        raise ValidationError('The selected Challenge book is unavailable or outside your scope.')
    return book


@transaction.atomic
def capture_intent(submission, *, actor, botm_id=None, tbr_id=None, tokens=None):
    """Prepare an unsaved submission; this does not save, approve or award it."""
    if not actor.is_authenticated or not actor.is_active or submission.participant.user_id != actor.pk:
        raise PermissionDenied('Only the submitting Reader may record selection intent.')
    if not submission._state.adding or submission.month.status != 'active':
        raise ValidationError('Selection intent is recorded only for a new Active-Challenge submission.')
    evidence = {}
    for domain, pk in [('botm', botm_id), ('tbr', tbr_id)]:
        if pk is None:
            continue
        book = eligible_entry(submission, domain, pk)
        try:
            signed = signing.loads((tokens or {}).get(domain), salt=SALT, max_age=1800)
        except (signing.BadSignature, TypeError):
            raise ValidationError('Selection expired. Reload the Challenge books.') from None
        if signed != {'actor': actor.pk, 'entry': entry_evidence(book)}:
            raise ValidationError('Selection changed. Reload the Challenge books.')
        evidence[domain] = entry_evidence(book)
    submission.selected_botm_book_id = botm_id
    submission.selected_tbr_book_id = tbr_id
    submission.reader_intent = evidence
    validate_intent(submission, initial=True)
    return submission


def validate_intent(submission, *, initial):
    month = ChallengeMonth.objects.get(pk=submission.month_id)
    member = Membership.objects.select_related('user').get(pk=submission.participant_id)
    if initial and month.status != 'active':
        raise ValidationError('New selections require an Active Challenge.')
    if initial and (member.group_id != month.group_id or not member.is_active or not member.user.is_active or member.user.is_superuser or not MonthEnrollment.objects.filter(
            participant_id=submission.participant_id, month_id=submission.month_id, is_active=True).exists()):
        raise ValidationError('An active Reader enrollment is required for selection.')
    if initial and ((submission.selected_botm_book_id and not month.botm_enabled)
                    or (submission.selected_tbr_book_id and not month.tbr_enabled)):
        raise ValidationError('The selected Challenge feature is disabled.')
    expected = {}
    for domain, pk in [('botm', submission.selected_botm_book_id), ('tbr', submission.selected_tbr_book_id)]:
        if pk is not None:
            if initial:
                expected[domain] = entry_evidence(eligible_entry(submission, domain, pk))
            else:
                saved = submission.reader_intent.get(domain, {})
                if saved.get('id') != pk:
                    raise ValidationError('Selection evidence does not identify its entry.')
    if initial and submission.reader_intent != expected:
        raise ValidationError('Selection evidence is missing or stale.')


class ReaderIntentForm(forms.Form):
    """Scoped intent validation shared by submission and read-only feedback."""
    selected_botm = forms.ModelChoiceField(queryset=BotmBook.objects.none(), required=False)
    selected_tbr = forms.ModelChoiceField(queryset=PersonalTBRBook.objects.none(), required=False)
    botm_token = forms.CharField(required=False)
    tbr_token = forms.CharField(required=False)

    def __init__(self, *args, submission, actor, **kwargs):
        self.submission, self.actor = submission, actor
        super().__init__(*args, **kwargs)
        self.fields['selected_botm'].queryset = BotmBook.objects.filter(month_id=submission.month_id, is_retired=False)
        self.fields['selected_tbr'].queryset = PersonalTBRBook.objects.filter(
            personal_tbr__enrollment__month_id=submission.month_id,
            personal_tbr__enrollment__participant_id=submission.participant_id,
            personal_tbr__confirmed_at__isnull=False)

    def clean(self):
        data = super().clean()
        if self.errors:
            return data
        if not data.get("selected_botm") and not data.get("selected_tbr"):
            return data
        capture_intent(self.submission, actor=self.actor,
            botm_id=data['selected_botm'].pk if data.get('selected_botm') else None,
            tbr_id=data['selected_tbr'].pk if data.get('selected_tbr') else None,
            tokens={'botm':data.get('botm_token'),'tbr':data.get('tbr_token')})
        return data
