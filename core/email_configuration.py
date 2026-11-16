"""Saved SMTP transport and intentionally sanitized delivery outcomes."""
import ipaddress
import re
import smtplib
import socket
import ssl
from email.utils import formataddr
from django import forms
from django.core.mail import EmailMessage
from django.core.mail.backends.smtp import EmailBackend
from django.views.decorators.debug import sensitive_variables
from .models import PlatformEmailSettings
from .integrations.secrets import decrypt_token, encrypt_token, TokenDecryptionError


class EmailSettingsForm(forms.ModelForm):
    password = forms.CharField(label='SMTP Password', required=False, strip=False, widget=forms.PasswordInput(render_value=False, attrs={'autocomplete': 'new-password'}), help_text='Leave blank to keep the saved password.')

    class Meta:
        model = PlatformEmailSettings
        fields = ['enabled', 'host', 'port', 'encryption', 'username', 'password', 'sender_name', 'sender_email', 'reply_to', 'password_resets_enabled']
        labels = {'enabled': 'Enable Email', 'host': 'SMTP Host', 'port': 'SMTP Port', 'username': 'SMTP Username', 'password': 'SMTP Password', 'reply_to': 'Reply-To Email', 'password_resets_enabled': 'Enable Password Resets', 'sender_name': 'Sender Name', 'sender_email': 'Sender Email'}

    def clean_host(self):
        host = self.cleaned_data['host'].strip()
        if host:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                try:
                    encoded = host.encode('idna').decode('ascii')
                except UnicodeError:
                    raise forms.ValidationError('Enter a valid SMTP hostname.')
                if len(encoded) > 253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in encoded.rstrip('.').split('.')):
                    raise forms.ValidationError('Enter a hostname without a scheme, path, or port.')
        return host

    def clean_port(self):
        port = self.cleaned_data['port']
        if not 1 <= port <= 65535:
            raise forms.ValidationError('Use a port from 1 to 65535.')
        return port

    def clean_sender_name(self):
        name = self.cleaned_data['sender_name']
        if '\r' in name or '\n' in name:
            raise forms.ValidationError('Use a single-line sender name.')
        return name

    def clean(self):
        data = super().clean()
        if data.get('enabled'):
            for field in ['host', 'sender_email']:
                if not data.get(field):
                    self.add_error(field, 'Required when email is enabled.')
        if data.get('username') and not (data.get('password') or self.instance.encrypted_password):
            self.add_error('password', 'Enter an SMTP password for authenticated SMTP.')
        if data.get('password') and not data.get('username'):
            self.add_error('username', 'Enter the SMTP username for this password.')
        return data

    @sensitive_variables()
    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.cleaned_data.get('password'):
            instance.encrypted_password = encrypt_token(self.cleaned_data['password'])
        if self.has_changed():
            instance.last_test_at = None
            instance.last_test_result = ''
            instance.last_test_error = ''
        if commit:
            instance.save()
        return instance


class TestEmailForm(forms.Form):
    recipient = forms.EmailField(label='Recipient')


def configuration_complete(config):
    if not config or not config.host or not config.sender_email or not 1 <= config.port <= 65535:
        return False
    if config.encryption not in {'starttls', 'ssl', 'none'}:
        return False
    try:
        return not config.username or bool(decrypt_token(config.encrypted_password))
    except (TokenDecryptionError, ValueError):
        return False


@sensitive_variables()
def send_configured_email(config, subject, body, recipient):
    """Return a fixed, secret-free error. Never log SMTP exception text."""
    if not config or not config.enabled:
        return 'Outgoing email is disabled.'
    if not configuration_complete(config):
        return 'SMTP configuration is incomplete or the saved credential cannot be decrypted. Review settings and re-enter the password if needed.'
    try:
        password = decrypt_token(config.encrypted_password) if config.username else ''
        with EmailBackend(host=config.host, port=config.port, username=config.username, password=password,
                          use_tls=config.encryption == 'starttls', use_ssl=config.encryption == 'ssl',
                          timeout=10, fail_silently=False) as backend:
            message = EmailMessage(subject, body, formataddr((config.sender_name, config.sender_email)),
                                   [recipient], connection=backend, reply_to=[config.reply_to] if config.reply_to else None)
            if message.send() != 1:
                return 'The SMTP server did not accept the message.'
    except smtplib.SMTPAuthenticationError:
        return 'SMTP authentication failed. Review the saved credentials.'
    except smtplib.SMTPRecipientsRefused:
        return 'The SMTP server rejected the recipient.'
    except (TimeoutError, socket.timeout):
        return 'The SMTP connection timed out.'
    except ssl.SSLError:
        return 'TLS negotiation failed. Review encryption and server settings.'
    except (OSError, smtplib.SMTPException):
        return 'Could not connect or send using the configured SMTP server. Review host, port, encryption and sender settings.'
    except Exception:
        return 'Email could not be sent. Review the saved configuration.'
    return ''
