from django import template
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

register = template.Library()


@register.filter
def safe_cover_url(value):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    try:
        URLValidator(schemes=["http", "https"])(value)
    except ValidationError:
        return ""
    return value
