"""Global Discord control: synthetic fixtures and mocked outbound HTTP only."""
import uuid
from unittest.mock import MagicMock, patch
from django.db import OperationalError
from django.test import TestCase, override_settings
from django.urls import reverse
from . import test_discord_notifications as fixtures
from . import test_discord_events as event_fixtures
from .discord_policy import delivery_policy
from .discord_notifications import configure_notifications, delivery_enabled, send_webhook, deliver_group_digest
from .discord_manual import deliver_manual, destination_diagnostics
from .discord_events import deliver_event, process_events
from .discord_scheduling import configure_report_schedule, deliver_scheduled_report, process_due_reports
from .models import (PlatformSettings, GroupDiscordNotification, DiscordReportSchedule, DiscordEventDelivery,
                     AuditEvent, Membership, ChallengeStaffAssignment)


@override_settings(NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=True, NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT=True,
                   NORTHBOUND_DISCORD_EVENTS_ENABLED=True, NORTHBOUND_DISCORD_SCHEDULER_ENABLED=True)
class GlobalDiscordPolicyTests(TestCase):
    book = fixtures.DiscordNotificationTests.book
    activate = event_fixtures.DiscordEventsTests.activate
    event = event_fixtures.DiscordEventsTests.event

    def setUp(self):
        fixtures.DiscordNotificationTests.setUp(self)
        self.platform = PlatformSettings.load()
        self.opener = MagicMock()
        self.response = MagicMock()
        self.response.__enter__.return_value.status = 204
        self.opener.open.return_value = self.response
        mocked = patch('core.discord_notifications.build_opener', return_value=self.opener)
        mocked.start(); self.addCleanup(mocked.stop)

    def override(self, value):
        PlatformSettings.objects.filter(pk=self.platform.pk).update(discord_notifications_override=value)

    def payload(self, value):
        return {'display_name':self.platform.display_name, 'timezone':self.platform.timezone,
                'allow_public_registration':'on', 'allow_user_group_creation':'on',
                'discord_notifications_override':value}

    def test_unset_inherits_and_gui_values_override_both_environment_values(self):
        for inherited in (False, True):
            with override_settings(NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=inherited):
                for override in (None, False, True):
                    self.override(override)
                    policy = delivery_policy()
                    self.assertEqual(policy.allowed, inherited if override is None else override)
                    self.assertEqual(policy.source, 'environment' if override is None else 'gui')
        self.platform.refresh_from_db()
        self.assertTrue(self.platform.discord_notifications_override)

    def test_database_read_failure_fails_closed_even_with_environment_on(self):
        with patch('core.models.PlatformSettings.objects.filter', side_effect=OperationalError('synthetic private error')):
            policy = delivery_policy()
            self.assertFalse(policy.allowed); self.assertEqual(policy.blocking_reason, 'policy_unavailable')
            self.assertEqual(send_webhook(fixtures.WEBHOOK, {}, detailed=True), ('policy_unavailable', 0))
        self.opener.open.assert_not_called()

    def test_invalid_saved_policy_value_is_not_treated_as_truthy(self):
        with patch('core.models.PlatformSettings.objects.filter') as query:
            query.return_value.values_list.return_value.first.return_value='invalid-state'
            self.assertFalse(delivery_policy().allowed)
            self.assertEqual(delivery_policy().blocking_reason,'policy_unavailable')

    def test_no_cached_policy_and_development_protection(self):
        self.override(True); self.assertTrue(delivery_enabled())
        self.override(False); self.assertFalse(delivery_enabled())
        self.override(True)
        with override_settings(NORTHBOUND_DEVELOPMENT_BANNER=True,NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT=False):
            self.assertEqual(delivery_policy().blocking_reason, 'development_blocked')
            self.assertEqual(send_webhook(fixtures.WEBHOOK, {}, detailed=True), ('development_blocked',0))
        self.opener.open.assert_not_called()

    def test_gui_inheritance_save_persistence_audit_and_other_settings(self):
        self.client.force_login(self.admin)
        url=reverse('platform-general-settings')
        response=self.client.get(url)
        self.assertContains(response,'Inherited from the deployment environment')
        self.assertContains(response,'Global Discord Notifications: On')
        original=(self.platform.display_name,self.platform.timezone,self.platform.allow_public_registration,self.platform.allow_user_group_creation)
        response=self.client.post(url,self.payload('on'),follow=True)
        self.assertContains(response,'General Settings were updated.')
        self.platform.refresh_from_db()
        self.assertIs(self.platform.discord_notifications_override,True)
        self.assertEqual(original,(self.platform.display_name,self.platform.timezone,self.platform.allow_public_registration,self.platform.allow_user_group_creation))
        self.assertContains(response,'Saved in Northbound')
        self.assertTrue(AuditEvent.objects.filter(action='platform.general_settings_updated',summary__contains='Inherited from environment').exists())
        self.client.post(url,self.payload('off'))
        self.platform.refresh_from_db();self.assertIs(self.platform.discord_notifications_override,False)
        with override_settings(NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=True):self.assertFalse(delivery_policy().allowed)

    def test_gui_three_states_round_trip_and_inherit_tracks_environment(self):
        self.client.force_login(self.admin)
        url = reverse('platform-general-settings')
        for environment in (False, True):
            with override_settings(NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=environment):
                for saved, choice in ((None, 'inherit'), (True, 'on'), (False, 'off')):
                    with self.subTest(environment=environment, saved=saved):
                        self.override(saved)
                        response = self.client.get(url)
                        self.assertEqual(response.context['form']['discord_notifications_override'].value(), choice)
                        for label in ('Inherit Environment', 'Enabled', 'Disabled'):
                            self.assertContains(response, label)
                        self.platform.refresh_from_db()
                        self.assertIs(self.platform.discord_notifications_override, saved)
                        self.client.post(url, self.payload(choice))
                        self.platform.refresh_from_db()
                        self.assertIs(self.platform.discord_notifications_override, saved)
                        self.assertEqual(delivery_policy().allowed, environment if saved is None else saved)
                response = self.client.post(url, self.payload('inherit'), follow=True)
                self.platform.refresh_from_db()
                self.assertIsNone(self.platform.discord_notifications_override)
                self.assertContains(response, 'Inherited from the deployment environment')
                self.assertEqual(delivery_policy().allowed, environment)

    def test_older_general_settings_payload_preserves_saved_override(self):
        self.client.force_login(self.admin)
        for saved in (None, False, True):
            self.override(saved)
            payload = self.payload('inherit')
            payload.pop('discord_notifications_override')
            self.client.post(reverse('platform-general-settings'), payload)
            self.platform.refresh_from_db()
            self.assertIs(self.platform.discord_notifications_override, saved)

    def test_only_platform_administrators_can_edit(self):
        from django.contrib.auth import get_user_model
        floater=get_user_model().objects.create_user('global-policy-floater')
        member=Membership.objects.create(group=self.group,user=floater,display_name='Floater')
        ChallengeStaffAssignment.objects.create(month=self.month,membership=member,role='floater')
        for user in (self.owner,self.moderator,self.reader,self.host,self.leader,floater):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse('platform-general-settings')).status_code,403)
            self.assertEqual(self.client.post(reverse('platform-general-settings'),self.payload('on')).status_code,403)
        self.platform.refresh_from_db();self.assertIsNone(self.platform.discord_notifications_override)

    def test_group_secrets_preferences_and_queue_are_unchanged_by_toggle(self):
        self.event()
        before=(list(GroupDiscordNotification.objects.values()),list(DiscordReportSchedule.objects.values()),list(DiscordEventDelivery.objects.values()))
        self.client.force_login(self.admin)
        self.client.post(reverse('platform-general-settings'),self.payload('off'))
        self.assertEqual(process_events(),[]);self.assertEqual(process_due_reports(),[])
        after=(list(GroupDiscordNotification.objects.values()),list(DiscordReportSchedule.objects.values()),list(DiscordEventDelivery.objects.values()))
        self.assertEqual(before,after)
        self.opener.open.assert_not_called()

    def test_manual_tests_and_diagnostics_respect_gui_off(self):
        config=configure_notifications(group=self.group,actor=self.owner,destination='moderator',enabled=True,webhook_url=fixtures.WEBHOOK)
        for value,expected in [(False,'global_disabled_gui'),(None,'global_disabled')]:
            self.override(value)
            with override_settings(NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=False):
                attempt,_=deliver_manual(group=self.group,actor=self.owner,destination='moderator',action='test',request_id=uuid.uuid4())
                self.assertEqual(attempt.failure_category,expected)
                diagnostics=destination_diagnostics(config,'moderator',group=self.group)
                self.assertNotIn(fixtures.WEBHOOK,str(diagnostics))
                self.assertNotIn(config.encrypted_webhook,str(diagnostics))
                self.assertIn('Platform Administration' if value is False else 'inherited environment', ' '.join(diagnostics['notices']))
        self.opener.open.assert_not_called()

    def test_explicit_on_permits_manual_delivery_with_environment_off(self):
        configure_notifications(group=self.group,actor=self.owner,destination='moderator',enabled=True,webhook_url=fixtures.WEBHOOK)
        self.override(True)
        with override_settings(NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=False):
            attempt,_=deliver_manual(group=self.group,actor=self.owner,destination='moderator',action='test',request_id=uuid.uuid4())
        self.assertEqual(attempt.outcome,'sent');self.opener.open.assert_called_once()

    def test_legacy_digest_gui_off_does_not_claim_or_send(self):
        configure_notifications(group=self.group,actor=self.owner,enabled=True,webhook_url=fixtures.WEBHOOK)
        self.book();self.override(False)
        self.assertEqual(deliver_group_digest(self.group.pk),'disabled')
        self.opener.open.assert_not_called()

    def test_scheduled_and_immediate_off_preserve_existing_records(self):
        event=self.event();schedule=DiscordReportSchedule.objects.get(configuration__group=self.group,destination='moderator')
        before=list(DiscordEventDelivery.objects.values());self.override(False)
        self.assertEqual(deliver_scheduled_report(schedule.pk),'environment_disabled')
        self.assertEqual(deliver_event(event.pk),'environment_disabled')
        self.assertEqual(before,list(DiscordEventDelivery.objects.values()))
        self.opener.open.assert_not_called()

    def test_worker_capabilities_are_not_bypassed(self):
        event=self.event();schedule=DiscordReportSchedule.objects.get(configuration__group=self.group,destination='moderator');self.override(True)
        with override_settings(NORTHBOUND_DISCORD_EVENTS_ENABLED=False):self.assertEqual(deliver_event(event.pk),'environment_disabled')
        with override_settings(NORTHBOUND_DISCORD_SCHEDULER_ENABLED=False):self.assertEqual(deliver_scheduled_report(schedule.pk),'environment_disabled')
        self.opener.open.assert_not_called()

    def test_off_at_transport_preserves_unsent_event_claim_and_retry_budget(self):
        event=self.event()
        from .discord_manual import _webhook
        def disable_at_webhook(*args):
            result=_webhook(*args);self.override(False);return result
        before=(event.attempts,event.next_attempt_at,event.expires_at)
        with patch('core.discord_manual._webhook',side_effect=disable_at_webhook):
            self.assertEqual(deliver_event(event.pk),'pending')
        event.refresh_from_db();self.assertEqual(event.state,'pending')
        self.assertEqual((event.attempts,event.next_attempt_at,event.expires_at),before)
        self.assertIsNone(event.claim_id)
        self.opener.open.assert_not_called()
        self.override(True)
        self.assertEqual(deliver_event(event.pk),'sent')
        self.opener.open.assert_called_once()

    def test_multipart_manual_continuation_rechecks_final_transport(self):
        configure_notifications(group=self.group,actor=self.owner,destination='moderator',enabled=True,webhook_url=fixtures.WEBHOOK)
        self.override(True)
        def first_post(*args,**kwargs):
            self.override(False);return self.response
        self.opener.open.side_effect=first_post
        # Even generic payloads and a permissive earlier eligibility check cannot bypass transport.
        with patch('core.discord_manual._payloads',return_value=[{'content':'first'},{'content':'second'}]), patch('core.discord_eligibility.payload_eligible',return_value=True):
            attempt,_=deliver_manual(group=self.group,actor=self.owner,destination='moderator',action='test',request_id=uuid.uuid4())
        self.assertEqual(attempt.failure_category,'global_disabled_gui')
        self.assertEqual(self.opener.open.call_count,1)

    def test_settings_form_opened_before_upgrade_does_not_reset_policy(self):
        self.client.force_login(self.admin)
        for original in (None,False,True):
            self.override(original)
            payload=self.payload('on');payload.pop('discord_notifications_override')
            self.assertEqual(self.client.post(reverse('platform-general-settings'),payload).status_code,302)
            self.platform.refresh_from_db();self.assertIs(self.platform.discord_notifications_override,original)


