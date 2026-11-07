"""Explicit administrative credit decisions, previewed through existing reward services."""
import hashlib
import json
from importlib import import_module
from django import forms
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .models import (BookSubmission, BotmBook, PersonalTBRBook, BotmMatch, PersonalTBRMatch,
    BotmCompletionAward, PersonalTBRCompletionAward, ModifierProvenance, AuditEvent,
    MonthEnrollment, Membership, HardcoverSyncOutbox, ProgressCheckpointResult)
from .permissions import membership_for, can_operate_challenge
from .matching_intent import eligible_entry, entry_evidence
from .matching_resolution import identity_evidence, identity_stamp, competing
from .submission_corrections import _reader_state, _fingerprint

SALT = 'northbound.administrative-credit-preview.v1'


def can_manage_credit(actor, submission):
    member = membership_for(actor, submission.month.group)
    return bool(actor.is_authenticated and actor.is_active and (actor.is_superuser or
        (member and member.role == Membership.Role.OWNER) or can_operate_challenge(actor, submission.month)))


def _hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,default=str).encode()).hexdigest()


def _load(pk):
    return BookSubmission.objects.select_for_update().select_related('month__group','participant__user','catalog_book','catalog_edition').get(pk=pk)


def _state(s):
    scope=dict(month=s.month,participant=s.participant)
    return _hash({'full_submission':BookSubmission.objects.filter(pk=s.pk).values().get(), 'correction':_fingerprint(s), 'reader':_reader_state(s),
        'month':list(type(s.month).objects.filter(pk=s.month_id).values()),
        'enrollment':list(MonthEnrollment.objects.filter(**scope).values()),
        'matching_history':{model.__name__:list(model.objects.filter(**scope).order_by('pk').values())
                            for model in (BotmMatch,PersonalTBRMatch,ModifierProvenance,BotmCompletionAward,PersonalTBRCompletionAward)},
        'books':list(BotmBook.objects.filter(month=s.month).values()),
        'tbr':list(PersonalTBRBook.objects.filter(personal_tbr__enrollment__month=s.month,
            personal_tbr__enrollment__participant=s.participant).values())})


def _summary(s):
    s.refresh_from_db()
    scope=dict(month=s.month,participant=s.participant)
    result={'approved_pages':s.approved_pages,'bonus_pages':s.bonus_pages,'scored_pages':s.final_scored_pages}
    for domain,model,award in [('botm',BotmMatch,BotmCompletionAward),('tbr',PersonalTBRMatch,PersonalTBRCompletionAward)]:
        result[domain+'_completed']=model.objects.filter(**scope,is_qualifying=True,status='confirmed').count()
        result[domain+'_completion_bonus']=sum(award.objects.filter(**scope,is_qualifying=True).values_list('bonus_amount_snapshot',flat=True))
        result[domain+'_completion_dates']=[str(day) for day in award.objects.filter(**scope,is_qualifying=True).order_by('completion_set_fingerprint').values_list('effective_date',flat=True)]
    return result


def _protected(s, model, match, domain):
    scope=dict(month=s.month,participant=s.participant)
    other_model=PersonalTBRMatch if domain=='botm' else BotmMatch
    other_award=PersonalTBRCompletionAward if domain=='botm' else BotmCompletionAward
    allowed_sources={f'botm_match:{match.pk}' if domain=='botm' else f'personal_tbr_match:{match.pk}'}
    # Domain completion provenance is the only permitted Reader-level scoring change.
    completion_type='botm_completion' if domain=='botm' else 'tbr_completion'
    original=dict(BookSubmission.objects.filter(pk=s.pk).values().get())
    for key in ['bonus_pages','final_scored_pages']:original.pop(key)
    return {'submission':original,
        'others':list(BookSubmission.objects.exclude(pk=s.pk).order_by('pk').values()),
        'other_matches':list(other_model.objects.order_by('pk').values()),
        'other_awards':list(other_award.objects.order_by('pk').values()),
        'same_matches':list(model.objects.exclude(pk=match.pk).order_by('pk').values()),
        'provenance':list(ModifierProvenance.objects.exclude(
            Q(source_reference__in=allowed_sources,source_type='botm_book' if domain=='botm' else 'tbr_book')
            | Q(month=s.month,participant=s.participant,source_type=completion_type)).order_by('pk').values()),
        'other_completion_awards':list((BotmCompletionAward if domain=='botm' else PersonalTBRCompletionAward).objects.exclude(**scope).order_by('pk').values()),
        'claims':list(s.theme_claims.order_by('pk').values()),
        'audits':list(AuditEvent.objects.filter(group=s.month.group,object_type='BookSubmission',object_id=str(s.pk)).order_by('pk').values()),
        'immersive':_reader_state(s)['ImmersiveRequest'],
        'checkpoints':list(ProgressCheckpointResult.objects.filter(checkpoint__month=s.month,participant=s.participant).order_by('pk').values()),
        'outbox':list(HardcoverSyncOutbox.objects.order_by('pk').values())}


