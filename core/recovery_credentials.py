from django.core.exceptions import ValidationError
from django.views.decorators.debug import sensitive_variables

from .integrations.secrets import TokenDecryptionError, decrypt_token
from .models import HardcoverConnection, ReaderHardcoverConnection
from .recovery import execute_recovery_operation


@sensitive_variables("plaintext")
def sanitized_credential_status(connection):
    """Return operational metadata only; never return credential material or hints."""
    decryptable = True
    try:
        plaintext = decrypt_token(connection.encrypted_token)
    except TokenDecryptionError:
        decryptable = False
    else:
        del plaintext
    if not decryptable:
        health = "Credential data cannot be decrypted"
    elif connection.is_valid:
        health = "Connected"
    else:
        health = "Needs reconnection"
    return {
        "connected": True,
        "health": health,
        "is_valid": connection.is_valid,
        "is_decryptable": decryptable,
        "connected_at": connection.connected_at,
        "updated_at": connection.updated_at,
        "tested_at": connection.tested_at,
    }


def reader_credential_label(connection):
    return f"Reader Hardcover connection #{connection.pk}: {connection.user.username}"


def group_credential_label(connection):
    return f"Group Hardcover connection #{connection.pk}: {connection.group.name}"


def clear_reader_hardcover_connection(*, connection, recovery_request, fail_after_step=None):
    def mutation():
        locked = ReaderHardcoverConnection.objects.select_for_update().select_related("user").get(
            pk=connection.pk,
        )
        status = sanitized_credential_status(locked)
        owner_id = locked.user_id
        locked.delete()
        if fail_after_step == "delete":
            raise RuntimeError("Injected Reader credential recovery failure.")
        return {
            "owner_type": "Reader",
            "owner_id": owner_id,
            "connection_existed": True,
            "sanitized_status_before": status,
            "connection_exists_after": False,
            "reconnect_through": "Reader personal Hardcover connection workflow",
        }

    if recovery_request.tier != 2:
        raise ValidationError("Reader Hardcover credential clearing requires Tier 2 recovery.")
    return execute_recovery_operation(recovery_request, mutation)


def clear_group_hardcover_connection(*, connection, recovery_request, fail_after_step=None):
    def mutation():
        locked = HardcoverConnection.objects.select_for_update().select_related("group").get(
            pk=connection.pk,
        )
        status = sanitized_credential_status(locked)
        owner_id = locked.group_id
        locked.delete()
        if fail_after_step == "delete":
            raise RuntimeError("Injected Group credential recovery failure.")
        return {
            "owner_type": "Group",
            "owner_id": owner_id,
            "connection_existed": True,
            "sanitized_status_before": status,
            "connection_exists_after": False,
            "reconnect_through": "Group Hardcover connection workflow",
        }

    if recovery_request.tier != 2:
        raise ValidationError("Group Hardcover credential clearing requires Tier 2 recovery.")
    return execute_recovery_operation(recovery_request, mutation)
