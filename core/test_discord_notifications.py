import io
import json
from datetime import date, timedelta
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError, URLError

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .discord_notifications import (configure_notifications, deliver_group_digest, digest_payload, digest_payloads,
    group_pending_review_counts, process_due_discord_digests, send_webhook, validate_webhook)
from .integrations.secrets import decrypt_token
from .models import (AuditEvent, BookSubmission, ChallengeMonth, ChallengeStaffAssignment,
    DiscordDigestDelivery, GroupDiscordNotification, ImmersiveRequest, Membership, MonthEnrollment,
    MonthTheme, ReadingGroup, ThemeClaim, Team, TeamAssignment)

WEBHOOK = 'https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz_SECRET'
REPLACEMENT = 'https://discord.com/api/webhooks/123456789012345679/abcdefghijklmnopqrstuvwxyz_NEWSECRET'


@override_settings(NORTHBOUND_URL='https://northbound.example', NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=True,
                   NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT=True)
class DiscordNotificationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user('digest-admin', is_superuser=True)
        self.owner = User.objects.create_user('digest-owner')
        self.moderator = User.objects.create_user('digest-moderator')
        self.reader = User.objects.create_user('digest-reader')
        self.host = User.objects.create_user('digest-host')
        self.leader = User.objects.create_user('digest-leader')
        self.outsider = User.objects.create_user('digest-outsider')
        self.group = ReadingGroup.objects.create(name='Digest Group', slug='digest')
        self.other = ReadingGroup.objects.create(name='Other Group', slug='digest-other')
        self.members = {}
        for user, role in [(self.owner,'owner'),(self.moderator,'moderator'),(self.reader,'member'),
                           (self.host,'member'),(self.leader,'member')]:
            self.members[user.pk] = Membership.objects.create(group=self.group, user=user, role=role, display_name=user.username)
        Membership.objects.create(group=self.other,user=self.outsider,role='owner',display_name='Other')
        self.month = ChallengeMonth.objects.create(group=self.group,name='November',status='active',
            starts_on=date(2026,11,1),ends_on=date(2026,11,30))
        self.member = self.members[self.reader.pk]
        self.enrollment = MonthEnrollment.objects.create(month=self.month,participant=self.member)
        ChallengeStaffAssignment.objects.create(month=self.month,membership=self.members[self.host.pk],role='host')
        team = Team.objects.create(month=self.month, name='Leader team')
        MonthEnrollment.objects.create(month=self.month, participant=self.members[self.leader.pk])
        TeamAssignment.objects.create(month=self.month, participant=self.members[self.leader.pk], team=team)
        ChallengeStaffAssignment.objects.create(month=self.month, membership=self.members[self.leader.pk], role='team_leader', team=team)
        self.url = reverse('group-discord-notifications',args=[self.group.slug])
        self.now = timezone.now().replace(minute=5,second=0,microsecond=0)

    def configure(self, **kwargs):
        return configure_notifications(group=self.group,actor=self.owner,enabled=True,webhook_url=WEBHOOK,**kwargs)

    def book(self, month=None, status='pending', removed=False):
        return BookSubmission.objects.create(month=month or self.month, participant=self.member,
            title='Private book title',author='Private author',notes='Private book notes',book_format='ebook',
            completed_on=date(2026,11,3),submitted_pages=100,approved_pages=100 if status=='approved' else None,
            status=status,is_removed=removed)

    def test_owner_admin_configure_and_moderator_readonly(self):
        for user in [self.owner,self.admin]:
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url).status_code,200)
            self.assertEqual(self.client.post(self.url,{'enabled':'on','webhook_url':WEBHOOK}).status_code,302)
        self.client.force_login(self.moderator)
        page=self.client.get(self.url)
        self.assertContains(page,'read-only access')
        self.assertNotContains(page,'name="webhook_url"')
        self.assertEqual(self.client.post(self.url,{'remove_webhook':'on'}).status_code,403)
        with self.assertRaises(PermissionDenied):
            configure_notifications(group=self.group,actor=self.moderator,enabled=False,remove=True)
        self.assertTrue(GroupDiscordNotification.objects.get(group=self.group).enabled)

    def test_denied_roles_scopes_and_delegated_capabilities(self):
        moderator=self.members[self.moderator.pk]
        moderator.permission_overrides={'manage_group_settings':True}
        moderator.save(update_fields=['permission_overrides'])
        for user in [self.reader,self.host,self.leader,self.outsider]:
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url).status_code,403)
            self.assertEqual(self.client.post(self.url,{'enabled':'on','webhook_url':WEBHOOK}).status_code,403)
            with self.assertRaises(PermissionDenied):
                configure_notifications(group=self.group,actor=user,enabled=True,webhook_url=WEBHOOK)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code,302)
        self.owner.is_active=False;self.owner.save(update_fields=['is_active'])
        with self.assertRaises(PermissionDenied):
            self.configure()
        self.assertFalse(GroupDiscordNotification.objects.exists())

    def test_group_navigation_discovery_and_moderator_visibility(self):
        for user,visible in [(self.owner,True),(self.moderator,True),(self.host,False),(self.reader,False)]:
            self.client.force_login(user)
            response=self.client.get(reverse('group-edit',args=[self.group.slug]))
            self.assertEqual(self.url in response.content.decode(),visible)

    def test_credentials_encrypted_never_echoed_and_safe_audit(self):
        config=self.configure()
        self.assertNotIn(WEBHOOK,config.encrypted_webhook)
        self.assertEqual(decrypt_token(config.encrypted_webhook),WEBHOOK)
        self.client.force_login(self.owner)
        for response in [self.client.get(self.url),self.client.post(self.url,{'enabled':'on','webhook_url':WEBHOOK,'remove_webhook':'on'})]:
            self.assertNotContains(response,WEBHOOK)
            self.assertNotContains(response,'abcdefghijklmnopqrstuvwxyz_SECRET')
            self.assertNotContains(response,config.encrypted_webhook)
        for audit in AuditEvent.objects.all():
            self.assertNotIn(WEBHOOK,audit.summary)
            self.assertNotIn('SECRET',audit.summary)
        self.assertEqual(AuditEvent.objects.filter(action='group.discord_notifications_updated').count(),1)

    def test_replace_remove_disable_and_missing_webhook_validation(self):
        with self.assertRaises(ValidationError):
            configure_notifications(group=self.group,actor=self.owner,enabled=True)
        self.assertFalse(GroupDiscordNotification.objects.exists())
        self.configure()
        config=configure_notifications(group=self.group,actor=self.owner,enabled=True,webhook_url=REPLACEMENT)
        self.assertEqual(decrypt_token(config.encrypted_webhook),REPLACEMENT)
        config=configure_notifications(group=self.group,actor=self.owner,enabled=False)
        self.assertFalse(config.enabled);self.assertTrue(config.encrypted_webhook)
        config=configure_notifications(group=self.group,actor=self.owner,enabled=True,remove=True)
        self.assertFalse(config.enabled);self.assertEqual(config.encrypted_webhook,'')

    def test_destination_validation_and_invalid_input_not_redisplayed(self):
        for url in [WEBHOOK,WEBHOOK.replace('discord.com','discordapp.com'),WEBHOOK.replace('/api/','/api/v10/')]:
            self.assertEqual(validate_webhook(url),url)
        self.client.force_login(self.owner)
        for url in [WEBHOOK.replace('https:','http:'), WEBHOOK.replace('discord.com','discord.com.evil.test'),
                    WEBHOOK.replace('discord.com','user@discord.com'),WEBHOOK+'?wait=true',WEBHOOK+'#token',
                    WEBHOOK.replace('discord.com','127.0.0.1'), WEBHOOK.replace('discord.com','discord.com:443'),
                    WEBHOOK.replace('/webhooks/','/%77ebhooks/')]:
            with self.subTest(url=url):
                with self.assertRaises(ValidationError):validate_webhook(url)
                response=self.client.post(self.url,{'enabled':'on','webhook_url':url})
                self.assertContains(response,'Enter a Discord webhook URL')
                self.assertNotContains(response,url)
        self.assertFalse(GroupDiscordNotification.objects.exists())

    def test_counts_group_boundaries_distinct_tasks_and_lifecycle(self):
        pending=self.book()
        approved=self.book(status='approved')
        rejected=self.book(status='rejected')
        removed=self.book(removed=True)
        theme=MonthTheme.objects.create(month=self.month,name='Theme',starts_on=self.month.starts_on,ends_on=self.month.ends_on)
        for book in [pending,approved,rejected,removed]:ThemeClaim.objects.create(submission=book,theme=theme,status='pending')
        # Waiting for book approval is not yet a separately actionable Immersive task.
        for book in [pending,approved,rejected,removed]:
            ImmersiveRequest.objects.create(submission=book,requester=self.reader,captured_percent=100)
        other_month=ChallengeMonth.objects.create(group=self.other,name='Other',status='active',starts_on=self.month.starts_on,ends_on=self.month.ends_on)
        other_member=Membership.objects.get(group=self.other,user=self.outsider)
        BookSubmission.objects.create(month=other_month,participant=other_member,title='Other',author='Other',book_format='ebook',completed_on=date(2026,11,3),submitted_pages=10)
        summary=group_pending_review_counts(self.group)
        self.assertEqual(summary['total'],5)
        self.assertEqual(len(summary['challenges']),1)
        row=summary['challenges'][0]
        self.assertEqual((row['books'],row['themes'],row['immersive']),(1,3,1))
        self.assertEqual(group_pending_review_counts(self.other)['total'],1)
        for status,expected in [('draft',0),('upcoming',0),('finalizing',5),('completed',1),('archived',0)]:
            ChallengeMonth.objects.filter(pk=self.month.pk).update(status=status)
            self.assertEqual(group_pending_review_counts(self.group)['total'],expected)

    def test_formatting_links_privacy_mentions_and_many_challenges(self):
        self.book()
        self.month.name='Review **name** @everyone <@123>'
        self.month.save(update_fields=['name'])
        summary=group_pending_review_counts(self.group);summary['group_slug']=self.group.slug
        payload=digest_payload(summary)
        self.assertEqual(payload['allowed_mentions']['parse'],[])
        self.assertEqual(payload['allowed_mentions']['users'],[])
        self.assertIn('https://northbound.example'+reverse('review-queue',args=[self.group.slug,self.month.pk]),payload['content'])
        for secret in ['Private book title','Private author','Private book notes',WEBHOOK]:self.assertNotIn(secret,payload['content'])
        summary['challenges']*=100
        payloads=digest_payloads(summary)
        self.assertGreater(len(payloads),1)
        self.assertTrue(all(len(p['content'].encode('utf-16-le'))//2<=2000 for p in payloads))
        self.assertEqual(sum(p['content'].count('Book submissions: 1') for p in payloads),100)

    @patch('core.discord_notifications.send_webhook',return_value=('sent',0))
    def test_hourly_repeat_unresolved_items_no_same_hour_or_rolling_hour_duplicates(self,send):
        self.configure();self.book()
        self.assertEqual(deliver_group_digest(self.group.pk,now=self.now),'sent')
        self.assertEqual(deliver_group_digest(self.group.pk,now=self.now+timedelta(minutes=59)),'not_due')
        self.assertEqual(deliver_group_digest(self.group.pk,now=self.now+timedelta(hours=1)),'sent')
        self.assertEqual(send.call_count,2)
        self.assertEqual(DiscordDigestDelivery.objects.count(),2)
        self.assertEqual(send.call_args.args[0],WEBHOOK)
        self.assertTrue(GroupDiscordNotification.objects.get(group=self.group).last_sent_at)

    @patch('core.discord_notifications.send_webhook')
    def test_unique_hour_claim_survives_reentrant_delivery(self,send):
        self.configure();self.book()
        def reenter(*args):
            self.assertEqual(deliver_group_digest(self.group.pk,now=self.now),'not_due')
            with self.assertRaises(IntegrityError),transaction.atomic():
                DiscordDigestDelivery.objects.create(group=self.group,hour=self.now.replace(minute=0),attempted_at=self.now)
            return 'sent',0
        send.side_effect=reenter
        self.assertEqual(deliver_group_digest(self.group.pk,now=self.now),'sent')
        self.assertEqual(send.call_count,1)

    @patch('core.discord_notifications.send_webhook')
    def test_empty_disabled_and_inactive_groups_never_send(self,send):
        self.configure()
        self.assertEqual(deliver_group_digest(self.group.pk,now=self.now),'empty')
        self.assertFalse(DiscordDigestDelivery.objects.exists())
        self.book();GroupDiscordNotification.objects.filter(group=self.group).update(enabled=False)
        self.assertEqual(deliver_group_digest(self.group.pk,now=self.now),'disabled')
        GroupDiscordNotification.objects.filter(group=self.group).update(enabled=True)
        self.group.is_active=False;self.group.save(update_fields=['is_active'])
        self.assertEqual(process_due_discord_digests(now=self.now),[])
        send.assert_not_called()

    @patch('core.discord_notifications.send_webhook')
    def test_global_and_development_safety_gates(self,send):
        self.configure();self.book()
        with override_settings(NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=False):
            self.assertEqual(process_due_discord_digests(),[])
        for field in ['DEBUG','NORTHBOUND_DEVELOPMENT_BANNER']:
            with override_settings(**{field:True,'NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT':False}):
                self.assertEqual(deliver_group_digest(self.group.pk),'disabled')
        self.assertFalse(DiscordDigestDelivery.objects.exists());send.assert_not_called()

    @patch('core.discord_notifications.send_webhook',side_effect=RuntimeError(WEBHOOK))
    def test_transport_failures_safe_logs_and_no_immediate_retry(self,send):
        self.configure();self.book()
        with self.assertLogs('core.discord_notifications',level='WARNING') as logs:
            self.assertEqual(deliver_group_digest(self.group.pk,now=self.now),'failed')
        self.assertNotIn(WEBHOOK,' '.join(logs.output))
        self.assertEqual(deliver_group_digest(self.group.pk,now=self.now),'not_due')
        self.assertEqual(DiscordDigestDelivery.objects.get().status,'failed')
        send.assert_called_once()

    @patch('core.discord_notifications.send_webhook',return_value=('rate_limited',7200))
    def test_rate_limit_persisted_across_hour_buckets(self,send):
        self.configure();self.book()
        self.assertEqual(deliver_group_digest(self.group.pk,now=self.now),'rate_limited')
        self.assertEqual(deliver_group_digest(self.group.pk,now=self.now+timedelta(hours=1)),'not_due')
        self.assertEqual(deliver_group_digest(self.group.pk,now=self.now+timedelta(hours=2)),'rate_limited')
        self.assertEqual(send.call_count,2)

    @patch('core.discord_notifications.send_webhook')
    def test_configuration_failures_never_expose_or_send(self,send):
        self.configure();self.book()
        with override_settings(NORTHBOUND_URL=''):
            self.assertEqual(deliver_group_digest(self.group.pk),'configuration_error')
        GroupDiscordNotification.objects.filter(group=self.group).update(encrypted_webhook='invalid')
        self.assertEqual(deliver_group_digest(self.group.pk),'configuration_error')
        send.assert_not_called();self.assertFalse(DiscordDigestDelivery.objects.exists())

    def test_safe_http_errors_redirect_rejection_and_timeout(self):
        for error,result in [(URLError(WEBHOOK),('failed',0)),
                (HTTPError(WEBHOOK,302,WEBHOOK,{'Location':'http://localhost/private'},io.BytesIO()),('failed',0)),
                (HTTPError(WEBHOOK,400,WEBHOOK,{},io.BytesIO()),('failed',0)),
                (HTTPError(WEBHOOK,429,WEBHOOK,{},io.BytesIO(b'{"retry_after":7200}')),('rate_limited',7200))]:
            opener=MagicMock();opener.open.side_effect=error
            with patch('core.discord_notifications.build_opener',return_value=opener):
                self.assertEqual(send_webhook(WEBHOOK,{'content':'safe'}),result)
                self.assertEqual(opener.open.call_args.kwargs['timeout'],10)
        opener=MagicMock();opener.open.return_value.__enter__.return_value.status=204
        with patch('core.discord_notifications.build_opener',return_value=opener):
            self.assertEqual(send_webhook(WEBHOOK,{}),('sent',0))
        from .discord_notifications import NoRedirect
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'http://localhost'))

    def test_audit_failure_configuration_rollback(self):
        with patch('core.discord_notifications.AuditEvent.objects.create',side_effect=RuntimeError('audit')):
            with self.assertRaises(RuntimeError):self.configure()
        self.assertFalse(GroupDiscordNotification.objects.exists())

    @patch('core.discord_notifications.send_webhook', return_value=('sent',0))
    def test_multipart_digest_uses_single_hourly_reservation(self,send):
        self.configure();self.book()
        summary=group_pending_review_counts(self.group)
        summary['challenges']*=60
        with patch('core.discord_notifications.group_pending_review_counts',return_value=summary):
            self.assertEqual(deliver_group_digest(self.group.pk,now=self.now),'sent')
        self.assertGreater(send.call_count,1)
        self.assertEqual(DiscordDigestDelivery.objects.count(),1)
        before=send.call_count
        self.assertEqual(deliver_group_digest(self.group.pk,now=self.now),'not_due')
        self.assertEqual(send.call_count,before)

    @patch('core.discord_notifications.send_webhook', side_effect=[('sent',0),('rate_limited',7200)])
    def test_multipart_digest_stops_on_rate_limit_without_same_hour_retry(self,send):
        self.configure();self.book()
        summary=group_pending_review_counts(self.group)
        summary['challenges']*=100
        with patch('core.discord_notifications.group_pending_review_counts',return_value=summary):
            self.assertEqual(deliver_group_digest(self.group.pk,now=self.now),'rate_limited')
        self.assertEqual(send.call_count,2)
        self.assertEqual(DiscordDigestDelivery.objects.count(),1)
        self.assertEqual(deliver_group_digest(self.group.pk,now=self.now+timedelta(hours=1)),'not_due')

    def test_scheduler_failures_do_not_interrupt_other_operations_or_groups(self):
        self.configure()
        configure_notifications(group=self.other,actor=self.outsider,enabled=True,webhook_url=WEBHOOK)
        with patch('core.discord_notifications.deliver_group_digest',side_effect=[RuntimeError(WEBHOOK),'empty']):
            with self.assertLogs('core.discord_notifications',level='WARNING') as logs:
                results=process_due_discord_digests()
            self.assertEqual(len(results),2);self.assertEqual(results[1][1],'empty')
            self.assertNotIn(WEBHOOK,' '.join(logs.output))
        with patch('core.management.commands.run_challenge_scheduler.process_due_challenge_schedules',side_effect=RuntimeError('schedule')):
            with patch('core.management.commands.run_challenge_scheduler.process_due_discord_digests') as digest:
                call_command('run_challenge_scheduler',once=True,stdout=io.StringIO(),stderr=io.StringIO())
                digest.assert_called_once()
        with patch('core.management.commands.run_challenge_scheduler.process_due_challenge_schedules',return_value=[]) as schedules:
            with patch('core.management.commands.run_challenge_scheduler.process_due_discord_digests',side_effect=RuntimeError(WEBHOOK)):
                error=io.StringIO();call_command('run_challenge_scheduler',once=True,stdout=io.StringIO(),stderr=error)
                schedules.assert_called_once();self.assertNotIn(WEBHOOK,error.getvalue())

    def test_default_configuration_disabled_and_unsaved_get(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url).status_code,200)
        self.assertFalse(GroupDiscordNotification.objects.exists())
        config=GroupDiscordNotification.objects.create(group=self.group)
        self.assertFalse(config.enabled)
        self.assertEqual(config.encrypted_webhook,'')

    def test_immersive_counts_exclude_inactive_readers_and_resolved_requests(self):
        approved=self.book(status='approved')
        request=ImmersiveRequest.objects.create(submission=approved,requester=self.reader,captured_percent=100)
        self.assertEqual(group_pending_review_counts(self.group)['total'],1)
        MonthEnrollment.objects.filter(pk=self.enrollment.pk).update(is_active=False)
        self.assertEqual(group_pending_review_counts(self.group)['total'],0)
        MonthEnrollment.objects.filter(pk=self.enrollment.pk).update(is_active=True)
        request.status='rejected';request.save(update_fields=['status'])
        self.assertEqual(group_pending_review_counts(self.group)['total'],0)

    @patch('core.discord_notifications.send_webhook',return_value=('sent',0))
    def test_completed_claim_is_not_resent_after_configuration_replacement(self,send):
        self.configure();self.book()
        self.assertEqual(deliver_group_digest(self.group.pk,now=self.now),'sent')
        configure_notifications(group=self.group,actor=self.owner,enabled=True,webhook_url=REPLACEMENT)
        self.assertEqual(deliver_group_digest(self.group.pk,now=self.now),'not_due')
        # The uniqueness guard remains even if a cooldown was manually lost.
        GroupDiscordNotification.objects.filter(group=self.group).update(next_attempt_at=None)
        self.assertEqual(deliver_group_digest(self.group.pk,now=self.now),'duplicate')
        send.assert_called_once()

    @patch('core.discord_notifications.send_webhook')
    def test_delivery_rejects_enclosing_transaction_before_external_io(self,send):
        self.configure();self.book()
        with transaction.atomic():
            with self.assertRaises(RuntimeError):deliver_group_digest(self.group.pk,now=self.now)
        send.assert_not_called();self.assertFalse(DiscordDigestDelivery.objects.exists())


