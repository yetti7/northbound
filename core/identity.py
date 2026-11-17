"""Account identifiers and public presentation; ownership always uses user IDs."""
import unicodedata
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connections, transaction


def identifier_key(value):
    return unicodedata.normalize('NFKC', value or '').strip().casefold()


def public_name(user):
    if not user:
        return ''
    profile = getattr(user, 'northbound_profile', None)
    return profile.discord_username if profile and profile.discord_username.strip() else user.get_username()


def resolve_identifier(value):
    key = identifier_key(value)
    if not key:
        return None
    # Python normalization deliberately avoids SQLite's ASCII-only LOWER/iexact.
    matches = [pk for pk, username, email in get_user_model().objects.values_list('pk', 'username', 'email')
               if key in (identifier_key(username), identifier_key(email))]
    return matches[0] if len(matches) == 1 else None


def validate_identifiers(username, email, *, user_id=None, using='default'):
    errors = {}
    keys = {'username': identifier_key(username), 'email': identifier_key(email)}
    for field, key in keys.items():
        if not key:
            errors[field] = 'This field is required.'
    for pk, other_username, other_email in get_user_model().objects.using(using).exclude(pk=user_id).values_list('pk', 'username', 'email'):
        others = {identifier_key(other_username), identifier_key(other_email)} - {''}
        for field, key in keys.items():
            if key and key in others:
                errors[field] = 'This sign-in identifier is already in use by another account.'
    if errors:
        raise ValidationError(errors)


class IdentifierValidationMixin:
    """All interactive user creation/edit forms share the same Unicode policy."""
    def clean(self):
        cleaned = super().clean()
        if 'username' in cleaned and 'email' in cleaned:
            try:
                validate_identifiers(cleaned['username'], cleaned['email'],
                                     user_id=getattr(getattr(self, 'instance', None), 'pk', None))
            except ValidationError as error:
                for field, messages in error.message_dict.items():
                    for message in messages:
                        self.add_error(field, message)
        return cleaned

    @transaction.atomic
    def save(self, *args, **kwargs):
        # SQLite uses BEGIN IMMEDIATE. PostgreSQL serializes identifier edits here,
        # including Unicode equivalences that native LOWER indexes do not cover.
        lock_identifier_writes()
        validate_identifiers(self.cleaned_data['username'], self.cleaned_data['email'],
                             user_id=getattr(getattr(self, 'instance', None), 'pk', None))
        return super().save(*args, **kwargs)


def public_name_expression(user_path):
    from django.db.models import F, Value
    from django.db.models.functions import Coalesce, NullIf, Trim
    prefix = user_path + '__' if user_path else ''
    return Coalesce(NullIf(Trim(F(prefix + 'northbound_profile__discord_username')), Value('')),
                    F(prefix + 'username'))


def public_name_order(user_path):
    """Database presentation ordering with an untruncated username fallback."""
    from django.db.models.functions import Lower
    return Lower(public_name_expression(user_path))


def lock_identifier_writes():
    connection = connections['default']
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute('LOCK TABLE auth_user IN SHARE ROW EXCLUSIVE MODE')
