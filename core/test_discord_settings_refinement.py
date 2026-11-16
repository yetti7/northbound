"""Stage B.1 presentation/isolation coverage, with transport mocked throughout."""
import json
import uuid
from html.parser import HTMLParser
from unittest.mock import patch
from django.test import TestCase,override_settings
from . import test_discord_notifications as fixtures
from .discord_notifications import configure_notifications
from .discord_manual import deliver_manual
from .discord_embeds import manual_payloads,COMMUNITY_COLOR,MODERATOR_COLOR,SUCCESS_COLOR
from .discord_scheduling import configure_report_schedule,scheduled_payloads
from .models import DiscordReportSchedule,DiscordManualDelivery


class Roles(HTMLParser):
    def __init__(self,text):
        super().__init__();self.tabs=[];self.panels=[];self.feed(text)
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if attrs.get('role')=='tab':self.tabs.append(attrs)
        if attrs.get('role')=='tabpanel':self.panels.append(attrs)


@override_settings(NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=True,NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT=True,
    NORTHBOUND_URL='https://northbound.example.com')
class DiscordSettingsRefinementTests(TestCase):
    setUp=fixtures.DiscordNotificationTests.setUp
    book=fixtures.DiscordNotificationTests.book

    def configure(self):
        for destination in ('community','moderator'):
            configure_notifications(group=self.group,actor=self.owner,destination=destination,enabled=True,webhook_url=fixtures.WEBHOOK)

    def page(self,actor=None,**query):
        self.client.force_login(actor or self.owner)
        return self.client.get(self.url,query)

    def test_server_tabs_accessible_and_single_selected_panel(self):
        for selected in ('community','moderator'):
            page=self.page(destination=selected);markup=Roles(page.content.decode())
            self.assertEqual(len(markup.tabs),2);self.assertEqual(len(markup.panels),2)
            self.assertEqual([t['id'] for t in markup.tabs if t['aria-selected']=='true'],['discord-tab-'+selected])
            self.assertEqual([p['id'] for p in markup.panels if 'hidden' not in p],['discord-panel-'+selected])
            for tab in markup.tabs:self.assertTrue(any(p['id']==tab['aria-controls'] and p['aria-labelledby']==tab['id'] for p in markup.panels))
            self.assertContains(page,'without losing edits');self.assertContains(page,'js/discord_tabs.js')

    def test_compact_categories_placeholder_and_advanced_sections(self):
        page=self.page();text=page.content.decode()
        self.assertContains(page,'Enable immediate notifications')
        self.assertContains(page,'name="immediate_categories"')
        self.assertNotContains(page,'Active Challenges — unavailable')
        self.assertContains(page,'publicly shareable information')
        for label in ('Advanced Settings','Manage webhook and destination','Technical diagnostics','Detailed delivery history'):
            self.assertContains(page,label)
        for key in ('books','themes','immersive','bingo','scavenger','photos'):
            self.assertContains(page,'name="categories" value="'+key+'"')
        self.assertIn('data-frequency-controls hidden',text)

    @patch('core.discord_notifications.send_webhook')
    def test_save_and_validation_feedback_keep_moderator_tab(self,send):
        self.configure();self.client.force_login(self.owner)
        response=self.client.post(self.url,{'destination':'moderator','action':'save','enabled':'on'})
        self.assertEqual(response.url,self.url+'?destination=moderator')
        page=self.client.get(response.url);self.assertContains(page,'settings saved')
        self.assertEqual([p['id'] for p in Roles(page.content.decode()).panels if 'hidden' not in p],['discord-panel-moderator'])
        page=self.client.post(self.url,{'destination':'moderator','action':'save','webhook_url':'http://invalid/SECRET'})
        self.assertContains(page,'Enter a Discord webhook URL');self.assertNotContains(page,'http://invalid/SECRET')
        self.assertEqual([p['id'] for p in Roles(page.content.decode()).panels if 'hidden' not in p],['discord-panel-moderator'])
        self.assertIn('discord-advanced" open',page.content.decode());send.assert_not_called()

    def test_existing_stage_c_preferences_survive_new_compact_preferences_save(self):
        self.configure()
        form,saved=configure_report_schedule(group=self.group,actor=self.owner,destination='moderator',
            data={'frequency':'disabled','immediate_categories':['approved','new_submission']})
        self.assertTrue(form.is_valid(),form.errors)
        self.client.force_login(self.owner)
        response=self.client.post(self.url,{'destination':'moderator','action':'preferences','frequency':'disabled'})
        self.assertEqual(response.status_code,302)
        schedule=DiscordReportSchedule.objects.get(configuration__group=self.group,destination='moderator')
        self.assertEqual(schedule.immediate_categories,['approved','new_submission'])
        self.assertContains(self.client.get(response.url),'name="immediate_categories"')

    def test_moderator_readonly_and_sensitive_history_redacted(self):
        self.configure()
        DiscordManualDelivery.objects.create(group=self.group,request_id=uuid.uuid4(),destination='community',action='status',
            outcome='failed',failure_category=fixtures.WEBHOOK)
        page=self.page(self.moderator);self.assertContains(page,'read-only access')
        self.assertNotContains(page,'name="webhook_url"');self.assertNotContains(page,'Send Current Status</button>')
        self.assertNotContains(page,fixtures.WEBHOOK);self.assertContains(page,'Detailed delivery history')
        self.assertEqual(self.client.post(self.url,{'destination':'moderator','action':'preferences'}).status_code,403)

    @patch('core.discord_notifications.send_webhook',return_value=('sent',0))
    def test_shared_webhook_never_changes_destination_format_or_community_privacy(self,send):
        self.configure();self.book()
        self.month.discord_community_opt_in=True;self.month.save(update_fields=['discord_community_opt_in'])
        for destination in ('community','moderator'):
            for action in ('test','status'):
                attempt,_=deliver_manual(group=self.group,actor=self.owner,destination=destination,action=action,request_id=uuid.uuid4())
                self.assertEqual(attempt.outcome,'sent');self.assertEqual(send.call_args.args[0],fixtures.WEBHOOK)
                payload=send.call_args.args[1];text=json.dumps(payload,ensure_ascii=False);embed=payload['embeds'][0]
                if action=='test':
                    self.assertEqual(embed['color'],SUCCESS_COLOR)
                    self.assertIn('Community Announcements' if destination=='community' else 'Moderator Notifications',text)
                elif destination=='community':
                    self.assertEqual(embed['color'],COMMUNITY_COLOR);self.assertNotIn('Pending Reviews',text);self.assertNotIn('Books —',text)
                else:
                    self.assertEqual(embed['color'],MODERATOR_COLOR);self.assertIn('Books — 1',text)
        schedule=DiscordReportSchedule(configuration=self.group.discord_notifications,destination='community',send_empty=True)
        self.assertNotIn('Books —',json.dumps(scheduled_payloads(schedule),ensure_ascii=False))

    def test_multiple_challenges_show_one_count_each_and_no_redundant_group_total(self):
        from .discord_embeds import REVIEW_FIELDS
        teams=[{'team_name':'Actual Team',**{key:1 for key,_ in REVIEW_FIELDS},'total':6}]
        summary={'total':12,'challenges':[{'challenge_id':self.month.pk,'challenge_name':'First Challenge','teams':teams,'total':6},
            {'challenge_id':self.month.pk,'challenge_name':'Second Challenge','teams':teams,'total':6}]}
        with patch('core.discord_notifications.group_pending_review_counts',return_value=summary):payloads=manual_payloads(self.group,'moderator','status')
        embeds=[e for p in payloads for e in p['embeds']];self.assertEqual(len(embeds),2)
        for e in embeds:
            self.assertIn(self.group.name,e['title']);self.assertNotIn('author',e)
            self.assertIn('6 review tasks need attention',e['description']);self.assertNotIn('12 review',e['description'])
            for key,label in REVIEW_FIELDS:self.assertIn(label+' — 1',e['fields'][0]['value'])
        self.assertIn('First Challenge',embeds[0]['description']);self.assertIn('Second Challenge',embeds[1]['description'])

    def test_paginated_challenge_does_not_repeat_its_total_on_every_page(self):
        teams=[{'team_name':'Team '+str(i),'books':1,'themes':0,'immersive':0,'bingo':0,'scavenger':0,'photos':0,'total':1} for i in range(80)]
        summary={'total':80,'challenges':[{'challenge_id':self.month.pk,'challenge_name':self.month.name,'teams':teams,'total':80}]}
        with patch('core.discord_notifications.group_pending_review_counts',return_value=summary):payloads=manual_payloads(self.group,'moderator','status')
        embeds=[e for p in payloads for e in p['embeds']]
        self.assertGreater(len(embeds),1);self.assertEqual(sum('80 review tasks' in e['description'] for e in embeds),1)
        self.assertEqual(sum(len([f for f in e['fields'] if f['name']!='🔗 Northbound']) for e in embeds),80)
