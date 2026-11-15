from django import template
from django.conf import settings
import re
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

register = template.Library()


@register.filter
def safe_cover_url(value):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    # Only the generated custom-cover namespace is an allowed local image URL.
    # Arbitrary relative/encoded paths and other local media remain disallowed.
    media = settings.MEDIA_URL
    if media.startswith('/') and not media.startswith('//') and value.startswith(media):
        relative = value[len(media):]
        if re.fullmatch(r'tbr-covers/[1-9][0-9]*/[0-9a-f]{32}\.png', relative):
            return value
    try:
        URLValidator(schemes=["http", "https"])(value)
    except ValidationError:
        return ""
    return value
