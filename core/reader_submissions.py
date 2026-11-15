"""Reader-owned pending edits and audited requests through existing match records."""
import json
from importlib import import_module
from django import forms
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from .models import (BookSubmission, MonthEnrollment, ChallengeMonth, BotmMatch, PersonalTBRMatch,
                     AuditEvent, ThemeClaim, PersonalTBRCompletionAward, BotmCompletionAward, ModifierProvenance)
from .forms import BookSubmissionForm
from .discord_events import observe_book
from .matching_intent import eligible_entry, entry_evidence, SALT as INTENT_SALT
from .matching_resolution import identity_evidence, identity_stamp, competing
from .reader_book_selection import suggestion_reason
from .permissions import can_view_challenge, is_active_floater

SALT = 'northbound.reader-submission.v1'
DOMAINS = {'botm': (BotmMatch, 'botm_book'), 'tbr': (PersonalTBRMatch, 'personal_tbr_book')}


def owns(actor, s):
    return bool(actor.is_authenticated and actor.is_active and s.participant.user_id == actor.pk
                and s.participant.is_active and s.participant.group_id == s.month.group_id
                and can_view_challenge(actor, s.month))


def eligible(actor, s, *, editing=False):
    from .historical_restoration import limits_automatic_credit
    return bool(owns(actor, s) and not actor.is_superuser and not s.is_removed
        and s.status in ({'pending'} if editing else {'pending', 'approved'})
        and s.month.status in {'active', 'finalizing'} and s.month.group.is_active
        and not s.late_approval_evidence and not limits_automatic_credit(s)
        and not is_active_floater(s.month, s.participant_id)
        and MonthEnrollment.objects.filter(month=s.month, participant=s.participant,
            is_active=True, awaiting_scoring_team=False).exists()
        and (not editing or (s.approved_pages is None and s.final_scored_pages is None and not s.bonus_pages and not (
            ModifierProvenance.objects.filter(submission=s).exists()
            or s.theme_claims.filter(reviewed_by__isnull=False).exists()
            or s.theme_claims.filter(award_finalized=True).exists()
            or s.botm_matches.filter(is_qualifying=True).exists()
            or s.personal_tbr_matches.filter(is_qualifying=True).exists()
            or s.immersive_requests.filter(status='approved', is_active=True).exists()
            or s.prompt_claims.filter(is_current=True,state='qualified').exists()))))


def load(pk, *, lock=False):
    query = BookSubmission.objects
    if lock:
        month_id = query.values_list('month_id', flat=True).get(pk=pk)
        ChallengeMonth.objects.select_for_update().get(pk=month_id)
        query = query.select_for_update()
    return query.select_related('month__group', 'participant__user', 'catalog_book',
                                'catalog_edition', 'scoring_catalog_edition').get(pk=pk)


def _state(s):
    from .submission_review_matching import review_state
    revision=AuditEvent.objects.filter(object_type='BookSubmission',object_id=str(s.pk),group=s.month.group).order_by('-pk').values_list('pk',flat=True).first()
    return [review_state(s),revision]


def version(s, actor):
    return signing.dumps({'actor':actor.pk, 'submission':s.pk, 'state':_state(s)}, salt=SALT)


def check_version(s, actor, token):
    try:
        expected = signing.loads(token, salt=SALT, max_age=1800)
    except (signing.BadSignature, TypeError):
        raise ValidationError('This form expired. Reload before trying again.') from None
    if expected != {'actor':actor.pk, 'submission':s.pk, 'state':_state(s)}:
        raise ValidationError('The submission, review, qualifications or participation changed. Reload before saving.')


