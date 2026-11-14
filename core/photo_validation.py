"""Discord references only: never fetch, embed or store photo content."""
import re
from urllib.parse import urlsplit
from django.core.exceptions import ValidationError

DISCORD_MESSAGE_HOSTS = frozenset({'discord.com', 'www.discord.com', 'canary.discord.com', 'ptb.discord.com', 'discordapp.com', 'www.discordapp.com'})


def validate_discord_message_url(value):
    if not value:
        return
    try:
        url = urlsplit(value)
        valid = (value == value.strip() and not any(c.isspace() for c in value)
                 and url.scheme == 'https' and url.hostname in DISCORD_MESSAGE_HOSTS
                 and not url.username and not url.password and url.port is None
                 and not url.query and not url.fragment
                 and re.fullmatch(r'/channels/[0-9]+/[0-9]+/[0-9]+', url.path))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValidationError({'discord_message_url': 'Use an HTTPS Discord message link, or leave this field blank.'})
