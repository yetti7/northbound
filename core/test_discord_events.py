"""Stage C uses synthetic records and mocked HTTP exclusively."""
import io
import json
import uuid
from datetime import timedelta
from unittest.mock import patch
from urllib.error import HTTPError

from django.core.management import call_command, CommandError
from django.db import transaction, IntegrityError
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from . import test_discord_notifications as fixtures
from .discord_events import (enqueue, record_book_created, observe_book, deliver_event, process_events,
    event_payload, observe_challenge, MAX_ATTEMPTS)
from .discord_notifications import configure_notifications, send_webhook
from .discord_scheduling import configure_report_schedule, scheduled_payloads, COMMUNITY_CATEGORIES
from .discord_community import community_embeds
from .models import (DiscordEventDelivery, DiscordReportSchedule, DiscordSchedulerHeartbeat, ChallengeMonth,
    BookSubmission, TeamAssignment, GroupDiscordNotification)


@override_settings(NORTHBOUND_DISCORD_EVENTS_ENABLED=True,NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=True,
    NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT=True,NORTHBOUND_URL='https://northbound.example.com')
class DiscordEventsTests(TestCase):
    setUp=fixtures.DiscordNotificationTests.setUp
    book=fixtures.DiscordNotificationTests.book

    def activate(self,destination='moderator',categories=None):
        configure_notifications(group=self.group,actor=self.owner,destination=destination,enabled=True,webhook_url=fixtures.WEBHOOK)
        form,_=configure_report_schedule(group=self.group,actor=self.owner,destination=destination,
            data={'immediate_enabled':'on','immediate_categories':categories or
                (['new_submission','review_required','approved','rejected'] if destination=='moderator' else ['challenge_start','challenge_close','challenge_complete','results','registration_open','registration_close']),
                'immediate_controls':'1'})
        self.assertTrue(form.is_valid(),form.errors)
        return DiscordReportSchedule.objects.get(configuration__group=self.group,destination=destination)

    def event(self):
        self.activate();book=self.book()
        with transaction.atomic():record_book_created(book)
        return DiscordEventDelivery.objects.get()

    def test_saved_categories_require_explicit_activation_and_no_backfill(self):
        form,_=configure_report_schedule(group=self.group,actor=self.owner,destination='moderator',data={'immediate_categories':['review_required']})
        self.assertTrue(form.is_valid());book=self.book()
        with transaction.atomic():record_book_created(book)
        self.assertFalse(DiscordEventDelivery.objects.exists())
        self.activate();self.assertFalse(DiscordEventDelivery.objects.exists())
        process_events();self.assertFalse(DiscordEventDelivery.objects.exists())

    def test_creation_coalesces_new_and_review_needed_and_has_no_private_payload(self):
        row=self.event();self.assertEqual(row.category,'review_required');self.assertEqual(row.review_category,'books')
        payload=event_payload(row);text=json.dumps(payload)
        for secret in ['Private book title','Private author','digest-reader',fixtures.WEBHOOK]:self.assertNotIn(secret,text)
        self.assertEqual(payload['allowed_mentions']['parse'],[])
        self.assertIn('Unassigned',text)
        self.assertEqual(row.source_id,BookSubmission.objects.get().pk)

    def test_new_only_and_autoapproved_creation(self):
        self.activate(categories=['new_submission'])
        for status in ['pending','approved']:
            book=self.book(status=status)
            with transaction.atomic():record_book_created(book)
        self.assertEqual(list(DiscordEventDelivery.objects.values_list('category',flat=True)),['new_submission','new_submission'])

    def test_observer_no_repeated_save_and_reentry_then_approval_rejection(self):
        self.activate();book=self.book(status='rejected')
        @observe_book
        def update(submission,status):
            submission.status=status;submission.approved_pages=100 if status=='approved' else None;submission.save();return submission
        update(book,'pending');update(book,'pending');update(book,'approved');update(book,'approved');update(book,'rejected')
        self.assertEqual(list(DiscordEventDelivery.objects.values_list('category',flat=True)),['review_required','approved','rejected'])

    def test_nested_observer_and_rolled_back_business_change(self):
        self.activate();book=self.book(status='rejected')
        @observe_book
        def inner(submission):submission.status='pending';submission.save()
        @observe_book
        def outer(submission):inner(submission)
        with self.assertRaises(ValueError),transaction.atomic():outer(book);raise ValueError()
        self.assertFalse(DiscordEventDelivery.objects.exists());book.refresh_from_db();self.assertEqual(book.status,'rejected')
        outer(book);self.assertEqual(DiscordEventDelivery.objects.count(),1)

    def test_authoritative_creation_view_records_transactional_event(self):
        from django.urls import reverse
        self.activate();self.client.force_login(self.reader)
        response=self.client.post(reverse('submission-create',args=[self.group.slug,self.month.pk]),
            {'title':'Private new book','author':'Secret author','book_format':'ebook','completed_on':'2026-11-03','submitted_pages':100})
        self.assertEqual(response.status_code,302)
        self.assertEqual(DiscordEventDelivery.objects.get().category,'review_required')

    def test_clearing_categories_explicitly_and_activation_does_not_change_schedule_slot(self):
        schedule=self.activate();schedule.frequency='1h';schedule.next_run_at=timezone.now()+timedelta(minutes=30);schedule.save()
        old=schedule.next_run_at
        form,_=configure_report_schedule(group=self.group,actor=self.owner,destination='moderator',
            data={'scheduled_enabled':'on','frequency':'1h','categories':['books'],'immediate_controls':'1'})
        self.assertTrue(form.is_valid(),form.errors)
        schedule.refresh_from_db();self.assertFalse(schedule.immediate_enabled);self.assertEqual(schedule.immediate_categories,[])
        # First set report categories before checking event-only changes.
        slot=schedule.next_run_at
        form,_=configure_report_schedule(group=self.group,actor=self.owner,destination='moderator',
            data={'scheduled_enabled':'on','frequency':'1h','categories':['books'],'immediate_enabled':'on','immediate_categories':['approved'],'immediate_controls':'1'})
        self.assertTrue(form.is_valid(),form.errors);schedule.refresh_from_db();self.assertEqual(schedule.next_run_at,slot)

    def test_stable_event_identity_has_unique_destination_receipt(self):
        self.activate();key=uuid.uuid4()
        with transaction.atomic():
            for _ in range(2):enqueue(month=self.month,categories=['review_required'],source_kind='book',source_id=1,review_category='books',event_key=key)
        self.assertEqual(DiscordEventDelivery.objects.count(),1)

    @patch('core.discord_notifications.send_webhook',return_value=('sent',0))
    def test_claim_restart_and_success_only_one_send(self,send):
        row=self.event();self.assertEqual(deliver_event(row.pk),'sent');self.assertEqual(deliver_event(row.pk),'sent')
        process_events();self.assertEqual(send.call_count,1)
        row.refresh_from_db();self.assertEqual(row.attempts,1);self.assertIsNotNone(row.claim_id)

    @patch('core.discord_notifications.send_webhook',return_value=('rate_limited',120))
    def test_rate_limit_backoff_and_retry_limit(self,send):
        row=self.event();now=timezone.now()
        for attempt in range(MAX_ATTEMPTS):
            self.assertEqual(deliver_event(row.pk,now=now),'failed' if attempt==MAX_ATTEMPTS-1 else 'pending')
            row.refresh_from_db();self.assertGreaterEqual(row.next_attempt_at,now+timedelta(seconds=120))
            now=row.next_attempt_at+timedelta(seconds=1)
        self.assertEqual(send.call_count,MAX_ATTEMPTS)

    @patch('core.discord_notifications.send_webhook',return_value=('server_error',0))
    def test_server_error_exponential_backoff(self,send):
        row=self.event();now=timezone.now()
        for seconds in [30,60,120]:
            self.assertEqual(deliver_event(row.pk,now=now),'pending');row.refresh_from_db()
            self.assertEqual(row.next_attempt_at-now,timedelta(seconds=seconds));now=row.next_attempt_at

    @patch('core.discord_notifications.send_webhook',return_value=('timeout',0))
    def test_ambiguous_transport_never_automatically_retried(self,send):
        row=self.event();self.assertEqual(deliver_event(row.pk),'uncertain');process_events()
        self.assertEqual(send.call_count,1)

    @patch('core.discord_notifications.send_webhook',return_value=('rejected',0))
    def test_permanent_failure(self,send):
        row=self.event();self.assertEqual(deliver_event(row.pk),'failed');process_events();self.assertEqual(send.call_count,1)

    @patch('core.discord_notifications.send_webhook')
    def test_crashed_claim_becomes_uncertain_without_retry(self,send):
        row=self.event();row.state='claimed';row.claimed_at=timezone.now()-timedelta(minutes=6);row.save()
        process_events();row.refresh_from_db();self.assertEqual(row.state,'uncertain');send.assert_not_called()

    @patch('core.discord_notifications.send_webhook')
    def test_disable_category_webhook_and_activation_fences(self,send):
        for change in ['destination','category','webhook','activation','expiry']:
            DiscordEventDelivery.objects.all().delete();row=self.event();schedule=DiscordReportSchedule.objects.get(configuration__group=self.group,destination='moderator')
            config=schedule.configuration
            if change=='destination':config.moderator_enabled=False;config.save()
            elif change=='category':schedule.immediate_categories=[];schedule.save()
            elif change=='webhook':configure_notifications(group=self.group,actor=self.owner,destination='moderator',enabled=True,webhook_url=fixtures.REPLACEMENT)
            elif change=='activation':schedule.immediate_activated_at=timezone.now();schedule.save()
            else:row.expires_at=timezone.now()-timedelta(seconds=1);row.save()
            self.assertEqual(deliver_event(row.pk),'skipped',change)
        send.assert_not_called()

    @patch('core.discord_notifications.send_webhook')
    def test_global_gate_and_development_gate(self,send):
        row=self.event()
        for overrides in [dict(NORTHBOUND_DISCORD_EVENTS_ENABLED=False),dict(NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=False),dict(DEBUG=True,NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT=False)]:
            with override_settings(**overrides):self.assertEqual(deliver_event(row.pk),'environment_disabled');self.assertEqual(process_events(),[])
        send.assert_not_called();row.refresh_from_db();self.assertEqual(row.attempts,0)

    @patch('core.discord_notifications.send_webhook',return_value=('sent',0))
    def test_order_and_independent_destinations(self,send):
        first=self.event();book=self.book()
        with transaction.atomic():record_book_created(book)
        last=DiscordEventDelivery.objects.last();self.assertEqual(deliver_event(last.pk),'ordered_wait')
        self.assertEqual(deliver_event(first.pk),'sent');self.assertEqual(deliver_event(last.pk),'sent')

    @patch('core.discord_notifications.send_webhook')
    def test_worker_skips_missed_first_attempts_without_historical_flood(self,send):
        row=self.event();row.created_at=timezone.now()-timedelta(minutes=3);row.save()
        process_events();row.refresh_from_db();self.assertEqual(row.state,'skipped');self.assertEqual(row.failure_category,'missed');send.assert_not_called()

    def test_event_transport_honors_short_discord_retry_after(self):
        error=HTTPError(fixtures.WEBHOOK,429,'rate limit',{'Retry-After':'7'},io.BytesIO(b'{"retry_after":7}'))
        with patch('core.discord_notifications.build_opener') as opener:
            opener.return_value.open.side_effect=error
            self.assertEqual(send_webhook(fixtures.WEBHOOK,{},detailed=True,retryable=True),('rate_limited',7))

    @patch('core.discord_notifications.send_webhook',side_effect=[('rejected',0),('sent',0)])
    def test_moderator_failure_does_not_block_community_destination(self,send):
        moderator=self.event();self.activate('community');self.month.discord_community_opt_in=True;self.month.save()
        self.month.transition_to('finalizing');community=DiscordEventDelivery.objects.filter(destination='community').get()
        self.assertEqual(deliver_event(moderator.pk),'failed')
        self.assertEqual(deliver_event(community.pk),'sent');self.assertEqual(send.call_count,2)

    def test_worker_dry_run_is_readonly_and_not_challenge_scheduler(self):
        self.event();before=list(DiscordEventDelivery.objects.values());out=io.StringIO()
        call_command('run_discord_events',dry_run=True,stdout=out)
        self.assertEqual(before,list(DiscordEventDelivery.objects.values()));self.assertFalse(DiscordSchedulerHeartbeat.objects.exists())
        with override_settings(NORTHBOUND_DISCORD_EVENTS_ENABLED=False),self.assertRaises(CommandError):call_command('run_discord_events',once=True)

    def test_team_group_challenge_and_limits(self):
        row=self.event();team=self.month.teams.first();TeamAssignment.objects.create(month=self.month,participant=self.member,team=team)
        book=self.book()
        with transaction.atomic():record_book_created(book)
        row=DiscordEventDelivery.objects.last();self.assertEqual(row.team,team)
        payload=event_payload(row);self.assertIn(team.name,json.dumps(payload));self.assertIn(self.month.name,json.dumps(payload))

    def test_community_lifecycle_registration_and_no_reversals(self):
        self.activate('community');self.month.discord_community_opt_in=True;self.month.save()
        self.month.transition_to('finalizing');self.month.transition_to('completed');self.month.transition_to('completed')
        self.month.transition_to('finalizing',confirm_reversal=True,confirm_completed_recovery=True)
        self.assertEqual(list(DiscordEventDelivery.objects.values_list('category',flat=True)),['challenge_close','challenge_complete'])
        self.assertFalse(DiscordEventDelivery.objects.filter(destination='moderator').exists())

    def test_registration_distinct_from_lifecycle_and_no_overdue_flood(self):
        self.activate('community');self.month.discord_community_opt_in=True;self.month.auto_close_registration=False
        self.month.registration_opens_at=timezone.now()-timedelta(days=2);self.month.save()
        self.month.apply_scheduled_actions(lifecycle=False)
        self.assertFalse(DiscordEventDelivery.objects.exists())
        @observe_challenge
        def manual(month):month.registration_is_open=False;month.save(update_fields=['registration_is_open'])
        manual(self.month);manual(self.month)
        self.assertEqual(list(DiscordEventDelivery.objects.values_list('category',flat=True)),['registration_close'])

    def test_community_optin_publication_privacy_categories_and_group_boundaries(self):
        self.activate('community');self.book();self.assertEqual(community_embeds(self.group,['active']),[])
        self.month.discord_community_opt_in=True;self.month.save()
        self.assertEqual(community_embeds(self.group,['results','participation']),[])
        values=community_embeds(self.group,['active']);text=json.dumps(values)
        self.assertIn(self.month.name,text);self.assertNotIn('Registration',text);self.assertNotIn('Private',text)
        self.assertEqual(community_embeds(self.other,['active']),[])
        self.month.transition_to('finalizing');self.assertEqual(community_embeds(self.group,['results']),[])
        self.month.transition_to('completed');text=json.dumps(community_embeds(self.group,['results','participation']))
        self.assertEqual(text,'[]');self.assertNotIn('approved_pages',text)
        with override_settings(NORTHBOUND_URL='http://192.168.0.11:8061'):
            self.assertNotIn('192.168',json.dumps(community_embeds(self.group,['results'])))

    def test_metadata_exception_does_not_grant_statistics_or_results_audience_access(self):
        from .discord_community import results_visible, participation_visible
        self.activate('community');self.month.discord_community_opt_in=True
        self.month.team_standings_visibility='everybody';self.month.reader_scores_visibility='everybody';self.month.save()
        self.book()
        for status in ['active','finalizing','completed','archived']:
            if self.month.status!=status:self.month.transition_to(status)
            self.assertFalse(results_visible(self.month));self.assertFalse(participation_visible(self.month))
            self.assertEqual(community_embeds(self.group,['participation','results']),[])
            text=json.dumps(community_embeds(self.group,['dates','participation','results']))
            self.assertIn(self.month.name,text);self.assertNotIn('registered Readers',text);self.assertNotIn('Final standings',text)
        self.assertFalse(DiscordEventDelivery.objects.filter(category='results').exists())
        self.assertTrue(DiscordEventDelivery.objects.filter(category='challenge_complete').exists())

    def test_community_drafts_and_inactive_groups_fail_closed(self):
        self.activate('community');self.month.discord_community_opt_in=True;self.month.save()
        self.month.transition_to('upcoming',confirm_reversal=True);self.month.transition_to('draft',confirm_reversal=True)
        self.assertEqual(community_embeds(self.group,['active','upcoming','registration','dates','participation','results']),[])
        self.assertFalse(DiscordEventDelivery.objects.exists())
        self.month.transition_to('upcoming');self.group.is_active=False;self.group.save()
        self.assertEqual(community_embeds(self.group,['upcoming']),[])

    @patch('core.discord_notifications.send_webhook')
    def test_delivery_rechecks_optin_and_results_visibility(self,send):
        self.activate('community');self.month.discord_community_opt_in=True;self.month.save();self.month.transition_to('finalizing')
        row=DiscordEventDelivery.objects.get();self.month.discord_community_opt_in=False;self.month.save()
        self.assertEqual(deliver_event(row.pk),'skipped');send.assert_not_called()

    def test_community_scheduled_categories_and_empty_behavior(self):
        schedule=self.activate('community');schedule.categories=['active'];schedule.send_empty=False
        self.assertEqual(scheduled_payloads(schedule),[])
        self.month.discord_community_opt_in=True;self.month.save()
        text=json.dumps(scheduled_payloads(schedule));self.assertIn(self.month.name,text);self.assertNotIn('Registration',text)
        schedule.categories=['results'];self.assertEqual(scheduled_payloads(schedule),[])
        schedule.send_empty=True;self.assertTrue(scheduled_payloads(schedule))

    def test_retryable_provider_errors_do_not_change_existing_transport_contract(self):
        error=HTTPError(fixtures.WEBHOOK,503,'unavailable',{},io.BytesIO(b''))
        with patch('core.discord_notifications.build_opener') as opener:
            opener.return_value.open.side_effect=error
            self.assertEqual(send_webhook(fixtures.WEBHOOK,{},detailed=True),('rejected',0))
            self.assertEqual(send_webhook(fixtures.WEBHOOK,{},detailed=True,retryable=True),('server_error',0))


