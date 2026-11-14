"""Presentation-only builders for explicit manual Discord messages.

Limits: https://discord.com/developers/docs/resources/message#embed-object-embed-limits
Use conservative UTF-16 units, including astral emoji, for every text budget.
"""
from copy import deepcopy
import ipaddress
import re
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from . import discord_notifications as discord

COMMUNITY_COLOR = 0x0D9488
MODERATOR_COLOR = 0xD97706
SUCCESS_COLOR = 0x16A34A
FOOTER = 'Northbound • Reading Challenges, Together'
TEST_FOOTER = 'Northbound • Discord Integration Test'
MENTIONS = {'parse': [], 'users': [], 'roles': [], 'replied_user': False}
REVIEW_FIELDS = (('books','📖 Books'), ('themes','🎨 Themes'), ('immersive','📚 Immersive Reading'),
    ('bingo','🎯 Bingo'), ('scavenger','🔎 Scavenger Hunt'), ('photos','📸 Photos'))


def units(text):
    return len(text.encode('utf-16-le')) // 2


def clip(text, limit):
    if units(text) <= limit:
        return text
    result = ''
    for char in text:
        if units(result) + units(char) > limit - 1:
            break
        result += char
    return result + '…'


def name(value, *, markdown=False):
    text = ' '.join(str(value).encode('utf-8', errors='replace').decode().split()) or 'Unnamed'
    return re.sub(r'([\\`*_~|<>\[\]()])', r'\\\1', text) if markdown else text


def application_url(route, args, *, public=False):
    """Use configured origin and existing routes; omit unsafe public/local links.

    No DNS/network lookup or inferred public-sharing policy. A private origin can
    appear only in the authorized private Moderator channel, never Community.
    """
    base = settings.NORTHBOUND_URL.rstrip('/')
    try:
        parsed = urlsplit(base)
        host = (parsed.hostname or '').lower().rstrip('.')
        if (parsed.scheme not in {'http', 'https'} or not host or parsed.username or parsed.password
                or parsed.path not in {'', '/'} or parsed.query or parsed.fragment):
            return ''
        parsed.port  # Reject malformed port numbers.
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if not all(re.fullmatch(r'(?!-)[a-z0-9-]{1,63}(?<!-)', label) for label in host.encode('idna').decode().split('.')):
                return ''
            if public and ('.' not in host or re.fullmatch(r'(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*', host) or host.endswith(('.localhost', '.local', '.internal', '.lan', '.home', '.home.arpa', '.localdomain', '.onion', '.test', '.invalid', '.example'))):
                return ''
        else:
            if public and (not address.is_global or address.is_multicast):
                return ''
        result = base + reverse(route, args=args)
        if len(result) > 1800 or any(char in result for char in (' ', '(', ')', '<', '>')):
            return ''
        return result
    except (ValueError, UnicodeError):
        return ''


def embed(*, group, title, description, color, timestamp, footer=FOOTER):
    return {'title': title, 'description': description, 'color': color,
        'author': {'name': clip(name(group.name), 256)},
        'timestamp': timestamp, 'footer': {'text': footer}}


def embed_units(value):
    return (units(value.get('title', '')) + units(value.get('description', ''))
        + units(value.get('author', {}).get('name', '')) + units(value.get('footer', {}).get('text', ''))
        + sum(units(field['name']) + units(field['value']) for field in value.get('fields', [])))


def link_field(label, url):
    value = f'[{label}]({url})'
    return {'name': '🔗 Northbound', 'value': value, 'inline': False} if url and units(value) <= 1024 else None


def field_chunks(label, lines):
    """Keep every category/count; split values instead of truncating review data."""
    values = []
    current = ''
    for line in lines:
        remaining = line
        while units(remaining) > 1024:
            # Counts normally fit on one line. Even unusually large values retain
            # all text; only an overlong *display name* may be visibly ellipsized.
            part = ''
            for char in remaining:
                if units(part) + units(char) > 1024:
                    break
                part += char
            if current:
                values.append(current)
                current = ''
            values.append(part)
            remaining = remaining[len(part):]
        combined = current + ('\n' if current else '') + remaining
        if units(combined) > 1024:
            values.append(current)
            current = remaining
        else:
            current = combined
    if current:
        values.append(current)
    return [{'name': clip(label + (' (continued)' if index else ''), 256), 'value': value, 'inline': False}
        for index, value in enumerate(values)]


def paginate(base, fields, link=None):
    """25 fields and 6000 text units per embed; reserve room for page labels."""
    pages = []
    current = []
    links = [link] if link else []
    for field in fields:
        candidate = {**base, 'fields': current + [field] + links}
        if len(candidate['fields']) > 25 or embed_units(candidate) > 5900:
            if not current:
                raise ValidationError('A review section cannot fit in a Discord embed.')
            pages.append({**base, 'fields': current + links})
            current = []
        current.append(field)
    if current or not pages:
        pages.append({**base, 'fields': current + links})
    return pages


