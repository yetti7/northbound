from django import template
from core.navigation_context import contextual_url
register=template.Library()
@register.simple_tag(takes_context=True)
def navigation_url(context,url):
    request=context.get('request')
    return contextual_url(request,url) if request else url

@register.simple_tag(takes_context=True)
def challenge_wrapped_url(context,month,url):
    from copy import copy
    from django.urls import resolve
    request=copy(context['request']);request.resolver_match=resolve(month.get_absolute_url())
    request.GET=request.GET.copy();request.GET.clear()
    return contextual_url(request,url)