def _operate(s, actor, *, domain, entry_id, operation, reason, acknowledge=False):
    if not can_manage_credit(actor,s):raise PermissionDenied('Administrative matching authority is required.')
    if s.month.status not in {'active','finalizing'}:
        raise ValidationError('Credit corrections require an Active or Finalizing Challenge.')
    if not reason or not reason.strip():raise ValidationError('Explain why this credit needs correction.')
    if operation not in {'assign','reverse','restore'} or domain not in {'botm','tbr'}:
        raise ValidationError('Choose a supported credit operation.')
    if (s.status!='approved' or s.is_removed or not s.participant.is_active or not s.participant.user.is_active
        or s.participant.user.is_superuser or not MonthEnrollment.objects.filter(month=s.month,participant=s.participant,is_active=True).exists()):
        raise ValidationError('The submission must be approved and its Reader actively enrolled.')
    book=eligible_entry(s,domain,entry_id)
    model,field=(BotmMatch,'botm_book') if domain=='botm' else (PersonalTBRMatch,'personal_tbr_book')
    module=import_module('.botm_matching' if domain=='botm' else '.personal_tbr_matching',__package__)
    match=model.objects.select_for_update().filter(submission=s,**{field:book}).first()
    if operation=='reverse' and (not match or not match.is_qualifying):
        raise ValidationError('Only a current qualification can be reversed.')
    if operation=='restore' and (not match or match.status!='rejected' or match.method!='manual_review'):
        raise ValidationError('Restore requires a previously rejected or reversed reviewed relationship.')
    if operation=='assign' and match and match.status=='rejected':
        raise ValidationError('Use Restore to explicitly replace the previous rejection.')
    strength,_=identity_evidence(s,book)
    if operation!='reverse' and strength=='conflict' and not acknowledge:
        raise ValidationError('Acknowledge the conflicting book identities before proceeding.')
    if operation!='reverse' and competing(model,s,book,field,match.pk if match else None):
        raise ValidationError('Another submission or relationship already holds this qualification. It cannot be reassigned here.')
    if operation!='reverse' and match and match.is_qualifying:
        raise ValidationError('This relationship already qualifies. No change is needed.')
    before_qualifying=bool(match and match.is_qualifying)
    if match is None:match=module._pending_candidate(book,s,'Explicit administrative credit review')
    before_match=dict(model.objects.filter(pk=match.pk).values().get())
    protected=_protected(s,model,match,domain)
    award_model=BotmCompletionAward if domain=='botm' else PersonalTBRCompletionAward
    awards={a.pk:(a.is_qualifying,a.bonus_amount_snapshot,a.effective_date,a.completion_set_fingerprint,
                  list(a.configured_books.order_by('pk').values()))
            for a in award_model.objects.filter(month=s.month,participant=s.participant)}
    provenance_before={r['id']:{k:v for k,v in r.items() if k!='is_active'}
        for r in ModifierProvenance.objects.filter(month=s.month,participant=s.participant).values()}
    before=_summary(s)
    previous=match.decision_evidence
    match.method='manual_review';match.status='rejected' if operation=='reverse' else 'confirmed'
    match.is_qualifying=operation!='reverse';match.reviewer=actor;match.decided_at=timezone.now();match.conflict_reason=''
    match.decision_evidence={'authority':'administrator','operation':operation,'reason':reason.strip(),
        'actor_id':actor.pk,'decided_at':match.decided_at.isoformat(),'fingerprint':identity_stamp(s,book),
        'entry':entry_evidence(book),'submission_title':s.title,'submission_author':s.author,
        'identity_conflict_acknowledged':bool(acknowledge),'previous_decision':previous}
    match.full_clean();match.save()
    module._finalize_submission_matches(s,[match])
    after=_summary(s)
    for row in ModifierProvenance.objects.filter(pk__in=provenance_before).values():
        if {k:v for k,v in row.items() if k!='is_active'} != provenance_before[row['id']]:
            raise ValidationError('This operation would rewrite historical reward evidence. Nothing was saved.')
    if _protected(s,model,match,domain)!=protected:
        raise ValidationError('This operation would alter protected or unrelated records. Nothing was saved.')
    for award in award_model.objects.filter(pk__in=awards):
        current=(award.is_qualifying,award.bonus_amount_snapshot,award.effective_date,award.completion_set_fingerprint,
                 list(award.configured_books.order_by('pk').values()))
        if current!=awards[award.pk]:
            raise ValidationError('This operation would change a frozen completion award. A separate historical-award review is required.')
    payload={'domain':domain,'operation':operation,'reason':reason.strip(),'before':before,'after':after,
        'before_match':before_match,'after_match':dict(model.objects.filter(pk=match.pk).values().get()),
        'identity_conflict_acknowledged':bool(acknowledge),
        'award_references':list(ModifierProvenance.objects.filter(month=s.month,participant=s.participant,
            source_type__in=['botm_book','tbr_book','botm_completion','tbr_completion']).values_list('source_reference',flat=True))}
    AuditEvent.objects.create(actor=actor,group=s.month.group,action='submission.credit_'+operation,
        object_type='BookSubmission',object_id=str(s.pk),summary=json.dumps(payload,default=str,sort_keys=True))
    return {'before':before,'after':after,'before_qualifying':before_qualifying,'entry_title':book.title_snapshot,'qualifies':match.is_qualifying,
            'bonus_change':after['bonus_pages']-before['bonus_pages']}


