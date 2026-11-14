"""Independent Stage B scheduler tests: all HTTP transport is mocked."""
import io
import json
from datetime import datetime,timedelta,timezone as dt_timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo
from django.core.exceptions import PermissionDenied
from django.core.management import call_command,CommandError
from django.db import IntegrityError,transaction
from django.test import TestCase,override_settings
from django.urls import reverse
from . import test_discord_notifications as fixtures
from . import discord_scheduling as scheduling
from .discord_manual import deliver_manual
from .discord_notifications import configure_notifications
from .models import (DiscordReportSchedule,DiscordReportDelivery,DiscordSchedulerHeartbeat,
    DiscordManualDelivery,DiscordDigestDelivery,GroupDiscordNotification,ChallengeMonth,Membership)

UTC=dt_timezone.utc


@override_settings(NORTHBOUND_DISCORD_SCHEDULER_ENABLED=True,NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=True,
    NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT=True,NORTHBOUND_URL='https://northbound.example.com')
class DiscordSchedulingTests(TestCase):
    book=fixtures.DiscordNotificationTests.book

    def setUp(self):
        fixtures.DiscordNotificationTests.setUp(self)
        transport_patch=patch('core.discord_notifications.send_webhook',return_value=('sent',0))
        self.transport=transport_patch.start()
        self.addCleanup(transport_patch.stop)
        self.start=datetime(2026,1,5,12,7,tzinfo=UTC)
        configure_notifications(group=self.group,actor=self.owner,destination='moderator',enabled=True,webhook_url=fixtures.WEBHOOK)
        configure_notifications(group=self.group,actor=self.owner,destination='community',enabled=True,webhook_url=fixtures.REPLACEMENT)

    def data(self,frequency='1h',destination='moderator',**changes):
        return {'scheduled_enabled':frequency!='disabled','frequency':frequency,
            'categories':[key for key,_ in scheduling.MODERATOR_CATEGORIES] if destination=='moderator' else [],
            'times':'08:00' if frequency in {'daily','weekly','custom'} else '',
            'weekdays':['0'] if frequency in {'weekly','custom'} else [],'send_empty':False,**changes}

    def configure(self,frequency='1h',destination='moderator',**changes):
        form,saved=scheduling.configure_report_schedule(group=self.group,actor=self.owner,destination=destination,
            data=self.data(frequency,destination,**changes),now=self.start)
        self.assertTrue(form.is_valid(),form.errors)
        return DiscordReportSchedule.objects.select_related('configuration__group').get(configuration__group=self.group,destination=destination)

    def test_all_frequencies_and_independent_category_preferences(self):
        for frequency,_ in DiscordReportSchedule.Frequency.choices:
            schedule=self.configure(frequency)
            self.assertEqual(schedule.frequency,frequency)
            self.assertEqual(schedule.next_run_at is None,frequency=='disabled')
        moderator=self.configure('custom',times='17:00, 08:00\n12:00',weekdays=['4','0','2','0'],categories=['books','photos'])
        community=self.configure('daily','community',send_empty=True)
        self.assertEqual(moderator.times,['08:00','12:00','17:00']);self.assertEqual(moderator.weekdays,[0,2,4])
        self.assertEqual(moderator.categories,['books','photos']);self.assertEqual(community.categories,[])
        self.assertFalse(DiscordReportDelivery.objects.exists());self.transport.assert_not_called()

    def test_invalid_times_days_categories_and_timezone_rejected(self):
        invalid=[{'times':'8:00'},{'times':'24:00'},{'times':'08:60'},{'times':'08:00,08:00'},
            {'times':''},{'weekdays':[]},{'weekdays':['7']},{'categories':['forged']},
            {'immediate_categories':['unvalidated_event']}]
        for extra in invalid:
            form=scheduling.ReportScheduleForm(self.data('custom',**extra),group=self.group,destination='moderator')
            self.assertFalse(form.is_valid(),extra)
        for frequency,extra in [('daily',{'times':'08:00,12:00'}),('weekly',{'weekdays':['1','2']})]:
            form=scheduling.ReportScheduleForm(self.data(frequency,**extra),group=self.group,destination='moderator')
            self.assertFalse(form.is_valid())
        self.group.timezone='Invalid/Timezone'
        form=scheduling.ReportScheduleForm(self.data(),group=self.group,destination='moderator')
        self.assertFalse(form.is_valid());self.assertIn('IANA',str(form.errors))
        self.group.timezone='UTC'
        self.assertTrue(scheduling.ReportScheduleForm(self.data(),group=self.group,destination='moderator').is_valid())

    def test_disabled_form_accepts_hidden_frequency_and_keeps_immediate_inactive(self):
        form,saved=scheduling.configure_report_schedule(group=self.group,actor=self.owner,destination='moderator',
            data={'immediate_categories':['new_submission','approved']},now=self.start)
        self.assertTrue(form.is_valid(),form.errors)
        schedule=DiscordReportSchedule.objects.get(configuration__group=self.group,destination='moderator')
        self.assertEqual(schedule.frequency,'disabled');self.assertIsNone(schedule.next_run_at)
        self.assertEqual(schedule.immediate_categories,['new_submission','approved'])
        self.transport.assert_not_called();self.assertFalse(DiscordReportDelivery.objects.exists())

    def test_interval_grid_is_utc_and_not_server_or_group_timezone(self):
        for frequency,seconds in scheduling.INTERVALS.items():
            schedule=self.configure(frequency)
            self.assertEqual(int(schedule.next_run_at.timestamp())%seconds,0)
            self.assertGreater(schedule.next_run_at,self.start)
            converted=self.start.astimezone(ZoneInfo('Asia/Tokyo'))
            self.assertEqual(scheduling.next_run(schedule,converted),schedule.next_run_at)

    def test_daily_weekly_custom_and_timezone_change(self):
        daily=self.configure('daily');self.assertEqual(daily.next_run_at,datetime(2026,1,5,13,tzinfo=UTC))
        weekly=self.configure('weekly',weekdays=['1']);self.assertEqual(weekly.next_run_at,datetime(2026,1,6,13,tzinfo=UTC))
        custom=self.configure('custom',times='17:00,08:00',weekdays=['0','1','2','3','4'])
        self.assertEqual(scheduling.next_run(custom,datetime(2026,1,9,23,tzinfo=UTC)),datetime(2026,1,12,13,tzinfo=UTC))
        self.group.timezone='America/Los_Angeles';self.group.save(update_fields=['timezone'])
        self.assertEqual(scheduling.deliver_scheduled_report(custom.pk,now=self.start),'rescheduled')
        custom.refresh_from_db();self.assertEqual(custom.next_run_at,datetime(2026,1,5,16,tzinfo=UTC))
        self.transport.assert_not_called()

    def test_dst_gap_skipped_fold_once_and_local_slot_key_stable(self):
        schedule=self.configure('daily',times='02:30')
        self.assertEqual(scheduling.next_run(schedule,datetime(2026,3,8,5,tzinfo=UTC)),datetime(2026,3,9,6,30,tzinfo=UTC))
        schedule.frequency='weekly';schedule.weekdays=[6]
        self.assertEqual(scheduling.next_run(schedule,datetime(2026,3,6,12,tzinfo=UTC)),datetime(2026,3,15,6,30,tzinfo=UTC))
        schedule.frequency='daily';schedule.times=['01:30']
        first=datetime(2026,11,1,5,30,tzinfo=UTC);second=datetime(2026,11,1,6,30,tzinfo=UTC)
        self.assertEqual(scheduling.slot_key(schedule,first),scheduling.slot_key(schedule,second))
        self.assertEqual(scheduling.next_run(schedule,first+timedelta(minutes=1)),datetime(2026,11,2,6,30,tzinfo=UTC))

    def test_selected_categories_keep_team_grouping_and_task_semantics(self):
        schedule=self.configure(categories=['photos','themes'])
        team={'team_name':'Actual Team','books':9,'themes':2,'immersive':4,'bingo':1,'scavenger':3,'photos':1,'total':20}
        summary={'total':20,'challenges':[{'challenge_id':self.month.pk,'challenge_name':self.month.name,'total':20,'teams':[team]}]}
        before=json.dumps(summary,sort_keys=True)
        with patch('core.discord_notifications.group_pending_review_counts',return_value=summary):payloads=scheduling.scheduled_payloads(schedule)
        text=json.dumps(payloads,ensure_ascii=False)
        self.assertIn('Actual Team',text);self.assertIn('Themes — 2',text);self.assertIn('Photos — 1',text)
        self.assertIn('3 review tasks need attention',text)
        self.assertNotIn('Books — 9',text);self.assertNotIn('Bingo prompts:',text)
        self.assertEqual(json.dumps(summary,sort_keys=True),before)

    def test_empty_suppression_and_branded_empty_reports(self):
        schedule=self.configure()
        self.assertEqual(scheduling.deliver_scheduled_report(schedule.pk,now=schedule.next_run_at),'empty')
        self.transport.assert_not_called();schedule.refresh_from_db();self.assertIsNone(schedule.last_sent_at)
        self.start=schedule.last_attempt_at+timedelta(minutes=1)
        schedule=self.configure(send_empty=True)
        self.assertEqual(scheduling.deliver_scheduled_report(schedule.pk,now=schedule.next_run_at),'sent')
        self.assertIn('All Caught Up',json.dumps(self.transport.call_args.args[1]))

    def test_community_optin_never_overrides_public_privacy_and_no_generic_empty_by_default(self):
        schedule=self.configure(destination='community')
        self.assertFalse(self.month.discord_community_opt_in)
        ChallengeMonth.objects.filter(pk=self.month.pk).update(discord_community_opt_in=True)
        self.book()
        self.assertEqual(scheduling.scheduled_payloads(schedule),[])
        schedule.send_empty=True
        text=json.dumps(scheduling.scheduled_payloads(schedule),ensure_ascii=False)
        for value in [self.month.name,'Private book','reviews','participation','results']:
            self.assertNotIn(value,text)
        form=scheduling.ReportScheduleForm(self.data(destination='community',categories=['results']),group=self.group,destination='community')
        self.assertFalse(form.is_valid())

    def test_reentrant_worker_and_unique_claim_prevent_duplicates(self):
        self.book();schedule=self.configure();due=schedule.next_run_at
        def nested(*args,**kwargs):
            DiscordReportSchedule.objects.filter(pk=schedule.pk).update(next_run_at=due)
            self.assertEqual(scheduling.deliver_scheduled_report(schedule.pk,now=due),'duplicate')
            return 'sent',0
        self.transport.side_effect=nested
        self.assertEqual(scheduling.deliver_scheduled_report(schedule.pk,now=due),'sent')
        self.transport.assert_called_once();self.assertEqual(DiscordReportDelivery.objects.count(),1)
        receipt=DiscordReportDelivery.objects.get()
        with self.assertRaises(IntegrityError),transaction.atomic():
            DiscordReportDelivery.objects.create(schedule=schedule,slot_key=receipt.slot_key,scheduled_for=due)

    def test_missed_runs_restart_and_unknown_claim_never_catch_up(self):
        self.book();schedule=self.configure();due=schedule.next_run_at
        now=due+timedelta(days=14)
        self.assertEqual(scheduling.deliver_scheduled_report(schedule.pk,now=now),'missed')
        self.transport.assert_not_called();schedule.refresh_from_db();self.assertGreater(schedule.next_run_at,now)
        self.assertEqual(DiscordReportDelivery.objects.count(),1)
        due=schedule.next_run_at
        DiscordReportDelivery.objects.create(schedule=schedule,slot_key=scheduling.slot_key(schedule,due),scheduled_for=due)
        self.assertEqual(scheduling.deliver_scheduled_report(schedule.pk,now=due),'duplicate')
        self.transport.assert_not_called()

    def test_environment_destination_and_no_lifecycle_side_effects(self):
        self.book();schedule=self.configure();due=schedule.next_run_at
        before=list(ChallengeMonth.objects.values());manual=list(DiscordManualDelivery.objects.values());digests=list(DiscordDigestDelivery.objects.values())
        with override_settings(NORTHBOUND_DISCORD_SCHEDULER_ENABLED=False):
            self.assertEqual(scheduling.process_due_reports(now=due),[])
            self.assertFalse(DiscordSchedulerHeartbeat.objects.exists())
        with override_settings(NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=False):
            self.assertEqual(scheduling.process_due_reports(now=due),[])
        GroupDiscordNotification.objects.filter(group=self.group).update(moderator_enabled=False)
        with patch('core.scheduling.process_due_challenge_schedules') as lifecycle:
            self.assertEqual(scheduling.deliver_scheduled_report(schedule.pk,now=due),'blocked')
            lifecycle.assert_not_called()
        self.transport.assert_not_called();self.assertEqual(before,list(ChallengeMonth.objects.values()))
        self.assertEqual(manual,list(DiscordManualDelivery.objects.values()));self.assertEqual(digests,list(DiscordDigestDelivery.objects.values()))

    def test_webhook_replacement_during_preparation_stops_delivery(self):
        self.book();schedule=self.configure();original=scheduling.scheduled_payloads
        def replace(current):
            payloads=original(current)
            configure_notifications(group=self.group,actor=self.owner,destination='moderator',enabled=True,webhook_url=fixtures.REPLACEMENT)
            return payloads
        with patch('core.discord_scheduling.scheduled_payloads',side_effect=replace):
            self.assertEqual(scheduling.deliver_scheduled_report(schedule.pk,now=schedule.next_run_at),'failed')
        self.transport.assert_not_called();self.assertEqual(DiscordReportDelivery.objects.get().failure_category,'configuration_changed')

    def test_rate_limit_retained_and_manual_does_not_advance_schedule(self):
        import uuid
        self.book();schedule=self.configure();due=schedule.next_run_at
        self.transport.return_value=('rate_limited',7200)
        self.assertEqual(scheduling.deliver_scheduled_report(schedule.pk,now=due),'failed')
        schedule.refresh_from_db();self.assertEqual(schedule.cooldown_until,due+timedelta(hours=2))
        self.transport.reset_mock()
        # Shared rate limit blocks scheduled attempts; history remains separate.
        self.assertEqual(scheduling.deliver_scheduled_report(schedule.pk,now=due+timedelta(hours=1)),'not_due')
        self.transport.assert_not_called()
        self.transport.return_value=('sent',0)
        DiscordReportSchedule.objects.filter(pk=schedule.pk).update(cooldown_until=None)
        schedule.refresh_from_db();before=schedule.next_run_at
        attempt,_=deliver_manual(group=self.group,actor=self.owner,destination='moderator',action='status',request_id=uuid.uuid4())
        self.assertEqual(attempt.outcome,'sent');schedule.refresh_from_db();self.assertEqual(schedule.next_run_at,before)

    def test_authorization_legacy_compatibility_and_no_secret_audit(self):
        for actor in [self.moderator,self.host,self.leader,self.reader,self.outsider]:
            with self.assertRaises(PermissionDenied):scheduling.configure_report_schedule(group=self.group,actor=actor,destination='moderator',data=self.data())
        GroupDiscordNotification.objects.filter(group=self.group).update(enabled=True)
        form,saved=scheduling.configure_report_schedule(group=self.group,actor=self.owner,destination='moderator',data=self.data())
        self.assertFalse(form.is_valid());self.assertFalse(saved)
        schedule=self.configure(retire_legacy=True)
        config=GroupDiscordNotification.objects.get(group=self.group);self.assertFalse(config.enabled)
        from .models import AuditEvent
        for event in AuditEvent.objects.all():self.assertNotIn(fixtures.WEBHOOK,event.summary)
        self.transport.assert_not_called()

    def test_command_dry_run_and_heartbeat_distinguish_configuration_from_operation(self):
        schedule=self.configure()
        with override_settings(NORTHBOUND_DISCORD_SCHEDULER_ENABLED=False):
            output=io.StringIO();call_command('run_discord_scheduler',once=True,dry_run=True,stdout=output)
            self.assertIn('No claims',output.getvalue());self.assertFalse(DiscordSchedulerHeartbeat.objects.exists())
            with self.assertRaises(CommandError):call_command('run_discord_scheduler',once=True)
            self.assertIn('Not activated',scheduling.schedule_diagnostics(schedule,group=self.group)['operational'])
        with patch('core.discord_scheduling.timezone.now',return_value=self.start):
            scheduling.process_due_reports(now=self.start)
            self.assertIn('one-time',scheduling.schedule_diagnostics(schedule,group=self.group,now=self.start)['operational'])
            scheduling.process_due_reports(now=self.start,daemon=True)
            self.assertIn('Recently active',scheduling.schedule_diagnostics(schedule,group=self.group,now=self.start)['operational'])
            self.assertIn('Not running',scheduling.schedule_diagnostics(schedule,group=self.group,now=self.start+timedelta(minutes=2))['operational'])

    def test_preferences_view_and_challenge_optin_permissions(self):
        self.client.force_login(self.owner)
        response=self.client.post(self.url,{'destination':'moderator','action':'preferences',**self.data('custom',times='17:00,08:00',weekdays=['0','4'])})
        self.assertEqual(response.status_code,302)
        page=self.client.get(self.url);self.assertContains(page,'Immediate Notifications — Coming Soon');self.assertContains(page,'Custom Times')
        url=reverse('challenge-general-settings',args=[self.group.slug,self.month.pk])
        response=self.client.post(url,{'name':self.month.name,'description':'','discord_community_opt_in':'on'})
        self.assertEqual(response.status_code,302);self.month.refresh_from_db();self.assertTrue(self.month.discord_community_opt_in)
        for actor in [self.moderator,self.reader,self.leader]:
            self.client.force_login(actor)
            self.assertEqual(self.client.post(url,{'discord_community_opt_in':'on'}).status_code,403)
            self.assertEqual(self.client.post(self.url,{'destination':'moderator','action':'preferences',**self.data()}).status_code,403)
        self.client.force_login(self.host)
        self.assertEqual(self.client.post(url,{'name':self.month.name,'description':'','discord_community_opt_in':'on'}).status_code,302)

    def test_schedule_changes_reset_future_cursor_without_touching_receipts(self):
        self.book();schedule=self.configure();due=schedule.next_run_at
        self.assertEqual(scheduling.deliver_scheduled_report(schedule.pk,now=due),'sent')
        receipts=list(DiscordReportDelivery.objects.values())
        self.start=due+timedelta(minutes=2)
        schedule=self.configure('30m')
        self.assertGreater(schedule.next_run_at,self.start)
        self.assertEqual(receipts,list(DiscordReportDelivery.objects.values()))
        self.assertEqual(scheduling.deliver_scheduled_report(schedule.pk,now=self.start),'not_due')

    def test_manual_respects_scheduled_cooldown_without_claiming_scheduled_delivery(self):
        import uuid
        from django.utils import timezone
        schedule=self.configure()
        future=timezone.now()+timedelta(hours=1)
        DiscordReportSchedule.objects.filter(pk=schedule.pk).update(cooldown_until=future)
        attempt,_=deliver_manual(group=self.group,actor=self.owner,destination='moderator',action='test',request_id=uuid.uuid4())
        self.assertEqual(attempt.failure_category,'rate_limited');self.transport.assert_not_called()
        self.assertFalse(DiscordReportDelivery.objects.exists())
        schedule.refresh_from_db();self.assertEqual(schedule.cooldown_until,future)

    def test_group_timezone_form_and_bad_schedule_post_validation(self):
        from .forms import GroupEditForm
        form=GroupEditForm({'name':self.group.name,'timezone':'Not/AZone'},instance=self.group)
        self.assertFalse(form.is_valid());self.assertIn('timezone',form.errors)
        self.client.force_login(self.owner)
        response=self.client.post(self.url,{'destination':'moderator','action':'preferences',
            **self.data('custom',times='08:00,08:00')})
        self.assertEqual(response.status_code,200);self.assertContains(response,'must be unique')
        self.assertFalse(DiscordReportSchedule.objects.exists());self.transport.assert_not_called()

    def test_timezone_change_during_preparation_cancels_old_slot(self):
        self.book();schedule=self.configure();original=scheduling.scheduled_payloads
        def change_zone(current):
            payloads=original(current)
            type(self.group).objects.filter(pk=self.group.pk).update(timezone='Asia/Tokyo')
            return payloads
        with patch('core.discord_scheduling.scheduled_payloads',side_effect=change_zone):
            self.assertEqual(scheduling.deliver_scheduled_report(schedule.pk,now=schedule.next_run_at),'failed')
        self.transport.assert_not_called()
        self.assertEqual(DiscordReportDelivery.objects.get().failure_category,'configuration_changed')

    def test_empty_filtered_dashboard_does_not_claim_all_categories_are_empty(self):
        self.book()
        schedule=self.configure(categories=['photos'],send_empty=True)
        payload=scheduling.scheduled_payloads(schedule)[0]
        text=json.dumps(payload,ensure_ascii=False)
        self.assertIn('All Caught Up',text);self.assertIn('in the selected report categories',text)
        self.assertNotIn('Book submissions:',text)
