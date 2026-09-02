from datetime import timedelta
import hashlib

from django.utils import timezone
from django.db import connection as database_connection, transaction
from django.views.decorators.debug import sensitive_variables

from .integrations.hardcover import HardcoverConnectionError, test_catalog_connection
from .integrations.credentials import BearerCredential, CredentialMethod, CredentialOwner
from .integrations.secrets import TokenDecryptionError, decrypt_token, encrypt_token
from .hardcover_oauth import HardcoverOAuthError, canonical_oauth_urls, exchange_refresh_token, oauth_application_status
from .models import AuditEvent, HardcoverOAuthApplication, ReaderHardcoverConnection


class ReaderHardcoverUnavailable(Exception):
    """The current Reader has no usable personal Hardcover credential."""


DECRYPTION_ERROR = "The saved personal Hardcover token could not be decrypted. Reconnect your account."
VALIDATION_ERROR = "Hardcover could not validate the personal connection. Reconnect or try again later."


def _mark_invalid(connection, message):
    connection.is_valid = False
    connection.last_error = message
    connection.tested_at = timezone.now()
    connection.save(update_fields=["is_valid", "last_error", "tested_at"])


@sensitive_variables("token")
def save_reader_hardcover_connection(user, token):
    """Validate first, then create or replace only this user's credential."""
    test_catalog_connection(token)
    connection, created = ReaderHardcoverConnection.objects.update_or_create(
        user=user,
        defaults={
            "connection_method": ReaderHardcoverConnection.ConnectionMethod.API_KEY,
            "encrypted_token": encrypt_token(token),
            "encrypted_refresh_token": "",
            "token_hint": token[-4:],
            "access_expires_at": None,
            "granted_scopes": [],
            "refreshed_at": None,
            "reconnect_required": False,
            "tested_at": timezone.now(),
            "is_valid": True,
            "last_error": "",
        },
    )
    return connection, created


def _mark_reconnect_required(connection, message, *, clear_refresh=True):
    connection.is_valid = False
    connection.reconnect_required = True
    connection.last_error = message
    connection.tested_at = timezone.now()
    if clear_refresh:
        connection.encrypted_refresh_token = ""
    connection.save(update_fields=[
        "is_valid", "reconnect_required", "last_error", "tested_at", "encrypted_refresh_token",
    ])
    AuditEvent.objects.create(
        actor=connection.user,
        action="reader_hardcover.reconnect_required",
        object_type="ReaderHardcoverConnection",
        object_id=str(connection.pk),
        summary="The personal Hardcover OAuth connection requires reconnection.",
    )


