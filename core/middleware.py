from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.shortcuts import redirect

from .models import UserProfile
from .health import health_response


class InternalHealthCheckMiddleware:
    """Handle only same-container health probes before normal host validation."""

    LOOPBACK_ADDRESSES = {"127.0.0.1", "::1"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.method in {"GET", "HEAD"}
            and request.path == "/health/"
            and request.META.get("REMOTE_ADDR") in self.LOOPBACK_ADDRESSES
        ):
            return health_response()
        return self.get_response(request)


class RequestSizeLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        content_length = request.META.get("CONTENT_LENGTH", "")
        try:
            request_size = int(content_length) if content_length else 0
        except ValueError:
            return HttpResponse("Invalid Content-Length header.", status=400, content_type="text/plain")
        limit = settings.NORTHBOUND_MAX_REQUEST_BYTES
        if request.path == "/config/settings/restore/" and request.user.is_authenticated and request.user.is_superuser:
            limit = settings.NORTHBOUND_MAX_BACKUP_BYTES
        if request_size > limit:
            return HttpResponse("Request body is too large.", status=413, content_type="text/plain")
        return self.get_response(request)


class RequirePlatformSetupMiddleware:
    EXEMPT_PREFIXES = ("/setup/", "/health/", "/static/", "/media/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith(self.EXEMPT_PREFIXES):
            if not get_user_model().objects.exists():
                return redirect("setup")
        return self.get_response(request)


class RequirePasswordChangeMiddleware:
    EXEMPT_PREFIXES = (
        "/accounts/password/change/",
        "/accounts/logout/",
        "/static/",
        "/media/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/health/":
            return self.get_response(request)
        if request.user.is_authenticated and not request.user.is_superuser:
            profile, _ = UserProfile.objects.get_or_create(user=request.user)
            if profile.must_change_password and not request.path.startswith(self.EXEMPT_PREFIXES):
                return redirect("password-change")
        return self.get_response(request)
