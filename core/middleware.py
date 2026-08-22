from django.shortcuts import redirect

from .models import UserProfile


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
        if request.user.is_authenticated and not request.user.is_superuser:
            profile, _ = UserProfile.objects.get_or_create(user=request.user)
            if profile.must_change_password and not request.path.startswith(self.EXEMPT_PREFIXES):
                return redirect("password-change")
        return self.get_response(request)
