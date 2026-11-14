"""Gated prompt selection and lifecycle bridge; scoring lives in prompt_scoring."""
from functools import wraps
import json
from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import IntegrityError,OperationalError,transaction
from django.utils import timezone
from .models import (AuditEvent,BookSubmission,ChallengeMonth,ChallengePrompt,PromptCategory,PromptClaim,
    MonthEnrollment,Membership,PersonalTBRMatch,ModifierProvenance)
from .permissions import can_edit_submission,is_active_floater
from .prompt_configuration import automation_excluded

FIELDS={'bingo':'bingo_prompt','scavenger':'scavenger_prompt'}
HOLDING=('reserved','qualified')


def reservations_ready():
    # Deliberately not environment activated in Phase 2. Tests may override settings.
    return getattr(settings,'NORTHBOUND_PROMPT_RESERVATIONS_READY',False) is True


def require_ready():
    if not reservations_ready():raise ValidationError('Bingo and Scavenger Hunt prompt selection is not activated yet.')


def atomic_reservations(function):
    @wraps(function)
    def wrapped(*args,**kwargs):
        try:
            with transaction.atomic():return function(*args,**kwargs)
        except IntegrityError:
            raise ValidationError('Another submission reserved this prompt. Nothing was saved; reload and choose an available prompt.') from None
        except OperationalError as exc:
            if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():raise
            raise ValidationError('Another update is using prompt reservations. Nothing was saved; reload before retrying.') from None
    return wrapped


def current_selections(submission):
    if not submission.pk:return {}
    return {c.category.kind:c for c in submission.prompt_claims.filter(is_current=True).select_related('prompt','category')}


def has_bingo_selection(submission):
    return bool(submission.pk and PromptClaim.objects.filter(submission_id=submission.pk,is_current=True,category__kind='bingo').exists())


def assert_tbr_allowed(submission):
    if has_bingo_selection(submission):
        raise ValidationError('This book has a Bingo selection. Remove it through an authorized correction before assigning Personal TBR credit.')


def assert_bingo_allowed(submission):
    if submission._state.adding and submission.selected_tbr_book_id:
        raise ValidationError('Choose Bingo or Personal TBR for this book, not both. Scavenger Hunt can be combined with Personal TBR.')
    if submission.pk and (PersonalTBRMatch.objects.filter(submission=submission,is_qualifying=True).exists()
        or ModifierProvenance.objects.filter(submission=submission,source_type='tbr_book',is_active=True).exists()):
        raise ValidationError('This book already has Personal TBR credit. An authorized audited credit correction is required before choosing Bingo.')


def category_available(month,kind):
    return bool(reservations_ready() and not automation_excluded(month) and month.status in {'active','finalizing'}
        and PromptCategory.objects.filter(month=month,kind=kind,enabled=True,prompts__is_active=True).exists())


def available_prompts(submission,kind):
    if not submission.participant_id or not category_available(submission.month,kind):return ChallengePrompt.objects.none()
    held=PromptClaim.objects.filter(participant_id=submission.participant_id,is_current=True,state__in=HOLDING).exclude(submission_id=submission.pk or 0).values('prompt_id')
    return ChallengePrompt.objects.filter(category__month_id=submission.month_id,category__kind=kind,category__enabled=True,is_active=True,submission_type='book').exclude(pk__in=held).order_by('number')


def configure_form(form):
    submission=form.instance
    if not submission.month_id or not submission.participant_id:return
    for kind,name in FIELDS.items():
        choices=available_prompts(submission,kind)
        existing=current_selections(submission).get(kind)
        if existing and category_available(submission.month,kind):choices=(choices | ChallengePrompt.objects.filter(pk=existing.prompt_id,submission_type='book')).distinct().order_by('number')
        if not choices.exists():continue
        field=forms.ModelChoiceField(queryset=choices,required=False,label='Bingo prompt' if kind=='bingo' else 'Scavenger Hunt prompt',
            help_text='Optional. Selection reserves a prompt for review; it does not award credit or points.',
            widget=forms.Select(attrs={'style':'width:100%;max-width:100%'}))
        field.label_from_instance=lambda prompt:f'{prompt.number}. {prompt.description}'
        form.fields[name]=field
        if existing:form.initial[name]=existing.prompt_id
        if existing and existing.earning_evidence and submission.pk:
            form.fields['prompt_decision_'+kind]=forms.ChoiceField(required=False,choices=[('','Preserve earned credit'),('qualified','Restore qualification'),('rejected','Reverse qualification')],label=existing.category.get_kind_display()+' credit correction',help_text='Changes require a signed scoring preview; established amounts are preserved.')


