import base64
import hashlib
import ipaddress
import json
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode, urlparse
from urllib.request import Request
from .integrations.http import urlopen

from django.conf import settings
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .integrations.secrets import TokenDecryptionError, decrypt_token, encrypt_token
from .models import default_hardcover_oauth_scopes


CALLBACK_PATH = "/account/hardcover/oauth/callback/"
OAUTH_SCOPES = tuple(default_hardcover_oauth_scopes())
AUTHORIZATION_ENDPOINT = "https://hardcover.app/oauth2/authorize"
TOKEN_ENDPOINT = "https://api.hardcover.app/oauth2/token"
REVOCATION_ENDPOINT = "https://api.hardcover.app/oauth2/revoke"
OAUTH_ISSUER = "https://api.hardcover.app"
STATE_TTL_SECONDS = 600
SAFE_DECRYPTION_ERROR = "The saved client secret cannot be decrypted. Replace it before enabling OAuth."


class HardcoverOAuthError(Exception):
    def __init__(self, message, *, permanent=False, ambiguous=False):
        super().__init__(message)
        self.permanent = permanent
        self.ambiguous = ambiguous


@dataclass(frozen=True, slots=True)
class OAuthTokenSet:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: object
    scopes: tuple


@dataclass(frozen=True, slots=True)
class CanonicalOAuthUrls:
    website_url: str
    redirect_uri: str
    error: str = ""


def canonical_oauth_urls(public_url=None):
    value = settings.NORTHBOUND_URL if public_url is None else public_url
    value = (value or "").strip().rstrip("/")
    if not value:
        return CanonicalOAuthUrls("", "", "Set NORTHBOUND_URL to the public address people use to reach Northbound.")
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return CanonicalOAuthUrls("", "", "NORTHBOUND_URL must be a complete http:// or https:// URL.")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        return CanonicalOAuthUrls("", "", "NORTHBOUND_URL must be a complete http:// or https:// installation origin without a path.")
    host = parsed.hostname.lower()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if parsed.scheme == "http" and not loopback:
        return CanonicalOAuthUrls("", "", "Hardcover OAuth requires an HTTPS NORTHBOUND_URL except for loopback development.")
    if ":" in host:
        host = f"[{host}]"
    origin = f"{parsed.scheme.lower()}://{host}{f':{port}' if port else ''}"
    return CanonicalOAuthUrls(origin, f"{origin}{CALLBACK_PATH}")


def save_oauth_application(*, application, client_id, client_secret, enabled, urls):
    from .models import ReaderHardcoverConnection
    from django.db import transaction
    previous_client_id = type(application).objects.filter(pk=application.pk).values_list("client_id", flat=True).first() if application.pk else None
    replacing_secret = bool(client_secret and application.encrypted_client_secret)
    application.client_id = client_id
    application.enabled = enabled
    application.configured_scopes = list(OAUTH_SCOPES)
    application.configured_website_url = urls.website_url
    application.configured_redirect_uri = urls.redirect_uri
    if client_secret:
        application.encrypted_client_secret = encrypt_token(client_secret)
    if application.configured_at is None:
        application.configured_at = timezone.now()
    application.is_valid = bool(client_id and application.encrypted_client_secret and not urls.error)
    application.last_error = "" if application.is_valid else (urls.error or "Complete the Client ID and Client Secret configuration.")
    with transaction.atomic():
        application.save()
        if previous_client_id and previous_client_id != client_id:
            ReaderHardcoverConnection.objects.filter(connection_method="oauth").update(
                is_valid=False, reconnect_required=True, encrypted_refresh_token="",
                last_error="The installation's Hardcover Developer App changed. Reconnect your personal account.",
            )
    return application, replacing_secret


def secret_is_usable(application):
    try:
        decrypt_token(application.encrypted_client_secret)
    except TokenDecryptionError:
        return False
    return True


def oauth_application_status(application, urls=None):
    urls = urls or canonical_oauth_urls()
    if application is None:
        return {"key": "not_configured", "label": "Not Configured", "css": "status-neutral", "reasons": []}
    reasons = []
    if not application.client_id:
        reasons.append("Client ID is missing.")
    if not application.encrypted_client_secret:
        reasons.append("Client Secret is missing.")
    elif not secret_is_usable(application):
        reasons.append(SAFE_DECRYPTION_ERROR)
    if urls.error:
        reasons.append(urls.error)
    elif application.configured_website_url and (
        application.configured_website_url != urls.website_url
        or application.configured_redirect_uri != urls.redirect_uri
    ):
        reasons.append("Your Northbound public URL has changed. Update the Redirect URI in your Hardcover Developer App before connecting Readers.")
    if application.last_error and application.last_error not in reasons:
        reasons.append(application.last_error)
    if reasons:
        return {"key": "needs_attention", "label": "Needs Attention", "css": "status-attention", "reasons": reasons}
    if not application.enabled:
        return {"key": "disabled", "label": "Disabled", "css": "status-inactive", "reasons": []}
    return {"key": "configured", "label": "Configured", "css": "status-success", "reasons": []}


def generate_pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorization_url(*, application, redirect_uri, state, code_challenge):
    return f"{AUTHORIZATION_ENDPOINT}?{urlencode({
        'response_type': 'code',
        'client_id': application.client_id,
        'redirect_uri': redirect_uri,
        'scope': ' '.join(OAUTH_SCOPES),
        'state': state,
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256',
    })}"


