from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect, render
from django.http import Http404, HttpResponseForbidden
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from .models import ChallengeMonth, PromptCategory, ChallengePrompt
from .prompt_configuration import (can_configure_prompts, automation_excluded, configuration_token,
    CategoryConfigurationForm, PromptDefinitionForm, save_category, save_prompt)


@login_required
@never_cache
@require_http_methods(['GET','POST'])
def prompt_settings(request,group_slug,month_pk,kind):
    month=get_object_or_404(ChallengeMonth.objects.select_related('group'),pk=month_pk,group__slug=group_slug)
    if kind not in PromptCategory.Kind.values: raise Http404
    if not can_configure_prompts(request.user,month):
        return HttpResponseForbidden('Challenge-wide prompt configuration is unavailable for your role or this lifecycle stage.')
    category=PromptCategory.objects.filter(month=month,kind=kind).first()
    prompts=ChallengePrompt.objects.filter(category=category).order_by('number') if category else ChallengePrompt.objects.none()
    prompt=get_object_or_404(prompts,pk=request.GET['edit']) if request.GET.get('edit') else None
    token=configuration_token(month,kind,request.user)
    action=request.POST.get('action')
    config_form=CategoryConfigurationForm(request.POST if request.method=='POST' and action=='category' else None,
        prefix='category',initial={'enabled':category.enabled if category else False,'completion_bonus':category.completion_bonus if category else 0,'exclude_automation':automation_excluded(month),'version':token})
    prompt_form=PromptDefinitionForm(request.POST if request.method=='POST' and action=='prompt' else None,
        prefix='prompt',initial={**({f:getattr(prompt,f) for f in ('number','description','points','is_active','submission_type')} if prompt else {'is_active':True,'submission_type':'book'}),'version':token})
    if prompt and category.structure_locked:
        for name in ('number','is_active','submission_type'):
            prompt_form.fields[name].disabled=True
            prompt_form.fields[name].help_text='Locked after the first reservation. Existing prompt identity is preserved.'
    if request.method=='POST':
        if action not in {'category','prompt'}:return HttpResponseForbidden('Choose a supported configuration action.')
        form=config_form if action=='category' else prompt_form
        if form.is_valid():
            values=dict(form.cleaned_data);version=values.pop('version')
            try:
                if action=='category': _,saved=save_category(month_id=month.pk,kind=kind,actor=request.user,values=values,version=version)
                else: _,saved=save_prompt(month_id=month.pk,kind=kind,actor=request.user,values=values,version=version,prompt_id=prompt.pk if prompt else None)
            except ValidationError as exc:form.add_error(None,' '.join(exc.messages))
            except IntegrityError:form.add_error(None,'Prompt configuration changed concurrently. Reload and try again.')
            else:
                messages.success(request,'Configuration saved. No submissions or scores were changed.' if saved else 'No configuration changes to save.')
                return redirect('challenge-prompt-settings',group_slug=group_slug,month_pk=month.pk,kind=kind)
    return render(request,'core/prompt_settings.html',{'month':month,'category':category,'kind':kind,
        'label':PromptCategory.Kind(kind).label,'prompts':prompts,'prompt':prompt,'config_form':config_form,'prompt_form':prompt_form,
        'structure_locked':category.structure_locked if category else False,'excluded':automation_excluded(month)})