class PendingReaderForm(BookSubmissionForm):
    version = forms.CharField(widget=forms.HiddenInput())
    clear_catalog = forms.BooleanField(required=False,label='Remove Hardcover association and use manual details')

    class Meta(BookSubmissionForm.Meta):
        fields=('title','author','book_format','completed_on','submitted_pages','reference_url')
        # The catalog UI expects this element; only verified catalog selection
        # may update it. References, notes and start dates are outside Reader edits.
        widgets={**BookSubmissionForm.Meta.widgets,'reference_url':forms.HiddenInput()}

    def __init__(self, *args, **kwargs):
        s = kwargs['instance']
        self.original_catalog={key:getattr(s,key) for key in ('catalog_book_id','catalog_edition_id','scoring_catalog_edition_id','metadata_pages','verification_url')}
        super().__init__(*args, **kwargs)
        self.fields['reference_url'].disabled=True
        for name in list(self.fields):
            if name in {'bingo_prompt','scavenger_prompt'} or name.startswith('prompt_decision_'):
                self.fields.pop(name)
        self.initial['themes'] = list(s.theme_claims.exclude(status='rejected').values_list('theme_id',flat=True))
        # Retain already-selected pending Themes even if later hidden/retired.
        # New selections still come exclusively from the active visible queryset.
        retained=s.month.themes.filter(pk__in=self.initial['themes'])
        self.fields['themes'].queryset=(self.fields['themes'].queryset | retained).distinct()
        for theme in retained.exclude(prompt=''):
            name='theme_response_'+str(theme.pk)
            if name not in self.fields:
                self.fields[name]=forms.CharField(label=theme.prompt,required=False,
                    widget=forms.Textarea(attrs={'rows':2,'data-theme-response':theme.pk}))

        for claim in s.theme_claims.all():
            self.initial['theme_response_'+str(claim.theme_id)] = claim.response

    def clean(self):
        if self.cleaned_data.get('clear_catalog'):
            self.cleaned_data['catalog_selection']=''
        cleaned = super().clean()
        if not cleaned.get('catalog_selection') and not cleaned.get('clear_catalog'):
            for key,value in self.original_catalog.items():
                setattr(self.instance,key,value)
        # Reuse verified catalog inputs, but creation-time auto-approval is never
        # valid for a Reader edit. The retained provider metadata is evidence only.
        self.instance.status = 'pending'
        self.instance.approved_pages = None
        self.instance.verification_method = 'manual'
        return cleaned

    def save(self, *args, **kwargs):
        raise TypeError('Use save_pending; Reader forms do not save directly.')


def request_data(match):
    data = match.decision_evidence.get('reader_request', {}) if match else {}
    return data if isinstance(data, dict) else {}


def requested_entry_id(s, domain):
    """An audited pending request supersedes original intent without rewriting it."""
    if not s.pk:
        return None
    model, field = DOMAINS[domain]
    requests = [m for m in model.objects.filter(submission_id=s.pk, status='pending_review') if request_data(m)]
    if len(requests) != 1:
        return None
    match = requests[0]
    book = getattr(match,field)
    return book.pk if request_data(match).get('fingerprint') == identity_stamp(s,book) else None


def is_requested(s, book):
    domain = 'botm' if book._meta.model_name == 'botmbook' else 'tbr'
    return requested_entry_id(s,domain) == book.pk


def available_books(s, domain):
    from .models import BotmBook, PersonalTBRBook
    if not getattr(s.month, 'botm_enabled' if domain == 'botm' else 'tbr_enabled'):
        return []
    query = (BotmBook.objects.filter(month=s.month,is_retired=False) if domain=='botm' else
        PersonalTBRBook.objects.filter(personal_tbr__enrollment__month=s.month,
            personal_tbr__enrollment__participant=s.participant,personal_tbr__confirmed_at__isnull=False))
    return list(query.select_related('catalog_book','catalog_edition__book').order_by('position','pk'))


def candidate_reason(s, book):
    strength, reason = identity_evidence(s,book)
    if strength in {'work','exact'}:
        return strength, 'Strong identity evidence; qualification still requires review.'
    possible = suggestion_reason(s,book)
    if strength != 'conflict' and (possible or strength == 'pending'):
        # Do not offer arbitrary incomplete identities or original-selection-only
        # ambiguity unless it already has an explicit scoped pending relationship.
        model, field = DOMAINS['botm' if book._meta.model_name=='botmbook' else 'tbr']
        existing = model.objects.filter(submission=s, **{field:book},status='pending_review').exists()
        if possible or existing:
            return 'suggested', possible or reason
    return '', ''


