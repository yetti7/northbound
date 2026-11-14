from django import forms
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, render, redirect
from django.contrib import messages
from django.views.decorators.http import require_http_methods
from django.views.decorators.cache import never_cache
from .models import ReadingGroup, ChallengeMonth, Team
from .branding import can_manage_branding, can_view_branding_settings, update_branding, community_branding, _asset, branding_form_class, team_branding


@login_required
@never_cache
@require_http_methods(['GET','POST'])
def branding_settings(request, group_slug, month_pk=None, team_pk=None):
    group = get_object_or_404(ReadingGroup, slug=group_slug, is_active=True)
    target = get_object_or_404(ChallengeMonth, pk=month_pk, group=group) if month_pk else group
    team = get_object_or_404(Team.objects.select_related('month__group'), pk=team_pk, month=target) if team_pk else None
    if team is not None:
        target = team
    if not can_view_branding_settings(request.user, target):
        return HttpResponseForbidden('Branding settings permission is required.')
    editable = can_manage_branding(request.user, target)
    if request.method == 'POST':
        if not editable:
            return HttpResponseForbidden('Branding settings are read-only.')
        form, saved = update_branding(actor=request.user,target=target,data=request.POST,files=request.FILES)
        if saved:
            messages.success(request,'Branding updated.')
            return redirect(request.path)
    else:
        form = branding_form_class(target)(instance=target)
    artwork_cards = []
    if month_pk:
        level = 'Team' if team else 'Challenge'
        for kind in ('logo', 'banner'):
            stored = _asset(getattr(target, kind))
            candidates = [(team.month, 'Challenge'), (group, 'Group')] if team else [(group, 'Group')]
            inherited, source = '', ''
            for parent, label in candidates:
                inherited = _asset(getattr(parent, kind))
                if inherited:
                    source = label
                    break
            artwork_cards.append({'kind': kind, 'label': level + ' ' + kind.title(),
                'field': form[kind], 'stored': bool(getattr(target, kind)),
                'preview': stored or inherited, 'fallback': inherited,
                'source': 'Stored ' + level + ' artwork' if stored else 'Inherited from ' + source if inherited else 'No artwork configured',
                'fallback_label': 'Inherited from ' + source if inherited else 'No artwork configured',
                'remove': forms.CheckboxInput().value_from_datadict(request.POST, request.FILES, kind + '-clear') if request.method == 'POST' else False})
    return render(request,'core/branding_settings.html' if month_pk else 'core/group_branding_settings.html',{'artwork_cards':artwork_cards,'group':group,'month':team.month if team else target if month_pk else None,'team':team,'form':form,
        'remove_logo':forms.CheckboxInput().value_from_datadict(request.POST,request.FILES,'logo-clear') if request.method == 'POST' else False,
        'remove_banner':forms.CheckboxInput().value_from_datadict(request.POST,request.FILES,'banner-clear') if request.method == 'POST' else False,
        'editable':editable,'branding':team_branding(request.user,team) if team else community_branding(request.user,group,target if month_pk else None), 'preview_logo':_asset(target.logo), 'preview_banner':_asset(target.banner)})
