"""Destination-specific consent; synthetic records and mocked Discord only."""
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import test_discord_notifications as fixture
from . import test_discord_events as event_fixture
from .discord_eligibility import challenge_eligible, payload_eligible
from .discord_events import enqueue, deliver_event, event_payload
from .discord_embeds import manual_payloads, moderator_status_embeds, pack, embed_units
from .discord_manual import deliver_manual
from .discord_notifications import configure_notifications, deliver_group_digest, group_pending_review_counts
from .discord_scheduling import configure_report_schedule, scheduled_payloads, deliver_scheduled_report
from .models import (ChallengeMonth, DiscordReportSchedule, DiscordEventDelivery,
                     DiscordManualDelivery, GroupDiscordNotification)


@override_settings(NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=True,
    NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT=True, NORTHBOUND_DISCORD_EVENTS_ENABLED=True,
    NORTHBOUND_DISCORD_SCHEDULER_ENABLED=True, NORTHBOUND_URL='https://northbound.example.com')
class DiscordConsentTests(TestCase):
    book = fixture.DiscordNotificationTests.book
    activate = event_fixture.DiscordEventsTests.activate

    def setUp(self):
        fixture.DiscordNotificationTests.setUp(self)
        self.consent()
        for destination in ('community', 'moderator'):
            configure_notifications(group=self.group, actor=self.owner, destination=destination,
                                    enabled=True, webhook_url=fixture.WEBHOOK, cleanup_mode='none')
        transport = patch('core.discord_notifications.send_webhook', return_value=('sent', 0))
        self.send = transport.start()
        self.addCleanup(transport.stop)

    def consent(self, month=None, *, community=False, moderator=False):
        month = month or self.month
        ChallengeMonth.objects.filter(pk=month.pk).update(
            discord_community_opt_in=community, discord_moderator_opt_in=moderator)
        month.refresh_from_db()
        return month

    def schedule(self, destination):
        form, _ = configure_report_schedule(group=self.group, actor=self.owner, destination=destination,
            data={'scheduled_enabled':'on', 'frequency':'1h',
                  'categories':['dates'] if destination=='community' else ['books']})
        self.assertTrue(form.is_valid(), form.errors)
        return DiscordReportSchedule.objects.select_related('configuration__group').get(
            configuration__group=self.group, destination=destination)

    def manual(self, destination, request_id=None, action='status'):
        return deliver_manual(group=self.group, actor=self.owner, destination=destination,
                              action=action, request_id=request_id or uuid.uuid4())

    def event(self, destination):
        self.activate(destination)
        book = self.book()
        with transaction.atomic():
            return enqueue(month=self.month,
                categories=['review_required'] if destination=='moderator' else ['challenge_start'],
                review_category='books' if destination=='moderator' else '',
                source_kind='book' if destination=='moderator' else 'challenge',
                source_id=book.pk if destination=='moderator' else self.month.pk)

    def test_defaults_and_all_four_consent_combinations(self):
        other = ChallengeMonth.objects.create(group=self.group, name='Defaults', status='active')
        self.assertFalse(other.discord_community_opt_in)
        self.assertFalse(other.discord_moderator_opt_in)
        for community, moderator in [(False,False),(True,False),(False,True),(True,True)]:
            self.consent(community=community, moderator=moderator)
            self.assertEqual(challenge_eligible(self.month,'community'), community)
            self.assertEqual(challenge_eligible(self.month,'moderator'), moderator)
            for destination, allowed in [('community',community),('moderator',moderator)]:
                row = self.event(destination)
                self.assertEqual(row is not None, allowed)
                DiscordEventDelivery.objects.all().delete()

    def test_settings_controls_save_independently_and_keep_permissions(self):
        url = reverse('challenge-general-settings', args=[self.group.slug,self.month.pk])
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(url), 'Discord Notifications')
        for community, moderator in [(True,False),(False,True),(True,True),(False,False)]:
            data={'name':self.month.name,'description':''}
            if community:data['discord_community_opt_in']='on'
            if moderator:data['discord_moderator_opt_in']='on'
            self.assertEqual(self.client.post(url,data).status_code,302)
            self.month.refresh_from_db()
            self.assertEqual((self.month.discord_community_opt_in,self.month.discord_moderator_opt_in),(community,moderator))
        for actor in [self.reader,self.moderator,self.leader,self.outsider]:
            self.client.force_login(actor)
            self.assertEqual(self.client.post(url,{'discord_moderator_opt_in':'on'}).status_code,403)
        self.client.force_login(self.owner)
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status='completed')
        self.assertEqual(self.client.post(url,{'discord_moderator_opt_in':'on'}).status_code,403)

    def test_manual_no_consent_fails_without_transport_and_remains_idempotent(self):
        self.book()
        for destination in ['community','moderator']:
            key=uuid.uuid4();row,fresh=self.manual(destination,key)
            self.assertTrue(fresh);self.assertEqual(row.outcome,'failed')
            self.assertEqual(row.failure_category,'no_eligible_challenges')
            again,fresh=self.manual(destination,key)
            self.assertFalse(fresh);self.assertEqual(again.pk,row.pk)
        self.send.assert_not_called()

    def test_generic_test_has_no_challenge_information_without_consent(self):
        for destination in ['community','moderator']:
            row,_=self.manual(destination,action='test')
            self.assertEqual(row.outcome,'sent')
            self.assertNotIn(self.month.name,json.dumps(self.send.call_args.args[1]))

    def test_three_challenges_two_opted_in_separate_manual_and_scheduled_payloads(self):
        second=ChallengeMonth.objects.create(group=self.group,name='Second Named Challenge',status='active',
            starts_on=self.month.starts_on,ends_on=self.month.ends_on,
            discord_community_opt_in=True,discord_moderator_opt_in=True)
        third=ChallengeMonth.objects.create(group=self.group,name='Never Include This Challenge',status='active')
        self.consent(community=True,moderator=True)
        for month in [self.month,second,third]:self.book(month=month)
        for destination in ['community','moderator']:
            for payloads in [manual_payloads(self.group,destination,'status'),scheduled_payloads(self.schedule(destination))]:
                self.assertEqual(len(payloads),2)
                self.assertEqual({p.challenge_id for p in payloads},{self.month.pk,second.pk})
                for payload in payloads:
                    text=json.dumps(payload)
                    self.assertNotIn(third.name,text)
                    self.assertNotIn(second.name if payload.challenge_id==self.month.pk else self.month.name,text)
                    self.assertNotIn('challenge_id',text)  # Internal metadata must not enter Discord JSON.
                    self.assertTrue(payload_eligible(payload))
                    self.assertLessEqual(sum(embed_units(e) for e in payload['embeds']),6000)
            row,_=self.manual(destination);self.assertEqual(row.outcome,'sent')
        self.assertEqual(self.send.call_count,4)

    def test_optout_after_queue_and_after_payload_preparation_both_destinations(self):
        for destination in ['community','moderator']:
            for after_claim in [False,True]:
                self.consent(community=True,moderator=True)
                row=self.event(destination);self.assertIsNotNone(row)
                if after_claim:
                    original=event_payload
                    def prepare(event):
                        payload=original(event)
                        self.consent()
                        return payload
                    with patch('core.discord_events.event_payload',side_effect=prepare):
                        self.assertEqual(deliver_event(row.pk),'skipped')
                else:
                    self.consent();self.assertEqual(deliver_event(row.pk),'skipped')
                self.consent(community=True,moderator=True)
                self.assertEqual(deliver_event(row.pk),'skipped')
                DiscordEventDelivery.objects.all().delete()
        self.send.assert_not_called()

    def test_optout_save_then_reenable_cannot_revive_pending_or_claimed_event(self):
        for destination in ['community','moderator']:
            for after_claim in [False,True]:
                self.consent(community=True,moderator=True)
                row=self.event(destination)
                def toggle():
                    setattr(self.month,'discord_'+destination+'_opt_in',False)
                    self.month.save(update_fields=['discord_'+destination+'_opt_in'])
                    setattr(self.month,'discord_'+destination+'_opt_in',True)
                    self.month.save(update_fields=['discord_'+destination+'_opt_in'])
                if after_claim:
                    original=event_payload
                    def prepare(event):
                        payload=original(event);toggle();return payload
                    with patch('core.discord_events.event_payload',side_effect=prepare):
                        self.assertEqual(deliver_event(row.pk),'skipped')
                else:
                    toggle();self.assertEqual(deliver_event(row.pk),'skipped')
                row.refresh_from_db();self.assertEqual(row.state,'skipped')
                self.assertEqual(row.failure_category,'challenge_opted_out')
                DiscordEventDelivery.objects.all().delete()
        self.send.assert_not_called()

    def test_report_optout_after_preparation_blocks_manual_and_scheduled_both_destinations(self):
        self.book()
        for destination in ['community','moderator']:
            self.consent(community=True,moderator=True)
            original=manual_payloads
            def manual_prepare(*args):
                payloads=original(*args);self.consent();return payloads
            with patch('core.discord_manual._payloads',side_effect=manual_prepare):
                row,_=self.manual(destination)
            self.assertEqual(row.outcome,'failed');self.assertEqual(row.failure_category,'challenge_opted_out')
            self.consent(community=True,moderator=True)
            schedule=self.schedule(destination);original_scheduled=scheduled_payloads
            def report_prepare(value):
                payloads=original_scheduled(value);self.consent();return payloads
            with patch('core.discord_scheduling.scheduled_payloads',side_effect=report_prepare):
                self.assertEqual(deliver_scheduled_report(schedule.pk,now=schedule.next_run_at),'failed')
        self.send.assert_not_called()

    def test_scheduled_transport_separates_challenges_and_does_not_repeat_slot(self):
        self.consent(community=True,moderator=True)
        other=ChallengeMonth.objects.create(group=self.group,name='Another Challenge',status='active',
            starts_on=self.month.starts_on,ends_on=self.month.ends_on,
            discord_community_opt_in=True,discord_moderator_opt_in=True)
        self.book();self.book(month=other)
        for destination in ['community','moderator']:
            self.send.reset_mock()
            schedule=self.schedule(destination);due=schedule.next_run_at
            self.assertEqual(deliver_scheduled_report(schedule.pk,now=due),'sent')
            self.assertEqual(self.send.call_count,2)
            ids={call.args[1].challenge_id for call in self.send.call_args_list}
            self.assertEqual(ids,{self.month.pk,other.pk})
            self.assertEqual(deliver_scheduled_report(schedule.pk,now=due),'not_due')
            self.assertEqual(self.send.call_count,2)

    def test_optout_cancels_only_selected_destination(self):
        self.consent(community=True,moderator=True)
        community=self.event('community');moderator=self.event('moderator')
        self.month.discord_community_opt_in=False
        self.month.save(update_fields=['discord_community_opt_in'])
        community.refresh_from_db();moderator.refresh_from_db()
        self.assertEqual(community.state,'skipped');self.assertEqual(moderator.state,'pending')
        self.assertEqual(deliver_event(moderator.pk),'sent');self.assertEqual(self.send.call_count,1)

    def test_community_sensitive_information_is_rechecked_at_transport(self):
        self.consent(community=True)
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status='completed')
        with patch('core.discord_community.results_visible',return_value=True):
            payloads=manual_payloads(self.group,'community','status')
        self.assertTrue(payloads)
        self.assertIn('Published Results',json.dumps(payloads))
        with patch('core.discord_community.results_visible',return_value=False):
            self.assertFalse(payload_eligible(payloads[0]))

    def test_large_challenge_pages_are_valid_and_never_merge_with_another_challenge(self):
        teams=[{'team_name':'Team '+str(i),'books':1,'themes':0,'immersive':0,'bingo':0,'scavenger':0,'photos':0,'total':1} for i in range(600)]
        other=ChallengeMonth.objects.create(group=self.group,name='Separate Challenge',status='active')
        summary={'total':601,'challenges':[
            {'challenge_id':self.month.pk,'challenge_name':self.month.name,'teams':teams,'total':600},
            {'challenge_id':other.pk,'challenge_name':other.name,'teams':teams[:1],'total':1}]}
        values=pack(moderator_status_embeds(self.group,timezone.now().isoformat(),summary=summary))
        self.assertGreater(len(values),2)
        fields=0
        for payload in values:
            text=json.dumps(payload)
            self.assertNotIn(other.name if payload.challenge_id==self.month.pk else self.month.name,text)
            self.assertIn('Pending Reviews',text)
            self.assertLessEqual(len(payload['embeds']),10)
            self.assertLessEqual(sum(embed_units(e) for e in payload['embeds']),6000)
            fields+=sum(sum(f['name']!='🔗 Northbound' for f in e['fields']) for e in payload['embeds'])
        self.assertEqual(fields,601)

    def test_partial_manual_delivery_does_not_resend_success_on_same_request(self):
        self.consent(moderator=True)
        second=ChallengeMonth.objects.create(group=self.group,name='Second',status='active',discord_moderator_opt_in=True)
        self.book();self.book(month=second)
        key=uuid.uuid4()
        self.send.side_effect=[('sent',0),('network_error',0)]
        row,_=self.manual('moderator',key);self.assertEqual(row.outcome,'failed')
        self.manual('moderator',key);self.assertEqual(self.send.call_count,2)

    def test_legacy_filters_and_rechecks_moderator_consent_without_changing_activation(self):
        config=GroupDiscordNotification.objects.get(group=self.group);config.enabled=True;config.save(update_fields=['enabled'])
        self.book()
        self.assertEqual(deliver_group_digest(self.group.pk),'empty');self.send.assert_not_called()
        self.consent(moderator=True)
        from .discord_notifications import digest_payloads
        def prepare(summary):
            payloads=digest_payloads(summary);self.consent();return payloads
        with patch('core.discord_notifications.digest_payloads',side_effect=prepare):
            self.assertEqual(deliver_group_digest(self.group.pk),'failed')
        self.send.assert_not_called();config.refresh_from_db();self.assertTrue(config.enabled)

    def test_legacy_ui_removed_but_history_and_current_diagnostics_remain(self):
        self.client.force_login(self.owner)
        response=self.client.get(self.url+'?destination=moderator')
        self.assertNotContains(response,'Legacy hourly digest information')
        for label in ['Detailed delivery history','Event-worker','Scheduled','Manage webhook']:
            self.assertContains(response,label)


