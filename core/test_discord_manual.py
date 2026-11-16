"""Discord Stage A tests: synthetic webhooks and mocked transport only."""
import io
import json
import uuid
from datetime import timedelta
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError, URLError

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, Client, override_settings
from django.utils import timezone

from . import test_discord_notifications as fixture
from .discord_notifications import configure_notifications, send_webhook
from .discord_manual import (deliver_manual, destination_diagnostics, manual_request_token,
    read_manual_request, _payload)
from .integrations.secrets import decrypt_token
from .models import (AuditEvent, GroupDiscordNotification, DiscordDigestDelivery,
    DiscordManualDelivery, Membership)


@override_settings(NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=True, NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT=True)
class DiscordManualTests(TestCase):
    setUp = fixture.DiscordNotificationTests.setUp
    book = fixture.DiscordNotificationTests.book

    def configure(self, destination='moderator', **changes):
        return configure_notifications(group=self.group, actor=self.owner, destination=destination,
            enabled=changes.pop('enabled',True), webhook_url=changes.pop('webhook_url',
                fixture.WEBHOOK if destination=='moderator' else fixture.REPLACEMENT), **changes)

    def deliver(self, destination='moderator', action='test', **changes):
        return deliver_manual(group=changes.pop('group',self.group), actor=changes.pop('actor',self.owner),
            destination=destination, action=action, request_id=changes.pop('request_id',uuid.uuid4()), **changes)

    def token(self, destination='moderator', action='test', actor=None):
        return manual_request_token(group=self.group,actor=actor or self.owner,destination=destination,action=action)

    def post(self, destination='moderator', action='test', token=None):
        return self.client.post(self.url, {'destination':destination,'action':action,
            'request_token':token or self.token(destination,action)})

    def test_independent_encryption_keep_replace_clear_and_legacy_state(self):
        legacy=configure_notifications(group=self.group,actor=self.owner,enabled=True,webhook_url=fixture.WEBHOOK)
        ciphertext=legacy.encrypted_webhook
        self.assertFalse(legacy.community_enabled);self.assertFalse(legacy.moderator_enabled)
        self.configure('community');self.configure('moderator',webhook_url='')
        config=GroupDiscordNotification.objects.get(group=self.group)
        self.assertEqual(config.encrypted_webhook,ciphertext)
        self.assertEqual(decrypt_token(config.encrypted_community_webhook),fixture.REPLACEMENT)
        self.assertTrue(config.enabled);self.assertTrue(config.community_enabled);self.assertTrue(config.moderator_enabled)
        self.configure('community',enabled=False,webhook_url='')
        config.refresh_from_db();self.assertTrue(config.encrypted_community_webhook)
        self.configure('community',webhook_url='',remove=True)
        config.refresh_from_db();self.assertFalse(config.community_enabled);self.assertFalse(config.encrypted_community_webhook)
        self.assertEqual(config.encrypted_webhook,ciphertext);self.assertTrue(config.enabled)
        self.assertFalse(DiscordManualDelivery.objects.exists());self.assertFalse(DiscordDigestDelivery.objects.exists())
        for row in AuditEvent.objects.all():
            self.assertNotIn(fixture.WEBHOOK,row.summary);self.assertNotIn(fixture.REPLACEMENT,row.summary)

    @patch('core.discord_notifications.send_webhook')
    def test_saving_never_sends_or_enables_hourly_digest(self,send):
        self.client.force_login(self.owner)
        for destination in ('community','moderator'):
            response=self.client.post(self.url,{'destination':destination,'action':'save','enabled':'on','webhook_url':fixture.WEBHOOK})
            self.assertEqual(response.status_code,302)
        config=GroupDiscordNotification.objects.get(group=self.group)
        self.assertFalse(config.enabled);self.assertTrue(config.community_enabled and config.moderator_enabled)
        send.assert_not_called();self.assertFalse(DiscordManualDelivery.objects.exists())

    @patch('core.discord_notifications.send_webhook')
    def test_missing_disabled_global_development_and_unreadable_fail_closed(self,send):
        attempt,_=self.deliver();self.assertEqual(attempt.failure_category,'missing_webhook')
        self.configure(enabled=False)
        attempt,_=self.deliver();self.assertEqual(attempt.failure_category,'destination_disabled')
        self.configure(webhook_url='')
        with override_settings(NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=False):
            attempt,_=self.deliver();self.assertEqual(attempt.failure_category,'global_disabled')
        with override_settings(NORTHBOUND_DEVELOPMENT_BANNER=True,NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT=False):
            attempt,_=self.deliver();self.assertEqual(attempt.failure_category,'development_blocked')
        GroupDiscordNotification.objects.filter(group=self.group).update(encrypted_webhook='unreadable-secret')
        attempt,_=self.deliver();self.assertEqual(attempt.failure_category,'unreadable_webhook')
        send.assert_not_called()
        self.assertEqual(DiscordManualDelivery.objects.filter(outcome='failed').count(),5)

    @patch('core.discord_notifications.send_webhook',return_value=('sent',0))
    def test_manual_test_and_status_are_destination_specific_and_scheduler_independent(self,send):
        self.configure('community');self.configure();self.book()
        self.month.discord_community_opt_in=True;self.month.save(update_fields=['discord_community_opt_in'])
        with patch.dict('os.environ',{'NORTHBOUND_CHALLENGE_SCHEDULER_ENABLED':'0'}):
            for destination in ('community','moderator'):
                for action in ('test','status'):
                    attempt,fresh=self.deliver(destination,action)
                    self.assertTrue(fresh);self.assertEqual(attempt.outcome,'sent')
                    url,payload=send.call_args.args
                    self.assertEqual(url,fixture.WEBHOOK if destination=='moderator' else fixture.REPLACEMENT)
                    self.assertEqual(payload['allowed_mentions']['parse'],[])
                    self.assertNotIn('Private book',json.dumps(payload, ensure_ascii=False));self.assertNotIn('Private author',json.dumps(payload, ensure_ascii=False))
            config=GroupDiscordNotification.objects.get(group=self.group)
            self.assertIn('Scheduler disabled',destination_diagnostics(config,'moderator',group=self.group)['scheduler'])
        self.assertEqual(send.call_count,4)
        self.assertFalse(DiscordDigestDelivery.objects.exists())
        self.assertTrue(all(x.attempted_at for x in DiscordManualDelivery.objects.all()))

    def test_public_private_separation_and_task_count_semantics(self):
        self.configure();self.book()
        public=json.dumps(_payload(self.group,'community','status'), ensure_ascii=False)
        private=json.dumps(_payload(self.group,'moderator','status'), ensure_ascii=False)
        for forbidden in ('November','Private book','Pending','Book submissions','Reader','verified pages'):
            self.assertNotIn(forbidden,public)
        self.assertNotIn('Automatic community announcements',public)
        self.assertIn('1 review task needs attention',private)
        self.assertIn('Books — 1',private)
        self.assertIn('review task',private)
        for label in ('Theme reviews','Immersive Reading requests','Bingo prompts','Scavenger Hunt Book prompts','Scavenger Hunt photos'):
            self.assertNotIn(label,private)  # Empty categories are omitted.

    @patch('core.discord_notifications.send_webhook',return_value=('sent',0))
    def test_no_scheduled_state_mutation(self,send):
        config=configure_notifications(group=self.group,actor=self.owner,enabled=True,webhook_url=fixture.WEBHOOK)
        GroupDiscordNotification.objects.filter(pk=config.pk).update(next_attempt_at=self.now,last_attempt_at=self.now,last_sent_at=self.now,last_status='sent')
        digest=DiscordDigestDelivery.objects.create(group=self.group,hour=self.now,attempted_at=self.now,status='sent')
        self.configure(webhook_url='');config.refresh_from_db()
        fields=('enabled','encrypted_webhook','next_attempt_at','last_attempt_at','last_sent_at','last_status')
        before={field:getattr(config,field) for field in fields};history=list(DiscordDigestDelivery.objects.values())
        for action in ('test','status'):self.deliver(action=action)
        config.refresh_from_db();self.assertEqual(before,{field:getattr(config,field) for field in fields})
        self.assertEqual(history,list(DiscordDigestDelivery.objects.values()))

    @patch('core.discord_notifications.send_webhook',return_value=('sent',0))
    def test_post_redirect_refresh_and_reentrant_token_are_idempotent(self,send):
        self.configure();token=self.token();self.client.force_login(self.owner)
        def nested(*args,**kwargs):
            nested_response=self.post(token=token)
            self.assertEqual(nested_response.status_code,302)
            return 'sent',0
        send.side_effect=nested
        first=self.post(token=token);second=self.post(token=token)
        self.assertEqual(first.status_code,302);self.assertEqual(second.status_code,302)
        self.client.get(first.url)
        send.assert_called_once();self.assertEqual(DiscordManualDelivery.objects.count(),1)

    @patch('core.discord_notifications.send_webhook')
    def test_bad_expired_and_cross_destination_tokens_cannot_send(self,send):
        self.configure();self.client.force_login(self.owner)
        for token,destination,action in [('bad','moderator','test'),(self.token(),'community','test'),(self.token(),'moderator','status'),(self.token(actor=self.admin),'moderator','test')]:
            self.assertEqual(self.post(destination,action,token).status_code,302)
        with patch('django.core.signing.time.time',return_value=0):token=self.token()
        self.assertEqual(self.post(token=token).status_code,302)
        self.assertFalse(DiscordManualDelivery.objects.exists());send.assert_not_called()

    @patch('core.discord_notifications.send_webhook')
    def test_configuration_and_manual_permissions_and_csrf(self,send):
        self.configure()
        for actor in (self.moderator,self.reader,self.host,self.leader,self.outsider):
            self.client.force_login(actor)
            self.assertEqual(self.post(token=self.token(actor=actor)).status_code,403)
            with self.assertRaises(PermissionDenied):self.deliver(actor=actor)
        self.client.force_login(self.moderator)
        response=self.client.get(self.url)
        self.assertContains(response,'read-only access');self.assertNotContains(response,'Send Test Message</button>')
        self.assertNotContains(response,'name="webhook_url"')
        secure=Client(enforce_csrf_checks=True);secure.force_login(self.owner)
        secure.get(self.url)
        self.assertEqual(secure.post(self.url,{'destination':'moderator','action':'test','request_token':self.token()}).status_code,403)
        self.members[self.owner.pk].is_active=False;self.members[self.owner.pk].save(update_fields=['is_active'])
        with self.assertRaises(PermissionDenied):self.deliver()
        send.assert_not_called();self.assertFalse(DiscordManualDelivery.objects.exists())

    @patch('core.discord_notifications.send_webhook')
    def test_safe_failures_history_and_cooldown(self,send):
        self.configure()
        for status in ('timeout','rejected','network_error','unexpected'):
            send.return_value=(status,0)
            attempt,_=self.deliver();self.assertEqual(attempt.outcome,'failed')
            self.assertEqual(attempt.failure_category,status if status!='unexpected' else 'delivery_failed')
        send.side_effect=RuntimeError(fixture.WEBHOOK)
        attempt,_=self.deliver();self.assertEqual(attempt.failure_category,'delivery_failed')
        send.side_effect=None;send.return_value=('rate_limited',7200)
        attempt,_=self.deliver();self.assertTrue(attempt.retry_at>timezone.now())
        count=send.call_count;attempt,_=self.deliver();self.assertEqual(attempt.failure_category,'rate_limited')
        self.assertEqual(send.call_count,count)
        self.client.force_login(self.owner);page=self.client.get(self.url)
        self.assertContains(page,'Last manual delivery failed');self.assertContains(page,'rate limited')
        for row in DiscordManualDelivery.objects.values():
            self.assertNotIn(fixture.WEBHOOK,str(row));self.assertNotIn('SECRET',str(row))

    def test_sanitized_detailed_transport_categories(self):
        cases=[(TimeoutError(fixture.WEBHOOK),'timeout'),(URLError(TimeoutError()),'timeout'),
            (URLError(fixture.WEBHOOK),'network_error'),(HTTPError(fixture.WEBHOOK,403,'SECRET',{},io.BytesIO(b'SECRET')),'rejected')]
        for error,expected in cases:
            opener=MagicMock();opener.open.side_effect=error
            with patch('core.discord_notifications.build_opener',return_value=opener):
                self.assertEqual(send_webhook(fixture.WEBHOOK,{},detailed=True),(expected,0))

    def test_bad_config_and_secrets_not_redisplayed(self):
        self.configure('community');config=self.configure();self.client.force_login(self.owner)
        for destination in ('community','moderator'):
            for url in ('http://localhost/SECRET',fixture.WEBHOOK+'?SECRET'):
                response=self.client.post(self.url,{'destination':destination,'action':'save','webhook_url':url})
                self.assertContains(response,'Enter a Discord webhook URL')
                for secret in (url,fixture.WEBHOOK,fixture.REPLACEMENT,config.encrypted_webhook):
                    self.assertNotContains(response,secret)
        for destination in ('bad','community'):
            if destination=='bad':
                with self.assertRaises(ValidationError):self.configure(destination)
        self.assertFalse(DiscordManualDelivery.objects.exists())

    @patch('core.discord_notifications.send_webhook',return_value=('sent',0))
    def test_admin_manual_delivery_and_diagnostics_do_not_depend_on_scheduler(self,send):
        self.configure()
        attempt,_=self.deliver(actor=self.admin)
        self.assertEqual(attempt.outcome,'sent')
        config=GroupDiscordNotification.objects.get(group=self.group)
        with patch.dict('os.environ',{'NORTHBOUND_CHALLENGE_SCHEDULER_ENABLED':'0'}):
            status=destination_diagnostics(config,'moderator',group=self.group)
        self.assertIn('Last manual delivery succeeded',status['last_result'])
        self.assertTrue(any('Ready for manual testing' in n for n in status['notices']))
        with override_settings(NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=False,NORTHBOUND_DEVELOPMENT_BANNER=True,NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT=False):
            status=destination_diagnostics(config,'community',group=self.group)
            self.assertTrue(any('No webhook' in n for n in status['notices']))
            self.assertTrue(any('disabled' in n for n in status['notices']))
            self.assertTrue(any('blocked' in n for n in status['notices']))

    @patch('core.discord_notifications.send_webhook')
    def test_invalid_encrypted_url_inactive_group_and_snapshot_failure(self,send):
        from .integrations.secrets import encrypt_token
        self.configure()
        GroupDiscordNotification.objects.filter(group=self.group).update(encrypted_webhook=encrypt_token('https://invalid.example/SECRET'))
        attempt,_=self.deliver();self.assertEqual(attempt.failure_category,'invalid_webhook')
        self.configure()
        with patch('core.discord_notifications.group_pending_review_counts',side_effect=RuntimeError('SECRET')):
            attempt,_=self.deliver(action='status')
            self.assertEqual(attempt.failure_category,'snapshot_unavailable')
        self.group.is_active=False;self.group.save(update_fields=['is_active'])
        with self.assertRaises(PermissionDenied):self.deliver()
        send.assert_not_called()

    @patch('core.discord_notifications.send_webhook',return_value=('sent',0))
    def test_valid_csrf_manual_post_and_get_does_not_send(self,send):
        self.configure()
        secure=Client(enforce_csrf_checks=True);secure.force_login(self.owner)
        page=secure.get(self.url);self.assertEqual(page.status_code,200);send.assert_not_called()
        response=secure.post(self.url,{'destination':'moderator','action':'test','request_token':self.token()},
            HTTP_X_CSRFTOKEN=secure.cookies['csrftoken'].value)
        self.assertEqual(response.status_code,302);send.assert_called_once()
        self.assertEqual(DiscordManualDelivery.objects.get().outcome,'sent')
