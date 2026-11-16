import time
from urllib.parse import urlsplit
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth.views import PasswordResetView
from django.db import transaction
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from .hardcover_oauth import canonical_oauth_urls
from .models import PlatformEmailSettings, PasswordRecoveryLimit
from .email_configuration import EmailSettingsForm, TestEmailForm, configuration_complete, send_configured_email


def allowed_request(scope, value, maximum):
    key = salted_hmac('northbound-email-limit', scope + ':' + value).hexdigest()
    window = int(time.time()) // 3600
    with transaction.atomic():
        # Expired fixed-window entries are reused, bounding stored identifiers.
        PasswordRecoveryLimit.objects.filter(window__lt=int(time.time()) // 3600 - 48).delete()
        row, _ = PasswordRecoveryLimit.objects.select_for_update().get_or_create(key=key, defaults={'window': window})
        if row.window != window:
            row.window, row.attempts = window, 0
        row.attempts += 1
        row.save(update_fields=['window', 'attempts'])
        return row.attempts <= maximum


@login_required(login_url='config-login')
@sensitive_post_parameters('password')
@sensitive_variables()
def email_settings(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden('Platform owner access is required.')
    config = PlatformEmailSettings.objects.first()
    form = EmailSettingsForm(instance=config)
    test_form = TestEmailForm(initial={'recipient': request.user.email})
    if request.method == 'POST':
        if request.POST.get('action') == 'test':
            test_form = TestEmailForm(request.POST)
            if test_form.is_valid():
                if not config:
                    messages.error(request, 'Save email settings before testing.')
                elif not allowed_request('test', str(request.user.pk), 5):
                    messages.error(request, 'Please wait before sending another test email.')
                else:
                    error = send_configured_email(config, 'Northbound — Email Configuration Test',
                        'This is a test email from Northbound.\n\nYour outgoing email configuration has successfully connected to the configured mail provider.\n\nNo action is required.', test_form.cleaned_data['recipient'])
                    # Do not certify a different configuration saved during the SMTP call.
                    snapshot = {f.name: getattr(config, f.name) for f in config._meta.fields if not f.name.startswith('last_test')}
                    PlatformEmailSettings.objects.filter(**snapshot).update(
                        last_test_at=timezone.now(), last_test_result='failed' if error else 'accepted', last_test_error=error)
                    (messages.error if error else messages.success)(request, error or 'The SMTP server accepted the message. Inbox delivery has not been confirmed.')
                return redirect('platform-email-settings')
        else:
            form = EmailSettingsForm(request.POST, instance=config)
            if form.is_valid():
                form.save()
                messages.success(request, 'Email settings saved. Changed settings require a new test.')
                return redirect('platform-email-settings')
    return render(request, 'core/platform_email_settings.html', {'form': form, 'test_form': test_form, 'email_config': config, 'email_complete': configuration_complete(config)})


class LocalPasswordResetForm(PasswordResetForm):
    @sensitive_variables()
    def send_mail(self, subject_template_name, email_template_name, context, from_email, to_email, html_email_template_name=None):
        config = PlatformEmailSettings.objects.first()
        if config and config.password_resets_enabled:
            send_configured_email(config, 'Northbound password reset', render_to_string('registration/northbound_reset_email.txt', context), to_email)


class LocalPasswordResetView(PasswordResetView):
    form_class = LocalPasswordResetForm
    template_name = 'registration/northbound_reset_form.html'
    success_url = reverse_lazy('password_reset_done')

    @sensitive_variables()
    def form_valid(self, form):
        ip_allowed = allowed_request('reset-ip', self.request.META.get('REMOTE_ADDR', ''), 10)
        email_allowed = allowed_request('reset-email', form.cleaned_data['email'].casefold(), 3)
        config = PlatformEmailSettings.objects.first()
        origin_settings = canonical_oauth_urls()
        origin = urlsplit(origin_settings.website_url)
        if (not origin_settings.error and ip_allowed and email_allowed and config and config.enabled and config.password_resets_enabled
                and origin.scheme in {'http', 'https'} and origin.netloc and not origin.username
                and not origin.password and not origin.query and not origin.fragment and origin.path in {'', '/'}):
            form.save(domain_override=origin.netloc, use_https=origin.scheme == 'https', request=None)
        # Identical response for unknown/inactive/disabled/limited/failed deliveries.
        from django.http import HttpResponseRedirect
        return HttpResponseRedirect(self.get_success_url())
