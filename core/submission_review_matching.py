"""Read-only matching summaries and atomic, stale-safe unified review."""
import hashlib
import json
from django import forms
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from .models import BookSubmission, BotmBook, PersonalTBRBook, BotmMatch, PersonalTBRMatch, MonthEnrollment, normalize_book_identity
from .matching_resolution import identity_evidence, identity_stamp, competing, review_submission_with_matches, _legacy_stamp_matches, assess_entries

SALT = 'northbound.unified-review.v1'


def matching_cards(submission):
    cards = []
    enrolled = MonthEnrollment.objects.filter(month=submission.month,participant=submission.participant,is_active=True).exists()
    if submission.participant.user.is_superuser or submission.is_removed or not submission.participant.is_active or not submission.participant.user.is_active or not enrolled:
        return cards
    for domain, model, field in [('botm',BotmMatch,'botm_book'),('tbr',PersonalTBRMatch,'personal_tbr_book')]:
        if not getattr(submission.month, 'botm_enabled' if domain == 'botm' else 'tbr_enabled'):
            continue
        books = (BotmBook.objects.filter(month=submission.month,is_retired=False) if domain == 'botm' else
                 PersonalTBRBook.objects.filter(personal_tbr__enrollment__month=submission.month,
                    personal_tbr__enrollment__participant=submission.participant,personal_tbr__confirmed_at__isnull=False))
        books = list(books.select_related('catalog_book','catalog_edition__book'))
        matches = list(model.objects.filter(submission=submission).select_related(field+'__catalog_book',field+'__catalog_edition'))
        decisions = [match for match in matches if match.method == 'manual_review']
        administrative = [m for m in decisions if m.decision_evidence.get('authority') == 'administrator']
        if administrative:
            decisions = administrative
        assessed = assess_entries(submission, books, domain)
        if decisions:
            assessed = [(book,*identity_evidence(submission,book)) for book in books
                        if any(getattr(m,field+'_id') == book.pk for m in decisions)]
        for book,strength,reason in assessed:
            match = next((m for m in matches if getattr(m,field+'_id') == book.pk),None)
            state = 'automatic' if strength in {'work','exact'} else 'suggested'
            duplicate = competing(model,submission,book,field,match.pk if match else None)
            if strength in {'none','conflict'} or len(assessed)>1 or duplicate:
                state = 'conflict'
            if match and match.method == 'manual_review':
                if match.status == 'rejected':state='conflict';reason='A previous review rejected this relationship.'
                elif ((match.decision_evidence and match.decision_evidence.get('fingerprint') != identity_stamp(submission,book))
                      or (not match.decision_evidence and not _legacy_stamp_matches(match,submission))):
                    state='conflict';reason='The book changed after the previous matching decision.'
                elif not duplicate and len(assessed)==1:state='automatic';reason='An existing reviewer decision is preserved.'
            message = ('Credit will be checked automatically after approval.' if state == 'automatic' else
                       'Confirm only if these represent the same eligible book.' if state == 'suggested' else
                       'Matching needs elevated review. You may approve the book without this credit.')
            if state == 'conflict':
                if match and match.status == 'rejected':
                    explanation = 'A previous reviewer rejected this relationship.'
                elif duplicate:
                    explanation = 'Another submission or relationship already holds this qualification.'
                elif len(assessed)>1:
                    explanation = 'More than one challenge entry could match this book.'
                elif strength == 'conflict':
                    explanation = reason if reason.startswith(('Volume','Distinct literary')) else 'Hardcover identifies these as different books, or the saved book information is inconsistent.'
                else:
                    explanation = 'The book changed after a previous matching decision, or its identity is not supported.'
            elif state == 'suggested':
                explanation = ('The title or edition differs, or there is not enough book information to verify the match automatically.')
            else:
                explanation = message
            cards.append({'domain':domain,'label':'BOTM' if domain=='botm' else 'Personal TBR',
                'entry':book,'state':state,'message':message,'strength':strength,
                'reason':explanation,'stamp':identity_stamp(submission,book)})
    return cards


def review_state(submission):
    data = {'submission':BookSubmission.objects.filter(pk=submission.pk).values().get(),
            'claims':list(submission.theme_claims.order_by('pk').values()),
            'botm':list(submission.botm_matches.order_by('pk').values()),
            'tbr':list(submission.personal_tbr_matches.order_by('pk').values()),
            'cards':[(c['domain'],c['entry'].pk,c['state'],c['stamp']) for c in matching_cards(submission)],
            'lifecycle':submission.month.status}
    return hashlib.sha256(json.dumps(data,sort_keys=True,default=str).encode()).hexdigest()


def review_token(submission, actor):
    return signing.dumps({'actor':actor.pk,'submission':submission.pk,'state':review_state(submission)},salt=SALT)


class MatchingReviewForm(forms.Form):
    matching_version = forms.CharField(widget=forms.HiddenInput())
    confirm_botm = forms.BooleanField(required=False,label='Confirm the suggested BOTM relationship')
    confirm_tbr = forms.BooleanField(required=False,label='Confirm the suggested Personal TBR relationship')

    def __init__(self,*args,submission,actor,cards,**kwargs):
        super().__init__(*args,**kwargs)
        self.initial['matching_version']=review_token(submission,actor)
        for domain in ('botm','tbr'):
            if not any(c['domain']==domain and c['state']=='suggested' for c in cards):
                self.fields.pop('confirm_'+domain)


@transaction.atomic
def apply_unified_review(submission, claims, actor, reviewed_at, *, version, confirmations):
    from .permissions import can_review_submission
    current=BookSubmission.objects.select_for_update().select_related('month','participant__user','catalog_book','catalog_edition').get(pk=submission.pk)
    if not can_review_submission(actor,current) or current.is_removed or current.month.status not in {'active','finalizing'}:
        raise PermissionDenied('This submission can no longer be reviewed.')
    try:
        expected=signing.loads(version,salt=SALT,max_age=1800)
    except signing.BadSignature:
        raise ValidationError('This review expired. Reload the page before reviewing again.') from None
    if expected != {'actor':actor.pk,'submission':current.pk,'state':review_state(current)}:
        raise ValidationError('This submission or its matching information changed. Reload before reviewing again.')
    if confirmations and submission.status != 'approved':
        raise ValidationError('Matching confirmation requires approving the book. Clear the confirmations for another decision.')
    cards=matching_cards(current);suggestions={}
    for domain in confirmations:
        candidates=[c for c in cards if c['domain']==domain and c['state']=='suggested']
        if len(candidates)!=1:
            raise ValidationError('This suggestion is no longer available. Reload, or approve without confirming matching credit.')
        from .botm_matching import synchronize_submission as botm_sync
        from .personal_tbr_matching import synchronize_submission as tbr_sync
        # Only the existing foundation generates candidates, scoped to this submission.
        (botm_sync if domain=='botm' else tbr_sync)(current)
        model,field=(BotmMatch,'botm_book') if domain=='botm' else (PersonalTBRMatch,'personal_tbr_book')
        match=model.objects.get(submission=current,**{field:candidates[0]['entry']})
        suggestions[domain]={'match_id':match.pk,'fingerprint':match.suggestion_fingerprint}
    return review_submission_with_matches(submission,claims,actor,reviewed_at,suggestions=suggestions)