def validate_form(form,cleaned):
    posted={kind:form.data.get(form.add_prefix(name)) for kind,name in FIELDS.items() if form.add_prefix(name) in form.data}
    aliases={name+'_id' for name in FIELDS.values()} & set(form.data)
    decision_fields={name for name in form.data if name.startswith('prompt_decision_')}
    if posted or aliases or decision_fields:require_ready()
    if decision_fields-set(form.fields):raise ValidationError('Prompt credit correction is unavailable in this workflow.')
    if aliases:raise ValidationError('Use the prompt selectors rather than internal correction fields.')
    for kind,name in FIELDS.items():
        if kind in posted and name not in form.fields:
            raise ValidationError('This prompt category is unavailable in this workflow or Challenge.')
    if cleaned.get('bingo_prompt'):
        if form.instance._state.adding and form.data.get('selected_tbr'):
            raise ValidationError('Choose Bingo or Personal TBR for this book, not both.')
        assert_bingo_allowed(form.instance)
    return cleaned


def form_selections(form):
    return {kind:(form.cleaned_data[name].pk if form.cleaned_data.get(name) else None)
        for kind,name in FIELDS.items() if name in form.fields and form.add_prefix(name) in form.data}


def selection_changes(submission,selections):
    current=current_selections(submission)
    return any(pk!=(current[kind].prompt_id if kind in current else None) for kind,pk in selections.items())


def prompt_state(submission):
    claims=list(PromptClaim.objects.filter(category__month_id=submission.month_id,participant_id=submission.participant_id).order_by('pk').values())
    state={'submission_id':submission.pk,'ready':reservations_ready(),'claims':claims,
        'completions':list(submission.participant.prompt_completions.filter(category__month_id=submission.month_id).order_by('pk').values()),
        'prompt_provenance':list(ModifierProvenance.objects.filter(month_id=submission.month_id,participant_id=submission.participant_id,source_type__in=['bingo_prompt','scavenger_prompt','bingo_completion','scavenger_completion']).order_by('pk').values())}
    if claims or reservations_ready():
        state.update(excluded=automation_excluded(submission.month),categories=list(PromptCategory.objects.filter(month=submission.month).order_by('pk').values()),
            prompts=list(ChallengePrompt.objects.filter(category__month=submission.month).order_by('pk').values()))
    return state


def _selections(values):
    if not isinstance(values,dict) or set(values)-set(FIELDS):raise ValidationError('Use only Bingo and Scavenger Hunt prompt selections.')
    if any(pk is not None and (type(pk) is not int or pk<=0) for pk in values.values()):raise ValidationError('Choose a valid prompt.')
    return values


def _validate_prompt(submission,kind,pk):
    require_ready()
    month=ChallengeMonth.objects.select_related('group').get(pk=submission.month_id)
    if not category_available(month,kind):raise ValidationError('This prompt category is disabled, excluded, or unavailable in this Challenge stage.')
    prompt=available_prompts(submission,kind).select_for_update().filter(pk=pk).first()
    if not prompt:raise ValidationError('That prompt is retired, reserved, completed, or outside this Challenge.')
    if kind=='bingo':assert_bingo_allowed(submission)
    return prompt


def _audit(submission,actor,before):
    after={kind:{'claim_id':claim.pk,'prompt_id':claim.prompt_id,'state':claim.state} for kind,claim in current_selections(submission).items()}
    AuditEvent.objects.create(actor=actor,group=submission.month.group,action='prompt.selection_updated',object_type='BookSubmission',object_id=str(submission.pk),
        summary=json.dumps({'challenge_id':submission.month_id,'reader_id':submission.participant_id,'before':before,'after':after},sort_keys=True))


