import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class TokenDecryptionError(Exception):
    pass


def _fernet():
    configured_key = settings.TOKEN_ENCRYPTION_KEY.strip()
    if configured_key:
        key = configured_key.encode("ascii")
    else:
        digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
        key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_token(token):
    return _fernet().encrypt(token.encode("utf-8")).decode("ascii")


def decrypt_token(encrypted_token):
    try:
        return _fernet().decrypt(encrypted_token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise TokenDecryptionError("The saved token could not be decrypted with the configured encryption key.") from exc
