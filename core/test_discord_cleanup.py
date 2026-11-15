"""Focused Discord presentation/diagnostic regressions; no real transport."""
import json
import uuid
from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from . import test_discord_notifications as fixture
from .models import (ChallengeMonth, ChallengeStaffAssignment, DiscordReportSchedule,
    DiscordSchedulerHeartbeat, DiscordManualDelivery, DiscordEventDelivery, AuditEvent,
    GroupDiscordNotification)
from .discord_views import community_challenge_rows
from .discord_scheduling import schedule_diagnostics, ReportScheduleForm, scheduled_payloads
from .discord_events import event_diagnostics
from .discord_community import community_embeds, community_empty_reason, community_eligible
from .discord_embeds import manual_payloads, pack

@override_settings(NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=True,NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT=True,
    NORTHBOUND_DISCORD_SCHEDULER_ENABLED=True,NORTHBOUND_DISCORD_EVENTS_ENABLED=True,
    NORTHBOUND_URL='https://northbound.example.com')
class DiscordCleanupTests(TestCase):
    setUp=fixture.DiscordNotificationTests.setUp

    def connection(self):
        from .discord_notifications import configure_notifications
        return configure_notifications(group=self.group,actor=self.owner,destination='community',enabled=True,webhook_url=fixture.WEBHOOK)

    def report(self,**changes):
        config=self.connection()
        values=dict(configuration=config,destination='community',frequency='1h',categories=['active'],timezone_name=self.group.timezone)
        values.update(changes)
        return DiscordReportSchedule.objects.create(**values)

    def opt_in(self):
        self.month.discord_community_opt_in=True;self.month.save()

    def page(self,actor=None):
        self.client.force_login(actor or self.owner)
        return self.client.get(self.url)

    def snapshot(self):
        return {m.__name__:list(m.objects.order_by('pk').values()) for m in
            (ChallengeMonth,GroupDiscordNotification,DiscordReportSchedule,DiscordEventDelivery,DiscordManualDelivery,AuditEvent,DiscordSchedulerHeartbeat)}

    def test_list_shows_eligibility_and_only_existing_authorized_settings_links(self):
        self.opt_in()
        draft=ChallengeMonth.objects.create(group=self.group,name='Private Draft',status='draft',discord_community_opt_in=True)
        upcoming=ChallengeMonth.objects.create(group=self.group,name='Upcoming opted out',status='upcoming')
        ChallengeMonth.objects.create(group=self.other,name='Other Group Secret',status='active')
        rows,_=community_challenge_rows(self.owner,self.group);by_name={row['name']:row for row in rows}
        self.assertTrue(by_name[self.month.name]['eligible'])
        self.assertEqual(by_name['Private Draft']['reason'],'Draft')
        self.assertEqual(by_name['Upcoming opted out']['reason'],'Announcements not enabled')
        self.assertNotIn('Other Group Secret',by_name)
        self.assertEqual(by_name[self.month.name]['settings_url'],reverse('challenge-general-settings',args=[self.group.slug,self.month.pk]))
        rows,_=community_challenge_rows(self.moderator,self.group)
        self.assertNotIn('Private Draft',[row['name'] for row in rows]);self.assertTrue(all(not row['settings_url'] for row in rows))
        page=self.page(self.moderator);self.assertNotContains(page,'Private Draft');self.assertNotContains(page,'Other Group Secret')
        self.assertContains(page,'Included — eligible for Community reporting')
        self.assertNotContains(page,'name="discord_community_opt_in"')

    def test_completed_archived_metadata_eligibility_preserves_results_restrictions(self):
        for state in ('upcoming','active','finalizing','completed','archived'):
            month=ChallengeMonth.objects.create(group=self.group,name='Opted '+state,status=state,discord_community_opt_in=True)
            rows,_=community_challenge_rows(self.owner,self.group);row=next(row for row in rows if row['name']==month.name)
            self.assertTrue(row['eligible'])
            if state in ('completed','archived'):self.assertFalse(row['settings_url'])
        self.assertEqual(community_embeds(self.group,['results','participation']),[])

    @patch('core.discord_notifications.send_webhook')
    def test_settings_get_is_readonly_and_categories_preserve_keys_and_values(self,send):
        report=self.report(categories=['upcoming','dates','participation','results'],immediate_categories=['results'])
        before=self.snapshot();page=self.page();self.assertEqual(before,self.snapshot());send.assert_not_called()
        self.assertContains(page,'Information Included in Scheduled Updates')
        self.assertContains(page,'Categories select information, not which Challenges')
        self.assertContains(page,'Participation summaries — unavailable for the Community audience')
        self.assertContains(page,'Published results and standings — unavailable for the Community audience')
        self.assertContains(page,'Published results — unavailable for the Community audience')
        form=page.context['destinations'][0]['schedule_form']
        self.assertEqual(form['categories'].value(),report.categories)
        self.assertEqual(form['immediate_categories'].value(),['results'])
        data={'scheduled_enabled':'on','frequency':'1h','categories':report.categories,'immediate_categories':['results']}
        bound=ReportScheduleForm(data,group=self.group,destination='community',schedule=report)
        self.assertTrue(bound.is_valid(),bound.errors);self.assertEqual(bound.cleaned_data['categories'],report.categories)
        self.assertEqual(bound.cleaned_data['immediate_categories'],['results'])
        self.assertNotContains(page,fixture.WEBHOOK)

    def test_configuration_roles_remain_unchanged_and_history_is_historical(self):
        self.connection();DiscordManualDelivery.objects.create(group=self.group,destination='community',action='status',request_id=uuid.uuid4(),outcome='sent')
        for actor in (self.owner,self.admin,self.moderator):
            page=self.page(actor);self.assertEqual(page.status_code,200)
            self.assertContains(page,'Historical manual result:');self.assertContains(page,'not a current connection check')
        for actor in (self.host,self.leader,self.reader,self.outsider):
            self.assertEqual(self.page(actor).status_code,403)
        self.client.force_login(self.moderator)
        self.assertEqual(self.client.post(self.url,{'destination':'community','action':'status'}).status_code,403)

    def test_blockers_cannot_be_overwritten_by_privacy_notice(self):
        report=self.report();self.opt_in();config=report.configuration
        config.community_enabled=False
        status=schedule_diagnostics(report,group=self.group);self.assertIn('destination is disabled',status['readiness']);self.assertTrue(status['privacy_notice'])
        config.community_enabled=True;config.encrypted_community_webhook=''
        self.assertIn('No webhook configured',schedule_diagnostics(report,group=self.group)['readiness'])
        config.encrypted_community_webhook='saved'
        with override_settings(NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=False):
            self.assertIn('Global Discord delivery is disabled',schedule_diagnostics(report,group=self.group)['readiness'])
        with override_settings(NORTHBOUND_DISCORD_SCHEDULER_ENABLED=False):
            self.assertIn('global scheduling gate is disabled',schedule_diagnostics(report,group=self.group)['readiness'])

    def test_disabled_frequency_missing_frequency_and_worker_status(self):
        self.assertIn('no report frequency',schedule_diagnostics(None,group=self.group)['readiness'])
        report=self.report(frequency='disabled');self.assertIn('Disabled',schedule_diagnostics(report,group=self.group)['readiness'])
        report.frequency='1h';self.opt_in()
        self.assertIn('Worker inactive',schedule_diagnostics(report,group=self.group)['readiness'])
        DiscordSchedulerHeartbeat.objects.create(name='discord',last_seen=timezone.now(),daemon=False)
        self.assertNotIn('Running',schedule_diagnostics(report,group=self.group)['readiness'])
        DiscordSchedulerHeartbeat.objects.filter(name='discord').update(daemon=True)
        self.assertIn('Running',schedule_diagnostics(report,group=self.group)['readiness'])
        DiscordSchedulerHeartbeat.objects.filter(name='discord').update(last_seen=timezone.now()-timedelta(minutes=5))
        self.assertIn('Worker inactive',schedule_diagnostics(report,group=self.group)['readiness'])

    def test_no_eligible_or_no_selected_content_and_send_empty_diagnostics(self):
        report=self.report();self.assertIn('no eligible opted-in',schedule_diagnostics(report,group=self.group)['readiness'])
        self.opt_in();report.categories=['upcoming']
        self.assertIn('no shareable content',schedule_diagnostics(report,group=self.group)['readiness'])
        report.send_empty=True
        self.assertIn('Worker inactive',schedule_diagnostics(report,group=self.group)['readiness'])

    def test_immediate_preferences_gate_and_worker_are_distinct(self):
        report=self.report(immediate_enabled=True)
        with override_settings(NORTHBOUND_DISCORD_EVENTS_ENABLED=False):
            status=event_diagnostics(self.group,'community');self.assertTrue(status['enabled']);self.assertFalse(status['global_enabled'])
            self.assertIn('Paused',status['readiness'])
        self.assertIn('Worker inactive',event_diagnostics(self.group,'community')['readiness'])
        DiscordSchedulerHeartbeat.objects.create(name='discord-events',last_seen=timezone.now(),daemon=False)
        self.assertNotIn('Running',event_diagnostics(self.group,'community')['readiness'])
        DiscordSchedulerHeartbeat.objects.filter(name='discord-events').update(daemon=True)
        self.assertIn('Running',event_diagnostics(self.group,'community')['readiness'])
        self.assertIn('Disabled',event_diagnostics(self.group,'moderator')['readiness'])

    def test_empty_manual_status_names_no_private_challenges_and_has_safe_explanation(self):
        ChallengeMonth.objects.create(group=self.group,name='Secret Draft',status='draft',discord_community_opt_in=True)
        with override_settings(NORTHBOUND_URL='http://192.168.0.11:8061'):
            payloads=manual_payloads(self.group,'community','status')
        text=json.dumps(payloads,ensure_ascii=False)
        self.assertIn('No eligible Challenges',text);self.assertIn('eligible Challenge’s settings',text)
        self.assertNotIn('Secret Draft',text);self.assertNotIn(self.month.name,text);self.assertNotIn('192.168',text)
        self.assertEqual(payloads[0]['allowed_mentions']['parse'],[])
        self.assertIn(self.group.name,payloads[0]['embeds'][0]['title'])

    def test_empty_selected_categories_do_not_claim_no_opted_in_challenges(self):
        self.opt_in()
        for categories in ([],['upcoming'],['participation','results']):
            values=community_embeds(self.group,categories,send_empty=True)
            self.assertIn('selected report categories',values[0]['description'])
            self.assertNotIn('No eligible Challenges',values[0]['description'])
            pack(values)
        report=self.report(categories=['results'],send_empty=False)
        self.assertEqual(scheduled_payloads(report),[])

    def test_nonempty_payload_keeps_existing_structure_and_category_semantics(self):
        self.opt_in();self.month.registration_is_open=True;self.month.save()
        values=community_embeds(self.group,['dates','registration'])
        self.assertEqual(len(values),1)
        self.assertEqual(values[0]['title'],'📚 '+self.group.name+' — Challenge Update')
        self.assertEqual(values[0]['description'],'**'+self.month.name+'**')
        self.assertEqual([f['name'] for f in values[0]['fields']],['Registration','Challenge Dates','🔗 Northbound'])
        self.assertEqual(values[0]['fields'][0]['value'],'Open')
        self.assertEqual(community_embeds(self.group,['upcoming']),[])

    def test_empty_embed_limits_preserved(self):
        from .discord_embeds import community_status_embed
        self.group.name='Long name '*200
        payload=pack([community_status_embed(self.group,timezone.now().isoformat())])
        self.assertLessEqual(len(payload[0]['embeds'][0]['title']),256)

    def test_moderator_host_sees_only_authorized_draft_and_management_link(self):
        own=ChallengeMonth.objects.create(group=self.group,name='Hosted Draft',status='draft')
        ChallengeMonth.objects.create(group=self.group,name='Unhosted Draft',status='draft')
        ChallengeStaffAssignment.objects.create(month=own,membership=self.members[self.moderator.pk],role='host')
        rows,_=community_challenge_rows(self.moderator,self.group)
        self.assertNotIn('Unhosted Draft',[row['name'] for row in rows])
        row=next(row for row in rows if row['name']=='Hosted Draft')
        self.assertEqual(row['settings_url'],reverse('challenge-general-settings',args=[self.group.slug,own.pk]))
        self.assertFalse(row['eligible'])
        self.assertEqual(self.page(self.moderator).status_code,200)