@override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=True, NORTHBOUND_URL='https://northbound.example')
class TeamDigestReviewTests(TestCase):
    from .test_scavenger_photo_foundation import PhotoFoundationTests as _fixtures
    token = _fixtures.token
    prompt = _fixtures.prompt
    settings = _fixtures.settings
    create = _fixtures.create
    protected = _fixtures.protected

    def setUp(self):
        self._fixtures.setUp(self)
        from .prompt_reservations import replace_selections
        self.bingo = self.prompt(kind='bingo')
        self.settings(kind='bingo', enabled=True)
        self.second_photo_prompt = self.prompt(number=3, kind='scavenger', submission_type='photo')
        replace_selections(submission=self.book, actor=self.actor,
            selections={'bingo': self.bingo.pk, 'scavenger': self.book_prompt.pk})

    def row(self):
        return group_pending_review_counts(self.group)['challenges'][0]

    def test_mixed_independent_reviews_exact_counts_and_no_private_fields(self):
        from .models import PromptClaim
        photo = self.create()
        second_prompt = self.second_photo_prompt
        from .photo_submissions import create_photo_submission
        create_photo_submission(month_id=self.month.pk, actor=self.reader.user,
            prompt_id=second_prompt.pk, reader_confirmed=True)
        BookSubmission.objects.filter(pk=self.book.pk).update(status='pending')
        theme = MonthTheme.objects.create(month=self.month, name='Theme', starts_on=self.month.starts_on, ends_on=self.month.ends_on)
        ThemeClaim.objects.create(submission=self.book, theme=theme, status='pending')
        row = self.row()
        self.assertEqual((row['books'],row['bingo'],row['scavenger'],row['photos'],row['themes'],row['immersive']), (1,1,1,2,1,0))
        self.assertEqual(row['total'],6)
        self.assertEqual(row['teams'][0]['team_name'], self.team.name)
        self.assertEqual(row['teams'][0]['photos'],2)  # Two tasks, same Reader.
        BookSubmission.objects.filter(pk=self.book.pk).update(status='approved')
        ImmersiveRequest.objects.create(submission=self.book, requester=self.reader.user, captured_percent=100)
        self.assertEqual(self.row()['immersive'],1)
        payload = digest_payload(group_pending_review_counts(self.group))
        for value in [self.reader.user.username, self.book.title, self.photo_prompt.description]:
            self.assertNotIn(value, payload['content'])
        self.assertNotIn('Book submissions:', payload['content'])
        self.assertIn('Bingo prompts: 1', payload['content'])
        self.assertIn('Scavenger Hunt photos: 2', payload['content'])

    def test_transfers_use_current_team_and_departed_use_retained_credit(self):
        self.create()
        self.assertEqual(self.row()['teams'][0]['team_name'],self.team.name)
        TeamAssignment.objects.filter(month=self.month,participant=self.reader,ended_at__isnull=True).update(ended_at=timezone.now())
        TeamAssignment.objects.create(month=self.month,participant=self.reader,team=self.other_team)
        row = self.row()
        self.assertEqual(len(row['teams']),1)
        self.assertEqual(row['teams'][0]['team_name'],self.other_team.name)
        TeamAssignment.objects.filter(month=self.month,participant=self.reader,ended_at__isnull=True).update(ended_at=timezone.now())
        MonthEnrollment.objects.filter(month=self.month,participant=self.reader).update(is_active=False,credited_team=self.other_team)
        self.assertEqual(self.row()['teams'][0]['team_name'],self.other_team.name)
        MonthEnrollment.objects.filter(month=self.month,participant=self.reader).update(credited_team=None)
        self.assertEqual(self.row()['teams'][0]['team_name'],'Unassigned / Group Reviews')

    def test_multiple_teams_and_unassigned_tasks_not_dropped(self):
        member=self.members['leader']
        TeamAssignment.objects.filter(month=self.month,participant=member,ended_at__isnull=True).update(ended_at=timezone.now())
        TeamAssignment.objects.create(month=self.month,participant=member,team=self.other_team)
        BookSubmission.objects.create(month=self.month,participant=member,title='Private second book',author='Author',book_format='ebook',completed_on=self.month.starts_on,submitted_pages=10)
        row=self.row()
        self.assertEqual({team['team_name'] for team in row['teams']},{self.team.name,self.other_team.name})
        self.assertEqual(row['total'],3)
        TeamAssignment.objects.filter(month=self.month,participant=member,ended_at__isnull=True).update(ended_at=timezone.now())
        row=self.row()
        self.assertEqual({team['team_name'] for team in row['teams']},{self.team.name,'Unassigned / Group Reviews'})

    def test_resolved_removed_rejected_and_withdrawn_reviews_excluded(self):
        from .models import PromptClaim
        from .photo_submissions import transition_photo_reservation
        photo=self.create()
        for status in ['rejected','withdrawn','voided','approved']:
            # Isolated stored-state scenarios: no live data or service side effects.
            from .models import ScavengerPhotoSubmission
            ScavengerPhotoSubmission.objects.filter(pk=photo.pk).update(status=status,
                removed_at=timezone.now() if status in ['withdrawn','voided'] else None,
                reviewer_verified=status=='approved', reviewed_by=self.actor if status=='approved' else None,
                reviewed_at=timezone.now() if status=='approved' else None)
            self.assertEqual(self.row()['photos'],0)
        BookSubmission.objects.filter(pk=self.book.pk).update(status='rejected')
        self.assertEqual(group_pending_review_counts(self.group)['total'],0)
        BookSubmission.objects.filter(pk=self.book.pk).update(status='approved')
        for state in ['qualified','released','rejected']:
            PromptClaim.objects.filter(submission=self.book).update(state=state)
            self.assertEqual(group_pending_review_counts(self.group)['total'],0)
        PromptClaim.objects.filter(submission=self.book).update(state='reserved',is_current=False)
        self.assertEqual(group_pending_review_counts(self.group)['total'],0)

    def test_closed_gate_disabled_categories_exclusion_and_group_isolation(self):
        from .models import PromptCategory
        self.create()
        other=ReadingGroup.objects.create(name='Other',slug='other-digest')
        self.assertEqual(group_pending_review_counts(other)['total'],0)
        with override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=False):
            self.assertEqual(group_pending_review_counts(self.group)['total'],0)
        ChallengeMonth.objects.filter(pk=self.month.pk).update(prompt_automation_excluded=True)
        self.assertEqual(group_pending_review_counts(self.group)['total'],0)
        ChallengeMonth.objects.filter(pk=self.month.pk).update(prompt_automation_excluded=False)
        ReadingGroup.objects.filter(pk=self.group.pk).update(slug='guildwars')
        ChallengeMonth.objects.filter(pk=self.month.pk).update(starts_on=date(2026,11,1),ends_on=date(2026,11,30))
        self.assertEqual(group_pending_review_counts(self.group)['total'],0)
        ReadingGroup.objects.filter(pk=self.group.pk).update(slug='prompt-fixtures')
        PromptCategory.objects.filter(month=self.month).update(enabled=False)
        self.assertEqual(group_pending_review_counts(self.group)['total'],0)

    def test_multiple_immersive_tasks_for_same_reader_are_not_deduplicated(self):
        ImmersiveRequest.objects.create(submission=self.book, requester=self.reader.user, captured_percent=100)
        second=BookSubmission.objects.create(month=self.month, participant=self.reader,
            title='Second private book', author='Author',book_format='ebook',
            completed_on=self.month.starts_on,submitted_pages=100,approved_pages=100,status='approved')
        ImmersiveRequest.objects.create(submission=second, requester=self.reader.user, captured_percent=100)
        self.assertEqual(self.row()['immersive'],2)
        self.assertEqual(self.row()['teams'][0]['immersive'],2)

    def test_counting_and_formatting_do_not_write_source_or_audit_records(self):
        from .models import AuditEvent, PromptClaim, PromptCompletion, ModifierProvenance, ScavengerPhotoSubmission
        self.create()
        models=(BookSubmission,MonthEnrollment,TeamAssignment,AuditEvent,PromptClaim,PromptCompletion,ModifierProvenance,ScavengerPhotoSubmission)
        before={model:list(model.objects.order_by('pk').values()) for model in models}
        digest_payloads(group_pending_review_counts(self.group))
        for model,values in before.items():self.assertEqual(list(model.objects.order_by('pk').values()),values)

    def test_many_teams_split_without_losing_counts_or_links(self):
        summary=group_pending_review_counts(self.group)
        row=summary['challenges'][0]
        row['teams']=[{**row['teams'][0], 'team_name':f'Team {i} 🏆'} for i in range(80)]
        payloads=digest_payloads(summary)
        self.assertGreater(len(payloads),1)
        content='\n'.join(p['content'] for p in payloads)
        for i in range(80):self.assertIn(f'**Team {i} 🏆**',content)
        self.assertEqual(content.count('Bingo prompts: 1'),80)
        self.assertTrue(all(len(p['content'].encode('utf-16-le'))//2<=2000 for p in payloads))
        self.assertTrue(all(p['allowed_mentions']['parse']==[] for p in payloads))
        self.assertEqual(content.count('https://northbound.example'+row['review_path']),80)