def _replace(submission,selections,actor,*,allow_qualified=False):
    if not selections:return
    require_ready()
    before={kind:{'claim_id':claim.pk,'prompt_id':claim.prompt_id,'state':claim.state} for kind,claim in current_selections(submission).items()}
    changed=False
    for kind,pk in selections.items():
        old=current_selections(submission).get(kind)
        if pk==(old.prompt_id if old else None):continue
        if old and old.earning_evidence and not allow_qualified:raise ValidationError('Preview changes to earned prompt credit before applying.')
        prompt=_validate_prompt(submission,kind,pk) if pk else None
        if old:
            if old.earning_evidence:
                from .prompt_scoring import release_claim
                release_claim(old,actor,submission)
            else:
                old.is_current=False;old.state='released';old.save(update_fields=['is_current','state'])
        if prompt:
            PromptClaim.objects.create(category=prompt.category,prompt=prompt,submission=submission,participant_id=submission.participant_id,
                state='rejected' if submission.status=='rejected' else 'released' if submission.is_removed else 'reserved',
                selection_evidence={'prompt_id':prompt.pk,'number':prompt.number,'description':prompt.description,'points':prompt.points,'revision':prompt.revision,
                    'completion_bonus':prompt.category.completion_bonus,'required_prompt_set':prompt.category.frozen_prompt_set,
                    'selected_at':timezone.now().isoformat(),'selected_by_id':actor.pk})
        changed=True
    if changed:_audit(submission,actor,before)


def _reader_eligible(submission):
    participant=Membership.objects.select_related('user').get(pk=submission.participant_id)
    if (not participant.is_active or not participant.user.is_active or participant.user.is_superuser
        or participant.group_id!=submission.month.group_id or is_active_floater(submission.month,participant.pk)
        or not MonthEnrollment.objects.filter(month=submission.month,participant=participant,is_active=True,awaiting_scoring_team=False).exists()):
        raise ValidationError('Current Reader participation is required for prompt reservations.')


@atomic_reservations
def save_new_submission(*,submission,actor,selections):
    selections=_selections(selections)
    if not submission._state.adding or submission.pk:raise ValidationError('Use the authorized correction workflow for an existing submission.')
    if any(selections.values()):
        require_ready();submission.month=ChallengeMonth.objects.select_for_update().select_related('group').get(pk=submission.month_id)
        actor=get_user_model().objects.get(pk=actor.pk)
        if not actor.is_active or submission.participant.user_id!=actor.pk:raise PermissionDenied('Only the submitting Reader can reserve prompts for a new book.')
        if submission.month.status!='active':raise ValidationError('New prompt selections require an Active Challenge.')
        _reader_eligible(submission)
        if selections.get('bingo'):assert_bingo_allowed(submission)
    submission.save()
    _replace(submission,{k:v for k,v in selections.items() if v is not None},actor)
    return submission


@atomic_reservations
def replace_selections(*,submission,actor,selections,preview_authorization=None):
    selections=_selections(selections)
    if not selections:return submission
    require_ready()
    ChallengeMonth.objects.select_for_update().get(pk=submission.month_id)
    current=BookSubmission.objects.select_for_update().select_related('month__group','participant__user').get(pk=submission.pk)
    actor=get_user_model().objects.get(pk=actor.pk)
    if not can_edit_submission(actor,current):raise PermissionDenied('Current submission correction authority is required.')
    _reader_eligible(current)
    allow_qualified=False
    if preview_authorization:
        from .prompt_scoring import validate_ticket
        validate_ticket(preview_authorization,current,actor,selections);allow_qualified=True
    _replace(current,selections,actor,allow_qualified=allow_qualified)
    return current


@atomic_reservations
def synchronize_lifecycle(submission,previous=None,actor=None):
    submission=BookSubmission.objects.select_for_update().select_related('month__group','participant__user').get(pk=submission.pk)
    if not submission.prompt_claims.filter(is_current=True).exists():return
    from .prompt_scoring import lifecycle
    lifecycle(submission,previous=previous,actor=actor)
