"""Correction-owned transactions and Discord observation; synthetic data only."""
import json
import time
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings

from . import discord_events, submission_corrections as corrections
from . import test_submission_editing as editing
from . import test_submission_hotfix as fixture
from .discord_notifications import configure_notifications
from .discord_scheduling import configure_report_schedule
from .models import (AuditEvent, BookSubmission, DiscordEventDelivery, DiscordReportSchedule,
    DiscordSchedulerHeartbeat, MonthTheme, ThemeClaim)


@override_settings(NORTHBOUND_DISCORD_EVENTS_ENABLED=False, NORTHBOUND_DISCORD_SCHEDULER_ENABLED=False,
    NORTHBOUND_DISCORD_NOTIFICATIONS_ENABLED=True, NORTHBOUND_DISCORD_ALLOW_DEVELOPMENT=True)
class DiscordCorrectionTransactionTests(TransactionTestCase):
    actor = editing.SubmissionEditingTests.actor
    submission = editing.SubmissionEditingTests.submission

    def setUp(self):
        fixture.SubmissionHotfixTests.setUp(self)
        self.editor = self.actor('host')
        administrator = self.actor('platform')
        configure_notifications(group=self.month.group, actor=administrator, destination='moderator',
            enabled=True, webhook_url='https://discord.com/api/webhooks/123456789012345678/synthetic_correction_test_token')
        form,_=configure_report_schedule(group=self.month.group, actor=administrator, destination='moderator',
            data={'immediate_controls':'1','immediate_enabled':'on','immediate_categories':['approved','review_required']})
        self.assertTrue(form.is_valid(),form.errors)
        self.transport_patch=patch('core.discord_notifications.send_webhook')
        self.send=self.transport_patch.start()
        self.addCleanup(self.transport_patch.stop)

    def tearDown(self):
        self.send.assert_not_called()
        self.assertFalse(DiscordSchedulerHeartbeat.objects.exists())
        super().tearDown()

    def correct(self,item,changes,**kwargs):
        return corrections.correct_submission(submission=item,actor=self.editor,changes=changes,
            version=corrections.correction_version(item,self.editor),**kwargs)

    def theme(self):
        return MonthTheme.objects.create(month=self.month,name='New review category',
            starts_on=self.month.starts_on,ends_on=self.month.ends_on,bonus_percentage=10)

    def snapshot(self):
        return {model.__name__:list(model.objects.order_by('pk').values()) for model in
            [BookSubmission,ThemeClaim,AuditEvent,DiscordEventDelivery]}

    def test_approved_correction_owns_transaction_and_preparation_stays_outside(self):
        item=self.submission()
        prepare=corrections._prepare_edition
        observe=corrections.observe_book_changes
        contexts=[]
        def preparation(*args,**kwargs):
            contexts.append(('prepare',connection.in_atomic_block))
            return prepare(*args,**kwargs)
        def observation(*args,**kwargs):
            contexts.append(('observe',connection.in_atomic_block))
            return observe(*args,**kwargs)
        self.assertFalse(connection.in_atomic_block)
        with patch.object(corrections,'_prepare_edition',side_effect=preparation),patch.object(
                corrections,'observe_book_changes',side_effect=observation):
            result=self.correct(item,{'page_count':400})
        self.assertEqual(contexts,[('prepare',False),('observe',True)])
        self.assertFalse(connection.in_atomic_block)
        self.assertEqual((result.status,result.approved_pages),('approved',400))
        self.assertTrue(AuditEvent.objects.filter(action='submission.corrected',object_id=str(item.pk)).exists())
        # A page-only edit is not a new submission/review state transition.
        self.assertFalse(DiscordEventDelivery.objects.exists())

    def test_unsupported_outer_transaction_is_still_rejected_before_observation(self):
        item=self.submission();before=self.snapshot()
        with patch.object(corrections,'observe_book_changes') as observe:
            with self.assertRaisesMessage(ValidationError,'outside an existing write transaction'),transaction.atomic():
                self.correct(item,{'page_count':400})
            observe.assert_not_called()
        self.assertEqual(self.snapshot(),before)

    def test_eligible_correction_records_one_event_inside_same_transaction(self):
        item=self.submission();theme=self.theme();original=discord_events.enqueue
        def checked_enqueue(**kwargs):
            self.assertTrue(connection.in_atomic_block)
            self.assertTrue(AuditEvent.objects.filter(action='submission.corrected',object_id=str(item.pk)).exists())
            return original(**kwargs)
        with patch.object(discord_events,'enqueue',side_effect=checked_enqueue):
            result=self.correct(item,{'theme_ids':[theme.pk]})
        row=DiscordEventDelivery.objects.get()
        self.assertEqual((row.destination,row.review_category,row.category,row.state),('moderator','themes','approved','pending'))
        self.assertEqual(row.source_id,ThemeClaim.objects.get(submission=item,theme=theme).pk)
        self.assertEqual(result.status,'approved')
        # Nested observed correction helpers must not create duplicate events.
        self.assertEqual(DiscordEventDelivery.objects.count(),1)
        self.assertEqual(discord_events.process_events(),[])
        row.refresh_from_db();self.assertEqual(row.attempts,0)
        schedule=DiscordReportSchedule.objects.get(destination='moderator')
        self.assertTrue(schedule.immediate_enabled)

    def test_preview_rolls_back_simulated_correction_and_records_no_event(self):
        item=self.submission();theme=self.theme();before=self.snapshot()
        plan=corrections.preview_correction(submission=item,actor=self.editor,changes={'theme_ids':[theme.pk]},
            version=corrections.correction_version(item,self.editor))
        self.assertTrue(plan['token'])
        self.assertEqual(self.snapshot(),before)
        self.assertFalse(connection.in_atomic_block)
        self.correct(item,{'theme_ids':[theme.pk]})
        self.assertEqual(DiscordEventDelivery.objects.count(),1)

    def test_observation_failure_rolls_back_book_audit_and_already_recorded_outbox(self):
        item=self.submission();theme=self.theme();before=self.snapshot()
        original=discord_events.record_book_changes
        def fail_after_recording(*args,**kwargs):
            original(*args,**kwargs)
            self.assertEqual(DiscordEventDelivery.objects.count(),1)
            self.assertTrue(AuditEvent.objects.filter(action='submission.corrected',object_id=str(item.pk)).exists())
            raise RuntimeError('Synthetic post-observation failure')
        with patch.object(discord_events,'record_book_changes',side_effect=fail_after_recording),self.assertRaises(RuntimeError):
            self.correct(item,{'page_count':400,'theme_ids':[theme.pk]})
        self.assertEqual(self.snapshot(),before)
        # Failure must also release nested-observer suppression state.
        self.correct(item,{'theme_ids':[theme.pk]})
        self.assertEqual(DiscordEventDelivery.objects.count(),1)

    def test_failed_business_change_and_noop_leave_no_event_or_audit(self):
        item=self.submission();before=self.snapshot()
        for changes in [{'theme_ids':[999999]},{'title':item.title}]:
            with self.assertRaises(ValidationError):self.correct(item,changes)
            self.assertEqual(self.snapshot(),before)

    def test_expired_tampered_and_stale_preview_remain_rejected_without_events(self):
        item=self.submission();theme=self.theme();changes={'theme_ids':[theme.pk]}
        version=corrections.correction_version(item,self.editor)
        plan=corrections.preview_correction(submission=item,actor=self.editor,changes=changes,version=version)
        def apply(token):
            return corrections.apply_previewed_correction(submission=item,actor=self.editor,changes=changes,
                version=version,preview_token=token,confirm_approved=True)
        before=self.snapshot()
        with self.assertRaises(ValidationError):apply(plan['token']+'tampered')
        self.assertEqual(self.snapshot(),before)
        with patch('django.core.signing.time.time',return_value=time.time()+601),self.assertRaises(ValidationError):
            apply(plan['token'])
        self.assertEqual(self.snapshot(),before)
        BookSubmission.objects.filter(pk=item.pk).update(approved_pages=351)
        stale=self.snapshot()
        with self.assertRaises(ValidationError):apply(plan['token'])
        self.assertEqual(self.snapshot(),stale)

    def test_rejected_correction_retains_existing_pending_transition_event(self):
        item=self.submission(status='rejected')
        result=self.correct(item,{'title':'Corrected pending title'})
        self.assertEqual(result.status,'pending');self.assertIsNone(result.approved_pages)
        row=DiscordEventDelivery.objects.get()
        self.assertEqual((row.review_category,row.category),('books','review_required'))
