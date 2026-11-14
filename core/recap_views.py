from urllib.parse import urlencode
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from .recap_forms import RecapFilterForm
from .recaps import build_recap
from .recap_slides import build_wrapped_slides, team_bars


def _recap_context(request, *, include_book_highlights=False):
    data = request.GET.copy()
    data.setdefault('perspective','reader')
    data.setdefault('period','all')
    form = RecapFilterForm(data, viewer=request.user)
    recap = None
    query = ''
    if form.is_valid():
        values = form.cleaned_data
        target = values.get('reader') if values['perspective'] == 'reader' else values['team'] if values['perspective'] == 'team' else None
        try:
            recap = build_recap(viewer=request.user, perspective=values['perspective'], target_id=target or None,
                group_id=values['group'] or None, challenge_id=values['challenge'] or None,
                period=values['period'], anchor=values['anchor'], start=values['start'], end=values['end'],
                include_book_highlights=include_book_highlights)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            # Carry validated reporting fields only, never arbitrary URL parameters.
            query = urlencode({key: value for key, value in values.items() if value not in (None, '')})
            request._validated_recap_navigation = {'group':values['group'], 'challenge':values['challenge']}
            request.__dict__.pop('_logical_navigation_result', None)
    suffix = '?' + query if query else ''
    return {'form':form, 'recap':recap, 'dashboard_url':reverse('recap-dashboard') + suffix,
            'wrapped_url':reverse('recap-wrapped') + suffix}


@login_required
@require_GET
@never_cache
def recap_dashboard(request):
    context = _recap_context(request)
    context['dashboard_sections'] = [{**section, 'team_bars': team_bars(section['teams'])}
        for section in context['recap']['sections']] if context['recap'] else []
    return render(request, 'core/recap_dashboard.html', context)


@login_required
@require_GET
@never_cache
def recap_wrapped(request):
    context = _recap_context(request, include_book_highlights=True)
    context['slides'] = build_wrapped_slides(context['recap'])
    from .recap_exports import export_data
    from .wrapped_branding import WrappedBrandingForm
    branding_form = WrappedBrandingForm(request.GET,viewer=request.user,recap=context['recap'])
    context['branding_form'] = branding_form
    context['has_team_branding'] = bool(branding_form.options['teams'])
    if not branding_form.is_valid():
        context['form'].add_error(None,'Choose an authorized branding source and Team.')
        context['slides'] = []
        context['export_data'] = None
        return render(request,'core/recap_wrapped.html',context,status=400)
    context['export_data'] = export_data(context['recap'], context['slides'])
    if context['export_data']:
        # Only the presentation-specific authorized choices supply Wrapped artwork.
        context['export_data'].pop('branding', None)
        context['export_data']['branding_options'] = branding_form.options
        context['export_data']['branding_selection'] = branding_form.cleaned_data
    return render(request, 'core/recap_wrapped.html', context)