@transaction.atomic
def preview_credit(*, submission, actor, **operation):
    actor=get_user_model().objects.get(pk=actor.pk);s=_load(submission.pk)
    if not can_manage_credit(actor,s):raise PermissionDenied('Administrative matching authority is required.')
    state=_state(s)
    with transaction.atomic():
        impact=_operate(s,actor,**operation)
        transaction.set_rollback(True)
    token=signing.dumps({'submission':s.pk,'actor':actor.pk,'state':state,'operation':operation,'impact':impact},salt=SALT)
    return impact,token


@transaction.atomic
def apply_credit(*, submission, actor, token, **operation):
    actor=get_user_model().objects.get(pk=actor.pk);s=_load(submission.pk)
    if not can_manage_credit(actor,s):raise PermissionDenied('Administrative matching authority is required.')
    try:expected=signing.loads(token,salt=SALT,max_age=600)
    except signing.BadSignature:raise ValidationError('The preview expired. Generate a new preview.') from None
    if (expected.get('submission')!=s.pk or expected.get('actor')!=actor.pk or expected.get('state')!=_state(s)
        or expected.get('operation')!=operation):
        raise ValidationError('The preview or its inputs changed. Generate a new preview.')
    impact=_operate(s,actor,**operation)
    if impact!=expected['impact']:raise ValidationError('The scoring impact changed. Generate a new preview.')
    return impact


class CreditForm(forms.Form):
    operation=forms.ChoiceField(choices=[('assign','Assign qualification'),('reverse','Reverse qualification'),('restore','Restore rejected or reversed qualification')])
    entry=forms.ModelChoiceField(queryset=BotmBook.objects.none(),label='Challenge book')
    reason=forms.CharField(widget=forms.Textarea(attrs={'rows':2}),max_length=2000)
    acknowledge=forms.BooleanField(required=False,label='I acknowledge that the submitted book and selection have conflicting identities')
    preview_token=forms.CharField(required=False,widget=forms.HiddenInput())
    confirm=forms.BooleanField(required=False,label='I confirm this preview and its scoring impact')

    def __init__(self,*args,submission,domain,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['entry'].queryset=(BotmBook.objects.filter(month=submission.month,is_retired=False) if domain=='botm' else
            PersonalTBRBook.objects.filter(personal_tbr__enrollment__month=submission.month,
                personal_tbr__enrollment__participant=submission.participant,personal_tbr__confirmed_at__isnull=False))
        self.fields['entry'].label_from_instance=lambda book:book.title_snapshot+' — '+book.author_snapshot

    def operation_data(self,domain):
        return dict(domain=domain,entry_id=self.cleaned_data['entry'].pk,operation=self.cleaned_data['operation'],
                    reason=self.cleaned_data['reason'],acknowledge=self.cleaned_data['acknowledge'])