class DiscordModeratorConsentMigrationTests(TransactionTestCase):
    def test_add_field_preserves_community_configuration_receipts_and_queue(self):
        executor=MigrationExecutor(connection)
        try:
            executor.migrate([('core','0082_personal_tbr_custom_cover')])
            old=executor.loader.project_state([('core','0082_personal_tbr_custom_cover')]).apps
            group=old.get_model('core','ReadingGroup').objects.create(name='Migration Group',slug='migration-consent')
            month=old.get_model('core','ChallengeMonth').objects.create(group=group,name='Keep Community',discord_community_opt_in=True)
            config=old.get_model('core','GroupDiscordNotification').objects.create(group=group,encrypted_webhook='unchanged-encrypted-value',moderator_enabled=True)
            request=uuid.uuid4()
            old.get_model('core','DiscordManualDelivery').objects.create(group=group,request_id=request,destination='moderator',action='test',outcome='sent')
            now=timezone.now()
            schedule=old.get_model('core','DiscordReportSchedule').objects.create(
                configuration=config,destination='moderator',frequency='daily',categories=['books'],
                send_empty=True,immediate_enabled=True,immediate_categories=['review_required'],
                immediate_activated_at=now)
            receipt=old.get_model('core','DiscordReportDelivery').objects.create(
                schedule=schedule,slot_key='preserved-slot',scheduled_for=now,outcome='sent')
            event=old.get_model('core','DiscordEventDelivery').objects.create(group=group,challenge=month,destination='moderator',category='review_required',source_kind='book',source_id=123,created_at=now,activated_at=now,expires_at=now+timedelta(hours=1),next_attempt_at=now)
            executor=MigrationExecutor(connection);executor.migrate([('core','0083_challenge_discord_moderator_opt_in')])
            new=executor.loader.project_state([('core','0083_challenge_discord_moderator_opt_in')]).apps
            saved=new.get_model('core','ChallengeMonth').objects.get(pk=month.pk)
            self.assertTrue(saved.discord_community_opt_in);self.assertFalse(saved.discord_moderator_opt_in)
            self.assertEqual(new.get_model('core','GroupDiscordNotification').objects.get(pk=config.pk).encrypted_webhook,'unchanged-encrypted-value')
            self.assertTrue(new.get_model('core','DiscordManualDelivery').objects.filter(request_id=request,outcome='sent').exists())
            self.assertTrue(new.get_model('core','DiscordEventDelivery').objects.filter(pk=event.pk,state='pending').exists())
            saved_schedule=new.get_model('core','DiscordReportSchedule').objects.get(pk=schedule.pk)
            self.assertEqual((saved_schedule.frequency,saved_schedule.categories,saved_schedule.send_empty,
                saved_schedule.immediate_enabled,saved_schedule.immediate_categories),
                ('daily',['books'],True,True,['review_required']))
            self.assertTrue(new.get_model('core','DiscordReportDelivery').objects.filter(pk=receipt.pk,outcome='sent').exists())
        finally:
            executor=MigrationExecutor(connection);executor.migrate(executor.loader.graph.leaf_nodes())