@sensitive_variables("refresh_token", "token_set")
def _refresh_oauth_connection(connection, *, force=False):
    if connection.connection_method != ReaderHardcoverConnection.ConnectionMethod.OAUTH:
        return connection
    application = HardcoverOAuthApplication.objects.first()
    if oauth_application_status(application, canonical_oauth_urls())["key"] != "configured":
        raise ReaderHardcoverUnavailable("The Hardcover OAuth application is unavailable. Ask the Platform Owner to check its configuration.")
    refresh_due = connection.access_expires_at and connection.access_expires_at <= timezone.now() + timedelta(seconds=60)
    if not force and not refresh_due:
        return connection
    if not connection.encrypted_refresh_token:
        if refresh_due:
            _mark_reconnect_required(connection, "The Hardcover authorization expired. Reconnect your account.")
            raise ReaderHardcoverUnavailable("The Hardcover authorization expired. Reconnect your account.")
        return connection
    if any(not getattr(block, "_from_testcase", False) for block in database_connection.atomic_blocks):
        raise ReaderHardcoverUnavailable("Refresh the personal Hardcover connection from My Account before retrying this operation.")
    # Commit a compare-and-swap BEFORE contacting the provider. SQLite has no
    # SELECT FOR UPDATE; a transaction held across rotation can replay a spent
    # token after a crash/rollback. A crash here deliberately requires reconnect.
    original_access = connection.encrypted_token
    claimed = ReaderHardcoverConnection.objects.filter(
        pk=connection.pk, encrypted_refresh_token=connection.encrypted_refresh_token,
        encrypted_token=original_access, is_valid=True, reconnect_required=False,
    ).update(encrypted_refresh_token="", is_valid=False, reconnect_required=True,
             last_error="Authorization refresh was interrupted or is in progress. Try again; reconnect if this persists.")
    if not claimed:
        raise ReaderHardcoverUnavailable("The personal authorization changed or is being refreshed. Try again.")
    try:
        refresh_token = decrypt_token(connection.encrypted_refresh_token)
        token_set = exchange_refresh_token(
            application=application,
            refresh_token=refresh_token,
            granted_scopes=connection.granted_scopes,
        )
    except TokenDecryptionError as exc:
        raise ReaderHardcoverUnavailable(DECRYPTION_ERROR) from exc
    except HardcoverOAuthError as exc:
        raise ReaderHardcoverUnavailable("Hardcover could not safely refresh the saved authorization. Reconnect your account.") from exc
    connection.encrypted_token = encrypt_token(token_set.access_token)
    connection.encrypted_refresh_token = encrypt_token(token_set.refresh_token) if token_set.refresh_token else ""
    connection.token_hint = ""
    connection.access_expires_at = token_set.expires_at
    connection.granted_scopes = list(token_set.scopes)
    connection.refreshed_at = timezone.now()
    connection.tested_at = timezone.now()
    connection.is_valid = True
    connection.reconnect_required = False
    connection.last_error = ""
    fields = [
        "encrypted_token", "encrypted_refresh_token", "token_hint", "access_expires_at",
        "granted_scopes", "refreshed_at", "tested_at", "is_valid", "reconnect_required", "last_error",
    ]
    with transaction.atomic():
        updated = ReaderHardcoverConnection.objects.filter(
            pk=connection.pk, encrypted_token=original_access,
            encrypted_refresh_token="", reconnect_required=True,
        ).update(**{name: getattr(connection, name) for name in fields})
        if not updated:
            raise ReaderHardcoverUnavailable("The personal authorization changed during refresh. Try again.")
        AuditEvent.objects.create(
            actor=connection.user, action="reader_hardcover.refreshed",
            object_type="ReaderHardcoverConnection", object_id=str(connection.pk),
            summary="Refreshed the personal Hardcover OAuth credential.",
        )
    return connection


def get_reader_hardcover_credential(user, *, force_refresh=False):
    """Return only this Reader's bearer credential; never consult a Group."""
    if not getattr(user, "is_authenticated", False) or not user.pk:
        raise ReaderHardcoverUnavailable("A personal Hardcover connection is unavailable.")
    connection = ReaderHardcoverConnection.objects.filter(user=user).first()
    if connection is None or not connection.is_valid or connection.reconnect_required:
        raise ReaderHardcoverUnavailable("A valid personal Hardcover connection is unavailable.")
    connection = _refresh_oauth_connection(connection, force=force_refresh)
    try:
        token = decrypt_token(connection.encrypted_token)
    except TokenDecryptionError as exc:
        _mark_invalid(connection, DECRYPTION_ERROR)
        raise ReaderHardcoverUnavailable(DECRYPTION_ERROR) from exc
    return BearerCredential(
        bearer_token=token,
        method=CredentialMethod(connection.connection_method),
        owner=CredentialOwner.READER,
        connection_fingerprint=hashlib.sha256(connection.encrypted_token.encode()).hexdigest(),
    )


def get_reader_hardcover_token(user):
    """Compatibility wrapper for catalog services that still accept a token."""
    return get_reader_hardcover_credential(user).bearer_token


@sensitive_variables("token")
def test_reader_hardcover_connection(user):
    connection = ReaderHardcoverConnection.objects.filter(user=user).first()
    if connection is None:
        raise ReaderHardcoverUnavailable("Connect your personal Hardcover account first.")
    try:
        credential = get_reader_hardcover_credential(
            user,
            force_refresh=connection.connection_method == ReaderHardcoverConnection.ConnectionMethod.OAUTH,
        )
        token = credential.bearer_token
        test_catalog_connection(token)
    except TokenDecryptionError as exc:
        _mark_invalid(connection, DECRYPTION_ERROR)
        raise ReaderHardcoverUnavailable(DECRYPTION_ERROR) from exc
    except HardcoverConnectionError as exc:
        _mark_invalid(connection, VALIDATION_ERROR)
        raise ReaderHardcoverUnavailable(VALIDATION_ERROR) from exc
    connection.is_valid = True
    connection.last_error = ""
    connection.tested_at = timezone.now()
    connection.save(update_fields=["is_valid", "last_error", "tested_at"])
    return connection
