"""Rich manual message tests; all delivery transport is mocked."""
import json
import uuid
from unittest.mock import patch
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.utils import timezone

from . import discord_notifications as discord
from .discord_embeds import (manual_payloads, embed_units, units, application_url,
    COMMUNITY_COLOR, MODERATOR_COLOR, SUCCESS_COLOR, FOOTER, TEST_FOOTER)
from .discord_manual import deliver_manual
from .models import GroupDiscordNotification, DiscordDigestDelivery, DiscordManualDelivery, TeamAssignment
from . import test_discord_notifications as fixtures


@override_settings(NORTHBOUND_URL='https://northbound.example.com',
    NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=True,NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT=True)
class DiscordEmbedTests(TestCase):
    setUp = fixtures.DiscordNotificationTests.setUp
    book = fixtures.DiscordNotificationTests.book

    def assert_valid(self, payloads):
        self.assertTrue(payloads)
        for payload in payloads:
            self.assertNotIn('content',payload)
            self.assertEqual(payload['allowed_mentions'],{'parse':[],'users':[],'roles':[],'replied_user':False})
            self.assertTrue(1<=len(payload['embeds'])<=10)
            self.assertLessEqual(sum(embed_units(e) for e in payload['embeds']),6000)
            for e in payload['embeds']:
                self.assertTrue(0<units(e['title'])<=256);self.assertLessEqual(units(e['description']),4096)
                self.assertLessEqual(units(e.get('author',{}).get('name','')),256);self.assertLessEqual(units(e['footer']['text']),2048)
                self.assertIsNotNone(parse_datetime(e['timestamp']));self.assertLessEqual(len(e.get('fields',[])),25)
                self.assertTrue(0<=e['color']<=0xffffff)
                for field in e.get('fields',[]):
                    self.assertTrue(0<units(field['name'])<=256);self.assertTrue(0<units(field['value'])<=1024)
                    self.assertFalse(field['inline'])

    def summary(self, count=1):
        teams=[{'team_name':f'Team {i:04}',**dict.fromkeys((k for k,_ in discord.REVIEW_LABELS),0),'books':1,'photos':2,'total':3} for i in range(count)]
        return {'group_id':self.group.pk,'total':3*count,'challenges':[{'challenge_id':self.month.pk,
            'challenge_name':self.month.name,'total':3*count,'teams':teams}]}

    def test_tests_are_green_and_destination_specific_with_actual_group(self):
        for destination,label in [('community','Community Announcements'),('moderator','Moderator Notifications')]:
            payloads=manual_payloads(self.group,destination,'test');self.assert_valid(payloads)
            e=payloads[0]['embeds'][0]
            self.assertEqual(e['author']['name'],self.group.name);self.assertIn(label,e['description'])
            self.assertEqual(e['color'],SUCCESS_COLOR);self.assertEqual(e['footer']['text'],TEST_FOOTER)
            self.assertIn(reverse('group-detail',args=[self.group.slug]),json.dumps(e))

    def test_community_is_public_neutral_and_never_queries_private_services(self):
        self.book();self.month.name='Secret unpublished Challenge';self.month.save(update_fields=['name'])
        def no_query(execute,sql,params,many,context):raise AssertionError('Private data query')
        with connection.execute_wrapper(no_query),patch('core.discord_notifications.group_pending_review_counts') as reviews:
            payloads=manual_payloads(self.group,'community','status')
        reviews.assert_not_called();self.assert_valid(payloads)
        e=payloads[0]['embeds'][0];text=json.dumps(payloads,ensure_ascii=False)
        for forbidden in ['Private book','Private author','Secret unpublished','pending','moderation','participation','results','Scheduler','enabled','announcements: off','Book submissions']:
            self.assertNotIn(forbidden,text)
        self.assertEqual(e['color'],COMMUNITY_COLOR);self.assertEqual(e['footer']['text'],FOOTER)
        self.assertEqual(e['author']['name'],self.group.name)

    def test_missing_or_unsafe_public_urls_are_omitted_and_private_links_are_scoped(self):
        for origin in ['', 'http://192.168.0.11:8061','http://localhost:8061','http://127.1',
            'http://[::1]:8061','http://100.100.10.10','http://host.local','https://user:secret@example.com',
            'https://example.com?token=secret','https://example.com/#secret','https://example.com:bad',
            'http://2130706433','http://0x7f000001','http://host.internal']:
            with self.subTest(origin=origin),override_settings(NORTHBOUND_URL=origin):
                payloads=manual_payloads(self.group,'community','status');self.assert_valid(payloads)
                self.assertNotIn('fields',payloads[0]['embeds'][0])
        with override_settings(NORTHBOUND_URL='http://192.168.0.11:8061'):
            self.assertTrue(application_url('group-detail',[self.group.slug]))
            self.assertFalse(application_url('group-detail',[self.group.slug],public=True))
        with override_settings(NORTHBOUND_URL=''):
            payloads=manual_payloads(self.group,'moderator','status');self.assert_valid(payloads)
            self.assertNotIn('fields',payloads[0]['embeds'][0])

    def test_empty_queue_is_one_green_success_card(self):
        payloads=manual_payloads(self.group,'moderator','status');self.assert_valid(payloads)
        self.assertEqual(len(payloads),1);self.assertEqual(len(payloads[0]['embeds']),1)
        e=payloads[0]['embeds'][0];self.assertEqual(e['title'],'✅ All Caught Up!')
        self.assertEqual(e['color'],SUCCESS_COLOR);self.assertIn(self.group.name,e['description'])
        self.assertNotIn('Team',json.dumps(e))

    def test_actual_team_and_unassigned_counts_and_task_totals(self):
        self.book()
        # Reader starts unassigned; the Leader has a separate current Team.
        payloads=manual_payloads(self.group,'moderator','status');self.assert_valid(payloads)
        text=json.dumps(payloads,ensure_ascii=False)
        self.assertIn('Unassigned / Group Reviews',text);self.assertIn('Books — 1',text)
        self.assertIn('1 review task needs attention',text);self.assertNotIn('Bingo prompts:',text)
        self.assertNotIn('Leader team',text);self.assertNotIn('Private book title',text)
        self.assertIn('review task',text)
        self.assertIn(reverse('review-queue',args=[self.group.slug,self.month.pk]),text)

    def test_many_teams_unicode_and_large_counts_are_not_dropped(self):
        self.group.name='📚'*120
        summary=self.summary(180)
        summary['challenges'][0]['challenge_name']='🏆'*80
        for index,team in enumerate(summary['challenges'][0]['teams']):team['team_name']=f'{index:04} '+'🛡️'*80
        with patch('core.discord_notifications.group_pending_review_counts',return_value=summary):
            payloads=manual_payloads(self.group,'moderator','status')
        self.assert_valid(payloads);self.assertGreater(len(payloads),1)
        fields=[f for p in payloads for e in p['embeds'] for f in e['fields'] if f['name']!='🔗 Northbound']
        self.assertEqual(len(fields),180)
        for index,field in enumerate(fields):
            self.assertIn(f'{index:04}',field['name']);self.assertIn('Books — 1',field['value']);self.assertIn('Photos — 2',field['value'])
        self.assertTrue(all(' · ' in e['title'] for p in payloads for e in p['embeds']))
        summary=self.summary();summary['total']=10**30;summary['challenges'][0]['total']=10**30
        summary['challenges'][0]['teams'][0]['books']=10**30
        with patch('core.discord_notifications.group_pending_review_counts',return_value=summary):
            payloads=manual_payloads(self.group,'moderator','status')
        self.assert_valid(payloads);self.assertIn(str(10**30),json.dumps(payloads))

    def test_oversized_names_and_category_values_split_without_losing_counts(self):
        summary=self.summary();summary['challenges'][0]['teams'][0]['team_name']='*'*500
        huge=10**500
        summary['challenges'][0]['teams'][0].update({key:huge for key,_ in discord.REVIEW_LABELS})
        with patch('core.discord_notifications.group_pending_review_counts',return_value=summary):
            payloads=manual_payloads(self.group,'moderator','status')
        self.assert_valid(payloads)
        text='\n'.join(f['value'] for p in payloads for e in p['embeds'] for f in e['fields'])
        from .discord_embeds import REVIEW_FIELDS
        for _,label in REVIEW_FIELDS:self.assertIn(f'{label} — {huge}',text)
        self.assertTrue(any('(continued)' in f['name'] or '…' in f['name'] for p in payloads for e in p['embeds'] for f in e['fields']))

    @patch('core.discord_notifications.send_webhook',return_value=('sent',0))
    def test_multipart_status_has_one_claim_and_refresh_never_resends(self,send):
        discord.configure_notifications(group=self.group,actor=self.owner,destination='moderator',enabled=True,
            webhook_url='https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz_SECRET')
        request=uuid.uuid4()
        with patch('core.discord_notifications.group_pending_review_counts',return_value=self.summary(180)):
            attempt,fresh=deliver_manual(group=self.group,actor=self.owner,destination='moderator',action='status',request_id=request)
        self.assertTrue(fresh);self.assertEqual(attempt.outcome,'sent');self.assertGreater(send.call_count,1)
        count=send.call_count
        again,fresh=deliver_manual(group=self.group,actor=self.owner,destination='moderator',action='status',request_id=request)
        self.assertFalse(fresh);self.assertEqual(send.call_count,count)
        self.assertEqual(DiscordManualDelivery.objects.count(),1);self.assertFalse(DiscordDigestDelivery.objects.exists())
        for call in send.call_args_list:self.assert_valid([call.args[1]])

    @patch('core.discord_notifications.send_webhook',side_effect=[('sent',0),('rate_limited',7200)])
    def test_partial_delivery_stops_and_preserves_existing_cooldown_and_history(self,send):
        config=discord.configure_notifications(group=self.group,actor=self.owner,destination='moderator',enabled=True,
            webhook_url='https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz_SECRET')
        before=list(GroupDiscordNotification.objects.values());request=uuid.uuid4()
        with patch('core.discord_notifications.group_pending_review_counts',return_value=self.summary(180)):
            attempt,_=deliver_manual(group=self.group,actor=self.owner,destination='moderator',action='status',request_id=request)
        self.assertEqual(send.call_count,2);self.assertEqual(attempt.outcome,'failed')
        self.assertEqual(attempt.failure_category,'rate_limited');self.assertIsNotNone(attempt.retry_at)
        deliver_manual(group=self.group,actor=self.owner,destination='moderator',action='status',request_id=request)
        self.assertEqual(send.call_count,2);self.assertEqual(before,list(GroupDiscordNotification.objects.values()))
        self.assertFalse(DiscordDigestDelivery.objects.exists())


