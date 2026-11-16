from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from core.email_views import LocalPasswordResetView
from core.forms import RegularAuthenticationForm
from core.views import NorthboundPasswordChangeView
from reading_challenge.media import serve_media

urlpatterns = [
    path("accounts/password/reset/", LocalPasswordResetView.as_view(), name="password_reset"),
    path("accounts/password/reset/done/", auth_views.PasswordResetDoneView.as_view(template_name="registration/northbound_reset_done.html"), name="password_reset_done"),
    path("accounts/password/reset/<uidb64>/<token>/", auth_views.PasswordResetConfirmView.as_view(template_name="registration/northbound_reset_confirm.html"), name="password_reset_confirm"),
    path("accounts/password/reset/complete/", auth_views.PasswordResetCompleteView.as_view(template_name="registration/northbound_reset_complete.html"), name="password_reset_complete"),
    path("platform-admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(template_name="registration/login.html", authentication_form=RegularAuthenticationForm), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("accounts/password/change/", NorthboundPasswordChangeView.as_view(), name="password-change"),
    path(f"{settings.MEDIA_URL.strip('/')}/<path:path>", serve_media, name="northbound-media"),
    path("", include("core.urls")),
]
