from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from core.forms import RegularAuthenticationForm
from core.views import NorthboundPasswordChangeView
from reading_challenge.media import serve_media

urlpatterns = [
    path("platform-admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(template_name="registration/login.html", authentication_form=RegularAuthenticationForm), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("accounts/password/change/", NorthboundPasswordChangeView.as_view(), name="password-change"),
    path(f"{settings.MEDIA_URL.strip('/')}/<path:path>", serve_media, name="northbound-media"),
    path("", include("core.urls")),
]