@override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=True,NORTHBOUND_URL='https://northbound.example.com')
class RichReviewCountIntegrationTests(TestCase):
    _fixtures=fixtures.TeamDigestReviewTests._fixtures
    token=fixtures.TeamDigestReviewTests.token
    prompt=fixtures.TeamDigestReviewTests.prompt
    settings=fixtures.TeamDigestReviewTests.settings
    create=fixtures.TeamDigestReviewTests.create
    protected=fixtures.TeamDigestReviewTests.protected
    setUp=fixtures.TeamDigestReviewTests.setUp

    def test_same_book_independent_tasks_all_categories_and_team_transfer(self):
        from .models import ThemeClaim,MonthTheme,BookSubmission,ImmersiveRequest
        self.create()
        BookSubmission.objects.filter(pk=self.book.pk).update(status='pending')
        theme=MonthTheme.objects.create(month=self.month,name='Theme',starts_on=self.month.starts_on,ends_on=self.month.ends_on)
        ThemeClaim.objects.create(submission=self.book,theme=theme,status='pending')
        # Separate approved book has a pending Immersive request.
        approved=BookSubmission.objects.create(month=self.month,participant=self.reader,title='Private immersive title',
            author='Private author',book_format='ebook',completed_on=self.month.starts_on,submitted_pages=10,approved_pages=10,status='approved')
        ImmersiveRequest.objects.create(submission=approved,requester=self.reader.user,captured_percent=100)
        payloads=manual_payloads(self.group,'moderator','status');text=json.dumps(payloads,ensure_ascii=False)
        for phrase in ['Books — 1','Bingo — 1','Scavenger Hunt — 1','Photos — 1','Themes — 1','Immersive Reading — 1']:
            self.assertIn(phrase,text)
        self.assertIn('6 review tasks need attention',text)
        self.assertIn(self.team.name,text);self.assertNotIn(self.other_team.name,text)
        for private in [self.book.title,approved.title,self.reader.user.username]:self.assertNotIn(private,text)
        TeamAssignment.objects.filter(month=self.month,participant=self.reader,ended_at__isnull=True).update(ended_at=timezone.now())
        payloads=manual_payloads(self.group,'moderator','status')
        self.assertIn('Unassigned / Group Reviews',json.dumps(payloads,ensure_ascii=False))
