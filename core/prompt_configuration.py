"""Configuration only. Does not qualify submissions, synchronize matches, or score."""
import hashlib
import json
from django import forms
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from .models import AuditEvent, ChallengeMonth, PromptCategory, ChallengePrompt
from .permissions import can_run_challenge_wide_operations

SALT='northbound.prompt-configuration.v1'
STAGES={'draft','upcoming','active'}


def can_configure_prompts(actor,month):
    return bool(actor.is_authenticated and actor.is_active and month.status in STAGES and can_run_challenge_wide_operations(actor,month))


def automation_excluded(month):
    # The persistent flag is independent of IDs and survives subsequent renaming/rescheduling.
    return bool(month.prompt_automation_excluded or (month.group.slug == 'guildwars'
        and month.starts_on and month.ends_on
        and (month.starts_on.year,month.starts_on.month)==(2026,11)
        and (month.ends_on.year,month.ends_on.month)==(2026,11)))


def _persist_exclusion(month,actor):
    if automation_excluded(month) and not month.prompt_automation_excluded:
        month.prompt_automation_excluded=True
        month.save(update_fields=['prompt_automation_excluded'])
        AuditEvent.objects.create(actor=actor,group=month.group,action='prompt.automation_excluded',object_type='ChallengeMonth',object_id=str(month.pk),summary=json.dumps({'challenge_id':month.pk,'before':False,'after':True,'reason':'Protected November 2026 Challenge or explicit exclusion'}))
        return True
    return False


def _state(month,kind):
    data={'status':month.status,'excluded':automation_excluded(month),
        'categories':list(PromptCategory.objects.filter(month=month,kind=kind).values()),
        'prompts':list(ChallengePrompt.objects.filter(category__month=month,category__kind=kind).order_by('pk').values())}
    return hashlib.sha256(json.dumps(data,sort_keys=True,default=str).encode()).hexdigest()


def configuration_token(month,kind,actor):
    return signing.dumps({'month':month.pk,'kind':kind,'actor':actor.pk,'state':_state(month,kind)},salt=SALT)


def _authorize(month_id,kind,actor,version):
    month=ChallengeMonth.objects.select_for_update().select_related('group').get(pk=month_id)
    actor=get_user_model().objects.get(pk=actor.pk)
    if kind not in PromptCategory.Kind.values or not can_configure_prompts(actor,month):
        raise PermissionDenied('Challenge-wide prompt configuration authority is required in an editable stage.')
    try: payload=signing.loads(version,salt=SALT,max_age=1800)
    except signing.BadSignature: raise ValidationError('This configuration form expired. Reload before saving.') from None
    if payload!={'month':month.pk,'kind':kind,'actor':actor.pk,'state':_state(month,kind)}:
        raise ValidationError('Prompt configuration or activity changed. Reload before saving.')
    return month,actor


def _audit(month,kind,actor,action,pk,before,after):
    AuditEvent.objects.create(actor=actor,group=month.group,action=action,object_type='ChallengePrompt' if action=='prompt.updated' else 'PromptCategory',object_id=str(pk),summary=json.dumps({'challenge_id':month.pk,'category':kind,'before':before,'after':after},sort_keys=True))


class CategoryConfigurationForm(forms.Form):
    enabled=forms.BooleanField(required=False,label='Enable category')
    completion_bonus=forms.IntegerField(min_value=0,max_value=2147483647,label='Completion bonus points')
    exclude_automation=forms.BooleanField(required=False,label='Permanently exclude this Challenge from automated Bingo and Scavenger Hunt')
    version=forms.CharField(widget=forms.HiddenInput)


class PromptDefinitionForm(forms.Form):
    submission_type=forms.ChoiceField(choices=ChallengePrompt.SubmissionType.choices, required=False, label='Submission type')
    number=forms.IntegerField(min_value=1,max_value=30,label='Prompt number')
    description=forms.CharField(widget=forms.Textarea(attrs={'rows':4}),label='Full prompt description')
    points=forms.IntegerField(min_value=0,max_value=2147483647,label='Prompt points')
    is_active=forms.BooleanField(required=False,initial=True,label='Active (required for completion)')
    version=forms.CharField(widget=forms.HiddenInput)


def _validated(form_type,values):
    allowed=set(form_type.base_fields)-{'version'}
    if set(values)-allowed: raise ValidationError('Unsupported configuration fields.')
    if form_type is PromptDefinitionForm:
        values = {'submission_type': 'book', **values}
    form=form_type({**values,'version':'service-validated'})
    if not form.is_valid(): raise ValidationError(form.errors.as_json())
    result = {k:v for k,v in form.cleaned_data.items() if k!='version'}
    if form_type is PromptDefinitionForm:
        result['submission_type'] = result['submission_type'] or 'book'
    return result


@transaction.atomic
def save_category(*,month_id,kind,actor,values,version):
    month,actor=_authorize(month_id,kind,actor,version)
    values=_validated(CategoryConfigurationForm,values)
    marker_changed=_persist_exclusion(month,actor)
    excluded=automation_excluded(month) or values['exclude_automation']
    if excluded and values['enabled']:
        raise ValidationError('This Challenge is excluded from automated prompts; both categories must remain disabled.')
    category=PromptCategory.objects.select_for_update().filter(month=month,kind=kind).first()
    category=category or PromptCategory(month=month,kind=kind)
    before={'enabled':category.enabled,'completion_bonus':category.completion_bonus,'excluded':month.prompt_automation_excluded}
    if excluded and month.prompt_categories.filter(enabled=True).exists():
        raise ValidationError('Disable both categories before marking this Challenge excluded.')
    has_required = category.pk and category.prompts.filter(is_active=True).exists()
    if values['enabled'] and not has_required:
        raise ValidationError('Configure at least one active prompt before enabling this category.')
    category.enabled=values['enabled'];category.completion_bonus=values['completion_bonus']
    after={'enabled':category.enabled,'completion_bonus':category.completion_bonus,'excluded':excluded}
    if before==after: return category,marker_changed
    category.revision+=1;category.save()
    if excluded and not month.prompt_automation_excluded:
        month.prompt_automation_excluded=True;month.save(update_fields=['prompt_automation_excluded'])
    _audit(month,kind,actor,'prompt.category_updated',category.pk,before,after)
    return category,True


@transaction.atomic
def save_prompt(*,month_id,kind,actor,values,version,prompt_id=None):
    month,actor=_authorize(month_id,kind,actor,version)
    values=_validated(PromptDefinitionForm,values)
    marker_changed=_persist_exclusion(month,actor)
    category=PromptCategory.objects.select_for_update().filter(month=month,kind=kind).first()
    if category is None: category=PromptCategory.objects.create(month=month,kind=kind)
    prompt=ChallengePrompt.objects.select_for_update().filter(pk=prompt_id,category=category).first() if prompt_id else None
    if prompt_id and prompt is None: raise PermissionDenied('The prompt is outside this Challenge/category.')
    if prompt is None: prompt=ChallengePrompt(category=category)
    fields=('number','description','points','is_active','submission_type')
    before={k:getattr(prompt,k) for k in fields} if prompt.pk else None
    for key,value in values.items():setattr(prompt,key,value)
    after={k:getattr(prompt,k) for k in fields}
    if before==after:return prompt,marker_changed
    if category.enabled and not values['is_active'] and not category.prompts.exclude(pk=prompt.pk).filter(is_active=True).exists():
        raise ValidationError('An enabled category must retain at least one required prompt.')
    prompt.save()
    _audit(month,kind,actor,'prompt.updated',prompt.pk,before,after)
    return prompt,True
