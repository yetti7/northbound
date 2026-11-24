import io
import json
from datetime import timedelta
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError
from django.test import TestCase, TransactionTestCase, override_settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
from . import test_discord_scheduling as fixture
from .discord_notifications import configure_notifications, send_webhook
from .discord_scheduling import deliver_scheduled_report
from .discord_cleanup import clean_generation, retry_cleanup, delete_message as real_delete_message
from .discord_eligibility import ChallengeMessage
from .models import DiscordReportSchedule, DiscordReportGeneration, PlatformSettings, GroupDiscordNotification


@override_settings(NORTHBOUND_DISCORD_SCHEDULER_ENABLED=True, NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=True,
                   NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT=True, NORTHBOUND_URL='https://northbound.example.com')
class ScheduledCleanupTests(TestCase):
    book = fixture.DiscordSchedulingTests.book
    data = fixture.DiscordSchedulingTests.data
    configure = fixture.DiscordSchedulingTests.configure

    def setUp(self):
        fixture.DiscordSchedulingTests.setUp(self)
        GroupDiscordNotification.objects.filter(group=self.group).update(moderator_cleanup='scheduled')
        self.payloads = [self.payload(self.month.pk)]
        self.posts = patch('core.discord_scheduling.scheduled_payloads', side_effect=lambda _: self.payloads).start()
        self.addCleanup(patch.stopall)
        self.transport.side_effect = self.sent
        self.sequence = 100000000000000000
        self.deletes = patch('core.discord_cleanup.delete_message', return_value=('deleted',0)).start()
        self.schedule = self.configure()

    def payload(self, challenge):
        return ChallengeMessage({'content':'Fresh scheduled report'}, challenge_id=challenge,group_id=self.group.pk,destination='moderator')

    def sent(self, *args, **kwargs):
        self.sequence += 1
        return ('sent',0,str(self.sequence)) if kwargs.get('capture_message') else ('sent',0)

    def deliver(self):
        self.schedule.refresh_from_db()
        return deliver_scheduled_report(self.schedule.pk, now=self.schedule.next_run_at + timedelta(seconds=1))

    def test_successful_multipart_replacement_and_duplicate_prevention(self):
        self.payloads *= 2
        self.assertEqual(self.deliver(),'sent')
        first=DiscordReportGeneration.objects.get();self.assertEqual(len(first.message_ids),2)
        self.deletes.assert_not_called()
        self.assertEqual(self.deliver(),'sent')
        first.refresh_from_db();self.assertEqual(first.cleanup_state,'done')
        self.assertEqual([call.args[1] for call in self.deletes.call_args_list],first.message_ids)
        latest=DiscordReportGeneration.objects.exclude(pk=first.pk).get()
        self.assertTrue(latest.complete);self.assertFalse(latest.superseded)
        posts=self.transport.call_count
        self.assertEqual(deliver_scheduled_report(self.schedule.pk,now=self.schedule.last_sent_at),'not_due')
        self.assertEqual(self.transport.call_count,posts)

    def test_none_and_destinations_remain_independent(self):
        GroupDiscordNotification.objects.filter(group=self.group).update(moderator_cleanup='none')
        self.assertEqual(self.deliver(),'sent');self.assertEqual(self.deliver(),'sent')
        self.assertFalse(DiscordReportGeneration.objects.exists());self.deletes.assert_not_called()
        self.assertNotIn('capture_message',self.transport.call_args.kwargs)
        self.assertEqual(GroupDiscordNotification.objects.get(group=self.group).community_cleanup,'none')

    def test_community_replaces_only_its_own_messages(self):
        self.deliver(); moderator=DiscordReportGeneration.objects.get()
        self.month.discord_community_opt_in=True; self.month.save(update_fields=['discord_community_opt_in'])
        configure_notifications(group=self.group,actor=self.owner,destination='community',enabled=True,cleanup_mode='scheduled')
        self.schedule=self.configure(destination='community',categories=['active'])
        self.payloads=[ChallengeMessage({'content':'Community'},challenge_id=self.month.pk,group_id=self.group.pk,destination='community')]
        self.assertEqual(self.deliver(),'sent')
        community=DiscordReportGeneration.objects.exclude(pk=moderator.pk).get()
        self.assertEqual(self.deliver(),'sent')
        moderator.refresh_from_db();community.refresh_from_db()
        self.assertFalse(moderator.superseded);self.assertEqual(community.cleanup_state,'done')
        self.assertEqual(self.deletes.call_args.args[0],fixture.fixtures.REPLACEMENT)
        self.assertEqual(self.deletes.call_args.args[1],community.message_ids[0])

    def test_paused_cleanup_and_overlapping_cleanup_claim_do_not_send(self):
        self.deliver();old=DiscordReportGeneration.objects.get()
        self.deletes.return_value=('network_error',0);self.deliver();old.refresh_from_db()
        PlatformSettings.objects.update_or_create(pk=1,defaults={'discord_notifications_override':False})
        requests=self.deletes.call_count
        self.assertEqual(clean_generation(old.pk,now=old.cleanup_after),'paused')
        self.assertEqual(self.deletes.call_count,requests)
        with patch('core.discord_notifications.build_opener') as opener:
            self.assertEqual(real_delete_message(fixture.fixtures.WEBHOOK,old.message_ids[0]),('paused',0))
            opener.assert_not_called()
        PlatformSettings.objects.filter(pk=1).update(discord_notifications_override=True)
        at=old.cleanup_after
        def concurrent_cleanup(*args):
            self.assertEqual(clean_generation(old.pk,now=at),'not_due')
            return 'deleted',0
        self.deletes.side_effect=concurrent_cleanup
        self.assertEqual(clean_generation(old.pk,now=at),'done')

    def test_failure_after_first_part_preserves_previous_generation(self):
        self.assertEqual(self.deliver(),'sent');old=DiscordReportGeneration.objects.get()
        self.payloads *= 2
        self.transport.side_effect=[('sent',0,'111111111111111111'),('timeout',0,None)]
        self.assertEqual(self.deliver(),'failed')
        old.refresh_from_db();self.assertFalse(old.superseded);self.deletes.assert_not_called()
        self.assertTrue(DiscordReportGeneration.objects.filter(complete=False,message_ids=['111111111111111111']).exists())

    def test_failed_delete_retries_without_reposting_and_is_bounded(self):
        self.deliver();old=DiscordReportGeneration.objects.get()
        self.deletes.return_value=('network_error',0);self.deliver()
        old.refresh_from_db();self.assertEqual(old.cleanup_state,'pending')
        posts=self.transport.call_count
        for _ in range(4):
            old.refresh_from_db();clean_generation(old.pk,now=old.cleanup_after)
        old.refresh_from_db();self.assertEqual(old.cleanup_state,'abandoned');self.assertEqual(old.cleanup_attempts,5)
        self.assertEqual(self.transport.call_count,posts)

    def test_partial_multipart_cleanup_retries_only_remaining_ids(self):
        self.payloads *= 2
        self.deliver();old=DiscordReportGeneration.objects.get()
        self.deletes.side_effect=[('deleted',0),('rate_limited',7)]
        self.deliver();old.refresh_from_db()
        self.assertEqual(old.deleted_ids,[old.message_ids[0]])
        self.assertEqual(old.cleanup_state,'pending')
        posts=self.transport.call_count
        self.deletes.reset_mock();self.deletes.side_effect=None;self.deletes.return_value=('deleted',0)
        self.assertEqual(clean_generation(old.pk,now=old.cleanup_after),'done')
        self.deletes.assert_called_once_with(fixture.fixtures.WEBHOOK,old.message_ids[1])
        self.assertEqual(self.transport.call_count,posts)

    def test_new_destination_default_does_not_reset_an_existing_explicit_none(self):
        GroupDiscordNotification.objects.filter(group=self.group).update(
            moderator_cleanup='none',encrypted_webhook='',moderator_enabled=False)
        config=configure_notifications(group=self.group,actor=self.owner,destination='moderator',
            enabled=True,webhook_url=fixture.fixtures.WEBHOOK)
        self.assertEqual(config.moderator_cleanup,'scheduled')
        configure_notifications(group=self.group,actor=self.owner,destination='moderator',enabled=True,cleanup_mode='none')
        config=configure_notifications(group=self.group,actor=self.owner,destination='moderator',enabled=True)
        self.assertEqual(config.moderator_cleanup,'none')

    def test_webhook_rotation_global_off_and_empty_reports_never_delete(self):
        self.deliver();old=DiscordReportGeneration.objects.get()
        self.payloads=[];self.assertEqual(self.deliver(),'empty');self.deletes.assert_not_called()
        self.payloads=[self.payload(self.month.pk)]
        configure_notifications(group=self.group,actor=self.owner,destination='moderator',enabled=True,
                                webhook_url=fixture.fixtures.REPLACEMENT)
        self.deliver();old.refresh_from_db();self.assertFalse(old.superseded);self.deletes.assert_not_called()
        PlatformSettings.objects.update_or_create(pk=1,defaults={'discord_notifications_override':False})
        self.assertEqual(self.deliver(),'environment_disabled');self.assertEqual(retry_cleanup(),[])

    def test_overlapping_slot_is_blocked_and_does_not_steal_lease(self):
        self.deliver();previous=DiscordReportGeneration.objects.get().delivery
        DiscordReportSchedule.objects.filter(pk=self.schedule.pk).update(replacement_delivery=previous)
        posts=self.transport.call_count
        self.assertEqual(self.deliver(),'blocked');self.assertEqual(self.transport.call_count,posts)
        self.schedule.refresh_from_db();self.assertEqual(self.schedule.replacement_delivery_id,previous.pk)

    def test_challenge_streams_never_delete_each_other(self):
        self.deliver();first=DiscordReportGeneration.objects.get()
        self.payloads=[self.payload(self.month.pk+1)];self.deliver()
        first.refresh_from_db();self.assertFalse(first.superseded);self.deletes.assert_not_called()

    def test_delete_404_and_rate_limit_use_specific_message_endpoint(self):
        delete_message=real_delete_message
        opener=MagicMock();url=fixture.fixtures.WEBHOOK
        with patch('core.discord_notifications.build_opener',return_value=opener):
            opener.open.side_effect=HTTPError(url,404,'missing',{},io.BytesIO())
            self.assertEqual(delete_message(url,'123456789012345678'),('deleted',0))
            request=opener.open.call_args.args[0]
            self.assertEqual(request.method,'DELETE');self.assertTrue(request.full_url.endswith('/messages/123456789012345678'))
            opener.open.side_effect=HTTPError(url,429,'limited',{},io.BytesIO(b'{"retry_after":7}'))
            self.assertEqual(delete_message(url,'123456789012345678'),('rate_limited',7))

    def test_tracked_transport_requests_confirmation_and_validates_id(self):
        # Restore the real transport only within this test; outbound HTTP stays mocked.
        from . import discord_notifications as discord
        real=send_webhook;opener=MagicMock();response=opener.open.return_value.__enter__.return_value
        response.status=200;response.read.return_value=b'{"id":"123456789012345678"}'
        with patch('core.discord_notifications.build_opener',return_value=opener):
            self.assertEqual(real(fixture.fixtures.WEBHOOK,{},capture_message=True),('sent',0,'123456789012345678'))
            self.assertTrue(opener.open.call_args.args[0].full_url.endswith('?wait=true'))
            response.read.return_value=b'{}'
            self.assertEqual(real(fixture.fixtures.WEBHOOK,{},capture_message=True),('uncertain',0,None))
            PlatformSettings.objects.update_or_create(pk=1,defaults={'discord_notifications_override':False})
            calls=opener.open.call_count
            self.assertEqual(real(fixture.fixtures.WEBHOOK,{},capture_message=True),('disabled',0,None))
            self.assertEqual(opener.open.call_count,calls)


class CleanupDefaultsMigrationTests(TransactionTestCase):
    def test_existing_destinations_stay_none_new_moderators_default_to_scheduled(self):
        executor=MigrationExecutor(connection);old=[('core','0087_independent_branding_modes')]
        try:
            executor.migrate(old);apps=executor.loader.project_state(old).apps
            Group=apps.get_model('core','ReadingGroup');Config=apps.get_model('core','GroupDiscordNotification')
            group=Group.objects.create(name='Existing',slug='existing-cleanup')
            configuration=Config.objects.create(group=group,moderator_enabled=True)
            executor=MigrationExecutor(connection);target=[('core','0088_scheduled_report_cleanup')]
            executor.migrate(target);apps=executor.loader.project_state(target).apps
            Config=apps.get_model('core','GroupDiscordNotification');config=Config.objects.get(pk=configuration.pk)
            self.assertEqual((config.community_cleanup,config.moderator_cleanup),('none','none'))
            self.assertTrue(config.moderator_enabled)
            self.assertEqual(Config().moderator_cleanup,'scheduled')
        finally:
            executor=MigrationExecutor(connection);executor.migrate(executor.loader.graph.leaf_nodes())