def qualifications(s, actor):
    from .matching_intent import selection_token
    groups = []
    for domain, (model, field) in DOMAINS.items():
        matches = list(model.objects.filter(submission=s).select_related(field))
        candidates = []
        domain_decided = any(m.method=='manual_review' for m in matches)
        for book in available_books(s,domain):
            strength, reason = candidate_reason(s,book)
            if not strength:
                continue
            existing = next((m for m in matches if getattr(m,field+'_id')==book.pk),None)
            blocked = (existing is not None or domain_decided or competing(model,s,book,field)
                       or any(request_data(m) and m.status=='pending_review' for m in matches))
            if not blocked and eligible(actor,s):
                candidates.append({'book':book,'reason':reason,'strong':strength in {'work','exact'},
                    'token':selection_token(book,actor)})
        if len(candidates)>3:
            candidates=[]
        groups.append({'domain':domain, 'label':'BOTM' if domain=='botm' else 'Personal TBR',
            'matches':[{'match':m,'book':getattr(m,field),'requested':bool(request_data(m)),
                        'stale':bool(request_data(m) and request_data(m).get('fingerprint') != identity_stamp(s,getattr(m,field)))} for m in matches],
            'candidates':candidates})
    return groups


def selected_book(s,actor,domain,token):
    try:
        signed = signing.loads(token,salt=INTENT_SALT,max_age=1800)
        book = eligible_entry(s,domain,signed['entry']['id'])
        if signed != {'actor':actor.pk,'entry':entry_evidence(book)}:
            raise ValueError
    except (signing.BadSignature, TypeError, KeyError, ValueError):
        raise ValidationError('The selected qualification changed or expired. Reload and choose again.') from None
    return book


def _request(s, actor, domain, token):
    if domain not in DOMAINS or not eligible(actor,s):
        raise PermissionDenied('Current Reader ownership and participation are required.')
    book = selected_book(s,actor,domain,token)
    if domain=='tbr':
        from .prompt_reservations import assert_tbr_allowed
        assert_tbr_allowed(s)
    model, field = DOMAINS[domain]
    matches=list(model.objects.select_for_update().filter(submission=s))
    if any(m.is_qualifying or m.method=='manual_review' or (m.status=='rejected' and getattr(m,field+'_id')==book.pk)
           or (request_data(m) and m.status=='pending_review') for m in matches):
        raise ValidationError('This qualification already has credit, a pending request or a prior decision. No new request was saved.')
    if competing(model,s,book,field):
        raise ValidationError('This entry already has qualifying credit.')
    strength, reason = candidate_reason(s,book)
    if not strength:
        raise ValidationError('No supported identity relationship is available for this request.')
    module=import_module('.botm_matching' if domain=='botm' else '.personal_tbr_matching',__package__)
    match=next((m for m in matches if getattr(m,field+'_id')==book.pk),None)
    if match is None:
        match=module._pending_candidate(book,s,reason)
    match.status='pending_review';match.method='suggested';match.is_qualifying=False
    match.conflict_reason='';match.evidence_summary=reason;match.suggestion_fingerprint=identity_stamp(s,book)
    match.decision_evidence={**match.decision_evidence, 'reader_request':{'actor_id':actor.pk,'domain':domain,'target':entry_evidence(book),
        'created_at':timezone.now().isoformat(),'fingerprint':match.suggestion_fingerprint}}
    match.save()
    AuditEvent.objects.create(actor=actor,group=s.month.group,action='submission.qualification_requested',
        object_type='BookSubmission',object_id=str(s.pk),summary=json.dumps({'domain':domain,'match_id':match.pk,
            'request':request_data(match)},sort_keys=True))
    return match


@transaction.atomic
def request_qualification(*, submission, actor, domain, token, state):
    actor=get_user_model().objects.get(pk=actor.pk)
    s=load(submission.pk,lock=True)
    if not eligible(actor,s):
        raise PermissionDenied('Current Reader ownership and participation are required.')
    check_version(s,actor,state)
    return _request(s,actor,domain,token)