from django.test import TransactionTestCase
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


class DiscordOverrideMigrationTests(TransactionTestCase):
    previous=('core','0085_account_identity')
    target=('core','0086_platform_discord_override')

    def test_existing_platform_and_group_settings_remain_inherited_and_unchanged(self):
        executor=MigrationExecutor(connection)
        executor.migrate([self.previous])
        old=executor.loader.project_state([self.previous]).apps
        try:
            old.get_model('core','PlatformSettings').objects.create(pk=1,display_name='Preserved Platform',timezone='UTC',allow_public_registration=False,allow_user_group_creation=False)
            group=old.get_model('core','ReadingGroup').objects.create(name='Preserved Group',slug='preserved-group')
            old.get_model('core','GroupDiscordNotification').objects.create(group_id=group.pk,enabled=True,moderator_enabled=True,community_enabled=False,encrypted_webhook='opaque-synthetic-ciphertext')
            before=list(old.get_model('core','GroupDiscordNotification').objects.values())
            MigrationExecutor(connection).migrate([self.target])
            saved=PlatformSettings.objects.get(pk=1)
            self.assertIsNone(saved.discord_notifications_override)
            self.assertEqual((saved.display_name,saved.timezone,saved.allow_public_registration,saved.allow_user_group_creation),('Preserved Platform','UTC',False,False))
            self.assertEqual(before,list(GroupDiscordNotification.objects.values()))
        finally:
            MigrationExecutor(connection).migrate([self.target])
