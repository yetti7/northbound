"""Presentation helpers for the compact Team Books gallery only."""
from django import template
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

register = template.Library()


@register.filter
def safe_reference_url(value):
    if not isinstance(value, str):
        return ''
    value = value.strip()
    try:
        URLValidator(schemes=['http', 'https'])(value)
    except ValidationError:
        return ''
    return value


@register.filter
def recorded_pages(book):
    # Use saved base pages, never scored totals or qualification/Theme modifiers.
    if book.get('status') == 'approved' and book.get('approved_pages') is not None:
        return book['approved_pages']
    return book.get('submitted_pages')