@transaction.atomic
def save_pending(*, submission, actor, data):
    from .discord_events import observe_book_changes
    from .botm_matching import synchronize_submission as sync_botm
    from .personal_tbr_matching import synchronize_submission as sync_tbr
    actor=get_user_model().objects.get(pk=actor.pk)
    s=load(submission.pk,lock=True)
    if not eligible(actor,s,editing=True):
        raise PermissionDenied('Only your eligible Pending submission can be edited.')
    check_version(s,actor,data.get('version'))
    before={key:getattr(s,key) for key in BookSubmissionForm.Meta.fields}
    before_themes=list(s.theme_claims.order_by('pk').values())
    form=PendingReaderForm(data,month=s.month,instance=s)
    if not form.is_valid():
        raise ValidationError(form.errors.as_json())
    with observe_book_changes(s):
        # ModelForm validation constructs only its permitted model fields and
        # verified catalog associations. Original acceptance/intent stays intact.
        s.full_clean();s.save()
        selected={t.pk:t for t in form.cleaned_data['themes']}
        for claim in s.theme_claims.all():
            claim.status='pending' if claim.theme_id in selected else 'rejected'
            if claim.theme_id in selected:
                claim.response=form.cleaned_data.get('theme_response_'+str(claim.theme_id),'').strip()
            if claim.status=='pending':
                claim.full_clean()
            claim.save();selected.pop(claim.theme_id,None)
        for theme in selected.values():
            ThemeClaim.objects.create(submission=s,theme=theme,response=form.cleaned_data.get('theme_response_'+str(theme.pk),'').strip())
        for domain, (model, field) in DOMAINS.items():
            if domain+'_choice' not in data:
                continue
            token=data.get(domain+'_choice')
            target=selected_book(s,actor,domain,token) if token else None
            old_selection=getattr(s,'selected_'+domain+'_book_id')
            current=[m for m in model.objects.select_for_update().filter(submission=s,status='pending_review')
                if request_data(m) or getattr(m,field+'_id')==old_selection]
            if target and any(getattr(m,field+'_id')==target.pk for m in current):
                continue
            for match in current:
                if match.method=='manual_review' or match.is_qualifying:
                    raise ValidationError('Reviewed qualifications require staff correction.')
                match.status='rejected'
                match.decision_evidence={**match.decision_evidence,'reader_withdrawal':{
                    'actor_id':actor.pk,'created_at':timezone.now().isoformat()}}
                match.save()
                AuditEvent.objects.create(actor=actor,group=s.month.group,action='submission.qualification_withdrawn',
                    object_type='BookSubmission',object_id=str(s.pk),summary=json.dumps({'domain':domain,'match_id':match.pk}))
            if target:
                _request(s,actor,domain,token)
        sync_botm(s);sync_tbr(s)
        AuditEvent.objects.create(actor=actor,group=s.month.group,action='submission.reader_edited',
            object_type='BookSubmission',object_id=str(s.pk),summary=json.dumps({'before':before,
                'before_themes':before_themes,'after_themes':list(s.theme_claims.order_by('pk').values()),
                'after':{key:getattr(s,key) for key in BookSubmissionForm.Meta.fields}},default=str,sort_keys=True))
    return s


def protected_request_state(s):
    """Qualification approval may change only supported qualification scoring."""
    row=BookSubmission.objects.filter(pk=s.pk).values().get()
    for key in ('bonus_pages','final_scored_pages'):
        row.pop(key)
    state={'book':row,'themes':list(s.theme_claims.order_by('pk').values())}
    for model in (PersonalTBRCompletionAward,BotmCompletionAward):
        state[model.__name__]=[(a.pk,a.is_qualifying,a.bonus_amount_snapshot,a.effective_date,a.completion_set_fingerprint,
            list(a.configured_books.order_by('pk').values())) for a in model.objects.filter(month=s.month,participant=s.participant).order_by('pk')]
    return state


@transaction.atomic
@observe_book
def review_request(*, submission, actor, domain, match_id, state, decision):
    from .permissions import can_review_submission
    from .matching_resolution import confirm_suggestion
    actor=get_user_model().objects.get(pk=actor.pk);s=load(submission.pk,lock=True)
    if not can_review_submission(actor,s):
        raise PermissionDenied('Current Challenge review scope is required.')
    check_version(s,actor,state)
    if domain not in DOMAINS:
        raise ValidationError('Choose a qualification request.')
    model,_=DOMAINS[domain]
    match=model.objects.select_for_update().filter(pk=match_id,submission=s).first()
    if not request_data(match):
        raise ValidationError('The Reader request is unavailable.')
    result=confirm_suggestion(match=match,actor=actor,decision=decision,expected_fingerprint=match.suggestion_fingerprint)
    return result