def _basic_authorization(client_id, client_secret):
    value = base64.b64encode(f"{quote_plus(client_id)}:{quote_plus(client_secret)}".encode("utf-8")).decode("ascii")
    return f"Basic {value}"


@sensitive_variables()
def _oauth_post(endpoint, *, application, data, allow_empty=False):
    try:
        client_secret = decrypt_token(application.encrypted_client_secret)
    except TokenDecryptionError as exc:
        raise HardcoverOAuthError("The Hardcover OAuth application needs attention.", permanent=True) from exc
    body = urlencode(data).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": _basic_authorization(application.client_id, client_secret),
            "User-Agent": "Northbound Reading Challenges/0.1",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            raw_response = response.read()
            payload = json.loads(raw_response.decode("utf-8")) if raw_response else ({} if allow_empty else None)
    except HTTPError as exc:
        error = ""
        try:
            error = json.loads(exc.read().decode("utf-8")).get("error", "")
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            pass
        permanent = error in {"invalid_grant", "invalid_client", "unauthorized_client"}
        raise HardcoverOAuthError(
            "Hardcover rejected the OAuth request.",
            permanent=permanent,
            ambiguous=exc.code >= 500,
        ) from exc
    except (URLError, TimeoutError) as exc:
        raise HardcoverOAuthError("Hardcover could not be reached.", ambiguous=True) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HardcoverOAuthError("Hardcover returned an unreadable OAuth response.", ambiguous=True) from exc
    if not isinstance(payload, dict):
        raise HardcoverOAuthError("Hardcover returned an unexpected OAuth response.", ambiguous=True)
    return payload


@sensitive_variables()
def _parse_token_payload(
    payload, *, requested_scopes=OAUTH_SCOPES, prior_refresh_token="",
    require_refresh_token=False, require_rotated_refresh=False,
):
    access_token = payload.get("access_token")
    token_type = str(payload.get("token_type", ""))
    if not isinstance(access_token, str) or not access_token or token_type.casefold() != "bearer":
        raise HardcoverOAuthError("Hardcover did not return a usable bearer credential.", permanent=True)
    refresh_token = payload.get("refresh_token", prior_refresh_token)
    if not isinstance(refresh_token, str):
        refresh_token = prior_refresh_token
    if require_refresh_token and not refresh_token:
        raise HardcoverOAuthError("Hardcover did not return the required refresh credential.", permanent=True)
    if require_rotated_refresh and (not refresh_token or refresh_token == prior_refresh_token):
        raise HardcoverOAuthError("Hardcover did not rotate the refresh credential safely.", permanent=True)
    raw_scope = payload.get("scope")
    if raw_scope is None:
        scopes = tuple(requested_scopes)
    elif isinstance(raw_scope, str):
        scopes = tuple(part for part in raw_scope.replace(",", " ").split() if part)
    elif isinstance(raw_scope, list) and all(isinstance(item, str) for item in raw_scope):
        scopes = tuple(raw_scope)
    else:
        raise HardcoverOAuthError("Hardcover returned an unreadable granted-scope value.", permanent=True)
    if not set(OAUTH_SCOPES).issubset(scopes):
        raise HardcoverOAuthError("Hardcover did not grant every required Reader permission.", permanent=True)
    expires_at = None
    expires_in = payload.get("expires_in")
    if expires_in is not None:
        try:
            seconds = int(expires_in)
        except (TypeError, ValueError) as exc:
            raise HardcoverOAuthError("Hardcover returned an unreadable token lifetime.", permanent=True) from exc
        if seconds <= 0:
            raise HardcoverOAuthError("Hardcover returned an expired bearer credential.", permanent=True)
        try:
            expires_at = timezone.now() + timedelta(seconds=seconds)
        except OverflowError as exc:
            raise HardcoverOAuthError("Hardcover returned an unreadable token lifetime.", permanent=True) from exc
    elif require_refresh_token:
        expires_at = timezone.now() + timedelta(days=7)
    return OAuthTokenSet(access_token, refresh_token, expires_at, scopes)


@sensitive_variables("code", "code_verifier", "payload")
def exchange_authorization_code(*, application, code, code_verifier, redirect_uri):
    payload = _oauth_post(TOKEN_ENDPOINT, application=application, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    })
    return _parse_token_payload(payload, require_refresh_token=True)


@sensitive_variables("refresh_token", "payload")
def exchange_refresh_token(*, application, refresh_token, granted_scopes):
    payload = _oauth_post(TOKEN_ENDPOINT, application=application, data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    })
    return _parse_token_payload(
        payload,
        requested_scopes=granted_scopes or OAUTH_SCOPES,
        prior_refresh_token=refresh_token,
        require_refresh_token=True,
        require_rotated_refresh=True,
    )


@sensitive_variables("token")
def revoke_oauth_token(*, application, token, token_type_hint):
    if not token:
        return False
    try:
        _oauth_post(
            REVOCATION_ENDPOINT,
            application=application,
            data={"token": token, "token_type_hint": token_type_hint},
            allow_empty=True,
        )
    except HardcoverOAuthError:
        return False
    return True
