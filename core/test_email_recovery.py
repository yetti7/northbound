import smtplib
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import urlsplit
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.test import TestCase, override_settings, Client
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from .models import PlatformEmailSettings, PasswordRecoveryLimit
from .email_configuration import EmailSettingsForm, send_configured_email
from .integrations.secrets import decrypt_token


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'], NORTHBOUND_URL='https://books.example.org')
class EmailRecoveryTests(TestCase):
    def setUp(self):
        U=get_user_model()
        self.admin=U.objects.create_superuser('owner','owner@example.org','password')
        self.reader=U.objects.create_user('reader','reader@example.org','old-password')
        self.data={'enabled':'on','host':'smtp.example.org','port':587,'encryption':'starttls','username':'smtp-user','password':'smtp-secret','sender_name':'Northbound','sender_email':'mail@example.org','reply_to':'reply@example.org','password_resets_enabled':'on'}
        form=EmailSettingsForm(self.data);self.assertTrue(form.is_valid(),form.errors);self.config=form.save()

    def login(self):self.client.force_login(self.admin)
    def reset(self,email='reader@example.org',**kwargs):return self.client.post(reverse('password_reset'),{'email':email},**kwargs)

    def test_admin_page_and_prefilled_recipient(self):
        self.login();r=self.client.get(reverse('platform-email-settings'))
        self.assertContains(r,'owner@example.org');self.assertNotContains(r,'smtp-secret');self.assertNotContains(r,self.config.encrypted_password)

    def test_anonymous_redirect_and_reader_forbidden_get_and_post(self):
        self.assertEqual(self.client.get(reverse('platform-email-settings')).status_code,302)
        self.client.force_login(self.reader)
        for method in ['get','post']:
            self.assertEqual(getattr(self.client,method)(reverse('platform-email-settings')).status_code,403)

    def test_staff_without_platform_authority_forbidden(self):
        self.reader.is_staff=True;self.reader.save();self.client.force_login(self.reader)
        self.assertEqual(self.client.post(reverse('platform-email-settings'),self.data).status_code,403)

    def test_encryption_blank_preservation_and_replacement(self):
        old=self.config.encrypted_password;self.assertNotEqual(old,'smtp-secret');self.assertEqual(decrypt_token(old),'smtp-secret')
        for password,expected in [('', 'smtp-secret'),('replacement','replacement')]:
            form=EmailSettingsForm(dict(self.data,password=password),instance=self.config);self.assertTrue(form.is_valid(),form.errors);self.config=form.save();self.assertEqual(decrypt_token(self.config.encrypted_password),expected)

    def test_invalid_smtp_configuration(self):
        for key,value in [('host','https://smtp.example.org/path'),('host','bad host'),('port',0),('port',65536),('encryption','both'),('sender_email','bad'),('reply_to','bad'),('sender_name','bad\r\nHeader: yes')]:
            with self.subTest(key=key,value=value):self.assertFalse(EmailSettingsForm(dict(self.data,**{key:value}),instance=self.config).is_valid())

    def test_required_fields_and_credentials(self):
        for changes in [{'host':''},{'sender_email':''},{'password':''},{'username':''}]:
            with self.subTest(changes=changes):self.assertFalse(EmailSettingsForm(dict(self.data,**changes)).is_valid())

    def test_disabled_incomplete_configuration_can_save(self):
        f=EmailSettingsForm({'port':587,'encryption':'starttls','sender_name':'Northbound'})
        self.assertTrue(f.is_valid(),f.errors)

    def test_changed_configuration_invalidates_status(self):
        self.config.last_test_at=timezone.now();self.config.last_test_result='accepted';self.config.save()
        f=EmailSettingsForm(dict(self.data,password='',host='new.example.org'),instance=self.config);self.assertTrue(f.is_valid());f.save();self.config.refresh_from_db();self.assertIsNone(self.config.last_test_at);self.assertEqual(self.config.last_test_result,'')

    def test_unchanged_configuration_retains_status(self):
        self.config.last_test_result='accepted';self.config.save();f=EmailSettingsForm(dict(self.data,password=''),instance=self.config);self.assertTrue(f.is_valid());f.save();self.config.refresh_from_db();self.assertEqual(self.config.last_test_result,'accepted')

    @patch('core.email_configuration.EmailBackend')
    def test_transport_uses_saved_values(self,backend):
        backend.return_value.__enter__.return_value.send_messages.return_value=1
        self.assertEqual(send_configured_email(self.config,'subject','body','recipient@example.org'),'')
        self.assertEqual(backend.call_args.kwargs['password'],'smtp-secret');self.assertTrue(backend.call_args.kwargs['use_tls']);self.assertFalse(backend.call_args.kwargs['use_ssl']);self.assertEqual(backend.call_args.kwargs['timeout'],10)

    @patch('core.email_configuration.EmailBackend')
    def test_disabled_and_bad_key_never_connect(self,backend):
        self.config.enabled=False;self.assertTrue(send_configured_email(self.config,'s','b','a@b.org'));self.config.enabled=True;self.config.encrypted_password='unreadable';self.assertIn('re-enter',send_configured_email(self.config,'s','b','a@b.org'));backend.assert_not_called()

    @patch('core.email_configuration.EmailBackend')
    def test_sanitized_transport_failures(self,backend):
        for error,word in [(smtplib.SMTPAuthenticationError(535,b'smtp-secret'),'authentication'),(smtplib.SMTPRecipientsRefused({'a':'smtp-secret'}),'recipient'),(TimeoutError('smtp-secret'),'timed out')]:
            backend.return_value.__enter__.side_effect=error
            result=send_configured_email(self.config,'s','b','a@b.org');self.assertIn(word,result);self.assertNotIn('smtp-secret',result)

    @patch('core.email_views.send_configured_email',return_value='')
    def test_test_email_success_status_and_recipient(self,send):
        self.login();r=self.client.post(reverse('platform-email-settings'),{'action':'test','recipient':'alternate@example.org'},follow=True)
        self.assertContains(r,'SMTP server accepted');self.assertEqual(send.call_args.args[-1],'alternate@example.org');self.config.refresh_from_db();self.assertEqual(self.config.last_test_result,'accepted');self.assertIsNotNone(self.config.last_test_at)

    @patch('core.email_views.send_configured_email',return_value='SMTP authentication failed. Review the saved credentials.')
    def test_test_email_failure_status(self,send):
        self.login();self.client.post(reverse('platform-email-settings'),{'action':'test','recipient':'test@example.org'});self.config.refresh_from_db();self.assertEqual(self.config.last_test_result,'failed');self.assertIn('authentication',self.config.last_test_error)

    def test_csrf_protection(self):
        c=Client(enforce_csrf_checks=True);c.force_login(self.admin);self.assertEqual(c.post(reverse('platform-email-settings'),{'action':'test','recipient':'test@example.org'}).status_code,403)

    def test_forgot_link_and_form(self):
        self.assertContains(self.client.get(reverse('login')),'Forgot your password?');self.assertContains(self.client.get(reverse('password_reset')),'registered email')

    @patch('core.email_views.send_configured_email',return_value='')
    def test_valid_reset_uses_configured_origin_not_host(self,send):
        with override_settings(ALLOWED_HOSTS=['evil.example']):r=self.reset(HTTP_HOST='evil.example')
        self.assertRedirects(r,reverse('password_reset_done'),fetch_redirect_response=False)
        body=send.call_args.args[2];self.assertIn('https://books.example.org/accounts/password/reset/',body);self.assertNotIn('evil.example',body);self.assertIn('12 hours',body)

    @patch('core.email_views.send_configured_email',return_value='')
    def test_unknown_inactive_unusable_same_response(self,send):
        baseline=self.reset('unknown@example.org')
        self.reader.is_active=False;self.reader.save();r=self.reset();self.assertEqual(r.url,baseline.url);send.assert_not_called()
        self.reader.is_active=True;self.reader.set_unusable_password();self.reader.save();r=self.reset();self.assertEqual(r.url,baseline.url);send.assert_not_called()

    @patch('core.email_views.send_configured_email',return_value='')
    def test_disabled_email_or_resets_do_not_send(self,send):
        for field in ['enabled','password_resets_enabled']:
            setattr(self.config,field,False);self.config.save();self.assertEqual(self.reset().status_code,302);setattr(self.config,field,True)
        send.assert_not_called()

    @patch('core.email_views.send_configured_email',return_value='failure')
    def test_delivery_failure_remains_generic(self,send):
        self.assertRedirects(self.reset(),reverse('password_reset_done'));self.assertContains(self.client.get(reverse('password_reset_done')),'If recovery is available')

    @patch('core.email_views.send_configured_email',return_value='')
    def test_invalid_or_missing_origin_fails_closed(self,send):
        for origin in ['', 'https://user:secret@example.org', 'https://example.org/path']:
            with override_settings(NORTHBOUND_URL=origin):self.reset()
        send.assert_not_called()

    @patch('core.email_views.send_configured_email',return_value='')
    def test_email_rate_limit_and_hashed_identifiers(self,send):
        for _ in range(5):self.assertEqual(self.reset().status_code,302)
        self.assertEqual(send.call_count,3)
        for key in PasswordRecoveryLimit.objects.values_list('key',flat=True):self.assertNotIn('reader',key);self.assertNotIn('127.0.0.1',key)

    @patch('core.email_views.send_configured_email',return_value='')
    def test_ip_rate_limit(self,send):
        for i in range(11):self.reset('unknown%s@example.org'%i)
        self.reset();send.assert_not_called()

    def test_twelve_hour_expiration_and_password_invalidation(self):
        self.assertEqual(__import__('django.conf',fromlist=['settings']).settings.PASSWORD_RESET_TIMEOUT,43200)
        now=timezone.now().replace(tzinfo=None)
        with patch.object(default_token_generator,'_now',return_value=now):token=default_token_generator.make_token(self.reader)
        with patch.object(default_token_generator,'_now',return_value=now+timedelta(seconds=43199)):self.assertTrue(default_token_generator.check_token(self.reader,token))
        with patch.object(default_token_generator,'_now',return_value=now+timedelta(seconds=43201)):self.assertFalse(default_token_generator.check_token(self.reader,token))
        self.reader.set_password('changed');self.reader.save();self.assertFalse(default_token_generator.check_token(self.reader,token))

    def test_invalid_token_and_successful_reset(self):
        uid=urlsafe_base64_encode(force_bytes(self.reader.pk));url=reverse('password_reset_confirm',args=[uid,'bad-token']);self.assertContains(self.client.get(url),'invalid or expired')
        token=default_token_generator.make_token(self.reader);r=self.client.get(reverse('password_reset_confirm',args=[uid,token]));self.assertEqual(r.status_code,302)
        r=self.client.post(r.url,{'new_password1':'NewStrongPass!489','new_password2':'NewStrongPass!489'});self.assertRedirects(r,reverse('password_reset_complete'));self.reader.refresh_from_db();self.assertTrue(self.reader.check_password('NewStrongPass!489'));self.assertFalse(self.reader.check_password('old-password'));self.assertFalse(default_token_generator.check_token(self.reader,token))

    def test_navigation_uses_shared_count_and_keeps_owner_management(self):
        self.login();r=self.client.get(reverse('config-dashboard'));self.assertContains(r,'Email Settings');self.assertContains(r,'Platform Owners')
        html=r.content.decode();menu=html.split('account-menu-panel')[1].split('</details>')[0];self.assertNotIn('Platform Owners',menu);self.assertIn('Needs Attention',menu);self.assertIn(reverse('needs-attention'),menu);self.assertContains(r,'needs-attention-link');self.assertEqual(self.client.get(reverse('platform-owner-list')).status_code,200)
        total=r.context['needs_attention']['total']
        if total:self.assertIn(str(total),menu)
        self.client.force_login(self.reader);r=self.client.get(reverse('account'));menu=r.content.decode().split('account-menu-panel')[1].split('</details>')[0];self.assertNotIn('Platform Administration',menu);self.assertNotIn('Needs Attention',menu)

    def test_reset_access_log_redaction(self):
        import logging
        from .email_logging import ResetLinkRedaction
        record=logging.LogRecord('gunicorn.access',20,'',0,'%s',('GET /accounts/password/reset/MQ/secret-token/ HTTP/1.1',),None)
        ResetLinkRedaction().filter(record);self.assertNotIn('secret-token',record.getMessage());self.assertIn('[redacted]',record.getMessage())

    @patch('core.email_views.send_configured_email',return_value='')
    def test_duplicate_emails_follow_django_eligible_account_behavior(self,send):
        get_user_model().objects.create_user('second','reader@example.org','password')
        self.reset();self.assertEqual(send.call_count,2)

    @patch('core.email_configuration.EmailBackend')
    def test_tls_modes_and_provider_changes(self,backend):
        backend.return_value.__enter__.return_value.send_messages.return_value=1
        for mode,tls,ssl in [('starttls',True,False),('ssl',False,True),('none',False,False)]:
            self.config.encryption=mode;self.config.host='another.example.org';self.config.port=465
            self.assertEqual(send_configured_email(self.config,'s','b','a@b.org'),'');kwargs=backend.call_args.kwargs;self.assertEqual(kwargs['host'],'another.example.org');self.assertEqual((kwargs['use_tls'],kwargs['use_ssl']),(tls,ssl))

    @patch('core.email_views.send_configured_email')
    def test_test_get_does_not_send_and_invalid_recipient_rejected(self,send):
        self.login();self.client.get(reverse('platform-email-settings'),{'action':'test'});r=self.client.post(reverse('platform-email-settings'),{'action':'test','recipient':'invalid'});self.assertEqual(r.status_code,200);send.assert_not_called()

    @patch('core.email_views.send_configured_email',return_value='')
    def test_expired_rate_window_allows_new_attempts(self,send):
        for _ in range(4):self.reset()
        PasswordRecoveryLimit.objects.update(window=0);self.reset();self.assertEqual(send.call_count,4)

    def test_weak_password_rejected(self):
        uid=urlsafe_base64_encode(force_bytes(self.reader.pk));token=default_token_generator.make_token(self.reader);r=self.client.get(reverse('password_reset_confirm',args=[uid,token]));r=self.client.post(r.url,{'new_password1':'123','new_password2':'123'});self.assertEqual(r.status_code,200);self.reader.refresh_from_db();self.assertTrue(self.reader.check_password('old-password'))

    def test_admin_post_persists_configuration_without_password_in_html(self):
        self.login();r=self.client.post(reverse('platform-email-settings'),dict(self.data,host='updated.example.org'),follow=True);self.assertContains(r,'Email settings saved');self.assertNotContains(r,'smtp-secret');self.config.refresh_from_db();self.assertEqual(self.config.host,'updated.example.org')

    @patch('core.email_views.send_configured_email',return_value='')
    def test_concurrent_configuration_change_does_not_certify_new_settings(self,send):
        def changed(*args):
            PlatformEmailSettings.objects.filter(pk=self.config.pk).update(host='changed.example.org')
            return ''
        send.side_effect=changed;self.login();self.client.post(reverse('platform-email-settings'),{'action':'test','recipient':'test@example.org'});self.config.refresh_from_db();self.assertIsNone(self.config.last_test_at)

    @patch('core.email_configuration.EmailBackend')
    def test_sanitized_tls_and_configuration_failures(self,backend):
        import ssl
        for error in [ssl.SSLError('smtp-secret'),smtplib.SMTPException('smtp-secret'),ValueError('smtp-secret')]:
            backend.return_value.__enter__.side_effect=error;result=send_configured_email(self.config,'s','b','a@b.org');self.assertTrue(result);self.assertNotIn('smtp-secret',result)

    def test_failed_form_never_redisplays_submitted_password(self):
        self.login();r=self.client.post(reverse('platform-email-settings'),dict(self.data,host='invalid/host'));self.assertEqual(r.status_code,200);self.assertNotContains(r,'smtp-secret')

    @patch('core.context_processors.needs_attention_summary',return_value={'available':True,'total':7})
    def test_dropdown_and_desktop_share_nonzero_count(self,summary):
        self.login();r=self.client.get(reverse('config-dashboard'));html=r.content.decode();menu=html.split('account-menu-panel')[1].split('</details>')[0]
        self.assertIn('needs-attention-count">7</span>',menu);self.assertEqual(html.count('needs-attention-count">7</span>'),2)

    def test_expired_link_is_rejected_by_confirmation_view(self):
        now=timezone.now().replace(tzinfo=None)
        with patch.object(default_token_generator,'_now',return_value=now):token=default_token_generator.make_token(self.reader)
        uid=urlsafe_base64_encode(force_bytes(self.reader.pk))
        with patch.object(default_token_generator,'_now',return_value=now+timedelta(hours=13)):
            self.assertContains(self.client.get(reverse('password_reset_confirm',args=[uid,token])),'invalid or expired')