def pack(embeds):
    """Pack without exceeding 10 embeds / 6000 units across the whole message."""
    payloads = []
    current = []
    size = 0
    for value in embeds:
        count = embed_units(value)
        fields = value.get('fields', [])
        if (count > 6000 or units(value['title']) > 256 or units(value['description']) > 4096
                or units(value.get('author', {}).get('name', '')) > 256
                or units(value.get('footer', {}).get('text', '')) > 2048 or len(fields) > 25
                or any(not field['name'] or not field['value'] or units(field['name']) > 256
                       or units(field['value']) > 1024 for field in fields)):
            raise ValidationError('A Discord embed exceeds the text limit.')
        if len(current) == 10 or size + count > 6000:
            payloads.append({'embeds': current, 'allowed_mentions': deepcopy(MENTIONS)})
            current = []
            size = 0
        current.append(value)
        size += count
    if current:
        payloads.append({'embeds': current, 'allowed_mentions': deepcopy(MENTIONS)})
    return payloads


def community_status_embed(group, timestamp):
    value = embed(group=group, title='📚 Northbound — Community Update',
        description='Your next reading adventure starts here.\nVisit Northbound for the latest updates available to you.',
        color=COMMUNITY_COLOR, timestamp=timestamp)
    link = link_field('View Northbound', application_url('group-detail', [group.slug], public=True))
    if link:
        value['fields'] = [link]
    return value


def moderator_status_embeds(group, timestamp, *, summary=None, selected_only=False):
    if summary is None:
        summary = discord.group_pending_review_counts(group)
    if not summary['total']:
        scope = ' in the selected report categories' if selected_only else ''
        value = embed(group=group, title='✅ All Caught Up!',
            description=f'There are currently no pending reviews{scope} for **{clip(name(group.name, markdown=True), 1000)}**.',
            color=SUCCESS_COLOR, timestamp=timestamp)
        link = link_field('View Northbound', application_url('group-detail', [group.slug]))
        if link:
            value['fields'] = [link]
        return [value]
    result = []
    for row in summary['challenges']:
        fields = []
        for team in row['teams']:
            lines = [f'{label} — {team[key]}' for key, label in REVIEW_FIELDS if team[key]]
            if not lines:
                continue
            prefix = '📋 ' if team['team_name'] == 'Unassigned / Group Reviews' else '⚔️ '
            fields.extend(field_chunks(prefix + name(team['team_name'], markdown=True), lines))
        if not fields:
            continue
        challenge = f'**{clip(name(row["challenge_name"], markdown=True), 1000)}**'
        count = row['total']
        total = f"**{count} review {'task needs' if count == 1 else 'tasks need'} attention**"
        scope = '\n_Selected categories only._' if selected_only else ''
        description = challenge + '\n' + total + scope
        # Keep Group identity in the title once, rather than repeating it in an
        # author line and adding equivalent Group/Challenge totals.
        title = '📚 ' + clip(name(group.name), 195) + ' — Pending Reviews'
        value = embed(group=group, title=title, description=description,
            color=MODERATOR_COLOR, timestamp=timestamp)
        value.pop('author')
        link = link_field('Open Review Queue', application_url('review-queue', [group.slug, row['challenge_id']]))
        pages = paginate(value, fields, link)
        for continuation in pages[1:]:
            continuation['description'] = challenge + '\nReview tasks continued.' + scope
        result.extend(pages)
    if not result:
        raise ValidationError('Pending review details are unavailable.')
    if len(result) > 1:
        for index, value in enumerate(result, 1):
            value['title'] += f' · {index}/{len(result)}'
    return result


def manual_payloads(group, destination, action):
    timestamp = timezone.now().isoformat()
    if action == 'test':
        label = 'Community Announcements' if destination == 'community' else 'Moderator Notifications'
        value = embed(group=group, title='✅ Northbound — Connection Successful',
            description=f'**{label}**\nYour webhook is connected and receiving messages.',
            color=SUCCESS_COLOR, timestamp=timestamp, footer=TEST_FOOTER)
        link = link_field('View Northbound', application_url('group-detail', [group.slug], public=destination=='community'))
        if link:
            value['fields'] = [link]
        return pack([value])
    if destination == 'community':
        from .discord_community import community_embeds
        from .discord_scheduling import COMMUNITY_CATEGORIES
        return pack(community_embeds(group,[key for key,_ in COMMUNITY_CATEGORIES],send_empty=True))
    return pack(moderator_status_embeds(group, timestamp))