from . import test_scavenger_photo_foundation as photo_fixtures
from . import test_prompt_foundations as prompt_fixtures
from .photo_review import review_photo_submission, photo_review_version
from .prompt_reservations import replace_selections
from .prompt_scoring import review_prompts
from .scoring import apply_submission_review
from .immersive import request_immersive, review_immersive


@override_settings(NORTHBOUND_PROMPT_RESERVATIONS_READY=True)
class DiscordServiceIntegrationTests(TestCase):
    token=photo_fixtures.PhotoFoundationTests.token
    prompt=photo_fixtures.PhotoFoundationTests.prompt
    settings=photo_fixtures.PhotoFoundationTests.settings
    create=photo_fixtures.PhotoFoundationTests.create

    def setUp(self):
        photo_fixtures.PhotoFoundationTests.setUp(self)
        configure_notifications(group=self.group,actor=self.users['root'],destination='moderator',enabled=True,webhook_url=fixtures.WEBHOOK)
        form,_=configure_report_schedule(group=self.group,actor=self.users['root'],destination='moderator',
            data={'immediate_enabled':'on','immediate_categories':['new_submission','review_required','approved','rejected']})
        self.assertTrue(form.is_valid(),form.errors)

    def test_real_photo_creation_and_review_services_no_duplicate_nested_rejection(self):
        photo=self.create();self.assertEqual(DiscordEventDelivery.objects.count(),1)
        from .photo_submissions import transition_photo_reservation
        transition_photo_reservation(photo_id=photo.pk,actor=self.actor,version=photo.version,status='rejected')
        self.assertEqual(list(DiscordEventDelivery.objects.values_list('category',flat=True)),['review_required','rejected'])
        self.assertEqual(set(DiscordEventDelivery.objects.values_list('review_category',flat=True)),{'photos'})

    def test_photo_rollback_rolls_back_outbox_and_submission(self):
        from .models import ScavengerPhotoSubmission
        with self.assertRaises(ValueError),transaction.atomic():self.create();raise ValueError()
        self.assertFalse(ScavengerPhotoSubmission.objects.exists());self.assertFalse(DiscordEventDelivery.objects.exists())

    def test_real_prompt_selection_and_review_observers(self):
        self.settings(kind='scavenger',enabled=True)
        replace_selections(submission=self.book,actor=self.actor,selections={'scavenger':self.book_prompt.pk})
        self.assertEqual(DiscordEventDelivery.objects.get().review_category,'scavenger')
        review_prompts(self.book,self.actor,{'scavenger':'qualified'})
        self.assertEqual(list(DiscordEventDelivery.objects.values_list('category',flat=True)),['review_required','approved'])
        review_prompts(self.book,self.actor,{'scavenger':'qualified'})
        self.assertEqual(DiscordEventDelivery.objects.count(),2)

    def test_real_immersive_request_and_approval_observers(self):
        from .immersive import configure_immersive
        configure_immersive(month=self.month,actor=self.actor,enabled=True,percent=100)
        request=request_immersive(submission=self.book,actor=self.reader.user)
        self.assertEqual(DiscordEventDelivery.objects.get().review_category,'immersive')
        review_immersive(request=request,actor=self.actor,decision='approved')
        self.assertEqual(list(DiscordEventDelivery.objects.values_list('category',flat=True)),['review_required','approved'])

    @override_settings(NORTHBOUND_DISCORD_EVENTS_ENABLED=True,NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=True,NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT=True)
    @patch('core.discord_notifications.send_webhook',return_value=('sent',0))
    def test_actual_photo_prompt_and_immersive_events_pass_delivery_eligibility(self,send):
        self.create();self.assertEqual(deliver_event(DiscordEventDelivery.objects.last().pk),'sent')
        replace_selections(submission=self.book,actor=self.actor,selections={'scavenger':self.book_prompt.pk})
        self.assertEqual(deliver_event(DiscordEventDelivery.objects.last().pk),'sent')
        from .immersive import configure_immersive
        configure_immersive(month=self.month,actor=self.actor,enabled=True,percent=100)
        request_immersive(submission=self.book,actor=self.reader.user)
        self.assertEqual(deliver_event(DiscordEventDelivery.objects.last().pk),'sent')
        self.assertEqual(send.call_count,3)

    def test_real_book_and_theme_review_observer_preserves_scoring(self):
        from .matching_resolution import review_submission_with_matches
        self.book.status='pending';self.book.approved_pages=None;self.book.save()
        self.book.status='approved';self.book.approved_pages=368
        result=review_submission_with_matches(self.book,[],self.actor,timezone.now())
        self.assertEqual(result.status,'approved');self.assertEqual(result.final_scored_pages,368)
        self.assertEqual(DiscordEventDelivery.objects.get().category,'approved')


@override_settings(NORTHBOUND_DISCORD_EVENTS_ENABLED=True,NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=True,
    NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT=True)
class DiscordClaimConcurrencyTests(TransactionTestCase):
    setUp=fixtures.DiscordNotificationTests.setUp
    book=fixtures.DiscordNotificationTests.book
    activate=DiscordEventsTests.activate
    event=DiscordEventsTests.event

    @patch('core.discord_notifications.send_webhook',return_value=('sent',0))
    def test_two_workers_can_claim_at_most_one_post(self,send):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections, OperationalError
        row=self.event();barrier=Barrier(2)
        def run():
            close_old_connections();barrier.wait()
            try:return deliver_event(row.pk)
            except OperationalError:return 'database_busy'
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:outcomes=list(pool.map(lambda _:run(),range(2)))
        self.assertLessEqual(send.call_count,1)
        # A SQLite lock loser is safe to check later; it did no I/O.
        deliver_event(row.pk);self.assertEqual(send.call_count,1)
        row.refresh_from_db();self.assertEqual(row.state,'sent');self.assertEqual(row.attempts,1)
